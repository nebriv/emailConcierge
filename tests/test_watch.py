"""Tests for the `watch` command (tail + summary over processed_messages)."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from email_concierge.commands.watch import _parse_since, watch_command


def _seed(
    conn,
    *,
    message_id: str,
    status: str,
    stage: int | None = None,
    name: str | None = None,
    confidence: float | None = None,
    sender: str = "test@x",
    subject: str = "Hi",
    error: str | None = None,
    processed_at: datetime | None = None,
    received_at: datetime | None = None,
    account: str | None = None,
) -> None:
    now = datetime.now(tz=UTC)
    received = (received_at or now).isoformat()
    processed = (processed_at or now).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_messages
          (message_id, received_at, sender, subject,
           handled_by_stage, handled_by_name, confidence,
           status, error, processed_at, account)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, received, sender, subject,
            stage, name, confidence, status, error, processed, account,
        ),
    )
    conn.commit()


def _run(conn, **kwargs) -> str:
    buf = io.StringIO()
    with patch("email_concierge.commands.watch.db.connect", return_value=conn), \
         patch("email_concierge.commands.watch.db.init_schema"):
        rc = watch_command(output=buf, **kwargs)
    assert rc == 0
    return buf.getvalue()


def test_parse_since_relative_minutes():
    before = datetime.now(tz=UTC) - timedelta(minutes=15)
    parsed = _parse_since("15m")
    delta = abs((parsed - before).total_seconds())
    assert delta < 2  # parsing is instantaneous, small clock slop OK


def test_parse_since_relative_compound():
    before = datetime.now(tz=UTC) - timedelta(hours=2, minutes=30)
    parsed = _parse_since("2h30m")
    delta = abs((parsed - before).total_seconds())
    assert delta < 2


def test_parse_since_iso8601():
    parsed = _parse_since("2026-04-20T10:00:00+00:00")
    assert parsed == datetime(2026, 4, 20, 10, 0, tzinfo=UTC)


def test_parse_since_iso8601_naive_treated_as_utc():
    parsed = _parse_since("2026-04-20T10:00:00")
    assert parsed == datetime(2026, 4, 20, 10, 0, tzinfo=UTC)


def test_parse_since_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_since("soon")


def test_parse_since_rejects_zero_duration():
    with pytest.raises(ValueError):
        _parse_since("0m")


def test_watch_snapshot_prints_rows_in_window(tmp_db):
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<a@x>", status="processed",
        stage=2, name="united_airlines", confidence=0.95,
        sender="res@united.com", subject="Your flight UA123",
        processed_at=now - timedelta(minutes=5),
    )
    _seed(
        tmp_db, message_id="<b@x>", status="rejected",
        stage=4, name="llm", confidence=0.82,
        sender="receipts@opentable.com",
        subject="Thanks for dining with us",
        error="event_in_past (start=...)",
        processed_at=now - timedelta(minutes=2),
    )

    out = _run(tmp_db, since="15m")
    assert "united_airlines" in out
    assert "llm" in out
    assert "event_in_past" in out  # rejection reason surfaced
    # Row order is ascending by processed_at.
    assert out.index("united_airlines") < out.index("llm")


def test_watch_snapshot_excludes_rows_outside_window(tmp_db):
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<old@x>", status="processed",
        sender="old@x", subject="Old",
        processed_at=now - timedelta(hours=2),
    )
    _seed(
        tmp_db, message_id="<new@x>", status="processed",
        sender="new@x", subject="New",
        processed_at=now - timedelta(minutes=2),
    )

    out = _run(tmp_db, since="15m")
    assert "New" in out
    assert "Old" not in out


def test_watch_status_filter(tmp_db):
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<ok@x>", status="processed",
        subject="Kept", processed_at=now - timedelta(minutes=1),
    )
    _seed(
        tmp_db, message_id="<rej@x>", status="rejected",
        subject="Dropped", error="event_in_past",
        processed_at=now - timedelta(minutes=1),
    )

    out = _run(tmp_db, since="15m", status="rejected")
    assert "Dropped" in out
    assert "Kept" not in out


def test_watch_stage_filter(tmp_db):
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<s2@x>", status="processed",
        stage=2, name="united_airlines", subject="Plugin",
        processed_at=now - timedelta(minutes=1),
    )
    _seed(
        tmp_db, message_id="<s4@x>", status="processed",
        stage=4, name="llm", subject="Fallback",
        processed_at=now - timedelta(minutes=1),
    )

    out = _run(tmp_db, since="15m", stage=4)
    assert "Fallback" in out
    assert "Plugin" not in out


def test_watch_summary_counts_by_status(tmp_db):
    now = datetime.now(tz=UTC)
    for i in range(3):
        _seed(
            tmp_db, message_id=f"<ok{i}@x>", status="processed",
            stage=2, name="united_airlines",
            processed_at=now - timedelta(minutes=i),
        )
    _seed(
        tmp_db, message_id="<rej@x>", status="rejected",
        stage=4, name="llm", error="event_in_past (start=...)",
        processed_at=now - timedelta(minutes=1),
    )

    out = _run(tmp_db, since="15m", summary=True)
    assert "total=4" in out
    assert "processed" in out and "  3" in out
    assert "rejected" in out and "  1" in out
    assert "event_in_past" in out  # reason grouping
    assert "united_airlines" in out
    assert "llm" in out


def test_watch_invalid_status_returns_2(tmp_db):
    with patch("email_concierge.commands.watch.db.connect", return_value=tmp_db), \
         patch("email_concierge.commands.watch.db.init_schema"):
        rc = watch_command(since="15m", status="bogus", output=io.StringIO())
    assert rc == 2


def test_watch_invalid_since_returns_2(tmp_db):
    with patch("email_concierge.commands.watch.db.connect", return_value=tmp_db), \
         patch("email_concierge.commands.watch.db.init_schema"):
        rc = watch_command(since="nope", output=io.StringIO())
    assert rc == 2


def test_watch_empty_window_prints_nothing(tmp_db):
    out = _run(tmp_db, since="5m")
    assert out == ""


def test_watch_summary_empty_window_reports_zero(tmp_db):
    out = _run(tmp_db, since="5m", summary=True)
    assert "total=0" in out


def test_watch_show_ids_appends_message_id(tmp_db):
    """--ids surfaces the Message-ID so operators can paste it into
    `mark-event` / `forget` / `label` without guessing at truncation."""
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<CAF=abc123@mail.example.com>", status="no_extraction",
        sender="bookings@vendor.com", subject="Your trip",
        processed_at=now - timedelta(minutes=1),
    )
    out_ids = _run(tmp_db, since="15m", show_ids=True)
    assert "id=<CAF=abc123@mail.example.com>" in out_ids


def test_watch_account_filter(tmp_db):
    """--account restricts rows to a single configured mailbox."""
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<p1@x>", status="processed",
        sender="s@x", subject="Personal row",
        processed_at=now - timedelta(minutes=1),
        account="personal",
    )
    _seed(
        tmp_db, message_id="<g1@x>", status="processed",
        sender="s@x", subject="Gmail row",
        processed_at=now - timedelta(minutes=1),
        account="gmail",
    )
    out = _run(tmp_db, since="15m", account="personal")
    assert "Personal row" in out
    assert "Gmail row" not in out


def test_watch_auto_prefixes_account_when_mixed(tmp_db):
    """Without --account, a window spanning multiple accounts prefixes
    each row with the account name so output stays disambiguated."""
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<p1@x>", status="processed",
        subject="Personal row",
        processed_at=now - timedelta(minutes=2),
        account="personal",
    )
    _seed(
        tmp_db, message_id="<g1@x>", status="processed",
        subject="Gmail row",
        processed_at=now - timedelta(minutes=1),
        account="gmail",
    )
    out = _run(tmp_db, since="15m")
    assert "[personal" in out
    assert "[gmail" in out


def test_watch_no_prefix_for_single_account_window(tmp_db):
    """When only one account appears, skip the account prefix."""
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<p1@x>", status="processed",
        subject="Lonely row",
        processed_at=now - timedelta(minutes=1),
        account="personal",
    )
    out = _run(tmp_db, since="15m")
    assert "[personal" not in out
    assert "Lonely row" in out


def test_watch_summary_by_account(tmp_db):
    now = datetime.now(tz=UTC)
    for i in range(2):
        _seed(
            tmp_db, message_id=f"<p{i}@x>", status="processed",
            processed_at=now - timedelta(minutes=i + 1),
            account="personal",
        )
    _seed(
        tmp_db, message_id="<g0@x>", status="processed",
        processed_at=now - timedelta(minutes=1),
        account="gmail",
    )
    out = _run(tmp_db, since="15m", summary=True)
    assert "By account:" in out
    assert "personal" in out
    assert "gmail" in out


def test_watch_default_output_omits_message_id(tmp_db):
    """Without --ids the Message-ID stays out of the tail line."""
    now = datetime.now(tz=UTC)
    _seed(
        tmp_db, message_id="<CAF=abc123@mail.example.com>", status="no_extraction",
        sender="bookings@vendor.com", subject="Your trip",
        processed_at=now - timedelta(minutes=1),
    )
    out = _run(tmp_db, since="15m")
    assert "<CAF=abc123@mail.example.com>" not in out
