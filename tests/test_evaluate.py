"""Tests for the `evaluate` command — cross-stage disagreement reporting.

Focus is the disagreement logic: once the validator is wired in, outcomes
that production would have dropped at the same gate should not count as
disagreements.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from email_concierge.commands.evaluate import _disagreement_summary, _run_all
from email_concierge.models import Email, ExtractionResult, ParsedEvent


def _outcome(
    name: str,
    stage: int,
    *,
    result: dict | None = None,
    rejected_reason: str | None = None,
) -> dict:
    o: dict = {"name": name, "stage": stage}
    if result is not None:
        o["result"] = result
        o["rejected_reason"] = rejected_reason
    elif rejected_reason is None:
        o["result"] = None
    return o


def _mk_result(stage: int, name: str, **kwargs) -> ExtractionResult:
    return ExtractionResult(
        handled_by_stage=stage,
        handled_by_name=name,
        confidence=kwargs.pop("confidence", 0.9),
        parsed=ParsedEvent(
            title=kwargs.pop("title", "Something"),
            start=kwargs.pop("start", datetime(2026, 8, 1, 12, 0, tzinfo=UTC)),
        ),
        latency_ms=1,
        commitment_evidence=kwargs.pop("commitment_evidence", None),
    )


def _mk_email(received_at: datetime) -> Email:
    return Email(
        message_id="<e@x>",
        sender="sender@x",
        recipients=["me@x"],
        subject="subj",
        body_text="body",
        received_at=received_at,
    )


def test_all_null_is_agreement():
    outcomes = [
        _outcome("ics", 1, result=None),
        _outcome("llm", 4, result=None),
    ]
    assert _disagreement_summary(outcomes) is None


def test_all_accepted_same_title_is_agreement():
    outcomes = [
        _outcome("plugin", 2, result={"title": "Flight UA123", "start": "x"}),
        _outcome("llm", 4, result={"title": "flight ua123", "start": "x"}),
    ]
    assert _disagreement_summary(outcomes) is None


def test_accepted_vs_null_is_disagreement():
    outcomes = [
        _outcome("plugin", 2, result={"title": "Flight", "start": "x"}),
        _outcome("llm", 4, result=None),
    ]
    summary = _disagreement_summary(outcomes)
    assert summary is not None
    assert "plugin" in summary


def test_all_rejected_counts_as_agreement():
    """Validator rejected both → production would drop both → no conflict."""
    outcomes = [
        _outcome("ner", 3, result={"title": "x", "start": "y"},
                 rejected_reason="missing_commitment_evidence"),
        _outcome("llm", 4, result={"title": "x", "start": "y"},
                 rejected_reason="event_in_past (start=...)"),
    ]
    assert _disagreement_summary(outcomes) is None


def test_one_accepted_one_rejected_is_disagreement_with_reason():
    """When stages disagree AND the validator drops one, surface the reason."""
    outcomes = [
        _outcome("plugin", 2, result={"title": "Flight", "start": "x"}),
        _outcome("llm", 4, result={"title": "Other", "start": "y"},
                 rejected_reason="missing_commitment_evidence"),
    ]
    summary = _disagreement_summary(outcomes)
    assert summary is not None
    assert "validator dropped" in summary
    assert "missing_commitment_evidence" in summary


def test_accepted_title_mismatch_is_disagreement():
    outcomes = [
        _outcome("plugin", 2, result={"title": "Flight UA123", "start": "x"}),
        _outcome("llm", 4, result={"title": "Hotel Marriott", "start": "x"}),
    ]
    summary = _disagreement_summary(outcomes)
    assert summary is not None
    assert "title mismatch" in summary


def test_run_all_threads_validator_past_event():
    """Integration: _run_all should include rejected_reason on past-dated
    LLM extractions so _disagreement_summary silences them."""
    class FakeLlm:
        name = "llm"
        stage = 4

        def can_handle(self, _e):
            return 1.0

        def extract(self, _e):
            return _mk_result(
                4, "llm",
                start=datetime(2026, 3, 1, tzinfo=UTC),  # past relative to email
                commitment_evidence="Order #X1Y2",
            )

    email = _mk_email(datetime(2026, 4, 20, tzinfo=UTC))
    outcomes = _run_all(email, [FakeLlm()])
    assert len(outcomes) == 1
    assert outcomes[0]["rejected_reason"] is not None
    assert "event_in_past" in outcomes[0]["rejected_reason"]


def test_run_all_threads_validator_accepts_valid():
    """A valid future event with commitment evidence passes the validator."""
    class FakeLlm:
        name = "llm"
        stage = 4

        def can_handle(self, _e):
            return 1.0

        def extract(self, _e):
            return _mk_result(
                4, "llm",
                start=datetime(2026, 8, 1, tzinfo=UTC),
                commitment_evidence="Confirmation #ABC123",
            )

    email = _mk_email(datetime(2026, 4, 20, tzinfo=UTC))
    outcomes = _run_all(email, [FakeLlm()])
    assert len(outcomes) == 1
    assert outcomes[0]["rejected_reason"] is None


def test_run_all_skipped_extractor_has_no_validator_field():
    """An extractor that can_handle says skip should not surface validator output."""
    class SkippingExt:
        name = "s"
        stage = 2

        def can_handle(self, _e):
            return 0.0

        def extract(self, _e):
            raise AssertionError("should not be called")

    email = _mk_email(datetime.now(tz=UTC) + timedelta(days=10))
    outcomes = _run_all(email, [SkippingExt()])
    assert outcomes == [{"name": "s", "stage": 2, "skipped": True}]
