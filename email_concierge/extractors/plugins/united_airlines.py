"""United Airlines stage-2 extractor.

United has iterated through at least three distinct email templates over
the years:

  A. Current eTicket receipt ("Flight 1 of 2 UA2389 ... (LGA) ... (ORD)")
  B. Older eTicket receipt (compact date "Thu, 27FEB14"; city line and
     "(IATA - NAME)" on separate lines)
  C. Booking confirmation ("Flight to Bozeman" header; bare IATA codes on
     their own lines; flight number *after* the times and cities)

Rather than parse each layout structurally, we collect loose signals
from the body text — dates, times, IATA codes, flight numbers — and
build a single best-effort event from their order of appearance. This
is deliberately lossy: the router accepts it only if confidence clears
the floor, and stage 4 is always available as a fallback.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult, ParsedEvent

log = get_logger(__name__)


_SENDER_RE = re.compile(r"@(?:united\.com|uafrequentflyer\.com)\b", re.IGNORECASE)
_SUBJECT_STRONG = (
    "eticket itinerary",
    "booking confirmation",
    "your flight receipt",
    "flight confirmation",
)

_CONFIRM_SUBJECT_RE = re.compile(
    r"(?:confirmation(?:\s*(?:number|#))?\s*[:\-]?\s*|-|\u2013\s*)\s*([A-Z0-9]{6})\b",
    re.IGNORECASE,
)
_CONFIRM_BODY_RE = re.compile(
    r"confirmation\s*(?:number|#)?\s*:?\s*([A-Z0-9]{6})\b",
    re.IGNORECASE,
)

# "UA 2389" or "UA2389" — flight number, 2-4 digits.
_FLIGHT_RE = re.compile(r"\bUA\s*0*(\d{2,4})\b")

# IATA code in parens, with optional descriptor: "(LGA)", "(EWR - LIBERTY)".
_IATA_PAREN_RE = re.compile(r"\(([A-Z]{3})(?:\s*[\-\u2013][^)]*)?\)")
# Bare IATA on its own line (layout C uses this).
_IATA_BARE_LINE_RE = re.compile(r"^([A-Z]{3})$", re.MULTILINE)
# Three-letter tokens that show up in emails but aren't airports.
_IATA_BLACKLIST = {"USA", "USD", "EUR", "GMT", "UTC", "AMP", "URL",
                   "PDF", "CSV", "PDT", "EST", "PST", "CST", "MST",
                   "EDT", "CDT", "MDT", "GST", "AST"}

# Time: "06:10 AM", "6:10 PM".
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP]M)\b", re.IGNORECASE)

# Dates — three variants United uses:
# Layout A: "Wed, Apr 01, 2026"
# Layout A: "Apr 01, 2026" / "April 01, 2026"
# Layout B: "Thu, 27FEB14"  (compact, 2-digit year)
_DATE_LONG_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_NO_DAY_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_COMPACT_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
    r"(\d{1,2})([A-Z]{3})(\d{2})\b",
)

_DEST_HEADER_RE = re.compile(r"\bFlight to ([A-Z][A-Za-z .'\-]+)", re.IGNORECASE)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}


class UnitedAirlinesExtractor:
    name = "united_airlines"
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
        text = _body_text(email)
        if not text:
            return None

        dates = _collect_dates(text)
        times = _collect_times(text)
        iatas = _collect_iatas(text)
        flights = _collect_flights(text)

        if not dates or not times or not iatas:
            log.debug(
                "united_missing_signals",
                n_dates=len(dates),
                n_times=len(times),
                n_iatas=len(iatas),
                subject=email.subject,
            )
            return None

        tz = ZoneInfo(settings().user_timezone)
        # Use the most-frequent date for both start and end. Emails often
        # contain unrelated dates (booking date, ticket issue date, "valid
        # through"); the travel date is almost always the one repeated
        # most. Ties break by order of appearance.
        mode_date = _mode_date(dates)
        start = _combine(mode_date, times[0], tz)
        if start is None:
            return None

        end: datetime | None = None
        if len(times) >= 2:
            # If the times wrap past midnight (e.g., 11:15 PM → 5:24 AM)
            # and we only captured one date, the trip spans multiple days
            # and we can't guess the return date. Leave end empty rather
            # than collapsing the whole trip onto one day.
            minutes = [_time_to_minutes(t) for t in times]
            wraps_midnight = any(
                a >= b for a, b in zip(minutes, minutes[1:], strict=False)
            )
            unique_dates = {d for d in dates}
            if wraps_midnight and len(unique_dates) == 1:
                end = None
            else:
                end = _combine(mode_date, times[-1], tz)
                if end is not None and end <= start:
                    end = None

        origin = iatas[0]
        # Destination detection is brittle in multi-leg itineraries: a
        # connection hub (e.g. ORD on an LGA→ORD→BZN trip) can show up
        # more often in the body than the turnaround airport. So we only
        # trust it when:
        #  (a) there's an explicit "Flight to <city>" header, or
        #  (b) there are exactly two distinct IATAs (one-way or simple round trip).
        # For 3+ distinct IATAs we leave it None and drop confidence
        # below the router floor so the LLM fallback can handle it.
        has_header = _DEST_HEADER_RE.search(text) is not None
        unique_iatas = set(iatas)
        destination: str | None
        if len(unique_iatas) == 2:
            destination = next(c for c in iatas if c != origin)
        else:
            destination = None

        title = _build_title(text, email.subject or "", destination, flights, iatas)
        conf = _confirmation_code(text, email.subject or "")

        description = _build_description(flights, origin, destination, conf)

        # Confidence tiers:
        #   0.9  — simple trip (has header or 2 IATAs) with flights + confirmation
        #   0.75 — partial: missing one of flights / confirmation
        #   0.6  — multi-leg with no destination header: let LLM take over
        if len(unique_iatas) >= 3 and not has_header:
            confidence = 0.6
        elif flights and conf and (destination or has_header):
            confidence = 0.9
        else:
            confidence = 0.75

        parsed = ParsedEvent(
            title=title,
            start=start,
            end=end,
            location=origin,
            description=description,
            ical_uid=None,
        )
        return ExtractionResult(
            handled_by_stage=self.stage,
            handled_by_name=self.name,
            confidence=confidence,
            parsed=parsed,
            latency_ms=0,
        )


# --------------------------------------------------------------------- helpers

def _body_text(email: Email) -> str:
    if email.body_html:
        tree = HTMLParser(email.body_html)
        if tree.body:
            return tree.body.text(separator="\n", strip=True)
    return email.body_text or ""


def _collect_dates(text: str) -> list[tuple[int, int, int]]:
    """Return date tuples (year, month, day) in order of appearance.
    Tries long-with-day, then no-day, then compact-2014-style patterns.
    """
    out: list[tuple[int, int, int]] = []

    def _add(year: int, month: int, day: int) -> None:
        out.append((year, month, day))

    for m in _DATE_LONG_RE.finditer(text):
        month = _MONTHS.get(m.group(1).lower()[:3])
        if month:
            try:
                _add(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass
    if out:
        return out
    for m in _DATE_NO_DAY_RE.finditer(text):
        month = _MONTHS.get(m.group(1).lower()[:3])
        if month:
            try:
                _add(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass
    if out:
        return out
    for m in _DATE_COMPACT_RE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if month:
            try:
                year = 2000 + int(m.group(3))
                _add(year, month, int(m.group(1)))
            except ValueError:
                pass
    return out


def _collect_times(text: str) -> list[tuple[str, str, str]]:
    return _TIME_RE.findall(text)


def _collect_iatas(text: str) -> list[str]:
    """Ordered list of airport codes. Parenthesized matches take priority;
    fall back to bare 3-letter codes on their own line when none are
    parenthesized (layout C: booking confirmation).

    When some codes repeat (itinerary) and others don't (footer marketing
    copy like "Orange County (SNA)"), keep only the repeated ones — the
    singletons are almost always not part of this trip.
    """
    parens = [
        code for code in _IATA_PAREN_RE.findall(text)
        if code not in _IATA_BLACKLIST
    ]
    if parens:
        counts = Counter(parens)
        repeated = {c for c, n in counts.items() if n >= 2}
        # Drop singleton IATAs only when they occur *after* all repeated
        # codes — that's the marketing-footer pattern (e.g., "(SNA)" in a
        # list of destinations United serves). A singleton inside the
        # itinerary block is likely a real one-way leg.
        if len(repeated) >= 2:
            last_repeat_idx = max(
                i for i, c in enumerate(parens) if c in repeated
            )
            parens = [
                c for i, c in enumerate(parens)
                if i <= last_repeat_idx or c in repeated
            ]
        return parens
    bare = [
        code for code in _IATA_BARE_LINE_RE.findall(text)
        if code not in _IATA_BLACKLIST
    ]
    return bare


def _collect_flights(text: str) -> list[str]:
    seen: list[str] = []
    for m in _FLIGHT_RE.finditer(text):
        no = m.group(1)
        if no not in seen:
            seen.append(no)
    return seen


def _time_to_minutes(time_match: tuple[str, str, str]) -> int:
    hour = int(time_match[0])
    minute = int(time_match[1])
    if time_match[2].upper() == "PM" and hour != 12:
        hour += 12
    elif time_match[2].upper() == "AM" and hour == 12:
        hour = 0
    return hour * 60 + minute


def _mode_date(dates: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    """Most frequent date; ties break to the earliest occurrence."""
    counts = Counter(dates)
    top = counts.most_common(1)[0][1]
    for d in dates:
        if counts[d] == top:
            return d
    return dates[0]


def _combine(
    date_tuple: tuple[int, int, int],
    time_match: tuple[str, str, str],
    tz: ZoneInfo,
) -> datetime | None:
    year, month, day = date_tuple
    hour = int(time_match[0])
    minute = int(time_match[1])
    ampm = time_match[2].upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None


def _build_title(
    text: str,
    subject: str,
    destination: str | None,
    flights: list[str],
    iatas: list[str],
) -> str:
    # Booking-confirmation template has a prominent "Flight to <city>" header.
    m = _DEST_HEADER_RE.search(text)
    if m:
        return f"Flight to {m.group(1).strip()}"
    if destination:
        return f"Flight to {destination}"
    if flights:
        return f"United flight UA {flights[0]}"
    if iatas:
        return f"United flight from {iatas[0]}"
    return "United flight"


def _build_description(
    flights: list[str],
    origin: str | None,
    destination: str | None,
    conf: str | None,
) -> str | None:
    bits: list[str] = []
    if flights:
        bits.append("Flights: " + ", ".join(f"UA {f}" for f in flights))
    if origin and destination:
        bits.append(f"{origin} \u2192 {destination}")
    elif origin:
        bits.append(f"From {origin}")
    if conf:
        bits.append(f"Confirmation: {conf}")
    return "\n".join(bits) if bits else None


def _confirmation_code(text: str, subject: str) -> str | None:
    """Prefer body match (explicit label), fall back to subject. Subject
    patterns: "... for Confirmation FAKE01", "... booking confirmation – FAKE01".
    """
    m = _CONFIRM_BODY_RE.search(text)
    if m:
        return m.group(1).upper()
    m = _CONFIRM_SUBJECT_RE.search(subject)
    if m:
        return m.group(1).upper()
    return None
