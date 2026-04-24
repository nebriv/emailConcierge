from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id          TEXT PRIMARY KEY,
    received_at         TEXT NOT NULL,
    sender              TEXT NOT NULL,
    subject             TEXT NOT NULL,
    handled_by_stage    INTEGER,
    handled_by_name     TEXT,
    confidence          REAL,
    status              TEXT NOT NULL,
    error               TEXT,
    processed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pm_handled_by
    ON processed_messages(handled_by_stage, handled_by_name);
CREATE INDEX IF NOT EXISTS idx_pm_received
    ON processed_messages(received_at);

CREATE TABLE IF NOT EXISTS calendar_events (
    ical_uid            TEXT PRIMARY KEY,
    message_id          TEXT NOT NULL,
    caldav_url          TEXT NOT NULL,
    summary             TEXT,
    starts_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES processed_messages(message_id)
);

CREATE TABLE IF NOT EXISTS training_examples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT NOT NULL UNIQUE,
    sender              TEXT NOT NULL,
    subject             TEXT NOT NULL,
    body_preview        TEXT NOT NULL,
    label               TEXT,
    label_source        TEXT,
    extracted_json      TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES processed_messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_te_label ON training_examples(label);

CREATE TABLE IF NOT EXISTS model_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT NOT NULL,
    version             TEXT NOT NULL,
    artifact_path       TEXT NOT NULL,
    training_n_examples INTEGER NOT NULL,
    metrics_json        TEXT NOT NULL,
    trained_at          TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS google_sync_state (
    kind                TEXT PRIMARY KEY,
    cursor              TEXT,
    last_synced_at      TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
