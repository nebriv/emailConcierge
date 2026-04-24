"""Gmail read-only message fetcher.

Fetches by internal message ID (the hex string Google Calendar's
`source.url` points at) and returns our canonical `Email` shape so
downstream code is agnostic to where the message came from.

READ-ONLY: we only call `users().messages().get()`. The Gmail API
exposes mutation methods (`modify`, `trash`, `delete`, `send`) but this
module does not touch them; the ruff TID rule enforces that no other
module imports `googleapiclient` to bypass us.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from email_concierge.log import get_logger
from email_concierge.models import Attachment, Email

log = get_logger(__name__)

_SEARCH_WINDOW_HOURS = 72
_SEARCH_MAX_RESULTS = 10
_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB — skip anything bigger


def _decode_b64url(data: str) -> bytes:
    """Gmail encodes bodies with URL-safe base64 and may omit padding."""
    padding = -len(data) % 4
    return base64.urlsafe_b64decode(data + "=" * padding)


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in payload.get("headers", [])}


def _walk_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the MIME tree Gmail returns into a list of leaf parts."""
    parts = payload.get("parts")
    if not parts:
        return [payload]
    leaves: list[dict[str, Any]] = []
    for p in parts:
        leaves.extend(_walk_parts(p))
    return leaves


@dataclass
class _PendingAttachment:
    """Attachment metadata from a Gmail payload, bytes not yet fetched."""

    filename: str
    content_type: str
    attachment_id: str | None  # None if bytes were inlined in `body.data`
    inline_payload: bytes | None  # set when body.data held the bytes directly
    size: int


def _extract_bodies(
    payload: dict[str, Any],
) -> tuple[str, str | None, list[_PendingAttachment]]:
    text_parts: list[str] = []
    html: str | None = None
    pending: list[_PendingAttachment] = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        filename = part.get("filename") or ""
        if filename:
            # Attachment. Small ones are inlined in `body.data`; larger
            # ones expose `attachmentId` for a follow-up fetch.
            inline_bytes = _decode_b64url(data) if data else None
            pending.append(
                _PendingAttachment(
                    filename=filename,
                    content_type=mime or "application/octet-stream",
                    attachment_id=body.get("attachmentId"),
                    inline_payload=inline_bytes,
                    size=int(body.get("size") or (len(inline_bytes) if inline_bytes else 0)),
                )
            )
            continue
        if not data:
            continue
        decoded = _decode_b64url(data).decode("utf-8", errors="replace")
        if mime == "text/plain":
            text_parts.append(decoded)
        elif mime == "text/html":
            html = decoded
    return "\n".join(text_parts), html, pending


def _parse_received_at(headers: dict[str, str], internal_date_ms: str | None) -> datetime:
    date_hdr = headers.get("date")
    if date_hdr:
        try:
            parsed = parsedate_to_datetime(date_hdr)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except (TypeError, ValueError):
            pass
    if internal_date_ms:
        return datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=UTC)
    return datetime.now(tz=UTC)


