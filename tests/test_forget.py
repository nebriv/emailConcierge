"""Tests for the `forget` command.

Goal: give the user a way to delete a calendar event *without* the
feedback scanner turning the deletion into a negative training label.
The command drops the calendar_events row (so the scan no longer sees
it) and optionally also deletes the event on the CalDAV server.
Training labels are deliberately untouched.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from email_concierge import db
from email_concierge.commands.forget import forget_command


class FakeEvent:
    def __init__(self, uid: str, *, fail_delete: bool = False) -> None:
        self.uid = uid
        self.fail_delete = fail_delete
        self.deleted = False

    def delete(self) -> None:
        if self.fail_delete:
            raise RuntimeError("caldav offline")
        self.deleted = True


class FakeCalendar:
    """Stand-in for caldav.Calendar."""

    def __init__(self, events: dict[str, FakeEvent] | None = None) -> None:
        self.events = events or {}
        self.lookups: list[str] = []

    def event_by_uid(self, uid: str):
        self.lookups.append(uid)
        event = self.events.get(uid)
        if event is None:
            raise RuntimeError("not found")
        return event


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_CONCIERGE_DB_PATH", str(tmp_path / "forget.db"))
    monkeypatch.setenv("EMAIL_CONCIERGE_DRY_RUN", "true")
    from email_concierge.config import settings as settings_fn
    settings_fn.cache_clear()
    yield tmp_path
    settings_fn.cache_clear()


def _seed(conn: sqlite3.Connection, *, uid: str, message_id: str, label: str = "event") -> None:
    now = datetime.now(tz=UTC).isoformat()
    conn.execute(
        """INSERT INTO processed_messages
             (message_id, received_at, sender, subject, status, processed_at)
           VALUES (?, ?, 'x@y.com', 's', 'processed', ?)""",
        (message_id, now, now),
    )
    conn.execute(
        """INSERT INTO training_examples
             (message_id, sender, subject, body_preview, label, label_source,
              extracted_json, created_at)
           VALUES (?, 'x@y.com', 's', 'preview', ?, 'auto', '{}', ?)""",
        (message_id, label, now),
    )
    conn.execute(
        """INSERT INTO calendar_events
             (ical_uid, message_id, caldav_url, summary, starts_at,
              created_at, updated_at)
           VALUES (?, ?, 'http://fake/', 'Some Event', ?, ?, ?)""",
        (uid, message_id, now, now, now),
    )
    conn.commit()


def test_forget_drops_calendar_row_but_leaves_training_label(isolated_db):
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    _seed(conn, uid="uid-a", message_id="<m1>")

    rc = forget_command(uid="uid-a")
    assert rc == 0

    row = conn.execute(
        "SELECT 1 FROM calendar_events WHERE ical_uid = ?", ("uid-a",),
    ).fetchone()
    assert row is None, "calendar_events row should be gone"

    label_row = conn.execute(
        "SELECT label, label_source FROM training_examples WHERE message_id='<m1>'"
    ).fetchone()
    assert label_row["label"] == "event"
    assert label_row["label_source"] == "auto"


def test_forget_without_delete_remote_does_not_contact_calendar(isolated_db):
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    _seed(conn, uid="uid-a", message_id="<m1>")
    cal = FakeCalendar(events={"uid-a": FakeEvent("uid-a")})

    rc = forget_command(uid="uid-a", calendar=cal)
    assert rc == 0
    assert cal.lookups == [], "should not query CalDAV without --delete-remote"


def test_forget_with_delete_remote_also_deletes_on_server(isolated_db):
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    _seed(conn, uid="uid-a", message_id="<m1>")
    event = FakeEvent("uid-a")
    cal = FakeCalendar(events={"uid-a": event})

    rc = forget_command(uid="uid-a", delete_remote=True, calendar=cal)
    assert rc == 0
    assert event.deleted
    row = conn.execute(
        "SELECT 1 FROM calendar_events WHERE ical_uid = ?", ("uid-a",),
    ).fetchone()
    assert row is None


def test_forget_with_delete_remote_when_already_gone_on_server(isolated_db):
    """User already deleted on the calendar app; they just want us to forget."""
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    _seed(conn, uid="uid-a", message_id="<m1>")
    cal = FakeCalendar(events={})  # server has nothing

    rc = forget_command(uid="uid-a", delete_remote=True, calendar=cal)
    assert rc == 0
    # Local row still gets dropped — that's the whole point.
    row = conn.execute(
        "SELECT 1 FROM calendar_events WHERE ical_uid = ?", ("uid-a",),
    ).fetchone()
    assert row is None


def test_forget_dry_run_touches_nothing(isolated_db):
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    _seed(conn, uid="uid-a", message_id="<m1>")
    event = FakeEvent("uid-a")
    cal = FakeCalendar(events={"uid-a": event})

    rc = forget_command(uid="uid-a", delete_remote=True, dry_run=True, calendar=cal)
    assert rc == 0
    assert not event.deleted
    row = conn.execute(
        "SELECT 1 FROM calendar_events WHERE ical_uid = ?", ("uid-a",),
    ).fetchone()
    assert row is not None


def test_forget_unknown_uid_returns_1(isolated_db):
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    # Nothing seeded.
    rc = forget_command(uid="uid-missing")
    assert rc == 1


def test_forget_empty_uid_rejected(isolated_db):
    rc = forget_command(uid="")
    assert rc == 2


def test_forget_remote_delete_failure_still_drops_local_row(isolated_db):
    """If the server errors on delete we log and move on — next-run retry
    is safe, and we don't want the user stuck with an un-forgettable row."""
    conn = db.connect(isolated_db / "forget.db")
    db.init_schema(conn)
    _seed(conn, uid="uid-a", message_id="<m1>")
    event = FakeEvent("uid-a", fail_delete=True)
    cal = FakeCalendar(events={"uid-a": event})

    rc = forget_command(uid="uid-a", delete_remote=True, calendar=cal)
    assert rc == 0
    row = conn.execute(
        "SELECT 1 FROM calendar_events WHERE ical_uid = ?", ("uid-a",),
    ).fetchone()
    assert row is None
