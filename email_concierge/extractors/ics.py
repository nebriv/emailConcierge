from __future__ import annotations

import time
from datetime import UTC, date, datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from icalendar import Calendar

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult, ParsedEvent

log = get_logger(__name__)


class IcsExtractor:
    """Stage 1: parse .ics attachments. This is the deterministic gold path."""

    name = "ics"
    stage = 1
    priority = 0

    def can_handle(self, email: Email) -> float:
        for att in email.attachments:
            if _is_ics_attachment(att.filename, att.content_type):
                return 1.0
        return 0.0

    def extract(self, email: Email) -> ExtractionResult | None:
        t0 = time.perf_counter()
        for att in email.attachments:
            if not _is_ics_attachment(att.filename, att.content_type):
                continue
            try:
                cal = Calendar.from_ical(att.payload)
            except Exception:
                log.exception("ics_parse_failed", filename=att.filename)
                continue

            event = _pick_vevent(cal)
            if event is None:
                continue

            parsed = _vevent_to_parsed_event(event)
            if parsed is None:
                continue

            latency_ms = int((time.perf_counter() - t0) * 1000)
            return ExtractionResult(
                handled_by_stage=self.stage,
                handled_by_name=self.name,
                confidence=1.0,
                parsed=parsed,
                latency_ms=latency_ms,
            )
        return None


def _is_ics_attachment(filename: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    fn = (filename or "").lower()
    return ct.startswith("text/calendar") or fn.endswith(".ics")


def _pick_vevent(cal: Calendar):
    vevents = [c for c in cal.walk() if c.name == "VEVENT"]
    if not vevents:
        return None

    now_utc = datetime.now(tz=UTC)
    future: list = []
    for v in vevents:
        start = _extract_dt(v, "DTSTART")
        if start is None:
            continue
        if start >= now_utc:
            future.append((start, v))
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]

    # No future events; return the first one with a parseable DTSTART.
    for v in vevents:
        if _extract_dt(v, "DTSTART") is not None:
            return v
    return vevents[0]


def _vevent_to_parsed_event(vevent) -> ParsedEvent | None:
    start = _extract_dt(vevent, "DTSTART")
    if start is None:
        return None
    end = _extract_dt(vevent, "DTEND")

    summary = _as_str(vevent.get("SUMMARY")) or "(no title)"
    location = _as_str(vevent.get("LOCATION")) or None
    description = _as_str(vevent.get("DESCRIPTION")) or None
    uid = _as_str(vevent.get("UID")) or None

    return ParsedEvent(
        title=summary,
        start=start,
        end=end,
        location=location,
        description=description,
        ical_uid=uid,
    )


def _extract_dt(vevent, key: str) -> datetime | None:
    prop = vevent.get(key)
    if prop is None:
        return None
    value = getattr(prop, "dt", prop)
    return _coerce_datetime(value)


def _coerce_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            tz = _user_tz()
            return value.replace(tzinfo=tz)
        return value
    if isinstance(value, date):
        tz = _user_tz()
        return datetime.combine(value, dtime(0, 0), tzinfo=tz)
    return None


def _user_tz():
    try:
        return ZoneInfo(settings().user_timezone)
    except Exception:
        return UTC


def _as_str(value) -> str | None:
    if value is None:
        return None
    try:
        return str(value).strip() or None
    except Exception:
        return None
