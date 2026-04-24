"""Eventbrite stage-2 extractor.

Eventbrite ships a schema.org JSON-LD blob in every order/registration
email, inside a <script type="application/ld+json"> tag at the bottom
of the HTML body. Shape (reservationFor → Event → location → address):

    {
      "@type": "EventReservation",
      "reservationFor": {
        "@type": "Event",
        "name": "Intrepid Museum Presents Astronomy Night",
        "startDate": "2026-04-24 17:30:00",
        "endDate":   "2026-04-24 21:00:00",
        "location": {
          "name": "Intrepid Museum",
          "address": { "streetAddress": "...", "addressLocality": "New York",
                       "addressRegion": "NY", "postalCode": "10036",
                       "addressCountry": "US" }
        }
      }
    }

Structured data is always the cheapest extraction path: no regex
guessing, no locale parsing. If the JSON-LD is missing or malformed
we return None and the router falls through to stage 3/4.

startDate has no timezone on it; Eventbrite stores times in the
event's local timezone. We localize to the configured user_timezone,
which is right when the user is attending in their home timezone and
close enough for calendar purposes otherwise.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult, ParsedEvent

log = get_logger(__name__)


_SENDER_RE = re.compile(r"@(?:order\.)?eventbrite\.(?:com|ca|co\.uk)\b", re.IGNORECASE)

_SUBJECT_STRONG = (
    "order confirmation",
    "registration confirmation",
    "your tickets",
    "ticket confirmation",
)

# JSON-LD script tag; flexible about attribute order and whitespace.
_JSONLD_RE = re.compile(
    r"<script[^>]*\btype\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


class EventbriteExtractor:
    name = "eventbrite"
    stage = 2
    priority = 10

    def can_handle(self, email: Email) -> float:
        if not _SENDER_RE.search(email.sender or ""):
            return 0.0
        subject = (email.subject or "").lower()
        if any(h in subject for h in _SUBJECT_STRONG):
            return 1.0
        return 0.5

    def extract(self, email: Email) -> ExtractionResult | None:
        source = email.body_html or email.body_text or ""
        if not source:
            return None

        event = _find_event_blob(source)
        if event is None:
            log.debug("eventbrite_no_json_ld", subject=email.subject)
            return None

        tz = ZoneInfo(settings().user_timezone)
        start = _parse_naive_datetime(event.get("startDate"), tz)
        if start is None:
            return None
        end = _parse_naive_datetime(event.get("endDate"), tz)

        title = (event.get("name") or "").strip() or "Eventbrite event"
        location = _format_location(event.get("location"))

        parsed = ParsedEvent(
            title=title,
            start=start,
            end=end,
            location=location,
            description=f"Eventbrite: {title}" if location else None,
            ical_uid=None,
        )
        return ExtractionResult(
            handled_by_stage=self.stage,
            handled_by_name=self.name,
            # JSON-LD is structured and authoritative — high confidence.
            # We only back off when the blob is missing (handled above).
            confidence=0.95,
            parsed=parsed,
            latency_ms=0,
        )


# --------------------------------------------------------------------- helpers

def _find_event_blob(source: str) -> dict[str, Any] | None:
    """Return the Event dict from the first parseable JSON-LD block.

    Eventbrite's production template emits *malformed* JSON — URL values
    like `@context`, `reservationStatus`, `modifyReservationUrl` end with
    `~` and a newline but have no closing double-quote. We try strict
    parsing first, then fall back to a targeted repair.
    """
    for m in _JSONLD_RE.finditer(source):
        blob = m.group(1).strip()
        parsed = _safe_json(blob) or _safe_json(_repair_unterminated_urls(blob))
        if parsed is None:
            continue
        event = _as_event(parsed)
        if event is not None:
            return event
    return None


def _safe_json(blob: str) -> Any | None:
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


# URL string that opens a quote, runs a scheme, and ends at a line break
# without ever closing the quote. Eventbrite-specific breakage.
_UNTERMINATED_URL_RE = re.compile(r'"(https?://[^"\n\r]*?)(\s*[\r\n])')


def _repair_unterminated_urls(blob: str) -> str:
    """Inject a closing double-quote when a URL string runs into a line
    break without closing. Safe to call on well-formed JSON: the regex
    requires absence of a closing quote before the newline, so properly
    terminated strings (which always close the quote before any newline)
    don't match.
    """
    return _UNTERMINATED_URL_RE.sub(r'"\1"\2', blob)


def _as_event(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    typ = str(obj.get("@type", "")).lower()
    if typ == "event":
        return obj
    if typ == "eventreservation":
        ev = obj.get("reservationFor")
        if isinstance(ev, dict):
            return ev
    return None


def _parse_naive_datetime(value: Any, tz: ZoneInfo) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    # Try ISO with 'T', then Eventbrite's space-separated variant.
    for candidate in (value, value.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    return None


def _format_location(loc: Any) -> str | None:
    if not isinstance(loc, dict):
        return None
    name = (loc.get("name") or "").strip()
    addr = loc.get("address")
    parts: list[str] = []
    if name:
        parts.append(name)
    if isinstance(addr, dict):
        for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode"):
            val = (addr.get(key) or "").strip()
            if val:
                parts.append(val)
    joined = ", ".join(parts)
    return joined or None
