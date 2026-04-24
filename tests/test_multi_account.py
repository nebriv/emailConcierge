"""Multi-account support: config parsing, schema migration, pipeline
threading of the account tag through DB writes.

Does not cover live IMAP listener spin-up across threads — that's
exercised by integration-style tests that run the listener loop.
"""

from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from email_concierge import db
from email_concierge.config import Account, Settings
from email_concierge.pipeline import process_email


def _clear_account_env(monkeypatch, tmp_path=None) -> None:
    """Isolate tests from the dev's real config:
      1. drop any EMAIL_CONCIERGE_ACCOUNT_<N> env vars currently set,
      2. chdir to a fresh temp dir so _read_indexed_env can't pick up
         the repo's real .env (which has the dev's personal accounts).
    """
    import os
    for key in list(os.environ):
        if key.startswith("EMAIL_CONCIERGE_ACCOUNT_"):
            monkeypatch.delenv(key, raising=False)
    if tmp_path is not None:
        monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Every test in this module runs with a clean ACCOUNT_* env and in
    a directory with no .env — keeps the dev's real creds from leaking in."""
    _clear_account_env(monkeypatch, tmp_path)


def test_accounts_defaults_to_single_legacy_from_imap_env(monkeypatch):
    _clear_account_env(monkeypatch)
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


def test_accounts_url_form_overrides_single_account(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://me%40personal.com:pw1@mail.personal.com/INBOX#personal",
    )
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_2",
        "imaps://me%40gmail.com:pw2@imap.gmail.com/INBOX#gmail",
    )
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_HOST", "ignored.example.com")

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    accts = s.accounts

    assert [a.name for a in accts] == ["personal", "gmail"]
    assert accts[0].host == "mail.personal.com"
    assert accts[0].username == "me@personal.com"  # %40 decoded
    assert accts[0].password == "pw1"
    assert accts[0].port == 993  # imaps default
    assert accts[0].use_ssl is True
    assert accts[1].host == "imap.gmail.com"


def test_accounts_url_form_imap_scheme_is_plaintext(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imap://u:p@mail.local/INBOX#local",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    accts = s.accounts
    assert accts[0].use_ssl is False
    assert accts[0].port == 143  # imap default


def test_accounts_url_form_explicit_port_overrides_default(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://u:p@host.example.com:9993/INBOX#custom",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.accounts[0].port == 9993


def test_accounts_url_form_empty_path_defaults_to_inbox(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://u:p@host.example.com#shortname",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.accounts[0].folder == "INBOX"


def test_accounts_url_form_url_encoded_password(monkeypatch):
    """Percent-encoded forms still work (RFC-strict users, copy-paste
    from older examples). Kept so we don't regress on strict encoders."""
    _clear_account_env(monkeypatch)
    # password is "p@ss:w/rd#1"
    encoded = "p%40ss%3Aw%2Frd%231"
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        f"imaps://user:{encoded}@host.example.com/INBOX#acct",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.accounts[0].password == "p@ss:w/rd#1"


def test_accounts_url_form_unencoded_at_in_username(monkeypatch):
    """Email-address usernames should not require percent-encoding the '@'.
    Parser uses rightmost '@' as the user/host separator so this is safe."""
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://contact@nebriv.com:plainpw@mail.nebriv.com/INBOX#nebriv",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    a = s.accounts[0]
    assert a.username == "contact@nebriv.com"
    assert a.password == "plainpw"
    assert a.host == "mail.nebriv.com"


def test_accounts_url_form_unencoded_at_in_password(monkeypatch):
    """Passwords containing '@' (a common PW-generator output) should not
    require percent-encoding — rightmost '@' delimits the host."""
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://contact@nebriv.com:Hbw]u@^=Dz~g@mail.nebriv.com/INBOX#nebriv",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    a = s.accounts[0]
    assert a.username == "contact@nebriv.com"
    assert a.password == "Hbw]u@^=Dz~g"
    assert a.host == "mail.nebriv.com"


def test_accounts_url_form_unencoded_hash_in_password(monkeypatch):
    """Passwords with '#' work unencoded as long as the required '#name'
    fragment is still present — rightmost '#' wins for the name split."""
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://ben@benvirgilio.com:BCbo#PaA6Mwy@mail.benvirgilio.com/INBOX#benv",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    a = s.accounts[0]
    assert a.username == "ben@benvirgilio.com"
    assert a.password == "BCbo#PaA6Mwy"
    assert a.host == "mail.benvirgilio.com"
    assert a.name == "benv"


def test_accounts_url_form_nested_folder_preserved(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://u:p@host.example.com/Work%2FProjects#work",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.accounts[0].folder == "Work/Projects"


def test_accounts_url_form_non_contiguous_indices(monkeypatch):
    """Gaps in EMAIL_CONCIERGE_ACCOUNT_<N> numbering are fine and sort
    numerically (ACCOUNT_2 before ACCOUNT_10)."""
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_10",
        "imaps://u:p@h10/INBOX#ten",
    )
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_2",
        "imaps://u:p@h2/INBOX#two",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert [a.name for a in s.accounts] == ["two", "ten"]


def test_accounts_url_form_missing_fragment_rejected(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://u:p@host.example.com/INBOX",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="missing account name"):
        _ = s.accounts


def test_accounts_url_form_bad_scheme_rejected(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "pop3://u:p@host.example.com/INBOX#oops",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="scheme must be 'imaps' or 'imap'"):
        _ = s.accounts


def test_accounts_url_form_rejects_duplicate_names(monkeypatch):
    _clear_account_env(monkeypatch)
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_1",
        "imaps://u:p@h1/INBOX#dup",
    )
    monkeypatch.setenv(
        "EMAIL_CONCIERGE_ACCOUNT_2",
        "imaps://u:p@h2/INBOX#dup",
    )
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="duplicate account name"):
        _ = s.accounts


def test_accounts_url_form_error_message_names_env_var(monkeypatch):
    """Errors should surface the exact EMAIL_CONCIERGE_ACCOUNT_<N> that
    caused them so ops know which var to fix."""
    _clear_account_env(monkeypatch)
    monkeypatch.setenv("EMAIL_CONCIERGE_ACCOUNT_7", "imaps://u:p@h/INBOX")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="EMAIL_CONCIERGE_ACCOUNT_7"):
        _ = s.accounts


def test_accounts_url_form_blank_value_ignored(monkeypatch):
    """An empty EMAIL_CONCIERGE_ACCOUNT_<N> shouldn't be treated as a
    configured account — fall back to the legacy single-account path."""
    _clear_account_env(monkeypatch)
    monkeypatch.setenv("EMAIL_CONCIERGE_ACCOUNT_1", "")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_HOST", "mail.example.com")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_USERNAME", "ben@example.com")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_PASSWORD", "secret")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    accts = s.accounts
    assert len(accts) == 1
    assert accts[0].name == "ben@example.com"


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
