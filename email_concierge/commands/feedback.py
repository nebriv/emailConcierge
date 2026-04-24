"""feedback — convert user-deleted CalDAV events into negative training labels.

When Concierge creates an event and the user deletes it from their calendar
within `feedback_window_hours`, that's the strongest negative signal we can
get: the extraction was clearly wrong. Flip the matching `training_examples`
row to `label='neither'` (source 'feedback_delete') so the next classifier
train pulls in the correction.

Deletions *outside* the window are ignored — people also delete events for
legitimate reasons (event passed, plans changed, calendar hygiene), and
those shouldn't become negative labels.

Idempotent: re-running over the same state is a no-op.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from email_concierge import db
from email_concierge.config import settings
from email_concierge.log import get_logger

log = get_logger(__name__)


class _CalendarProtocol(Protocol):
    """Subset of caldav.Calendar we rely on. Tests inject a fake."""

    def event_by_uid(self, uid: str) -> Any: ...


def feedback_command(
    *,
    calendar: _CalendarProtocol | None = None,
) -> int:
    """Scan calendar_events within the feedback window and mark deletions.

    Args:
        calendar: optional pre-built CalDAV calendar handle. Tests inject
                  a fake; production leaves this None and the command
                  builds one from settings.
    """
    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    if calendar is None:
        calendar = _open_calendar()
        if calendar is None:
            return 2

    cutoff = datetime.now(tz=UTC) - timedelta(hours=cfg.feedback_window_hours)
    candidates = _recent_events(conn, since=cutoff)
    log.info(
        "feedback_scan_started",
        window_hours=cfg.feedback_window_hours,
        candidates=len(candidates),
    )

    n_deleted = 0
    n_present = 0
    for row in candidates:
        uid = row["ical_uid"]
        if _exists_on_server(calendar, uid):
            n_present += 1
            continue
        # The event we wrote is gone. Flip the matching training row.
        _mark_negative(conn, message_id=row["message_id"], uid=uid, summary=row["summary"])
        n_deleted += 1

    log.info(
        "feedback_scan_done",
        candidates=len(candidates),
        still_present=n_present,
        deletions_recorded=n_deleted,
    )
    return 0


def _recent_events(
    conn: sqlite3.Connection, *, since: datetime
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ical_uid, message_id, summary, created_at
          FROM calendar_events
         WHERE created_at >= ?
        """,
        (since.isoformat(),),
    ).fetchall()


def _exists_on_server(calendar: _CalendarProtocol, uid: str) -> bool:
    try:
        result = calendar.event_by_uid(uid)
    except Exception:  # noqa: BLE001 — caldav raises several types on not-found
        return False
    return result is not None


def _mark_negative(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    uid: str,
    summary: str | None,
) -> None:
    """Flip training_examples to label='neither' and record why.

    We overwrite any existing label rather than skipping — if an earlier
    pass labeled this `event` (because stage 4 extracted it) and the user
    then deleted it, the *latest* signal wins. The `extracted_json` note
    preserves the history for later inspection.
    """
    existing = conn.execute(
        "SELECT label, extracted_json FROM training_examples WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    if existing is None:
        # No training row — e.g. the event was written before training_examples
        # started logging. Nothing to update; log and move on.
        log.warning("feedback_no_training_row", message_id=message_id, uid=uid)
        return

    if existing["label"] == "neither" and existing["extracted_json"]:
        try:
            prev = json.loads(existing["extracted_json"])
        except json.JSONDecodeError:
            prev = {}
        if prev.get("feedback_delete", {}).get("uid") == uid:
            # Already recorded — idempotent.
            return

    try:
        prev_json = json.loads(existing["extracted_json"] or "{}")
    except json.JSONDecodeError:
        prev_json = {}
    prev_json["feedback_delete"] = {
        "uid": uid,
        "summary": summary,
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "prior_label": existing["label"],
    }

    conn.execute(
        """
        UPDATE training_examples
           SET label = 'neither',
               label_source = 'feedback_delete',
               extracted_json = ?
         WHERE message_id = ?
        """,
        (json.dumps(prev_json), message_id),
    )
    log.info(
        "feedback_marked_negative",
        message_id=message_id,
        uid=uid,
        prior_label=existing["label"],
    )


def _open_calendar() -> _CalendarProtocol | None:
    cfg = settings()
    if not cfg.caldav_url:
        log.error("feedback_no_caldav_configured")
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
    except Exception:  # noqa: BLE001 — network / auth / not-found all log-and-skip
        log.exception("feedback_caldav_open_failed")
        return None


__all__ = ["feedback_command"]
