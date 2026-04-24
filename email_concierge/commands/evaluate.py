"""evaluate — replay recent emails through every stage, surface disagreements.

Reads recent `training_examples` rows (the body_preview + sender + subject
+ extracted_json are all we need; no IMAP fetch). For each sample, runs
every registered extractor — not just the first that accepts, as the
router does in production — and diffs their outputs.

Useful for:
  - Finding plugins that silently break when a vendor changes their
    template (plugin disagrees with LLM).
  - Sanity-checking that Stage 3's classifier gate isn't turning away
    emails that Stage 4 would happily extract.
  - Producing a "cross-stage agreement" report before committing to a
    new classifier artifact.

Strictly offline: no IMAP, no CalDAV. Can be run from a laptop against a
prod DB copy.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import UTC, datetime
from typing import Any

from email_concierge import db
from email_concierge.config import settings
from email_concierge.extractors.base import Extractor
from email_concierge.extractors.discovery import discover_plugins
from email_concierge.extractors.ics import IcsExtractor
from email_concierge.extractors.llm import LlmExtractor
from email_concierge.extractors.ner import NerEventExtractor
from email_concierge.log import get_logger
from email_concierge.models import Email

log = get_logger(__name__)


def evaluate_command(
    *,
    sample: int = 100,
    seed: int | None = None,
    require_plugin: str | None = None,
) -> int:
    """Run N sample emails through every extractor and log disagreements.

    Args:
        sample: how many rows to draw from `training_examples`.
        seed: RNG seed for reproducible samples (tests / regression runs).
        require_plugin: if set, only sample rows where the extracted_json
                        was produced by that extractor name.
    """
    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    rows = _load_sample(conn, sample=sample, seed=seed, require_plugin=require_plugin)
    if not rows:
        log.warning("evaluate_no_rows")
        return 0

    extractors: list[Extractor] = [
        IcsExtractor(),
        *discover_plugins(),
        NerEventExtractor(),
        LlmExtractor(),
    ]

    agreements = 0
    disagreements = 0
    for row in rows:
        email = _row_to_email(row)
        outcomes = _run_all(email, extractors)
        diff = _disagreement_summary(outcomes)
        if diff is None:
            agreements += 1
            continue
        disagreements += 1
        log.warning(
            "evaluate_disagreement",
            message_id=row["message_id"],
            sender=email.sender,
            subject=email.subject,
            outcomes=outcomes,
            summary=diff,
        )

    log.info(
        "evaluate_done",
        total=len(rows),
        agreements=agreements,
        disagreements=disagreements,
    )
    return 0


def _load_sample(
    conn: sqlite3.Connection,
    *,
    sample: int,
    seed: int | None,
    require_plugin: str | None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT te.message_id, te.sender, te.subject, te.body_preview,
               te.extracted_json, te.created_at, pm.handled_by_name
          FROM training_examples te
          LEFT JOIN processed_messages pm ON pm.message_id = te.message_id
         WHERE te.body_preview IS NOT NULL
    """
    args: list[Any] = []
    if require_plugin:
        sql += " AND pm.handled_by_name = ?"
        args.append(require_plugin)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    rng = random.Random(seed)
    rng.shuffle(rows)
    return rows[:sample]


def _row_to_email(row: sqlite3.Row) -> Email:
    # Synthesize a minimal Email from the stored preview. This is sufficient
    # for re-running classifier + NER + LLM; it would not be enough to replay
    # the ICS parser, since attachments aren't persisted here — those stay
    # `no_extraction` which the agreement check handles the same as any miss.
    return Email(
        message_id=row["message_id"],
        sender=row["sender"] or "",
        subject=row["subject"] or "",
        body_text=row["body_preview"] or "",
        received_at=datetime.now(tz=UTC),  # preview has no timestamp field
    )


def _run_all(
    email: Email, extractors: list[Extractor]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ext in extractors:
        try:
            applicable = ext.can_handle(email)
        except Exception as e:  # noqa: BLE001
            out.append({"name": ext.name, "stage": ext.stage, "error": f"can_handle: {e}"})
            continue
        if applicable < 0.5:
            out.append({"name": ext.name, "stage": ext.stage, "skipped": True})
            continue
        try:
            result = ext.extract(email)
        except Exception as e:  # noqa: BLE001
            out.append({"name": ext.name, "stage": ext.stage, "error": f"extract: {e}"})
            continue
        if result is None:
            out.append({"name": ext.name, "stage": ext.stage, "result": None})
        else:
            out.append(
                {
                    "name": ext.name,
                    "stage": ext.stage,
                    "result": {
                        "title": result.parsed.title,
                        "start": result.parsed.start.isoformat(),
                        "location": result.parsed.location,
                        "confidence": result.confidence,
                    },
                }
            )
    return out


def _disagreement_summary(outcomes: list[dict[str, Any]]) -> str | None:
    """Return a short description if extractors disagree, else None.

    We flag two things: (a) at least one produced a result and one did
    not, or (b) all produced results but titles differ materially.
    """
    produced = [o for o in outcomes if o.get("result")]
    nulls = [o for o in outcomes if "result" in o and o["result"] is None]
    if produced and nulls:
        return (
            f"{len(produced)} extracted / {len(nulls)} returned None "
            f"(produced: {[p['name'] for p in produced]})"
        )
    if len(produced) >= 2:
        titles = {p["result"]["title"].strip().lower() for p in produced}
        if len(titles) > 1:
            return f"title mismatch across {[p['name'] for p in produced]}: {sorted(titles)}"
    return None


# Re-exported for the CLI + tests.
__all__ = ["evaluate_command"]
