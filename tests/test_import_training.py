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
from email_concierge.models import Attachment, Email


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
    heuristic_map: dict[str, str | None] | None = None,
    plid_map: dict[str, str | None] | None = None,
    thread_emails: dict[str, Email | None] | None = None,
    resolve_plids: bool = False,
) -> int:
    """Invoke the command with canned calendar events and Gmail responses.

    `emails` maps gmail_message_id -> Email (or None to simulate 404).
    Missing keys in `emails` also treated as None.

    `heuristic_map` maps event.summary -> gmail_id (or None) for the
    heuristic fallback path. Defaults to always returning None so
    fallback is a no-op unless the test opts in.

    `plid_map` maps plid -> thread_id (or None) for the resolver path.
    `thread_emails` maps thread_id -> Email (or None) for fetch_first_in_thread.
    `resolve_plids=True` enables the plid-resolver branch.
    """
    calendar_mock = MagicMock()
    calendar_mock.list_auto_events.return_value = iter(events)

    gmail_mock = MagicMock()
    gmail_mock.fetch_message.side_effect = lambda gid: emails.get(gid)

    heur = heuristic_map or {}
    gmail_mock.find_best_message.side_effect = lambda **kw: heur.get(kw.get("summary"))

    threads = thread_emails or {}
    gmail_mock.fetch_first_in_thread.side_effect = lambda tid: threads.get(tid)

    plid_resolver_factory = None
    if resolve_plids:
        pmap = plid_map or {}
        resolver_mock = MagicMock()
        resolver_mock.resolve.side_effect = lambda plid: pmap.get(plid)
        plid_resolver_factory = lambda _cfg: resolver_mock  # noqa: E731

    with patch(
        "email_concierge.commands.import_training.load_credentials_from_settings",
        return_value=MagicMock(),
    ):
        return import_training_command(
            source="google",
            since=since,
            limit=limit,
            resolve_plids=resolve_plids,
            calendar_src_factory=lambda creds: calendar_mock,
            gmail_src_factory=lambda creds: gmail_mock,
            plid_resolver_factory=plid_resolver_factory,
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


def test_heuristic_fallback_when_direct_fetch_fails(test_db: Path) -> None:
    """A plid-only URL gives a 400/404 on direct fetch; heuristic must rescue."""
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    # plid-like token: not directly fetchable.
    events = [
        GoogleEvent(
            event_id="e1",
            summary="Snowshoe Lodge",
            start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            source_url="https://mail.google.com/mail?extsrc=cal&plid=ACUX6DNb_plid_token_here",
            event_type="fromGmail",
            updated=updated,
        )
    ]
    # Direct fetch of the plid returns None (simulating 400/404).
    # Heuristic search resolves the summary to a real Gmail internal ID.
    real_gmail_id = "17f2e3a9b1c4d5e6"
    emails: dict[str, Email | None] = {
        "ACUX6DNb_plid_token_here": None,
        real_gmail_id: _make_email("<real@hotel.com>"),
    }
    heuristic_map = {"Snowshoe Lodge": real_gmail_id}

    rc = _run_with_mocks(events, emails, heuristic_map=heuristic_map)
    assert rc == 0

    conn = db.connect(test_db)
    rows = conn.execute(
        "SELECT message_id, extracted_json FROM training_examples"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["message_id"] == "<real@hotel.com>"
    blob = json.loads(rows[0]["extracted_json"])
    # The heuristic-found ID should be what we persist.
    assert blob["gmail_message_id"] == real_gmail_id


def test_attachments_are_persisted(test_db: Path) -> None:
    """Attachment bytes returned by Gmail should land in training_example_attachments."""
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [_make_event("e1", "aaaaaaaaaaaaaaaa", updated)]
    email = _make_email("<m1@united.com>")
    email.attachments = [
        Attachment(filename="ticket.pdf", content_type="application/pdf", payload=b"PDF-BYTES"),
        Attachment(filename="invite.ics", content_type="text/calendar", payload=b"BEGIN:VCALENDAR"),
        # Empty payload (oversized/skipped) — must NOT produce a row.
        Attachment(filename="huge.bin", content_type="application/octet-stream", payload=b""),
    ]
    emails = {"aaaaaaaaaaaaaaaa": email}

    _run_with_mocks(events, emails)

    conn = db.connect(test_db)
    rows = conn.execute(
        """SELECT filename, content_type, payload, size_bytes
           FROM training_example_attachments
           WHERE message_id = ?
           ORDER BY filename""",
        ("<m1@united.com>",),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["filename"] == "invite.ics"
    assert bytes(rows[0]["payload"]) == b"BEGIN:VCALENDAR"
    assert rows[1]["filename"] == "ticket.pdf"
    assert bytes(rows[1]["payload"]) == b"PDF-BYTES"
    assert rows[1]["size_bytes"] == len(b"PDF-BYTES")


def test_heuristic_miss_leaves_row_unwritten(test_db: Path) -> None:
    """If both direct fetch and heuristic miss, nothing is written."""
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [
        GoogleEvent(
            event_id="e1",
            summary="Unknown Event",
            start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            source_url="https://mail.google.com/mail?extsrc=cal&plid=plid_no_match",
            event_type="fromGmail",
            updated=updated,
        )
    ]
    # Direct fetch returns None; heuristic also returns None.
    _run_with_mocks(events, {}, heuristic_map={"Unknown Event": None})

    conn = db.connect(test_db)
    count = conn.execute(
        "SELECT COUNT(*) as n FROM training_examples"
    ).fetchone()["n"]
    assert count == 0


def test_bad_source_returns_error(test_db: Path) -> None:
    rc = import_training_command(source="nope")
    assert rc == 2


def _make_plid_event(
    event_id: str, plid: str, updated: datetime, summary: str = "United reservation"
) -> GoogleEvent:
    return GoogleEvent(
        event_id=event_id,
        summary=summary,
        start=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
        source_url=f"https://mail.google.com/mail?extsrc=cal&plid={plid}",
        event_type="fromGmail",
        updated=updated,
    )


def test_plid_resolver_pairs_via_thread_fetch(test_db: Path) -> None:
    """resolver plid → thread id → first-in-thread email."""
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [_make_plid_event("e1", "PLID_TOKEN", updated)]
    thread_id = "1868052c9b0dfe8b"
    email = _make_email("<booking@united.com>", subject="Your reservation is confirmed")

    rc = _run_with_mocks(
        events,
        emails={},
        plid_map={"PLID_TOKEN": thread_id},
        thread_emails={thread_id: email},
        resolve_plids=True,
    )
    assert rc == 0

    conn = db.connect(test_db)
    rows = conn.execute(
        "SELECT message_id, extracted_json FROM training_examples"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["message_id"] == "<booking@united.com>"
    blob = json.loads(rows[0]["extracted_json"])
    # Thread ID is what we persist when resolver paired the row.
    assert blob["gmail_message_id"] == thread_id


def test_plid_resolver_unresolved_falls_through_to_heuristic(test_db: Path) -> None:
    """If the resolver can't resolve the plid, heuristic search still has a shot."""
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [_make_plid_event("e1", "BAD_PLID", updated, summary="Snowshoe Lodge")]
    rescue_id = "17f2e3a9b1c4d5e6"

    rc = _run_with_mocks(
        events,
        emails={rescue_id: _make_email("<from-heur@hotel.com>")},
        plid_map={"BAD_PLID": None},  # resolver gives up
        heuristic_map={"Snowshoe Lodge": rescue_id},
        resolve_plids=True,
    )
    assert rc == 0

    conn = db.connect(test_db)
    rows = conn.execute("SELECT message_id FROM training_examples").fetchall()
    assert len(rows) == 1
    assert rows[0]["message_id"] == "<from-heur@hotel.com>"


def test_plid_resolver_closed_even_when_run_errors(test_db: Path) -> None:
    """The resolver's driver must be torn down even if the loop raises.

    We use a calendar iterator that raises mid-iteration — those exceptions
    aren't swallowed by the per-event try/except blocks and must still
    trigger resolver teardown via the outer finally.
    """

    def _exploding_events():
        yield _make_plid_event(
            "e1", "TOKEN", datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
        )
        raise RuntimeError("calendar iteration blew up")

    calendar_mock = MagicMock()
    calendar_mock.list_auto_events.return_value = _exploding_events()

    gmail_mock = MagicMock()
    gmail_mock.fetch_first_in_thread.return_value = None
    gmail_mock.fetch_message.return_value = None
    gmail_mock.find_best_message.return_value = None

    resolver_mock = MagicMock()
    resolver_mock.resolve.return_value = None

    with patch(
        "email_concierge.commands.import_training.load_credentials_from_settings",
        return_value=MagicMock(),
    ):
        with pytest.raises(RuntimeError, match="calendar iteration blew up"):
            import_training_command(
                source="google",
                resolve_plids=True,
                calendar_src_factory=lambda creds: calendar_mock,
                gmail_src_factory=lambda creds: gmail_mock,
                plid_resolver_factory=lambda _cfg: resolver_mock,
            )

    resolver_mock.close.assert_called_once()


def test_resolver_not_built_when_flag_off(test_db: Path) -> None:
    """Without --resolve-plids, a plid event falls straight to heuristic/missing."""
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [_make_plid_event("e1", "TOKEN", updated, summary="Nothing Matches")]
    # No heuristic match either → counted as missing, nothing inserted.
    _run_with_mocks(events, emails={}, heuristic_map={"Nothing Matches": None})

    conn = db.connect(test_db)
    count = conn.execute("SELECT COUNT(*) as n FROM training_examples").fetchone()["n"]
    assert count == 0


def test_resolver_ensure_logged_in_called_once(test_db: Path) -> None:
    updated = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    events = [
        _make_plid_event("e1", "T1", updated),
        _make_plid_event("e2", "T2", updated),
    ]
    resolver_mock = MagicMock()
    resolver_mock.resolve.return_value = None  # don't actually pair anything

    calendar_mock = MagicMock()
    calendar_mock.list_auto_events.return_value = iter(events)
    gmail_mock = MagicMock()
    gmail_mock.fetch_message.return_value = None
    gmail_mock.find_best_message.return_value = None

    with patch(
        "email_concierge.commands.import_training.load_credentials_from_settings",
        return_value=MagicMock(),
    ):
        import_training_command(
            source="google",
            resolve_plids=True,
            calendar_src_factory=lambda creds: calendar_mock,
            gmail_src_factory=lambda creds: gmail_mock,
            plid_resolver_factory=lambda _cfg: resolver_mock,
        )

    resolver_mock.ensure_logged_in.assert_called_once()
    resolver_mock.close.assert_called_once()
    assert resolver_mock.resolve.call_count == 2
