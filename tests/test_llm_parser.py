from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from email_concierge.extractors.llm import LlmExtractor

# Long enough to pass the empty-body guard, short enough to keep fixtures tidy.
_SAMPLE_BODY = (
    "Hello, your reservation is confirmed for Thursday at 7:30 PM. "
    "We look forward to seeing you at the restaurant."
)


def _fake_response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _build_extractor_with_fake(content_or_exception) -> LlmExtractor:
    fake_client = MagicMock()
    if isinstance(content_or_exception, Exception):
        fake_client.chat.completions.create.side_effect = content_or_exception
    else:
        fake_client.chat.completions.create.return_value = _fake_response(content_or_exception)
    extractor = LlmExtractor(client=fake_client)
    # Guard against the module thinking we're disabled.
    extractor._disabled = False
    extractor._client = fake_client
    return extractor


def test_llm_can_handle_respects_disable_flag(make_email, monkeypatch):
    monkeypatch.setenv("EMAIL_CONCIERGE_DISABLE_LLM", "true")
    from email_concierge.config import settings
    settings.cache_clear()  # type: ignore[attr-defined]
    try:
        ext = LlmExtractor()
        assert ext.can_handle(make_email()) == 0.0
    finally:
        settings.cache_clear()  # type: ignore[attr-defined]


def test_llm_extracts_valid_event(make_email):
    payload = {
        "is_event": True,
        "confidence": 0.95,
        "title": "Dinner at Fake Restaurant",
        "start": "2050-07-04T19:30:00-04:00",
        "end": "2050-07-04T21:30:00-04:00",
        "location": "123 Fake St, Anytown",
        "description": "Party of 4",
    }
    ext = _build_extractor_with_fake(json.dumps(payload))
    result = ext.extract(make_email(subject="Your reservation", body_text=_SAMPLE_BODY))
    assert result is not None
    assert result.handled_by_stage == 4
    assert result.confidence == pytest.approx(0.95)
    assert result.parsed.title == "Dinner at Fake Restaurant"
    assert result.parsed.ical_uid is None  # stage 4 never sets UID


def test_llm_returns_none_when_is_event_false(make_email):
    payload = {"is_event": False, "confidence": 0.9, "title": None, "start": None}
    ext = _build_extractor_with_fake(json.dumps(payload))
    assert ext.extract(make_email(body_text=_SAMPLE_BODY)) is None


def test_llm_returns_none_on_invalid_json(make_email):
    ext = _build_extractor_with_fake("this is not json")
    assert ext.extract(make_email(body_text=_SAMPLE_BODY)) is None


def test_llm_returns_none_on_schema_mismatch(make_email):
    # is_event missing entirely.
    ext = _build_extractor_with_fake(json.dumps({"confidence": 0.9, "title": "x"}))
    assert ext.extract(make_email(body_text=_SAMPLE_BODY)) is None


def test_llm_returns_none_on_api_exception(make_email):
    ext = _build_extractor_with_fake(RuntimeError("network down"))
    assert ext.extract(make_email(body_text=_SAMPLE_BODY)) is None


def test_llm_returns_none_when_datetime_missing_timezone(make_email):
    payload = {
        "is_event": True,
        "confidence": 0.9,
        "title": "x",
        "start": "2050-07-04T19:30:00",  # no offset
    }
    ext = _build_extractor_with_fake(json.dumps(payload))
    assert ext.extract(make_email(body_text=_SAMPLE_BODY)) is None


def test_llm_returns_none_when_is_event_true_but_start_missing(make_email):
    payload = {
        "is_event": True,
        "confidence": 0.9,
        "title": "x",
        "start": None,
    }
    ext = _build_extractor_with_fake(json.dumps(payload))
    assert ext.extract(make_email(body_text=_SAMPLE_BODY)) is None


def test_llm_skips_call_when_body_too_short(make_email):
    """A near-empty body means no context — the LLM would hallucinate. Skip."""
    ext = _build_extractor_with_fake(json.dumps({"is_event": True, "confidence": 0.9}))
    result = ext.extract(make_email(subject="Your order", body_text="Thanks!"))
    assert result is None
    # Confirm the short-circuit fired before the HTTP layer was touched.
    ext._client.chat.completions.create.assert_not_called()
