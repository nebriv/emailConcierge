"""import-training — harvest labeled (email, event) pairs from Google.

Google Calendar has, for years, been auto-extracting events from
booking emails and creating matching calendar entries (think flights,
hotels, restaurant reservations). Each carries a `source.url` linking
back to the Gmail message. Pairing them gives us pre-labeled training
data for the Phase 5 classifier with no human annotation.

This command is strictly READ-ONLY: it only calls `events.list` on
Calendar and `messages.get` on Gmail. Gmail is never modified, and
nothing is written to Google Calendar.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from email_concierge import db
from email_concierge.config import settings
from email_concierge.integrations.google.auth import (
    CALENDAR_READONLY,
    GMAIL_READONLY,
    load_credentials_from_settings,
)
from email_concierge.integrations.google.calendar import GoogleCalendarSource
from email_concierge.integrations.google.gmail import GmailSource
from email_concierge.integrations.google.models import GoogleEvent
from email_concierge.log import get_logger
from email_concierge.models import Email

log = get_logger(__name__)

_DEFAULT_LOOKBACK = timedelta(days=365 * 2)  # 2 years
_CURSOR_KIND = "calendar"


@dataclass
class ImportStats:
    paired: int = 0
    already_seen: int = 0
    gmail_missing: int = 0
    non_gmail_skipped: int = 0
    errors: list[str] = field(default_factory=list)


def import_training_command(
    *,
    source: str = "google",
    since: datetime | None = None,
    limit: int | None = None,
    calendar_src_factory: Any = None,
    gmail_src_factory: Any = None,
) -> int:
    """Run one import pass. Returns process exit code.

    The *_factory parameters are injected by tests; production callers
    should leave them as None.
    """
    if source != "google":
        log.error("unknown_source", source=source)
        return 2

    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    creds = load_credentials_from_settings([CALENDAR_READONLY, GMAIL_READONLY], cfg=cfg)

    calendar = (
        calendar_src_factory(creds)
        if calendar_src_factory
        else GoogleCalendarSource(creds, calendar_id=cfg.google_calendar_id)
    )
    gmail = gmail_src_factory(creds) if gmail_src_factory else GmailSource(creds)

    effective_since, effective_updated_min = _resolve_cursor(conn, since)
    log.info(
        "import_training_started",
        since=effective_since.isoformat() if effective_since else None,
        updated_min=effective_updated_min.isoformat() if effective_updated_min else None,
        limit=limit,
    )

    stats = ImportStats()
    max_updated: datetime | None = None

    for event in calendar.list_auto_events(
        since=effective_since, updated_min=effective_updated_min
    ):
        if event.updated and (max_updated is None or event.updated > max_updated):
            max_updated = event.updated

        gmail_id = event.gmail_message_id
        if gmail_id is None:
            stats.non_gmail_skipped += 1
            continue

        try:
            email = gmail.fetch_message(gmail_id)
        except Exception as e:  # noqa: BLE001 — we surface + continue, never crash the run
            log.warning("gmail_fetch_failed", gmail_id=gmail_id, error=str(e))
            stats.errors.append(f"{gmail_id}: {e}")
            continue

        if email is None:
            stats.gmail_missing += 1
            continue

        inserted = _store_row(conn, email, event, gmail_id)
        if inserted:
            stats.paired += 1
            if limit is not None and stats.paired >= limit:
                log.info("import_limit_reached", limit=limit)
                break
        else:
            stats.already_seen += 1

    _persist_cursor(conn, max_updated)

    log.info(
        "import_training_done",
        paired=stats.paired,
        already_seen=stats.already_seen,
        gmail_missing=stats.gmail_missing,
        non_gmail_skipped=stats.non_gmail_skipped,
        error_count=len(stats.errors),
    )
    return 0


def _resolve_cursor(
    conn: sqlite3.Connection, since_override: datetime | None
) -> tuple[datetime | None, datetime | None]:
    """Return (since, updated_min) for the events.list request.

    - If the caller passed --since, use it and ignore any stored cursor
      (explicit override).
    - Else if a cursor is persisted, use it as updated_min.
    - Else default to 2 years ago as `since`.
    """
    if since_override is not None:
        return since_override, None

    row = conn.execute(
        "SELECT cursor FROM google_sync_state WHERE kind = ?", (_CURSOR_KIND,)
    ).fetchone()
    if row and row["cursor"]:
        return None, datetime.fromisoformat(row["cursor"])

    return datetime.now(tz=UTC) - _DEFAULT_LOOKBACK, None


def _persist_cursor(conn: sqlite3.Connection, max_updated: datetime | None) -> None:
    if max_updated is None:
        return
    now = datetime.now(tz=UTC).isoformat()
    conn.execute(
        """
        INSERT INTO google_sync_state (kind, cursor, last_synced_at)
        VALUES (?, ?, ?)
        ON CONFLICT(kind) DO UPDATE SET cursor = excluded.cursor,
                                        last_synced_at = excluded.last_synced_at
        """,
        (_CURSOR_KIND, max_updated.isoformat(), now),
    )


def _store_row(
    conn: sqlite3.Connection,
    email: Email,
    event: GoogleEvent,
    gmail_id: str,
) -> bool:
    """Insert one paired row into processed_messages + training_examples.

    Returns True if inserted, False if the message_id was already present
    (idempotent re-run via UNIQUE constraint on training_examples.message_id).
    """
    now = datetime.now(tz=UTC).isoformat()

    extracted_blob = json.dumps(
        {
            "google_event_id": event.event_id,
            "gmail_message_id": gmail_id,
            "source_url": event.source_url,
            "event": {
                "title": event.summary,
                "start": event.start.isoformat(),
                "end": event.end.isoformat() if event.end else None,
                "location": event.location,
            },
        }
    )

    try:
        conn.execute(
            """
            INSERT INTO processed_messages (
                message_id, received_at, sender, subject,
                handled_by_stage, handled_by_name, confidence,
                status, error, processed_at
            ) VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?)
            """,
            (
                email.message_id,
                email.received_at.isoformat(),
                email.sender,
                email.subject,
                "google_calendar_import",
                "imported_from_google",
                now,
            ),
        )
    except sqlite3.IntegrityError:
        # processed_messages row exists from a prior run — the training
        # row insert below will also fail with IntegrityError and we'll
        # report "already seen". Don't double-count here.
        pass

    try:
        conn.execute(
            """
            INSERT INTO training_examples (
                message_id, sender, subject, body_preview,
                label, label_source, extracted_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email.message_id,
                email.sender,
                email.subject,
                email.body_text,
                "event",
                "google",
                extracted_blob,
                now,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    return True
