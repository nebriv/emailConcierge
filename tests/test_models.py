from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from email_concierge.models import ExtractionResult, ParsedEvent


def test_parsed_event_rejects_naive_start():
    with pytest.raises(ValidationError):
        ParsedEvent(title="x", start=datetime(2026, 1, 1, 12, 0))  # no tz


def test_parsed_event_accepts_tz_aware_start():
    ev = ParsedEvent(title="x", start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    assert ev.start.tzinfo is not None


def test_parsed_event_rejects_naive_end():
    with pytest.raises(ValidationError):
        ParsedEvent(
            title="x",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2),
        )


def test_extraction_result_shape():
    r = ExtractionResult(
        handled_by_stage=1,
        handled_by_name="ics",
        confidence=1.0,
        parsed=ParsedEvent(title="x", start=datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert r.latency_ms == 0
    assert r.notes == []
