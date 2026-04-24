"""Tests for the Phase 6 feedback detector.

Active learning: when a user deletes a Concierge-written event from CalDAV
within the feedback window, the matching training_examples row should flip
to label='neither' so the next classifier train absorbs the correction.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from email_concierge import db
from email_concierge.commands.feedback import feedback_command


class FakeCalendar:
    """Stand-in for caldav.Calendar; `present` is the set of UIDs still
    on the server."""

    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.calls: list[str] = []

    def event_by_uid(self, uid: str):
        self.calls.append(uid)
        if uid in self.present:
            return object()  # truthy stand-in
        raise RuntimeError("not found")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_CONCIERGE_DB_PATH", str(tmp_path / "fb.db"))
    monkeypatch.setenv("EMAIL_CONCIERGE_FEEDBACK_WINDOW_HOURS", "24")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_URL", "http://fake.example.com/dav/")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_USERNAME", "u")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_PASSWORD", "p")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_CALENDAR", "c")
    monkeypatch.setenv("EMAIL_CONCIERGE_DRY_RUN", "true")
    from email_concierge.config import settings as settings_fn
    settings_fn.cache_clear()
    yield tmp_path
    settings_fn.cache_clear()


def _seed(conn: sqlite3.Connection, *, now: datetime, rows: list[dict]) -> None:
    """Seed processed_messages + training_examples + calendar_events together.

    Each row spec: {uid, message_id, created_offset_hours (negative=past), label}.
    """
    for r in rows:
        created_at = (now + timedelta(hours=r["created_offset_hours"])).isoformat()
        conn.execute(
            """
            INSERT INTO processed_messages
                (message_id, received_at, sender, subject, status, processed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (r["message_id"], created_at, "x@y.com", "subj", "extracted", created_at),
        )
        conn.execute(
            """
            INSERT INTO training_examples
                (message_id, sender, subject, body_preview, label, label_source,
                 extracted_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["message_id"],
                "x@y.com",
                "subj",
                "preview text",
                r.get("label", "event"),
                r.get("label_source", "auto"),
                r.get("extracted_json", "{}"),
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO calendar_events
                (ical_uid, message_id, caldav_url, summary, starts_at,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["uid"],
                r["message_id"],
                "http://fake/" + r["uid"],
                r.get("summary", "Some Event"),
                created_at,
                created_at,
                created_at,
            ),
        )


def test_deletion_within_window_marks_negative(isolated_settings):
    conn = db.connect(isolated_settings / "fb.db")
    db.init_schema(conn)
    now = datetime.now(tz=UTC)
    _seed(conn, now=now, rows=[
        {"uid": "uid-deleted", "message_id": "<m1>", "created_offset_hours": -2},
        {"uid": "uid-kept", "message_id": "<m2>", "created_offset_hours": -3},
    ])
    calendar = FakeCalendar(present={"uid-kept"})

    rc = feedback_command(calendar=calendar)
    assert rc == 0

    row = conn.execute(
        "SELECT label, label_source, extracted_json FROM training_examples WHERE message_id='<m1>'"
    ).fetchone()
    assert row["label"] == "neither"
    assert row["label_source"] == "feedback_delete"
    blob = json.loads(row["extracted_json"])
    assert blob["feedback_delete"]["uid"] == "uid-deleted"
    assert blob["feedback_delete"]["prior_label"] == "event"

    # The kept event should still be labeled event.
    kept = conn.execute(
        "SELECT label, label_source FROM training_examples WHERE message_id='<m2>'"
    ).fetchone()
    assert kept["label"] == "event"
    assert kept["label_source"] == "auto"


def test_deletion_outside_window_is_ignored(isolated_settings):
    conn = db.connect(isolated_settings / "fb.db")
    db.init_schema(conn)
    now = datetime.now(tz=UTC)
    _seed(conn, now=now, rows=[
        # Created 48h ago; window is 24h → below the cutoff, skip entirely.
        {"uid": "uid-old-delete", "message_id": "<m1>", "created_offset_hours": -48},
    ])
    calendar = FakeCalendar(present=set())  # not on server anymore

    rc = feedback_command(calendar=calendar)
    assert rc == 0

    row = conn.execute(
        "SELECT label, label_source FROM training_examples WHERE message_id='<m1>'"
    ).fetchone()
    # Event deleted long ago — treat as legit lifecycle, leave label alone.
    assert row["label"] == "event"
    assert row["label_source"] == "auto"
    # And we didn't even query the server for it.
    assert calendar.calls == []


def test_idempotent_rerun(isolated_settings):
    conn = db.connect(isolated_settings / "fb.db")
    db.init_schema(conn)
    now = datetime.now(tz=UTC)
    _seed(conn, now=now, rows=[
        {"uid": "uid-del", "message_id": "<m1>", "created_offset_hours": -1},
    ])
    calendar = FakeCalendar(present=set())

    assert feedback_command(calendar=calendar) == 0
    # Second pass should be a no-op — still labeled neither, metadata unchanged.
    first_blob = conn.execute(
        "SELECT extracted_json FROM training_examples WHERE message_id='<m1>'"
    ).fetchone()["extracted_json"]
    assert feedback_command(calendar=calendar) == 0
    second_blob = conn.execute(
        "SELECT extracted_json FROM training_examples WHERE message_id='<m1>'"
    ).fetchone()["extracted_json"]
    assert first_blob == second_blob


def test_missing_training_row_logs_but_does_not_crash(isolated_settings):
    """calendar_events can outlive training_examples if the DB predates Phase 4."""
    conn = db.connect(isolated_settings / "fb.db")
    db.init_schema(conn)
    now = datetime.now(tz=UTC).isoformat()
    # processed_message + calendar_events but no training_examples row.
    conn.execute(
        """INSERT INTO processed_messages
            (message_id, received_at, sender, subject, status, processed_at)
           VALUES ('<orphan>', ?, 'x@y.com', 's', 'extracted', ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO calendar_events
            (ical_uid, message_id, caldav_url, summary, starts_at, created_at, updated_at)
           VALUES ('uid-orphan', '<orphan>', 'http://fake/', 'S', ?, ?, ?)""",
        (now, now, now),
    )
    calendar = FakeCalendar(present=set())

    rc = feedback_command(calendar=calendar)
    assert rc == 0  # logs a warning, doesn't crash
