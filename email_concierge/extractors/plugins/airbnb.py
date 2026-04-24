"""Airbnb stage-2 extractor.

Three practical email layouts to handle:

  A. Automated confirmation / "reservation confirmed" / reservation reminder
     / "address of where you're staying" (sender: automated@airbnb.com).
     Shape:
         YOU'RE ALL SET FOR <CITY>          (or YOU'RE GOING TO:)
         <LISTING NAME>
         <Room type> hosted by <Host>
         Check-in        Checkout
         Fri, May 1      Sun, May 3
         After 3:00 PM   By 11:00 AM
         ADDRESS
         <full address>

  B. Trip invitation from another guest (sender: invitation@airbnb.com).
     Shape:
         <LISTING NAME>
         Entire home/apt hosted by <Host>
         Thursday November 3, 2022 - Monday November 7, 2022
         Address
         <full address>

  C. Receipt / "Confirmed: Your May 1 – 3 trip" — structurally identical to A,
     just a different subject.

Strategy mirrors the United plugin: collect loose signals (marker line,
date range, address block) and assemble a best-effort event. If any
required piece is missing we return None and the router falls through
to stage 3/4.

Check-in/checkout times default to 15:00 / 11:00 in the user's timezone,
matching Airbnb's own convention when the email omits them. When the
date line has no year (layout A), we infer the year from the email's
received_at: upcoming-trip emails never reference the past, so the next
occurrence of (month, day) on-or-after received_at is unambiguous.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from selectolax.parser import HTMLParser

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult, ParsedEvent

log = get_logger(__name__)


_SENDER_RE = re.compile(r"@airbnb\.com\b", re.IGNORECASE)

# Subject lines that mark a bookable/known trip. Everything else from
# Airbnb (host messages, reviews, "pricing tips", marketing) gets 0.0.
_SUBJECT_STRONG = (
    "reservation confirmed for",
    "confirmed: your",
    "invited you on their",
    "address of where",
    "reservation reminder",
    "you're going to",
    "you\u2019re going to",
)
_SUBJECT_WEAK = (
    "your airbnb",
    "your upcoming",
    "trip to",
)

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}

# Layout-B dash date range: "Thursday November 3, 2022 - Monday November 7, 2022"
_FULL_RANGE_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(\d{1,2}),?\s+(\d{4})\s*[\-\u2013]\s*"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)

# Layout-A compact date (year absent): "Fri, May 1", "Wed, April 15".
_COMPACT_DAY_RE = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(\d{1,2})\b",
    re.IGNORECASE,
)

# Time: "3:00 PM", "11:00 AM" — sometimes prefixed "After " / "By ".
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AP]M)\b", re.IGNORECASE)

# City-in-upper-case header line, e.g. "YOU'RE ALL SET FOR TROMSØ".
# Matches straight and curly apostrophes.
_YOURE_HEADER_RE = re.compile(
    r"YOU(?:'|\u2019)RE\s+(?:ALL\s+SET\s+FOR|GOING\s+TO)\b",
)

# The line that sits right before the dates and addresses in layout A.
_CHECKIN_LABEL_RE = re.compile(r"\bCheck[\-\s]?in\s+Check[\-\s]?out\b", re.IGNORECASE)

_ADDRESS_LABEL_RE = re.compile(r"^\s*ADDRESS\s*$", re.IGNORECASE | re.MULTILINE)

_HOSTED_BY_RE = re.compile(
    r"^(?P<room>(?:Entire|Private|Shared)\s+[A-Za-z/ ]+?)\s+hosted by\s+(?P<host>[^\n]+)$",
    re.IGNORECASE | re.MULTILINE,
)


class AirbnbExtractor:
    name = "airbnb"
    stage = 2
    priority = 10

    def can_handle(self, email: Email) -> float:
        if not _SENDER_RE.search(email.sender or ""):
            return 0.0
        subject = (email.subject or "").lower()
        if any(h in subject for h in _SUBJECT_STRONG):
            return 1.0
        if any(h in subject for h in _SUBJECT_WEAK):
            return 0.6
        return 0.0

    def extract(self, email: Email) -> ExtractionResult | None:
        text = _body_text(email)
        if not text:
            return None

        tz = ZoneInfo(settings().user_timezone)

        date_range = _parse_date_range(text, tz=tz, received_at=email.received_at)
        if date_range is None:
            log.debug("airbnb_no_date_range", subject=email.subject)
            return None
        start, end = date_range

        address = _extract_address(text)
        listing = _extract_listing(text)

        title = f"Stay at {listing}" if listing else _fallback_title(email.subject)

        # Confidence tiers:
        #   0.9  — dates + address + listing
        #   0.75 — dates + one of (address, listing)
        #   0.6  — dates only; router likely falls through to stage 3/4
        if address and listing:
            confidence = 0.9
        elif address or listing:
            confidence = 0.75
        else:
            confidence = 0.6

        description = _build_description(listing, address)

        parsed = ParsedEvent(
            title=title,
            start=start,
            end=end,
            location=address,
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


def _parse_date_range(
    text: str,
    *,
    tz: ZoneInfo,
    received_at: datetime,
) -> tuple[datetime, datetime] | None:
    """Return (start, end) with Airbnb's conventional 15:00 / 11:00 times.

    Layout B explicit year range wins when present, since it's unambiguous.
    Layout A has only (weekday, month, day); year is inferred from
    received_at (the next occurrence on-or-after the email date).
    """
    m = _FULL_RANGE_RE.search(text)
    if m:
        try:
            start_year = int(m.group(3))
            start_month = _MONTHS[m.group(1).lower()[:3]]
            start_day = int(m.group(2))
            end_year = int(m.group(6))
            end_month = _MONTHS[m.group(4).lower()[:3]]
            end_day = int(m.group(5))
            start = datetime(start_year, start_month, start_day, 15, 0, tzinfo=tz)
            end = datetime(end_year, end_month, end_day, 11, 0, tzinfo=tz)
        except (KeyError, ValueError):
            return None
        if end > start:
            return start, end

    # Layout A: need exactly two compact-day matches (check-in, check-out)
    # located near the "Check-in Check-out" label, in that order.
    compact = [
        (m.start(), _MONTHS.get(m.group(1).lower()[:3]), int(m.group(2)))
        for m in _COMPACT_DAY_RE.finditer(text)
    ]
    compact = [(pos, mo, d) for pos, mo, d in compact if mo is not None]
    if len(compact) < 2:
        return None

    label = _CHECKIN_LABEL_RE.search(text)
    if label is not None:
        # Take the first two date hits after the label.
        after = [c for c in compact if c[0] >= label.start()]
        if len(after) >= 2:
            compact = after[:2]
        else:
            compact = compact[:2]
    else:
        compact = compact[:2]

    received_local = received_at.astimezone(tz)
    start = _nearest_future(received_local, compact[0][1], compact[0][2], hour=15)
    if start is None:
        return None
    end = _nearest_future(received_local, compact[1][1], compact[1][2], hour=11)
    if end is None:
        return None
    # Check-out must be after check-in. If the year inference landed them
    # the wrong way round (check-out in next calendar year), bump it.
    if end <= start:
        try:
            end = end.replace(year=end.year + 1)
        except ValueError:
            return None
    # Sanity: reject absurd ranges (>90 days) — almost certainly a parse
    # error on unrelated dates elsewhere in the body.
    if (end - start) > timedelta(days=90):
        return None
    return start, end


def _nearest_future(
    anchor: datetime, month: int, day: int, *, hour: int
) -> datetime | None:
    """Return (year, month, day) as a datetime >= anchor at the given hour.

    Tries anchor.year first; if the resulting date already passed, bumps
    to next year. Handles Feb 29 on non-leap years by falling through.
    """
    for year in (anchor.year, anchor.year + 1):
        try:
            candidate = datetime(year, month, day, hour, 0, tzinfo=anchor.tzinfo)
        except ValueError:
            continue
        # Allow up to 2 days of slack: reminder emails go out right at
        # check-in; a small backwards tolerance keeps us from pushing
        # those into next year.
        if candidate >= anchor - timedelta(days=2):
            return candidate
    return None


def _extract_address(text: str) -> str | None:
    """Grab the first non-empty line after an ADDRESS label."""
    m = _ADDRESS_LABEL_RE.search(text)
    if m is None:
        return None
    tail = text[m.end():]
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip obvious link lines or Get-directions button text.
        lower = line.lower()
        if lower.startswith(("http", "get directions", "[http")):
            continue
        return line
    return None


def _extract_listing(text: str) -> str | None:
    """The listing name is the line just before "X hosted by Y".

    Layout A puts the listing in all-caps on a line of its own immediately
    after the "YOU'RE ALL SET FOR <CITY>" marker. The real email then
    appends a long tracking URL to the same line ("THE TRAILHEAD   https://
    www.airbnb.com/users/..."), so we split on whitespace and keep only
    the text before the URL. Layout B puts the listing in title case on
    its own line directly above "hosted by".
    """
    match = _HOSTED_BY_RE.search(text)
    if match is None:
        return None
    prefix = text[: match.start()].rstrip("\n")
    for line in reversed(prefix.splitlines()):
        line = line.strip()
        if not line:
            continue
        # Strip a trailing URL-with-whitespace if the line is listing+link.
        before_url = _strip_trailing_url(line)
        if not before_url:
            continue
        if _is_noise_line(before_url):
            continue
        # Trim trailing decorative stars ("Catskill Cabin * * * * *").
        cleaned = re.sub(r"(?:\s*[\*\u2605])+\s*$", "", before_url).strip()
        if not cleaned:
            continue
        return cleaned
    return None


_URL_ANYWHERE_RE = re.compile(r"\s*\[?https?://\S+\]?")


def _strip_trailing_url(line: str) -> str:
    """Return the text before any embedded URL. If the line is a bare
    URL (or starts with one), returns ''."""
    m = _URL_ANYWHERE_RE.search(line)
    if m is None:
        return line.strip()
    return line[: m.start()].strip()


def _is_noise_line(line: str) -> bool:
    """Lines we never want as a listing name."""
    lower = line.lower()
    if lower.startswith(("%opentrack", "get directions", "show more", "view full")):
        return True
    if _YOURE_HEADER_RE.search(line):
        return True
    # Pure punctuation / soft-hyphen spacer lines Airbnb uses for visual
    # spacing.
    if re.fullmatch(r"[\s\u00ad\u034f\u200b\u2060\-\u2013\u2014]+", line):
        return True
    return False


def _build_description(listing: str | None, address: str | None) -> str | None:
    bits: list[str] = []
    if listing:
        bits.append(f"Airbnb: {listing}")
    if address:
        bits.append(address)
    return "\n".join(bits) if bits else None


def _fallback_title(subject: str) -> str:
    # Pull the destination from subjects like "Reservation confirmed for Tromsø"
    # or "Brad invited you on their South Padre Island, TX trip".
    m = re.search(r"reservation confirmed for\s+(.+)$", subject, re.IGNORECASE)
    if m:
        return f"Airbnb stay in {m.group(1).strip()}"
    m = re.search(r"invited you on their\s+(.+?)\s+trip", subject, re.IGNORECASE)
    if m:
        return f"Airbnb stay in {m.group(1).strip()}"
    return "Airbnb stay"
