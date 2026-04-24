from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

from email_concierge.config import settings
from email_concierge.extractors.base import Extractor
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult
from email_concierge.router import route

log = get_logger(__name__)

BODY_PREVIEW_LEN = 500


class Sink(Protocol):
    def write(self, result: ExtractionResult, message_id: str) -> str: ...


def process_email(
    email: Email,
    conn: sqlite3.Connection,
    extractors: Iterable[Extractor],
    sink: Sink,
    source: str = "live",
) -> str:
    """Process a single email end-to-end. Returns the final status string.

    Status values: "processed" | "skipped_dedup" | "skipped_filter" |
    "no_extraction" | "failed".

    Always writes a processed_messages row (idempotent via PK) and a
    training_examples row (idempotent via UNIQUE constraint). The
    training_examples write is what lets Phase 5 train on data
    accumulated from day one.
    """
    if _already_processed(conn, email.message_id):
        log.debug("skipped_dedup", message_id=email.message_id, source=source)
        return "skipped_dedup"

    if _filtered_by_sender(email.sender):
        log.info(
            "skipped_filter",
            message_id=email.message_id,
            sender=email.sender,
            source=source,
        )
        _record_processed(
            conn, email, stage=None, name=None, confidence=None,
            status="skipped_filter", error=None,
        )
        _record_training_example(
            conn, email, label="neither", label_source="auto_filter", extracted=None,
        )
        return "skipped_filter"

    status: str
    error: str | None = None
    result: ExtractionResult | None = None

    try:
        result = route(email, extractors)
        if result is None:
            status = "no_extraction"
        else:
            reject = _validate(result, email)
            if reject is not None:
                log.info(
                    "extraction_rejected",
                    message_id=email.message_id,
                    sender=email.sender,
                    subject=email.subject,
                    stage=result.handled_by_stage,
                    name=result.handled_by_name,
                    reason=reject,
                )
                error = reject
                result = None
                status = "rejected"
            else:
                # The sink inserts into calendar_events, which has a
                # FK → processed_messages(message_id). Write the parent
                # row first so the FK is satisfied. The final
                # _record_processed call below will REPLACE this with
                # the same values on success, or with status='failed'
                # if the sink raises.
                _record_processed(
                    conn, email,
                    stage=result.handled_by_stage,
                    name=result.handled_by_name,
                    confidence=result.confidence,
                    status="processed",
                    error=None,
                )
                sink.write(result, email.message_id)
                status = "processed"
    except Exception as e:
        log.exception(
            "pipeline_failed",
            message_id=email.message_id,
            sender=email.sender,
            source=source,
        )
        status = "failed"
        error = str(e)

    _record_processed(
        conn,
        email,
        stage=result.handled_by_stage if result else None,
        name=result.handled_by_name if result else None,
        confidence=result.confidence if result else None,
        status=status,
        error=error,
    )
    _record_training_example(
        conn,
        email,
        label="event" if result else "neither",
        label_source="auto_rejected" if status == "rejected" else "auto",
        extracted=result.parsed.model_dump(mode="json") if result else None,
    )

    log.info(
        "email_processed",
        message_id=email.message_id,
        sender=email.sender,
        subject=email.subject,
        status=status,
        stage=result.handled_by_stage if result else None,
        name=result.handled_by_name if result else None,
        confidence=result.confidence if result else None,
        source=source,
    )
    return status


def validate_extraction(result: ExtractionResult, email: Email) -> str | None:
    """Cross-extractor sanity checks before we commit to writing the event.

    Returns None if the extraction looks trustworthy, or a short reason
    string if it should be rejected. The router already gates on
    confidence; this is the second, content-aware gate.

    Rules:
    - Temporal: the event must be in the future relative to when the
      email arrived. Past-tense receipts ("thanks for your ride on
      April 8") are frequent false positives and never belong on a
      forward-looking calendar.
    - Commitment: Stage 3 (NER) and Stage 4 (LLM) must produce
      commitment_evidence. Stages 1 (.ics) and 2 (plugins) have
      structural proof instead (a real calendar attachment, a matched
      vendor template) so they skip this check.
    """
    start = result.parsed.start
    end = result.parsed.end
    latest = end if end is not None and end > start else start
    # Small grace margin — an email arriving seconds after the event
    # begins still counts as a just-in-time confirmation, not a receipt.
    grace_seconds = 300
    if (latest - email.received_at).total_seconds() < -grace_seconds:
        return (
            f"event_in_past (start={start.isoformat()}, "
            f"received_at={email.received_at.isoformat()})"
        )

    if result.handled_by_stage in (3, 4):
        evidence = (result.commitment_evidence or "").strip()
        if len(evidence) < 8:
            return "missing_commitment_evidence"

    return None


# Back-compat alias; prefer validate_extraction in new code.
_validate = validate_extraction


def _already_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return row is not None


def _filtered_by_sender(sender: str) -> bool:
    cfg = settings()
    sender_lower = (sender or "").lower()

    allow = [s.lower() for s in cfg.sender_allow_list]
    deny = [s.lower() for s in cfg.sender_deny_list]

    if allow and not any(a in sender_lower for a in allow):
        return True
    if any(d in sender_lower for d in deny):
        return True
    return False


def _record_processed(
    conn: sqlite3.Connection,
    email: Email,
    *,
    stage: int | None,
    name: str | None,
    confidence: float | None,
    status: str,
    error: str | None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_messages
            (message_id, received_at, sender, subject,
             handled_by_stage, handled_by_name, confidence,
             status, error, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email.message_id,
            email.received_at.isoformat(),
            email.sender,
            email.subject,
            stage,
            name,
            confidence,
            status,
            error,
            datetime.now(tz=UTC).isoformat(),
        ),
    )


def _record_training_example(
    conn: sqlite3.Connection,
    email: Email,
    *,
    label: str,
    label_source: str,
    extracted: dict | None,
) -> None:
    body_preview = (email.body_text or "")[:BODY_PREVIEW_LEN]
    conn.execute(
        """
        INSERT OR IGNORE INTO training_examples
            (message_id, sender, subject, body_preview,
             label, label_source, extracted_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            email.message_id,
            email.sender,
            email.subject,
            body_preview,
            label,
            label_source,
            json.dumps(extracted) if extracted else None,
            datetime.now(tz=UTC).isoformat(),
        ),
    )
