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
    paired_via_heuristic: int = 0
    paired_via_plid: int = 0
    plid_unresolved: int = 0
    errors: list[str] = field(default_factory=list)


def import_training_command(
    *,
    source: str = "google",
    since: datetime | None = None,
    limit: int | None = None,
    resolve_plids: bool = False,
    calendar_src_factory: Any = None,
    gmail_src_factory: Any = None,
    plid_resolver_factory: Any = None,
) -> int:
    """Run one import pass. Returns process exit code.

    When `resolve_plids` is True, events whose `source.url` is a web-UI
    `plid=` permalink (common on Calendar auto-extracted events) are
    resolved to Gmail thread IDs via a browser session — the plid-
    resolver optional dependency group must be installed. Loading a
    plid URL in a browser marks the underlying email as read server-
    side, which is why this is an explicit opt-in.

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

    plid_resolver: Any = None
    if resolve_plids:
        plid_resolver = _build_plid_resolver(cfg, plid_resolver_factory)

    effective_since, effective_updated_min = _resolve_cursor(conn, since)
    log.info(
        "import_training_started",
        since=effective_since.isoformat() if effective_since else None,
        updated_min=effective_updated_min.isoformat() if effective_updated_min else None,
        limit=limit,
    )

    stats = ImportStats()
    max_updated: datetime | None = None

    try:
        for event in calendar.list_auto_events(
            since=effective_since, updated_min=effective_updated_min
        ):
            if event.updated and (max_updated is None or event.updated > max_updated):
                max_updated = event.updated

            gmail_id = event.gmail_message_id
            email: Email | None = None
            pairing_strategy = "direct"

            if gmail_id is not None:
                try:
                    email = gmail.fetch_message(gmail_id)
                except Exception as e:  # noqa: BLE001 — surface + continue, never crash the run
                    log.warning("gmail_fetch_failed", gmail_id=gmail_id, error=str(e))
                    stats.errors.append(f"{gmail_id}: {e}")

            if email is None and plid_resolver is not None and event.plid:
                thread_id = None
                try:
                    thread_id = plid_resolver.resolve(event.plid)
                except Exception as e:  # noqa: BLE001 — never crash the run on a browser hiccup
                    log.warning(
                        "plid_resolve_failed", plid=event.plid, error=str(e)
                    )
                    stats.errors.append(f"plid {event.plid}: {e}")
                if thread_id is None:
                    stats.plid_unresolved += 1
                else:
                    try:
                        email = gmail.fetch_first_in_thread(thread_id)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "gmail_fetch_failed",
                            thread_id=thread_id,
                            error=str(e),
                        )
                        stats.errors.append(f"thread {thread_id}: {e}")
                    if email is not None:
                        gmail_id = thread_id
                        pairing_strategy = "plid"

            if email is None and event.summary:
                around = event.updated or event.start
                if around is not None:
                    try:
                        candidate_id = gmail.find_best_message(
                            summary=event.summary, around=around
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "gmail_search_failed", summary=event.summary, error=str(e)
                        )
                        candidate_id = None
                    if candidate_id is not None:
                        try:
                            email = gmail.fetch_message(candidate_id)
                        except Exception as e:  # noqa: BLE001
                            log.warning(
                                "gmail_fetch_failed", gmail_id=candidate_id, error=str(e)
                            )
                            stats.errors.append(f"{candidate_id}: {e}")
                        if email is not None:
                            gmail_id = candidate_id
                            pairing_strategy = "heuristic"

            if email is None:
                stats.gmail_missing += 1
                log.info(
                    "import_skip_no_pairing",
                    summary=event.summary,
                    source_title=event.source_title,
                    source_url=event.source_url,
                    event_type=event.event_type,
                    start=event.start.isoformat() if event.start else None,
                    end=event.end.isoformat() if event.end else None,
                    location=event.location,
                    updated=event.updated.isoformat() if event.updated else None,
                    extracted_gmail_id=event.gmail_message_id,
                    plid=event.plid,
                )
                continue

            assert gmail_id is not None  # set by whichever branch produced `email`
            inserted = _store_row(conn, email, event, gmail_id)
            if inserted:
                stats.paired += 1
                if pairing_strategy == "heuristic":
                    stats.paired_via_heuristic += 1
                elif pairing_strategy == "plid":
                    stats.paired_via_plid += 1
                log.info(
                    "import_paired",
                    pairing_strategy=pairing_strategy,
                    sender=email.sender,
                    subject=email.subject,
                    gmail_id=gmail_id,
                    paired_total=stats.paired,
                )
                if limit is not None and stats.paired >= limit:
                    log.info("import_limit_reached", limit=limit)
                    break
            else:
                log.info("import_skipped, already seen")
                stats.already_seen += 1
    finally:
        if plid_resolver is not None:
            plid_resolver.close()

    _persist_cursor(conn, max_updated)

    log.info(
        "import_training_done",
        paired=stats.paired,
        paired_via_heuristic=stats.paired_via_heuristic,
        paired_via_plid=stats.paired_via_plid,
        plid_unresolved=stats.plid_unresolved,
        already_seen=stats.already_seen,
        gmail_missing=stats.gmail_missing,
        non_gmail_skipped=stats.non_gmail_skipped,
        error_count=len(stats.errors),
    )
    return 0


def _build_plid_resolver(cfg: Any, factory: Any) -> Any:
    """Construct a PlidResolver from settings or a test-supplied factory.

    Kept in a helper so that tests can inject a fake resolver without
    pulling in the Selenium dependency, and so that the ImportError path
    (user ran `--resolve-plids` without the plid-resolver extras
    installed) surfaces a readable message rather than a generic stack
    trace.
    """
    if factory is not None:
        resolver = factory(cfg)
    else:
        try:
            from email_concierge.integrations.google.plid_resolver import PlidResolver
        except ImportError as e:
            raise RuntimeError(
                "--resolve-plids requires the plid-resolver extras. "
                "Install with: pip install -e '.[plid-resolver]'"
            ) from e
        resolver = PlidResolver(
            profile_path=cfg.google_chrome_profile_path,
            chrome_major=cfg.google_chrome_major or None,
        )
    resolver.ensure_logged_in()
    return resolver


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
            "body_html": email.body_html,
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

    _store_attachments(conn, email, now)
    return True


def _store_attachments(
    conn: sqlite3.Connection, email: Email, now: str
) -> None:
    for att in email.attachments:
        if not att.payload:
            # Metadata-only (bytes either oversized or fetch disabled);
            # don't take up a row for zero content.
            continue
        conn.execute(
            """
            INSERT INTO training_example_attachments (
                message_id, filename, content_type, payload, size_bytes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                email.message_id,
                att.filename,
                att.content_type,
                att.payload,
                len(att.payload),
                now,
            ),
        )
