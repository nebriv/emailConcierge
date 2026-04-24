"""End-to-end tests for import-training --from-google.

Both Google API layers are mocked via factory parameters on the
command function. Auth is short-circuited by patching
load_credentials_from_settings.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from email_concierge import db
from email_concierge.commands.import_training import import_training_command
from email_concierge.integrations.google.models import GoogleEvent
from email_concierge.models import Email


def _make_event(
    event_id: str,
    gmail_id: str,
    updated: datetime,
    summary: str = "Flight UA123",
    location: str = "SFO",
) -> GoogleEvent:
    return GoogleEvent(
        event_id=event_id,
        summary=summary,
        start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        location=location,
        source_url=f"https://mail.google.com/mail/u/0/#inbox/{gmail_id}",
        event_type="fromGmail",
        updated=updated,
    )


def _make_email(message_id: str, subject: str = "Your flight") -> Email:
    return Email(
        message_id=message_id,
        sender="United <receipts@united.com>",
        recipients=["user@example.com"],
        subject=subject,
        body_text="Flight confirmation: UA123 SFO -> JFK on May 1.",
        body_html=None,
        attachments=[],
        received_at=datetime(2026, 4, 20, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("EMAIL_CONCIERGE_DB_PATH", str(db_path))
    # Reset the lru_cache on settings() so the monkeypatched env vars take effect.
    from email_concierge.config import settings

    settings.cache_clear()
    yield db_path
    settings.cache_clear()


def _run_with_mocks(
    events: list[GoogleEvent],
    emails: dict[str, Email | None],
    *,
    limit: int | None = None,
    since: datetime | None = None,
) -> int:
    """Invoke the command with canned calendar events and Gmail responses.

    `emails` maps gmail_message_id -> Email (or None to simulate 404).
    Missing keys in `emails` also treated as None.
    """
    calendar_mock = MagicMock()
    calendar_mock.list_auto_events.return_value = iter(events)

    gmail_mock = MagicMock()
    gmail_mock.fetch_message.side_effect = lambda gid: emails.get(gid)

    with patch(
        "email_concierge.commands.import_training.load_credentials_from_settings",
        return_value=MagicMock(),
    ):
        return import_training_command(
            source="google",
            since=since,
            limit=limit,
            calendar_src_factory=lambda creds: calendar_mock,
            gmail_src_factory=lambda creds: gmail_mock,
        )


def test_fresh_db_three_events_three_rows(test_db: Path) -> None:
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [
        _make_event("e1", "aaaaaaaaaaaaaaaa", updated),
        _make_event("e2", "bbbbbbbbbbbbbbbb", updated),
        _make_event("e3", "cccccccccccccccc", updated),
    ]
    emails = {
        "aaaaaaaaaaaaaaaa": _make_email("<m1@united.com>"),
        "bbbbbbbbbbbbbbbb": _make_email("<m2@united.com>"),
        "cccccccccccccccc": _make_email("<m3@united.com>"),
    }

    rc = _run_with_mocks(events, emails)
    assert rc == 0

    conn = db.connect(test_db)
    rows = conn.execute(
        "SELECT message_id, label, label_source, extracted_json "
        "FROM training_examples ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["label"] == "event" for r in rows)
    assert all(r["label_source"] == "google" for r in rows)
    blob = json.loads(rows[0]["extracted_json"])
    assert blob["gmail_message_id"] == "aaaaaaaaaaaaaaaa"
    assert blob["event"]["title"] == "Flight UA123"


def test_rerun_is_idempotent(test_db: Path) -> None:
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [_make_event("e1", "aaaaaaaaaaaaaaaa", updated)]
    emails = {"aaaaaaaaaaaaaaaa": _make_email("<m1@united.com>")}

    _run_with_mocks(events, emails)
    # Second run: identical input.
    _run_with_mocks(events, emails)

    conn = db.connect(test_db)
    count = conn.execute("SELECT COUNT(*) as n FROM training_examples").fetchone()["n"]
    assert count == 1


def test_limit_stops_after_n(test_db: Path) -> None:
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [
        _make_event(f"e{i}", f"{'a' * 16}{i}"[-16:], updated) for i in range(5)
    ]
    emails = {ev.gmail_message_id: _make_email(f"<m{i}@u.com>") for i, ev in enumerate(events)}

    _run_with_mocks(events, emails, limit=2)

    conn = db.connect(test_db)
    count = conn.execute("SELECT COUNT(*) as n FROM training_examples").fetchone()["n"]
    assert count == 2


def test_missing_gmail_message_skipped_without_crash(test_db: Path) -> None:
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [
        _make_event("e1", "aaaaaaaaaaaaaaaa", updated),
        _make_event("e2", "bbbbbbbbbbbbbbbb", updated),
    ]
    # Only first gmail id resolves.
    emails = {"aaaaaaaaaaaaaaaa": _make_email("<m1@united.com>")}

    _run_with_mocks(events, emails)

    conn = db.connect(test_db)
    rows = conn.execute("SELECT message_id FROM training_examples").fetchall()
    assert len(rows) == 1
    assert rows[0]["message_id"] == "<m1@united.com>"


def test_cursor_advances_to_max_updated(test_db: Path) -> None:
    t1 = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    t3 = datetime(2026, 4, 12, 10, 0, tzinfo=UTC)
    events = [
        _make_event("e1", "aaaaaaaaaaaaaaaa", t1),
        _make_event("e2", "bbbbbbbbbbbbbbbb", t2),
        _make_event("e3", "cccccccccccccccc", t3),
    ]
    emails = {
        "aaaaaaaaaaaaaaaa": _make_email("<m1@u.com>"),
        "bbbbbbbbbbbbbbbb": _make_email("<m2@u.com>"),
        "cccccccccccccccc": _make_email("<m3@u.com>"),
    }

    _run_with_mocks(events, emails)

    conn = db.connect(test_db)
    row = conn.execute(
        "SELECT cursor FROM google_sync_state WHERE kind = 'calendar'"
    ).fetchone()
    assert row is not None
    assert datetime.fromisoformat(row["cursor"]) == t2


def test_second_run_passes_cursor_as_updated_min(test_db: Path) -> None:
    t1 = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    events1 = [_make_event("e1", "aaaaaaaaaaaaaaaa", t1)]
    emails1 = {"aaaaaaaaaaaaaaaa": _make_email("<m1@u.com>")}
    _run_with_mocks(events1, emails1)

    # Second run: calendar should be invoked with updated_min == t1.
    calendar_mock = MagicMock()
    calendar_mock.list_auto_events.return_value = iter([])
    gmail_mock = MagicMock()
    with patch(
        "email_concierge.commands.import_training.load_credentials_from_settings",
        return_value=MagicMock(),
    ):
        import_training_command(
            source="google",
            since=None,
            limit=None,
            calendar_src_factory=lambda creds: calendar_mock,
            gmail_src_factory=lambda creds: gmail_mock,
        )

    call_kwargs = calendar_mock.list_auto_events.call_args.kwargs
    assert call_kwargs["updated_min"] == t1
    assert call_kwargs["since"] is None


def test_explicit_since_overrides_stored_cursor(test_db: Path) -> None:
    # Seed a cursor first.
    t_old = datetime(2026, 4, 10, 10, 0, tzinfo=UTC)
    _run_with_mocks(
        [_make_event("e1", "aaaaaaaaaaaaaaaa", t_old)],
        {"aaaaaaaaaaaaaaaa": _make_email("<m1@u.com>")},
    )

    explicit = datetime(2020, 1, 1, tzinfo=UTC)
    calendar_mock = MagicMock()
    calendar_mock.list_auto_events.return_value = iter([])
    with patch(
        "email_concierge.commands.import_training.load_credentials_from_settings",
        return_value=MagicMock(),
    ):
        import_training_command(
            source="google",
            since=explicit,
            limit=None,
            calendar_src_factory=lambda creds: calendar_mock,
            gmail_src_factory=lambda creds: MagicMock(),
        )

    call_kwargs = calendar_mock.list_auto_events.call_args.kwargs
    assert call_kwargs["since"] == explicit
    assert call_kwargs["updated_min"] is None


def test_bad_source_returns_error(test_db: Path) -> None:
    rc = import_training_command(source="nope")
    assert rc == 2
