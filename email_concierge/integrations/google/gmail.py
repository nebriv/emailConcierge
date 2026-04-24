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
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from email_concierge.log import get_logger
from email_concierge.models import Attachment, Email

log = get_logger(__name__)

_PREVIEW_MAX = 2000


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


def _extract_bodies(payload: dict[str, Any]) -> tuple[str, str | None, list[Attachment]]:
    text_parts: list[str] = []
    html: str | None = None
    attachments: list[Attachment] = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        filename = part.get("filename") or ""
        if filename:
            # Attachment. We store shape only; raw bytes are fetched on
            # demand via a separate attachments.get call, which this
            # pipeline doesn't need for training data.
            attachments.append(
                Attachment(
                    filename=filename,
                    content_type=mime or "application/octet-stream",
                    payload=b"",
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
    return "\n".join(text_parts), html, attachments


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

    def fetch_message(self, message_id: str) -> Email | None:
        """Fetch a single message by its Gmail internal ID.

        Returns None if the message is no longer available (404 — user
        may have deleted the email after Google Calendar auto-extracted
        the event from it). All other errors propagate.
        """
        try:
            raw = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as e:
            if getattr(e, "status_code", None) == 404 or e.resp.status in (404, 410):
                log.info("gmail_message_missing", gmail_id=message_id)
                return None
            raise

        payload = raw.get("payload") or {}
        headers = _headers(payload)
        body_text, body_html, attachments = _extract_bodies(payload)

        preview = body_text[:_PREVIEW_MAX]
        rfc_message_id = headers.get("message-id") or f"gmail-internal-{message_id}"

        return Email(
            message_id=rfc_message_id,
            sender=headers.get("from", ""),
            recipients=_parse_recipients(headers.get("to")),
            subject=headers.get("subject", ""),
            body_text=preview,
            body_html=body_html,
            attachments=attachments,
            received_at=_parse_received_at(headers, raw.get("internalDate")),
        )
