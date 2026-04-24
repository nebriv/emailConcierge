from __future__ import annotations

from email_concierge.router import route


def test_orders_by_stage_then_priority(make_email, make_result, stub_extractor):
    """Stage 1 should be tried before stage 2. Within a stage, lower priority first."""
    r1 = make_result(stage=1, name="s1", confidence=1.0)
    r2 = make_result(stage=2, name="s2", confidence=1.0)

    e_stage2 = stub_extractor("s2", stage=2, result=r2, priority=0)
    e_stage1 = stub_extractor("s1", stage=1, result=r1, priority=0)

    # Passed in reverse order to ensure the router sorts.
    result = route(
        make_email(), [e_stage2, e_stage1], can_handle_floor=0.5, min_confidence=0.7
    )
    assert result is not None
    assert result.handled_by_stage == 1


def test_skips_extractor_below_can_handle_floor(make_email, make_result, stub_extractor):
    low = stub_extractor(
        "low", stage=1, result=make_result(stage=1, confidence=1.0), applicability=0.1
    )
    high = stub_extractor(
        "high", stage=2, result=make_result(stage=2, confidence=1.0), applicability=1.0
    )
    result = route(make_email(), [low, high], can_handle_floor=0.5, min_confidence=0.7)
    assert result is not None
    assert result.handled_by_stage == 2


def test_catches_extractor_exceptions_and_falls_through(make_email, make_result, stub_extractor):
    exploder = stub_extractor(
        "boom",
        stage=1,
        result=make_result(stage=1, confidence=1.0),
        raise_in_extract=True,
    )
    backup = stub_extractor(
        "backup", stage=2, result=make_result(stage=2, name="backup", confidence=1.0)
    )
    result = route(make_email(), [exploder, backup], can_handle_floor=0.5, min_confidence=0.7)
    assert result is not None
    assert result.handled_by_name == "backup"


def test_rejects_below_min_confidence(make_email, make_result, stub_extractor):
    weak = stub_extractor("weak", stage=1, result=make_result(stage=1, confidence=0.5))
    result = route(make_email(), [weak], can_handle_floor=0.5, min_confidence=0.7)
    assert result is None


def test_returns_none_when_no_extractor_matches(make_email, stub_extractor):
    none_ext = stub_extractor("none", stage=1, result=None)
    result = route(make_email(), [none_ext], can_handle_floor=0.5, min_confidence=0.7)
    assert result is None


def test_returns_none_when_extractor_list_empty(make_email):
    assert route(make_email(), [], can_handle_floor=0.5, min_confidence=0.7) is None
