"""Tests for the airbnb stage-2 plugin.

Synthetic fixtures only — no real customer data committed. The fixtures
mirror the three layouts seen in live mail (automated confirmation,
trip invitation with explicit year range, reminder / address-disclosure
mail) closely enough that the same code that handles them there handles
them here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from email_concierge.extractors.plugins.airbnb import AirbnbExtractor
from email_concierge.models import Email


def _email(
    subject: str,
    body_text: str,
    *,
    sender: str = "Airbnb <automated@airbnb.com>",
    received_at: datetime | None = None,
) -> Email:
    return Email(
        message_id="airbnb-test@local",
        sender=sender,
        recipients=["user@example.com"],
        subject=subject,
        body_text=body_text,
        body_html=None,
        attachments=[],
        received_at=received_at or datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
    )


# --------------------------------------------------------------- can_handle


def test_can_handle_strong_confirmation():
    ex = AirbnbExtractor()
    email = _email("Reservation confirmed for Tromsø", "body")
    assert ex.can_handle(email) == 1.0


def test_can_handle_invitation_variant():
    ex = AirbnbExtractor()
    email = _email(
        "Brad Condreay invited you on their South Padre Island, TX trip",
        "body",
        sender="Airbnb <invitation@airbnb.com>",
    )
    assert ex.can_handle(email) == 1.0


def test_can_handle_reminder_variant():
    ex = AirbnbExtractor()
    email = _email("Reservation reminder - December 19, 2025", "body")
    assert ex.can_handle(email) == 1.0


def test_can_handle_ignores_marketing():
    ex = AirbnbExtractor()
    email = _email("Explore new destinations this summer", "body")
    assert ex.can_handle(email) == 0.0


def test_can_handle_non_airbnb_sender():
    ex = AirbnbExtractor()
    email = _email(
        "Reservation confirmed for somewhere",
        "body",
        sender="phisher <noreply@not-airbnb.com>",
    )
    assert ex.can_handle(email) == 0.0


# ------------------------------------------------------------------- extract


_AUTOMATED_BODY = """\
YOU'RE ALL SET FOR ELIZABETHTOWN

THE TRAILHEAD
Entire home/apt hosted by Nancy

Check-in        Checkout

Fri, May 1      Sun, May 3

After 3:00 PM   By 11:00 AM

ADDRESS
3 Bronson Way, Elizabethtown, NY 12932, USA

Get directions
"""


def test_extract_automated_confirmation_happy_path():
    ex = AirbnbExtractor()
    email = _email(
        "Confirmed: Your May 1 - 3 trip, here's your Airbnb receipt",
        _AUTOMATED_BODY,
        received_at=datetime(2026, 4, 19, 22, 50, tzinfo=UTC),
    )
    result = ex.extract(email)
    assert result is not None
    assert result.handled_by_stage == 2
    assert result.handled_by_name == "airbnb"
    assert result.confidence == pytest.approx(0.9)
    parsed = result.parsed
    assert parsed.title == "Stay at THE TRAILHEAD"
    assert parsed.start.month == 5
    assert parsed.start.day == 1
    assert parsed.start.year == 2026
    assert parsed.end is not None
    assert parsed.end.month == 5 and parsed.end.day == 3
    assert parsed.location == "3 Bronson Way, Elizabethtown, NY 12932, USA"


_INVITATION_BODY = """\
Get ready for your upcoming trip

Hi Ben,

Brad booked this entire home/apt in South Padre Island and added you as a guest.

Enchanting Beach House Escape wPrivate heated pool
Entire home/apt hosted by Jason Singleton

Thursday November 3, 2022 - Monday November 7, 2022

Address
111 E Constellation Dr, South Padre Island, TX 78597, USA
"""


def test_extract_invitation_full_year_range():
    ex = AirbnbExtractor()
    email = _email(
        "Brad Condreay invited you on their South Padre Island, TX trip",
        _INVITATION_BODY,
        sender="Airbnb <invitation@airbnb.com>",
        received_at=datetime(2022, 10, 1, 12, 0, tzinfo=UTC),
    )
    result = ex.extract(email)
    assert result is not None
    parsed = result.parsed
    assert parsed.start.year == 2022
    assert parsed.start.month == 11 and parsed.start.day == 3
    assert parsed.end is not None
    assert parsed.end.year == 2022
    assert parsed.end.month == 11 and parsed.end.day == 7
    assert parsed.location == "111 E Constellation Dr, South Padre Island, TX 78597, USA"
    assert parsed.title == "Stay at Enchanting Beach House Escape wPrivate heated pool"


def test_year_inferred_from_received_at_rolls_forward():
    """Dates that appear to be in the past relative to the email get
    bumped a year. A reminder dated Dec 5 about 'Jan 20' is next year."""
    ex = AirbnbExtractor()
    body = """\
YOU'RE ALL SET FOR SOMEWHERE

THE CABIN
Entire home/apt hosted by Alice

Check-in      Checkout
Tue, Jan 20   Thu, Jan 22
3:00 PM       11:00 AM

ADDRESS
1 Main St, Somewhere, NY 12345, USA
"""
    email = _email(
        "Reservation confirmed for Somewhere",
        body,
        received_at=datetime(2025, 12, 5, 9, 0, tzinfo=UTC),
    )
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.start.year == 2026
    assert result.parsed.end is not None and result.parsed.end.year == 2026


def test_missing_address_drops_confidence_but_still_extracts():
    ex = AirbnbExtractor()
    body = """\
YOU'RE ALL SET FOR PLACE

A NICE LISTING
Entire home/apt hosted by Host

Check-in      Checkout
Fri, May 1    Sun, May 3
"""
    email = _email(
        "Reservation confirmed for Place",
        body,
        received_at=datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
    )
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.location is None
    assert result.parsed.title == "Stay at A NICE LISTING"
    assert result.confidence == pytest.approx(0.75)


def test_no_dates_returns_none():
    ex = AirbnbExtractor()
    email = _email(
        "Reservation confirmed for Nowhere",
        "Thanks for booking! More details soon.",
    )
    assert ex.extract(email) is None


def test_extract_uses_user_timezone(monkeypatch):
    """start/end are timezone-aware in the configured user timezone."""
    ex = AirbnbExtractor()
    email = _email(
        "Reservation confirmed for Elizabethtown",
        _AUTOMATED_BODY,
        received_at=datetime(2026, 4, 19, 22, 50, tzinfo=UTC),
    )
    result = ex.extract(email)
    assert result is not None
    # User tz defaults to America/New_York per .env.example + config fallback.
    assert result.parsed.start.tzinfo is not None
    assert result.parsed.start.utcoffset() == ZoneInfo("America/New_York").utcoffset(
        result.parsed.start.replace(tzinfo=None)
    )
