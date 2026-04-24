"""Tests for Stage 3 (classifier-gated NER + heuristic assembler).

All ML deps mocked — no torch, sklearn, or gliner imports at test time,
so the test suite stays fast and runnable without the `ml` extras.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from email_concierge.extractors.ner import (
    NerEventExtractor,
    _assemble,
    _parse_natural_date,
)
from email_concierge.ml.ner_entities import Entity


def _ent(label: str, text: str, start: int = 0) -> Entity:
    return Entity(label=label, text=text, start=start, end=start + len(text), score=0.9)


@pytest.fixture
def stub_ner():
    """Build a NerExtractor stand-in with a canned `extract_entities`."""
    def _build(entities: list[Entity]):
        ner = MagicMock()
        ner.extract_entities.return_value = entities
        ner.available.return_value = True
        return ner

    return _build


@pytest.fixture
def stub_classifier():
    """Build an EventClassifier stand-in with a canned probability."""
    def _build(p_event: float):
        clf = MagicMock()
        clf.is_fitted = True
        clf.predict_proba_event.return_value = [p_event]
        return clf

    return _build


def test_parse_natural_date_iso():
    dt = _parse_natural_date("2026-06-01")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 6, 1)


def test_parse_natural_date_textual():
    dt = _parse_natural_date("Jun 1, 2026")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2026, 6, 1)


def test_parse_natural_date_unparseable_returns_none():
    assert _parse_natural_date("next Tuesday") is None


def test_assemble_flight_with_two_iata_and_date(make_email):
    entities = [
        _ent("iata airport code", "SFO"),
        _ent("iata airport code", "JFK"),
        _ent("date", "2026-06-01"),
    ]
    email = make_email(subject="UA123: Your flight")
    result = _assemble(entities, email)
    assert result is not None
    assert "SFO" in result.title and "JFK" in result.title
    assert result.title.startswith("Flight UA123")
    assert result.start.month == 6
    assert result.confidence == 0.75


def test_assemble_hotel_with_check_in_date(make_email):
    entities = [
        _ent("hotel name", "Marriott Union Square"),
        _ent("check-in date", "2026-07-15"),
        _ent("city", "San Francisco"),
    ]
    result = _assemble(entities, make_email())
    assert result is not None
    assert result.title == "Stay at Marriott Union Square"
    assert result.location == "San Francisco"
    assert result.start.month == 7


def test_assemble_returns_none_for_incomplete(make_email):
    # Only a date — nothing to title an event with.
    entities = [_ent("date", "2026-06-01")]
    assert _assemble(entities, make_email()) is None


def test_extractor_returns_none_when_ner_unavailable(make_email, monkeypatch):
    # Simulate the "ML extras not installed" case. The constructor autowires
    # a real NerExtractor when one is available, so we have to suppress that
    # path explicitly — passing ner=None is a "let me decide" signal, not a
    # "force-disable" one.
    monkeypatch.setattr(
        "email_concierge.extractors.ner.NerExtractor.available",
        staticmethod(lambda: False),
    )
    extractor = NerEventExtractor(ner=None, classifier=None)
    assert extractor.can_handle(make_email()) == 0.0
    assert extractor.extract(make_email()) is None


def test_extractor_gate_skips_low_probability(make_email, stub_ner, stub_classifier):
    extractor = NerEventExtractor(
        ner=stub_ner([_ent("hotel name", "Marriott")]),
        classifier=stub_classifier(p_event=0.2),
        gate_floor=0.5,
    )
    email = make_email(body_text="Book your stay at Marriott.")
    assert extractor.extract(email) is None


def test_extractor_runs_ner_when_classifier_confident(
    make_email, stub_ner, stub_classifier
):
    entities = [
        _ent("hotel name", "Marriott Union Square"),
        _ent("check-in date", "2026-07-15"),
    ]
    extractor = NerEventExtractor(
        ner=stub_ner(entities),
        classifier=stub_classifier(p_event=0.9),
    )
    email = make_email(body_text="You're checked in on 2026-07-15 at Marriott")
    result = extractor.extract(email)
    assert result is not None
    assert result.handled_by_stage == 3
    assert result.parsed.title == "Stay at Marriott Union Square"
    # Classifier probability threaded into notes.
    assert any("classifier_p_event" in n for n in result.notes)


def test_extractor_runs_without_classifier(make_email, stub_ner):
    """Missing classifier → NER runs on everything. Degradation, not failure."""
    entities = [
        _ent("event title", "Taylor Swift Concert"),
        _ent("date", "2026-08-10"),
        _ent("venue name", "Levi's Stadium"),
    ]
    extractor = NerEventExtractor(
        ner=stub_ner(entities),
        classifier=None,  # no gate
    )
    email = make_email(subject="Your ticket")
    result = extractor.extract(email)
    assert result is not None
    assert result.parsed.title == "Taylor Swift Concert"
    assert result.parsed.location == "Levi's Stadium"


def test_extractor_returns_none_on_empty_body(make_email, stub_ner, stub_classifier):
    extractor = NerEventExtractor(
        ner=stub_ner([_ent("hotel name", "Marriott")]),
        classifier=stub_classifier(p_event=0.9),
    )
    email = make_email(body_text="")
    assert extractor.extract(email) is None


def test_extractor_returns_none_when_no_entities(make_email, stub_ner, stub_classifier):
    extractor = NerEventExtractor(
        ner=stub_ner([]),
        classifier=stub_classifier(p_event=0.9),
    )
    email = make_email(body_text="Some email with no extractable entities.")
    assert extractor.extract(email) is None


def test_extractor_returns_none_when_assembler_has_nothing(make_email, stub_ner, stub_classifier):
    """Entities present, but no rule fires."""
    extractor = NerEventExtractor(
        ner=stub_ner([_ent("city", "Paris")]),  # city alone is not enough
        classifier=stub_classifier(p_event=0.9),
    )
    email = make_email(body_text="Some text about Paris.")
    assert extractor.extract(email) is None


def test_extract_timestamps_latency_ms(make_email, stub_ner, stub_classifier):
    entities = [
        _ent("iata airport code", "SFO"),
        _ent("iata airport code", "JFK"),
        _ent("date", "2026-06-01"),
    ]
    extractor = NerEventExtractor(
        ner=stub_ner(entities),
        classifier=stub_classifier(p_event=0.9),
    )
    email = make_email(
        subject="UA123 booking",
        body_text="2026-06-01 SFO -> JFK",
        received_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    result = extractor.extract(email)
    assert result is not None
    assert result.latency_ms >= 0
