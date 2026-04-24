"""Tests for the eventbrite stage-2 plugin.

JSON-LD is the only structural signal we rely on, so these fixtures
are minimal HTML bodies with a single <script type="application/ld+json">
block. Real emails have pages of other content around it but the
plugin ignores everything else.
"""

from __future__ import annotations

from datetime import UTC, datetime

from email_concierge.extractors.plugins.eventbrite import EventbriteExtractor
from email_concierge.models import Email


def _email(
    subject: str,
    body_html: str,
    *,
    sender: str = "Eventbrite <noreply@order.eventbrite.com>",
) -> Email:
    return Email(
        message_id="eventbrite-test@local",
        sender=sender,
        recipients=["user@example.com"],
        subject=subject,
        body_text="",
        body_html=body_html,
        attachments=[],
        received_at=datetime(2026, 4, 15, tzinfo=UTC),
    )


_HAPPY_BODY = """
<html><body>
<p>Thanks for your order!</p>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "EventReservation",
  "reservationNumber": "99999999999",
  "underName": {"@type": "Person", "name": "Example Person"},
  "reservationFor": {
    "@type": "Event",
    "name": "Intrepid Museum Presents Astronomy Night",
    "startDate": "2026-04-24 17:30:00",
    "endDate":   "2026-04-24 21:00:00",
    "location": {
      "@type": "Place",
      "name": "Intrepid Museum",
      "address": {
        "@type": "PostalAddress",
        "streetAddress": "West 46th Street",
        "addressLocality": "New York",
        "addressRegion": "NY",
        "postalCode": "10036",
        "addressCountry": "US"
      }
    }
  }
}
</script>
</body></html>
"""


def test_can_handle_strong_signal():
    ex = EventbriteExtractor()
    email = _email("Registration Confirmation for Intrepid Museum Presents Astronomy Night", "<p/>")
    assert ex.can_handle(email) == 1.0


def test_can_handle_non_eventbrite_sender():
    ex = EventbriteExtractor()
    email = _email("Order Confirmation for something", "<p/>", sender="phisher@other.com")
    assert ex.can_handle(email) == 0.0


def test_extract_happy_path():
    ex = EventbriteExtractor()
    email = _email(
        "Registration Confirmation for Intrepid Museum Presents Astronomy Night",
        _HAPPY_BODY,
    )
    result = ex.extract(email)
    assert result is not None
    assert result.handled_by_stage == 2
    assert result.handled_by_name == "eventbrite"
    assert result.confidence == 0.95
    parsed = result.parsed
    assert parsed.title == "Intrepid Museum Presents Astronomy Night"
    assert parsed.start.year == 2026
    assert parsed.start.month == 4 and parsed.start.day == 24
    assert parsed.start.hour == 17 and parsed.start.minute == 30
    assert parsed.end is not None
    assert parsed.end.hour == 21
    assert parsed.location == "Intrepid Museum, West 46th Street, New York, NY, 10036"


def test_extract_no_json_ld_returns_none():
    ex = EventbriteExtractor()
    email = _email(
        "Order Confirmation for thing",
        "<html><body>no structured data here, just copy</body></html>",
    )
    assert ex.extract(email) is None


def test_extract_missing_end_date_ok():
    ex = EventbriteExtractor()
    body = """
    <script type="application/ld+json">
    {"@type": "EventReservation", "reservationFor": {
      "@type": "Event", "name": "A show",
      "startDate": "2026-07-15T20:00:00",
      "location": {"@type": "Place", "name": "Venue"}
    }}
    </script>
    """
    email = _email("Order Confirmation for A show", body)
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.end is None
    assert result.parsed.location == "Venue"


def test_extract_malformed_json_returns_none():
    ex = EventbriteExtractor()
    body = """
    <script type="application/ld+json">
    { this is not valid json }
    </script>
    """
    email = _email("Order Confirmation for bad", body)
    assert ex.extract(email) is None


def test_extract_top_level_event_type():
    """Some Eventbrite templates put @type=Event at the top level (no
    EventReservation wrapper)."""
    ex = EventbriteExtractor()
    body = """
    <script type="application/ld+json">
    {"@type": "Event", "name": "Direct event",
     "startDate": "2026-06-01T10:00:00",
     "location": {"@type": "Place", "name": "Hall"}}
    </script>
    """
    email = _email("Order Confirmation for Direct event", body)
    result = ex.extract(email)
    assert result is not None
    assert result.parsed.title == "Direct event"
