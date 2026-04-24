"""Tests for the united_airlines stage-2 plugin.

All fixtures are synthetic inline HTML that mimics the structural
cues of real United emails (three distinct layouts: modern eTicket,
legacy eTicket with compact date, booking confirmation). No real
email contents are committed to the repo — see CLAUDE.md section 14.2.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from email_concierge.extractors.plugins.united_airlines import (
    UnitedAirlinesExtractor,
)
from email_concierge.models import Email


def _email(subject: str, body_html: str, sender: str = "Receipts@united.com") -> Email:
    return Email(
        message_id="test@local",
        sender=f"United <{sender}>",
        recipients=["user@example.com"],
        subject=subject,
        body_text="",
        body_html=body_html,
        attachments=[],
        received_at=datetime.now(tz=UTC),
    )


# --------------------------------------------------------------- can_handle

def test_can_handle_strong_signal():
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation ABC123", "<p/>")
    assert ex.can_handle(email) == 1.0


def test_can_handle_sender_only():
    ex = UnitedAirlinesExtractor()
    email = _email("Some unrelated subject", "<p/>")
    assert ex.can_handle(email) == 0.5


def test_can_handle_wrong_sender_rejected():
    ex = UnitedAirlinesExtractor()
    email = Email(
        message_id="x", sender="bob@delta.com", recipients=[], subject="Confirmation",
        body_text="", body_html="<p/>", attachments=[], received_at=datetime.now(tz=UTC),
    )
    assert ex.can_handle(email) == 0.0


def test_can_handle_accepts_uafrequentflyer_domain():
    ex = UnitedAirlinesExtractor()
    email = _email(
        "eTicket itinerary and receipt for confirmation ABC123",
        "<p/>",
        sender="news@uafrequentflyer.com",
    )
    assert ex.can_handle(email) == 1.0


# --------------------------------------------------------------- extraction


SIMPLE_ROUND_TRIP_HTML = """
<html><body>
  <h1>Flight Confirmation</h1>
  <p>Confirmation number: ABC123</p>
  <p>Wed, Apr 01, 2026</p>
  <table>
    <tr><td>UA 2389</td><td>(LGA)</td><td>(ORD)</td>
        <td>06:10 AM</td><td>07:49 AM</td></tr>
    <tr><td>UA 224</td><td>(ORD)</td><td>(LGA)</td>
        <td>09:05 AM</td><td>11:29 AM</td></tr>
  </table>
</body></html>
"""


def test_simple_round_trip_two_iatas():
    """Two distinct IATAs → destination inferred with high confidence."""
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation ABC123",
                   SIMPLE_ROUND_TRIP_HTML)
    result = ex.extract(email)
    assert result is not None
    assert result.confidence == pytest.approx(0.9)
    assert result.parsed.title == "Flight to ORD"
    assert result.parsed.location == "LGA"
    assert result.parsed.start.year == 2026
    assert result.parsed.start.month == 4
    assert result.parsed.start.day == 1
    assert result.parsed.start.hour == 6 and result.parsed.start.minute == 10
    assert "ABC123" in (result.parsed.description or "")


CONNECTING_NO_HEADER_HTML = """
<html><body>
  <p>Confirmation: XYZ789</p>
  <p>Wed, Apr 01, 2026</p>
  <table>
    <tr><td>UA 2389</td><td>(LGA)</td><td>(ORD)</td>
        <td>06:10 AM</td><td>07:49 AM</td></tr>
    <tr><td>UA 224</td><td>(ORD)</td><td>(BZN)</td>
        <td>09:05 AM</td><td>11:29 AM</td></tr>
  </table>
</body></html>
"""


def test_connecting_flight_without_header_falls_through():
    """3+ IATAs and no 'Flight to <city>' header → confidence below floor
    so the router delegates to the LLM. Hub-vs-destination is unreliable
    from raw counts."""
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation XYZ789",
                   CONNECTING_NO_HEADER_HTML)
    result = ex.extract(email)
    assert result is not None
    assert result.confidence < 0.7  # below EMAIL_CONCIERGE_MIN_CONFIDENCE default


BOOKING_HEADER_HTML = """
<html><body>
  <h1>Flight to Bozeman</h1>
  <p>Booking confirmation: FAKE01</p>
  <p>Wed, Apr 01, 2026</p>
  <table>
    <tr><td>UA 2389</td><td>LGA</td><td>ORD</td>
        <td>6:10 AM</td><td>7:49 AM</td></tr>
    <tr><td>UA 224</td><td>ORD</td><td>BZN</td>
        <td>9:05 AM</td><td>11:29 AM</td></tr>
  </table>
