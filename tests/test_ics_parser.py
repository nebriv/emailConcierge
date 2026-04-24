from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from email_concierge.extractors.ics import IcsExtractor
from email_concierge.models import Attachment

FIXTURES = Path(__file__).parent / "fixtures" / "ics"


def _ics_attachment(name: str) -> Attachment:
    payload = (FIXTURES / name).read_bytes()
    return Attachment(
        filename=name,
        content_type="text/calendar; charset=utf-8",
        payload=payload,
    )


def test_can_handle_true_when_ics_attachment_present(make_email):
    email = make_email(attachments=[_ics_attachment("flight_confirmation.ics")])
    assert IcsExtractor().can_handle(email) == 1.0


def test_can_handle_zero_without_ics(make_email):
    email = make_email(attachments=[])
    assert IcsExtractor().can_handle(email) == 0.0


def test_extract_preserves_uid_from_ics(make_email):
    email = make_email(attachments=[_ics_attachment("flight_confirmation.ics")])
    result = IcsExtractor().extract(email)
    assert result is not None
    assert result.handled_by_stage == 1
    assert result.confidence == 1.0
    assert result.parsed.ical_uid == "UA123-SFOEWR-20500614@united.com"
    assert "UA123" in result.parsed.title
    # DTSTART from fixture is 2050-06-14T15:00:00Z
    assert result.parsed.start == datetime.fromisoformat("2050-06-14T15:00:00+00:00")


def test_extract_all_day_event_upgrades_date_to_datetime(make_email):
    email = make_email(attachments=[_ics_attachment("all_day_event.ics")])
    result = IcsExtractor().extract(email)
    assert result is not None
    # DATE-only DTSTART should be promoted to a tz-aware datetime.
    assert isinstance(result.parsed.start, datetime)
    assert result.parsed.start.tzinfo is not None
    assert result.parsed.start.date().isoformat() == "2050-07-15"


def test_extract_returns_none_on_malformed_ics(make_email):
    email = make_email(
        attachments=[
            Attachment(
                filename="malformed.ics",
                content_type="text/calendar",
                payload=(FIXTURES / "malformed.ics").read_bytes(),
            )
        ]
    )
    assert IcsExtractor().extract(email) is None


def test_extract_returns_none_when_no_attachments(make_email):
    email = make_email(attachments=[])
    assert IcsExtractor().extract(email) is None


@pytest.mark.parametrize(
    "filename, content_type, expected",
    [
        ("invite.ics", "application/octet-stream", True),
        ("anything.txt", "text/calendar", True),
        ("invite.ICS", "application/x-unknown", True),
        ("image.png", "image/png", False),
    ],
)
def test_is_ics_attachment_matches_filename_or_content_type(
    make_email, filename, content_type, expected
):
    email = make_email(
        attachments=[
            Attachment(filename=filename, content_type=content_type, payload=b"")
        ]
    )
    assert (IcsExtractor().can_handle(email) == 1.0) is expected
