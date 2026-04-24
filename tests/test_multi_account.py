"""Multi-account support: config parsing, schema migration, pipeline
threading of the account tag through DB writes.

Does not cover live IMAP listener spin-up across threads — that's
exercised by integration-style tests that run the listener loop.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from email_concierge import db
from email_concierge.config import Account, Settings
from email_concierge.pipeline import process_email


def test_accounts_defaults_to_single_legacy_from_imap_env(monkeypatch):
    monkeypatch.delenv("EMAIL_CONCIERGE_ACCOUNTS", raising=False)
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_USERNAME", "ben@example.com")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_PASSWORD", "secret")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_FOLDER", "INBOX")

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    accts = s.accounts

    assert len(accts) == 1
    assert accts[0].name == "ben@example.com"
    assert accts[0].host == "mail.example.com"
    assert accts[0].username == "ben@example.com"
    assert accts[0].folder == "INBOX"
    assert accts[0].use_ssl is True


def test_accounts_json_overrides_single_account(monkeypatch):
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNTS",
        json.dumps([
            {
                "name": "personal",
                "host": "mail.personal.com",
                "username": "me@personal.com",
                "password": "pw1",
            },
            {
                "name": "gmail",
                "host": "imap.gmail.com",
                "username": "me@gmail.com",
                "password": "pw2",
                "folder": "INBOX",
            },
        ]),
    )
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_HOST", "ignored.example.com")

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    accts = s.accounts

    assert [a.name for a in accts] == ["personal", "gmail"]
    assert accts[0].host == "mail.personal.com"
    assert accts[1].host == "imap.gmail.com"
    # Port default carries over.
    assert accts[0].port == 993


def test_accounts_rejects_malformed_json(monkeypatch):
    monkeypatch.setenv("EMAIL_CONCIERGE_ACCOUNTS", "{not-json}")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="not valid JSON"):
        _ = s.accounts


def test_accounts_rejects_empty_array(monkeypatch):
    monkeypatch.setenv("EMAIL_CONCIERGE_ACCOUNTS", "[]")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="non-empty JSON array"):
        _ = s.accounts


def test_accounts_rejects_duplicate_names(monkeypatch):
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNTS",
        json.dumps([
            {"name": "dup", "host": "h1", "username": "u1", "password": "p1"},
            {"name": "dup", "host": "h2", "username": "u2", "password": "p2"},
        ]),
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="duplicate account name"):
        _ = s.accounts


def test_account_requires_core_fields():
    with pytest.raises(ValidationError):
        Account(name="x", host="h", username="u")  # type: ignore[call-arg]


def test_schema_adds_account_column_on_fresh_db(tmp_path):
    conn = db.connect(tmp_path / "fresh.db")
    db.init_schema(conn)
    pm_cols = {row["name"] for row in conn.execute("PRAGMA table_info(processed_messages)")}
    te_cols = {row["name"] for row in conn.execute("PRAGMA table_info(training_examples)")}
    assert "account" in pm_cols
    assert "account" in te_cols
    conn.close()


def test_schema_migration_is_idempotent_on_legacy_db(tmp_path):
    """A DB created without the `account` column should get it added, and
    a second init_schema call should be a no-op (not raise duplicate-column).
    """
    p = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(p))
    raw.executescript(
        """
        CREATE TABLE processed_messages (
            message_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            handled_by_stage INTEGER,
            handled_by_name TEXT,
            confidence REAL,
            status TEXT NOT NULL,
            error TEXT,
            processed_at TEXT NOT NULL
        );
        CREATE TABLE training_examples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            sender TEXT NOT NULL,
            subject TEXT NOT NULL,
            body_preview TEXT NOT NULL,
            label TEXT,
            label_source TEXT,
            extracted_json TEXT,
            created_at TEXT NOT NULL
        );
        INSERT INTO processed_messages VALUES ('<legacy@x>', '2026-01-01T00:00:00+00:00',
            's@x', 'subj', NULL, NULL, NULL, 'processed', NULL, '2026-01-01T00:00:00+00:00');
        """
    )
    raw.commit()
    raw.close()

    conn = db.connect(p)
    db.init_schema(conn)  # should ADD COLUMN
    db.init_schema(conn)  # idempotent

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(processed_messages)")}
    assert "account" in cols

    # Legacy row preserved with NULL account.
    row = conn.execute(
        "SELECT account FROM processed_messages WHERE message_id = '<legacy@x>'"
    ).fetchone()
    assert row["account"] is None
    conn.close()


def test_process_email_writes_account_tag(tmp_db, make_email, stub_extractor, recording_sink):
    email = make_email(message_id="<acct-tag@x>", sender="a@x.test", subject="Hi")
    sink = recording_sink()
    # No extractor matches → row is still recorded with account.
    extractors = [stub_extractor("noop", stage=2, result=None, applicability=0.0)]

    process_email(email, tmp_db, extractors, sink, source="live", account="personal")

    row = tmp_db.execute(
        "SELECT account FROM processed_messages WHERE message_id = ?",
        (email.message_id,),
    ).fetchone()
    assert row["account"] == "personal"

    te = tmp_db.execute(
        "SELECT account FROM training_examples WHERE message_id = ?",
        (email.message_id,),
    ).fetchone()
    assert te["account"] == "personal"


def test_process_email_account_tag_per_mailbox(tmp_db, make_email, stub_extractor, recording_sink):
    """Different accounts processing different message-ids should preserve
    their tags independently."""
    sink = recording_sink()
    extractors = [stub_extractor("noop", stage=2, result=None, applicability=0.0)]

    process_email(
        make_email(message_id="<m1@x>", sender="a@x.test"),
        tmp_db, extractors, sink, source="live", account="personal",
    )
    process_email(
        make_email(message_id="<m2@x>", sender="b@x.test"),
        tmp_db, extractors, sink, source="live", account="gmail",
    )

    rows = tmp_db.execute(
        "SELECT message_id, account FROM processed_messages ORDER BY message_id"
    ).fetchall()
    tags = {r["message_id"]: r["account"] for r in rows}
    assert tags == {"<m1@x>": "personal", "<m2@x>": "gmail"}
