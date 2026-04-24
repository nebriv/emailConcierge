"""train — fit the Stage 3 event classifier.

Reads labeled rows from `training_examples` (accumulated by live
listening, backfill, and Google Calendar import), fits a logistic
regression over MiniLM embeddings, cross-validates, and writes the
artifact to `settings.classifier_path`. The pipeline's Stage 3 picks
up the new artifact on next start.

Model metadata is appended to `model_versions` and the new row is
flagged `is_active=1` so readers know which pickle to load.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from email_concierge import db
from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.ml.classifier import (
    ClassifierMetrics,
    EventClassifier,
    compose_training_text,
)
from email_concierge.ml.embeddings import Embedder

log = get_logger(__name__)

_KIND = "classifier"
_MIN_PER_CLASS = 10  # below this, CV isn't meaningful


def train_command(*, kind: str = "classifier", dry_run: bool = False) -> int:
    """Fit the Stage 3 classifier and persist it.

    Args:
        kind: only "classifier" is supported today. Left parameterized so
              future phases (NER fine-tuning, etc.) can reuse the command
              surface.
        dry_run: fit + report metrics but don't write the artifact or the
                 `model_versions` row. Useful for checking if retraining
                 would improve things before overwriting the live model.
    """
    if kind != "classifier":
        log.error("unknown_train_kind", kind=kind)
        return 2

    if not EventClassifier.available():
        log.error(
            "train_requires_ml_extras",
            hint="pip install -e '.[ml]'",
        )
        return 2

    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    texts, labels = _load_labeled_rows(conn)
    n_event = labels.count("event")
    n_neither = labels.count("neither")
    log.info("train_loaded_rows", total=len(texts), event=n_event, neither=n_neither)

    if n_event < _MIN_PER_CLASS or n_neither < _MIN_PER_CLASS:
        log.error(
            "train_insufficient_data",
            min_per_class=_MIN_PER_CLASS,
            event=n_event,
            neither=n_neither,
            hint=(
                "Run backfill or import-training first to accumulate "
                "training rows before training the classifier."
            ),
        )
        return 2

    embedder = Embedder(
        model_name=cfg.embedding_model,
        cache_path=Path(cfg.models_dir) / "embedding_cache.sqlite",
    )
    classifier = EventClassifier(embedder=embedder)
    metrics = classifier.fit(texts=texts, labels=labels)

    if dry_run:
        log.info("train_dry_run_complete", metrics=metrics.to_json())
        return 0

    artifact_path = Path(cfg.classifier_path)
    classifier.save(artifact_path)
    _record_model_version(conn, artifact_path, metrics)
    log.info(
        "train_done",
        artifact=str(artifact_path),
        precision_event=metrics.precision_event,
        recall_event=metrics.recall_event,
    )
    return 0


def _load_labeled_rows(
    conn: sqlite3.Connection,
) -> tuple[list[str], list[str]]:
    rows = conn.execute(
        """
        SELECT sender, subject, body_preview, label
          FROM training_examples
         WHERE label IN ('event', 'neither')
        """
    ).fetchall()
    texts = [
        compose_training_text(r["sender"], r["subject"], r["body_preview"])
        for r in rows
    ]
    labels = [r["label"] for r in rows]
    return texts, labels


def _record_model_version(
    conn: sqlite3.Connection,
    artifact_path: Path,
    metrics: ClassifierMetrics,
) -> None:
    """Write a row to `model_versions` and flip it active.

    Prior active rows for the same `kind` are marked inactive so readers
    can just `SELECT ... WHERE is_active = 1`.
    """
    now = datetime.now(tz=UTC).isoformat()
    version = now.replace(":", "").replace("-", "").replace(".", "")[:14]  # e.g. 20260424T013044

    conn.execute("UPDATE model_versions SET is_active = 0 WHERE kind = ?", (_KIND,))
    conn.execute(
        """
        INSERT INTO model_versions (
            kind, version, artifact_path,
            training_n_examples, metrics_json, trained_at, is_active
        ) VALUES (?, ?, ?, ?, ?, ?, 1)
        """,
        (
            _KIND,
            version,
            str(artifact_path),
            metrics.n_train,
            metrics.to_json(),
            now,
        ),
    )
