"""forget — drop a calendar_events row so the feedback scan won't flag it.

The feedback scanner flips training_examples to label='neither' when a
Concierge-written event disappears from CalDAV within the feedback
window. That's the right default — but it means the user can't freely
delete an event without teaching the classifier the extraction was
wrong. This command breaks that coupling.

Usage:
    forget <uid>                       # drop the row; user deletes remotely
    forget <uid> --delete-remote       # also delete from CalDAV in one step
    forget <uid> --dry-run             # report what would happen

Does NOT touch training_examples. The label that was written at
extraction time stands — whatever it was, the user has judged it
neither a confirmation nor a rejection, just "remove this one".
"""

from __future__ import annotations

from typing import Any, Protocol

from email_concierge import db
from email_concierge.config import settings
from email_concierge.log import get_logger

log = get_logger(__name__)


class _CalendarProtocol(Protocol):
    """Subset of caldav.Calendar we rely on. Tests inject a fake."""

    def event_by_uid(self, uid: str) -> Any: ...


def forget_command(
    *,
    uid: str,
    delete_remote: bool = False,
    dry_run: bool = False,
    calendar: _CalendarProtocol | None = None,
) -> int:
    """Drop the calendar_events row for `uid`.

    Args:
        uid: the iCal UID of the event to forget.
        delete_remote: if True, also delete the event from CalDAV.
        dry_run: report what would happen without touching anything.
        calendar: optional pre-built CalDAV calendar handle. Tests inject
                  a fake; production leaves this None and the command
                  builds one from settings when --delete-remote is set.
    """
    if not uid:
        log.error("forget_no_uid")
        return 2

    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    row = conn.execute(
        """
        SELECT ical_uid, message_id, summary, starts_at
          FROM calendar_events
         WHERE ical_uid = ?
        """,
        (uid,),
    ).fetchone()
    if row is None:
        log.warning("forget_uid_not_found", uid=uid)
        return 1

    log.info(
        "forget_target",
        uid=uid,
        message_id=row["message_id"],
        summary=row["summary"],
        starts_at=row["starts_at"],
        delete_remote=delete_remote,
        dry_run=dry_run,
    )

    if dry_run:
        log.info("forget_dry_run_done", uid=uid)
        return 0

    if delete_remote:
        if calendar is None:
            calendar = _open_calendar()
            if calendar is None:
                # Don't delete the local row if we can't confirm remote
                # state — leaving the user with a half-applied change
                # is worse than failing loudly.
                log.error("forget_caldav_unavailable", uid=uid)
                return 2
        _delete_remote(calendar, uid)

    conn.execute("DELETE FROM calendar_events WHERE ical_uid = ?", (uid,))
    conn.commit()
    log.info("forget_done", uid=uid, deleted_remote=delete_remote)
    return 0


def _delete_remote(calendar: _CalendarProtocol, uid: str) -> None:
    """Best-effort remote delete. A 'not found' is fine — the calling
    user likely already deleted it in their calendar app and is now
    telling us to forget. A real failure is logged but does not abort
    the local drop; the user can re-run if they want to retry."""
    try:
        event = calendar.event_by_uid(uid)
    except Exception:  # noqa: BLE001
        log.info("forget_remote_not_found", uid=uid)
        return
    if event is None:
        log.info("forget_remote_not_found", uid=uid)
        return
    try:
        event.delete()
        log.info("forget_remote_deleted", uid=uid)
    except Exception:  # noqa: BLE001
        log.exception("forget_remote_delete_failed", uid=uid)


def _open_calendar() -> _CalendarProtocol | None:
    cfg = settings()
    if not cfg.caldav_url:
        log.error("forget_no_caldav_configured")
        return None
    try:
        import caldav

        client = caldav.DAVClient(
            url=cfg.caldav_url,
            username=cfg.caldav_username,
            password=cfg.caldav_password,
        )
        principal = client.principal()
        return principal.calendar(name=cfg.caldav_calendar)
    except Exception:  # noqa: BLE001
        log.exception("forget_caldav_open_failed")
        return None


__all__ = ["forget_command"]