def _parse_recipients(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


class GmailSource:
    """Read-only view over a user's Gmail mailbox."""

    def __init__(self, credentials: Any) -> None:
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def fetch_message(
        self, message_id: str, *, fetch_attachments: bool = True
    ) -> Email | None:
        """Fetch a single message by its Gmail internal ID.

        If `fetch_attachments` is True (the default), attachment bytes
        are hydrated via a follow-up `attachments.get` per part, up to
        `_ATTACHMENT_MAX_BYTES` each. Larger parts are retained as
        metadata with an empty payload.

        Returns None if the message is no longer available (404/410 —
        user may have deleted the email after Google Calendar
        auto-extracted it) or if the ID is not a valid Gmail internal
        ID (400 — happens when `source.url` gave us a web-UI `plid`
        permalink token instead of a REST-addressable ID). All other
        errors propagate.
        """
        try:
            raw = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as e:
            status = getattr(e, "status_code", None) or e.resp.status
            if status in (400, 404, 410):
                log.info("gmail_message_missing", gmail_id=message_id, status=status)
                return None
            raise

        return self._raw_to_email(raw, message_id, fetch_attachments=fetch_attachments)

    def fetch_first_in_thread(
        self, thread_id: str, *, fetch_attachments: bool = True
    ) -> Email | None:
        """Fetch the oldest message in a Gmail thread by thread ID.

        Used by the plid-resolver path: a resolved plid gives us a
        thread ID (hex), and the original booking email is the first
        message in the thread. Later replies/updates are ignored —
        the first message is the labeled training example.

        Same 400/404/410 handling as `fetch_message`.
        """
        try:
            thread = (
                self._service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
        except HttpError as e:
            status = getattr(e, "status_code", None) or e.resp.status
            if status in (400, 404, 410):
                log.info("gmail_thread_missing", thread_id=thread_id, status=status)
                return None
            raise

        messages = thread.get("messages") or []
        if not messages:
            return None
        msg = messages[0]
        return self._raw_to_email(
            msg, msg.get("id", thread_id), fetch_attachments=fetch_attachments
        )

    def _raw_to_email(
        self, raw: dict[str, Any], message_id: str, *, fetch_attachments: bool
    ) -> Email:
        payload = raw.get("payload") or {}
        headers = _headers(payload)
        body_text, body_html, pending = _extract_bodies(payload)

        attachments = [
            self._hydrate_attachment(message_id, p, fetch=fetch_attachments)
            for p in pending
        ]

        rfc_message_id = headers.get("message-id") or f"gmail-internal-{message_id}"

        return Email(
            message_id=rfc_message_id,
            sender=headers.get("from", ""),
            recipients=_parse_recipients(headers.get("to")),
            subject=headers.get("subject", ""),
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            received_at=_parse_received_at(headers, raw.get("internalDate")),
        )

    def _hydrate_attachment(
        self, message_id: str, pending: _PendingAttachment, *, fetch: bool
    ) -> Attachment:
        """Materialize a _PendingAttachment into an Attachment.

        Uses inline bytes if Gmail already included them, else fetches
        via attachments.get. Oversized or unfetched attachments come
        back with `payload=b""` so the caller always sees the metadata.
        """
        payload: bytes = b""
        if pending.inline_payload is not None:
            payload = pending.inline_payload
        elif fetch and pending.attachment_id and pending.size <= _ATTACHMENT_MAX_BYTES:
            try:
                resp = (
                    self._service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=pending.attachment_id)
                    .execute()
                )
            except HttpError as e:
                log.warning(
                    "gmail_attachment_fetch_failed",
                    message_id=message_id,
                    filename=pending.filename,
                    error=str(e),
                )
            else:
                data = resp.get("data")
                if data:
                    payload = _decode_b64url(data)
        elif pending.size > _ATTACHMENT_MAX_BYTES:
            log.info(
                "gmail_attachment_too_large",
                message_id=message_id,
                filename=pending.filename,
                size=pending.size,
            )
        return Attachment(
            filename=pending.filename,
            content_type=pending.content_type,
            payload=payload,
        )

    def find_best_message(
        self,
        *,
        summary: str,
        around: datetime,
        window_hours: int = _SEARCH_WINDOW_HOURS,
    ) -> str | None:
        """Heuristically locate the Gmail message that seeded an auto-event.

        Used as a fallback when `source.url` gives us a web-UI `plid`
        token rather than a REST-addressable message ID. Searches Gmail
        for the event summary text within ±`window_hours` of `around`,
        then picks the candidate whose internalDate is closest. Returns
        the Gmail internal message ID, or None if nothing matches.
        """
        summary = summary.strip()
        if not summary:
            return None

        window = timedelta(hours=window_hours)
        after = (around - window).strftime("%Y/%m/%d")
        before = (around + window + timedelta(days=1)).strftime("%Y/%m/%d")
        safe_summary = summary.replace('"', "")
        query = f'"{safe_summary}" after:{after} before:{before}'

        try:
            resp = (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=_SEARCH_MAX_RESULTS)
                .execute()
            )
        except HttpError as e:
            log.warning("gmail_search_failed", query=query, error=str(e))
            return None

        candidates = resp.get("messages") or []
        if not candidates:
            return None

        best_id: str | None = None
        best_delta: timedelta | None = None
        for entry in candidates:
            mid = entry.get("id")
            if not mid:
                continue
            try:
                meta = (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=mid, format="metadata", metadataHeaders=["Date"])
                    .execute()
                )
            except HttpError:
                continue
            internal_ms = meta.get("internalDate")
            if not internal_ms:
                continue
            when = datetime.fromtimestamp(int(internal_ms) / 1000, tz=UTC)
            delta = abs(when - around)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_id = mid
        return best_id
