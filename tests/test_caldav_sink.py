from __future__ import annotations

from email_concierge.sinks.caldav_sink import _build_vcalendar, _compose_description


def test_footer_contains_sender_subject_extractor_and_message_id(
    make_email, make_result
):
    email = make_email(
        message_id="<abc123@linkedin.com>",
        sender="Mattias Allen <hit-reply@linkedin.com>",
        subject="Inmail from Mattias Allen",
    )
    result = make_result(stage=4, name="llm", confidence=0.95)

    desc = _compose_description(result, email, account="benvirgilio.com")

    assert "Mattias Allen <hit-reply@linkedin.com>" in desc
    assert "Inmail from Mattias Allen" in desc
    assert "benvirgilio.com" in desc
    assert "llm" in desc
    assert "stage 4" in desc
    assert "0.95" in desc
    assert "<abc123@linkedin.com>" in desc


def test_footer_omitted_when_email_is_none(make_result):
    result = make_result(stage=1, name="ics")
    # When no email passed (only possible via older call-path), we should
    # fall back to the extractor-provided description alone.
    assert _compose_description(result, None, account=None) == ""


def test_footer_appended_after_extractor_description(make_email, make_result):
    email = make_email(subject="Snowshoe Lodge reservation")
    result = make_result(stage=2, name="airbnb")
    result.parsed.description = "Check-in 3pm Friday. Reservation ABC-123."

    desc = _compose_description(result, email, account="gmail")

    assert desc.startswith("Check-in 3pm Friday. Reservation ABC-123.")
    assert "Snowshoe Lodge reservation" in desc
    # Separator between extractor text and footer.
    assert "────────" in desc


def test_build_vcalendar_embeds_footer_in_description(make_email, make_result):
    email = make_email(
        message_id="<m@example.com>",
        sender="alice@example.com",
        subject="Dinner Tuesday",
    )
    result = make_result(stage=4, name="llm", confidence=0.9)

    ical = _build_vcalendar(result, uid="uid-1", email=email, account="personal")

    text = ical.decode("utf-8")
    assert "alice@example.com" in text
    assert "Dinner Tuesday" in text
    assert "personal" in text
    assert "<m@example.com>" in text
    # Programmatic source tag still present alongside the human-readable footer.
    assert "X-EMAIL-CONCIERGE-SOURCE" in text.upper()
