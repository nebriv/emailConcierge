from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import caldav
from icalendar import Calendar, Event

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.models import ExtractionResult

log = get_logger(__name__)


class CaldavSink:
    """Writes extracted events to CalDAV. Update-by-UID: if an event with the
    same iCal UID already exists, update it instead of creating a duplicate.

    Honors settings.dry_run — in that mode, logs the would-be write and
    touches nothing.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._cfg = settings()
        self._client = None
        self._calendar = None
        if not self._cfg.dry_run:
            self._connect()

    def _connect(self) -> None:
        self._client = caldav.DAVClient(
            url=self._cfg.caldav_url,
            username=self._cfg.caldav_username,
            password=self._cfg.caldav_password,
        )
        principal = self._client.principal()
        try:
            self._calendar = principal.calendar(name=self._cfg.caldav_calendar)
        except caldav.lib.error.NotFoundError:
            log.error("caldav_calendar_not_found", name=self._cfg.caldav_calendar)
            raise

    def write(self, result: ExtractionResult, message_id: str) -> str:
        """Write the event and record it in calendar_events. Returns the iCal UID used."""
        uid = result.parsed.ical_uid or _deterministic_uid(message_id)
        ical_bytes = _build_vcalendar(result, uid)

        if self._cfg.dry_run:
            log.info(
                "dry_run_would_write",
                uid=uid,
                title=result.parsed.title,
                start=result.parsed.start.isoformat(),
                message_id=message_id,
            )
            return uid

        assert self._calendar is not None  # populated by _connect when not dry-run
        existing = _find_by_uid(self._calendar, uid)
        now_iso = datetime.now(tz=UTC).isoformat()

        if existing is not None:
            existing.data = ical_bytes
            existing.save()
            log.info("caldav_updated", uid=uid, title=result.parsed.title, message_id=message_id)
            self._conn.execute(
                """
                UPDATE calendar_events
                   SET summary = ?, starts_at = ?, updated_at = ?
                 WHERE ical_uid = ?
                """,
                (
                    result.parsed.title,
                    result.parsed.start.isoformat(),
                    now_iso,
                    uid,
                ),
            )
            caldav_url = str(existing.url) if getattr(existing, "url", None) else ""
        else:
            event_obj = self._calendar.save_event(ical_bytes)
            log.info("caldav_created", uid=uid, title=result.parsed.title, message_id=message_id)
            caldav_url = str(event_obj.url) if getattr(event_obj, "url", None) else ""
            self._conn.execute(
                """
                INSERT OR REPLACE INTO calendar_events
                    (ical_uid, message_id, caldav_url, summary, starts_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    message_id,
                    caldav_url,
                    result.parsed.title,
                    result.parsed.start.isoformat(),
                    now_iso,
                    now_iso,
                ),
            )
        return uid


def _deterministic_uid(message_id: str) -> str:
    """Generate a stable UID so re-processing the same email won't duplicate."""
    digest = hashlib.sha1(message_id.encode("utf-8")).hexdigest()
    return f"{digest}@email-concierge"


def _build_vcalendar(result: ExtractionResult, uid: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//email-concierge//EN")
    cal.add("version", "2.0")

    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(tz=UTC))
    event.add("summary", result.parsed.title)
    event.add("dtstart", result.parsed.start)
    if result.parsed.end is not None:
        event.add("dtend", result.parsed.end)
    if result.parsed.location:
        event.add("location", result.parsed.location)
    if result.parsed.description:
        event.add("description", result.parsed.description)
    # Tag the event so users can tell what wrote it.
    event.add(
        "x-email-concierge-source",
        f"stage={result.handled_by_stage};name={result.handled_by_name};"
        f"confidence={result.confidence:.3f}",
    )

    cal.add_component(event)
    return cal.to_ical()


def _find_by_uid(calendar, uid: str):
    """Look up an event by UID. caldav.Calendar exposes event_by_uid on most versions."""
    try:
        return calendar.event_by_uid(uid)
    except caldav.lib.error.NotFoundError:
        return None
    except Exception:
        log.exception("caldav_lookup_failed", uid=uid)
        return None