</body></html>
"""


def test_booking_confirmation_header_used_for_title():
    """'Flight to <city>' header gives us the destination even on a
    connecting itinerary — confidence stays high."""
    ex = UnitedAirlinesExtractor()
    email = _email("Your United Airlines booking confirmation - FAKE01",
                   BOOKING_HEADER_HTML)
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.title == "Flight to Bozeman"
    assert result.confidence == pytest.approx(0.9)


LEGACY_COMPACT_DATE_HTML = """
<html><body>
  <p>Confirmation Number: FAKE02</p>
  <p>Thu, 27FEB14</p>
  <table>
    <tr><td>UA 4233</td><td>(BTV)</td><td>(EWR)</td>
        <td>8:05 AM</td><td>9:20 AM</td></tr>
    <tr><td>UA 1723</td><td>(EWR)</td><td>(BTV)</td>
        <td>11:40 AM</td><td>3:06 PM</td></tr>
  </table>
</body></html>
"""


def test_legacy_compact_date_parsed():
    """The 2014-era '27FEB14' date format is picked up when long-form
    patterns don't match."""
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation FAKE02",
                   LEGACY_COMPACT_DATE_HTML)
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.start.year == 2014
    assert result.parsed.start.month == 2
    assert result.parsed.start.day == 27


MARKETING_FOOTER_HTML = """
<html><body>
  <p>Confirmation: ABC123</p>
  <p>Wed, Apr 01, 2026</p>
  <table>
    <tr><td>UA 2389</td><td>(LGA)</td><td>(ORD)</td>
        <td>06:10 AM</td><td>07:49 AM</td></tr>
    <tr><td>UA 224</td><td>(ORD)</td><td>(LGA)</td>
        <td>09:05 AM</td><td>11:29 AM</td></tr>
  </table>
  <footer>
    Fly United to: Las Vegas, Maui, Miami, Newark,
    Orange County (SNA), Orlando, Philadelphia, Phoenix.
  </footer>
</body></html>
"""


def test_marketing_footer_iatas_ignored():
    """Airport codes mentioned once in marketing copy must not be
    confused for itinerary airports."""
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation ABC123",
                   MARKETING_FOOTER_HTML)
    result = ex.extract(email)
    assert result is not None
    # SNA is a footer singleton — must not sneak into origin/destination.
    assert result.parsed.location == "LGA"
    assert result.parsed.title == "Flight to ORD"
    assert "SNA" not in (result.parsed.description or "")


MISSING_SIGNALS_HTML = """
<html><body>
  <p>Thank you for your purchase. Your e-ticket will arrive shortly.</p>
</body></html>
"""


def test_missing_required_signals_returns_none():
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation ABC123",
                   MISSING_SIGNALS_HTML)
    assert ex.extract(email) is None


MIDNIGHT_EXPIRY_HTML = """
<html><body>
  <p>Confirmation Number: FAKE03</p>
  <p>Fri, Jan 06, 2017</p>
  <table>
    <tr><td>UA 628</td><td>(LGA)</td><td>(ORD)</td>
        <td>7:00 AM</td><td>8:37 AM</td></tr>
    <tr><td>UA 698</td><td>(ORD)</td><td>(LGA)</td>
        <td>8:50 PM</td><td>11:59 PM</td></tr>
  </table>
</body></html>
"""


def test_same_day_round_trip_end_time_preserved():
    """Same-day round-trip: last arrival time is legitimate, keep it as end."""
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation FAKE03",
                   MIDNIGHT_EXPIRY_HTML)
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.end is not None
    assert result.parsed.end.hour == 23 and result.parsed.end.minute == 59


CONF_IN_SUBJECT_ONLY_HTML = """
<html><body>
  <p>Wed, Apr 01, 2026</p>
  <table>
    <tr><td>UA 2389</td><td>(LGA)</td><td>(ORD)</td>
        <td>06:10 AM</td><td>07:49 AM</td></tr>
    <tr><td>UA 224</td><td>(ORD)</td><td>(LGA)</td>
        <td>09:05 AM</td><td>11:29 AM</td></tr>
  </table>
</body></html>
"""


def test_confirmation_code_from_subject_fallback():
    """Body has no explicit 'Confirmation:' label — subject provides the code."""
    ex = UnitedAirlinesExtractor()
    email = _email("eTicket Itinerary and Receipt for Confirmation KX7P2M",
                   CONF_IN_SUBJECT_ONLY_HTML)
    result = ex.extract(email)
    assert result is not None
    assert "KX7P2M" in (result.parsed.description or "")
