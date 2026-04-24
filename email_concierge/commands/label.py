"""label — manually correct training_examples labels.

Bad labels are the most expensive kind of bug: every future training
run inherits them and compounds the error. This command is how an
operator fixes a known-wrong row without editing the DB by hand.

Use case driving this: when the pipeline's auto-labeler writes
label='event' for a false positive (a past receipt, a mass-mail
announcement) and the feedback loop can't catch it — usually because
the row was never written to CalDAV — the row stays positively
mislabeled. Classifier retrains then get polluted.

Flips the row(s) to the given label and overwrites label_source to
'manual' so they're easy to audit later.
"""

from __future__ import annotations

from email_concierge import db
from email_concierge.config import settings
from email_concierge.log import get_logger

log = get_logger(__name__)

_VALID_LABELS = {"event", "neither"}


def label_command(
    *,
    message_ids: list[str],
    label: str,
    reason: str | None = None,
    dry_run: bool = False,
) -> int:
    """Flip training_examples rows to the given label.

    Args:
        message_ids: the Message-IDs to update. Must match exactly
                     (including the surrounding angle brackets, if any).
        label: 'event' or 'neither'.
        reason: free-text note, logged but not persisted to the row
                (the schema has no reason column and this command is
                expected to be rare enough that the log is sufficient).
        dry_run: report what would be changed without writing.
    """
    if label not in _VALID_LABELS:
        log.error("label_invalid", label=label, valid=sorted(_VALID_LABELS))
        return 2
    if not message_ids:
        log.error("label_no_message_ids")
        return 2

    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"""
        SELECT message_id, label, label_source, sender, subject
          FROM training_examples
         WHERE message_id IN ({placeholders})
        """,
        message_ids,
    ).fetchall()

    found_ids = {r["message_id"] for r in rows}
    missing = [mid for mid in message_ids if mid not in found_ids]
    for mid in missing:
        log.warning("label_row_not_found", message_id=mid)

    to_change = [
        r for r in rows
        if r["label"] != label or r["label_source"] != "manual"
    ]
    unchanged = len(rows) - len(to_change)

    for r in to_change:
        log.info(
            "label_update",
            message_id=r["message_id"],
            sender=r["sender"],
            subject=r["subject"],
            from_label=r["label"],
            from_source=r["label_source"],
            to_label=label,
            reason=reason,
            dry_run=dry_run,
        )

    if dry_run:
        log.info(
            "label_dry_run_done",
            would_update=len(to_change),
            unchanged=unchanged,
            missing=len(missing),
        )
        return 0

    if to_change:
        conn.executemany(
            """
            UPDATE training_examples
               SET label = ?, label_source = 'manual'
             WHERE message_id = ?
            """,
            [(label, r["message_id"]) for r in to_change],
        )
        conn.commit()

    log.info(
        "label_done",
        updated=len(to_change),
        unchanged=unchanged,
        missing=len(missing),
    )
    return 0


__all__ = ["label_command"]
