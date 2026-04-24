"""Read-only IMAP wrapper. The ONLY module in the codebase that imports imap_tools.

Safety contract (CLAUDE.md section 4):
- Layer 1: folders opened in EXAMINE mode via folder.set(readonly=True).
- Layer 2: fetch() always passes mark_seen=False.
- Layer 3: this class deliberately does not define delete, move, copy, append,
  expunge, seen, flag, unflag, store, or any other mutating method.
- Layer 4: test_imap_readonly.py asserts no mutating IMAP commands are issued.

If you are adding a method to this class, you are working on the most
safety-critical file in the codebase. Required before landing:
- A corresponding test in tests/test_imap_readonly.py.
- Explicit human review. Do not merge otherwise.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from imap_tools import MailBox, MailBoxUnencrypted  # noqa: TID251

from email_concierge.log import get_logger
from email_concierge.models import Attachment, Email

if TYPE_CHECKING:
    from imap_tools import MailMessage  # noqa: TID251


log = get_logger(__name__)


class ReadOnlyMailbox:
    """A deliberately narrow IMAP interface. Exposes only read operations.

    It is impossible to call a mutating method on this class because they
    simply are not defined here. Upstream imap-tools methods for mutation
    (delete, move, copy, append, expunge, seen, flag, unflag) are not
    exposed and must never be added without re-reading section 4 of
    CLAUDE.md and updating the layer-4 test.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
    ) -> None:
        cls = MailBox if use_ssl else MailBoxUnencrypted
        log.debug("imap_connect", host=host, port=port, ssl=use_ssl)
        self._mb = cls(host, port)
        # initial_folder=None avoids any auto-SELECT on login.
        self._mb.login(username, password, initial_folder=None)
        log.debug("imap_login_ok", user=username)

    def examine(self, folder: str) -> None:
        """Open folder in read-only (EXAMINE) mode.

        imap-tools' folder.set(readonly=True) issues EXAMINE rather than SELECT.
        """
        log.debug("imap_examine", folder=folder)
        self._mb.folder.set(folder, readonly=True)

    def folder_list(self) -> list[str]:
        return [f.name for f in self._mb.folder.list()]

    def fetch(
        self,
        criteria: str = "ALL",
        limit: int | None = None,
        reverse: bool = False,
    ) -> Iterator[Email]:
        """Fetch messages. Always uses mark_seen=False and BODY.PEEK[]."""
        log.debug("imap_fetch", criteria=criteria, limit=limit)
        for msg in self._mb.fetch(
            criteria,
            mark_seen=False,
            bulk=True,
            limit=limit,
            reverse=reverse,
        ):
            yield _to_email(msg)

    def idle_wait(self, timeout_seconds: int) -> bool:
        """Block in IDLE until activity or timeout. Returns True if mailbox changed."""
        log.debug("imap_idle", timeout_seconds=timeout_seconds)
        # imap-tools has had small API churn around idle; normalize here.
        result = self._mb.idle.wait(timeout=timeout_seconds)
        if isinstance(result, bool):
            return result
        if isinstance(result, (list, tuple)):
            return len(result) > 0
        return bool(result)

    def logout(self) -> None:
        log.debug("imap_logout")
        try:
            self._mb.logout()
        except Exception:
            log.exception("imap_logout_failed")

    def __enter__(self) -> ReadOnlyMailbox:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.logout()


def _to_email(msg: MailMessage) -> Email:
    message_id = _extract_message_id(msg)
    received_at = msg.date if getattr(msg, "date", None) else datetime.now(tz=UTC)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)

    attachments = [
        Attachment(
            filename=a.filename or "",
            content_type=a.content_type or "application/octet-stream",
            payload=a.payload or b"",
        )
        for a in getattr(msg, "attachments", [])
    ]

    return Email(
        message_id=message_id,
        sender=msg.from_ or "",
        recipients=list(getattr(msg, "to", ()) or ()),
        subject=msg.subject or "",
        body_text=msg.text or "",
        body_html=msg.html or None,
        attachments=attachments,
        received_at=received_at,
    )


def _extract_message_id(msg: MailMessage) -> str:
    raw = None
    obj = getattr(msg, "obj", None)
    if obj is not None:
        raw = obj.get("Message-ID") or obj.get("Message-Id")
    if not raw:
        headers = getattr(msg, "headers", {}) or {}
        val = headers.get("message-id") or headers.get("Message-ID")
        if isinstance(val, (tuple, list)) and val:
            raw = val[0]
        elif isinstance(val, str):
            raw = val
    if not raw:
        uid = getattr(msg, "uid", None) or "unknown"
        raw = f"<{uid}@imap-no-message-id>"
    return raw.strip().strip("<>").strip()
