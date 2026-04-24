"""Logistic regression over sentence embeddings.

Binary classifier gate for Stage 3: does this email look like an event?
Two labels:
  - "event"    → let Stage 3 try to extract
  - "neither"  → short-circuit Stage 3, fall through to Stage 4

Tiny model, trained on a few thousand `training_examples` rows. Trains
in under 2 minutes on CPU, loads instantly at listener startup.

Gracefully degrades when scikit-learn isn't installed (ml extras not
pulled in): `available()` returns False and Stage 3 skips the classifier
gate, running NER on everything the gate would otherwise filter. Better
than nothing; worse than the trained gate. Users who care install the
extras.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from email_concierge.log import get_logger
from email_concierge.ml.embeddings import Embedder

log = get_logger(__name__)

_LABEL_EVENT = "event"
_LABEL_NEITHER = "neither"
_CLASSES = (_LABEL_EVENT, _LABEL_NEITHER)


def compose_training_text(sender: str, subject: str, body_preview: str) -> str:
    """The exact text the classifier trains and predicts on.

    Changing this invalidates every cached embedding; bump the model
    version deliberately rather than tweaking in place.
    """
    return f"{sender}\n{subject}\n{body_preview or ''}"


@dataclass
class ClassifierMetrics:
    """Summary stats produced by `train`. Serialized into model_versions.metrics_json."""

    precision_event: float
    recall_event: float
    precision_neither: float
    recall_neither: float
    n_train: int
    n_folds: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "precision_event": self.precision_event,
                "recall_event": self.recall_event,
                "precision_neither": self.precision_neither,
                "recall_neither": self.recall_neither,
                "n_train": self.n_train,
                "n_folds": self.n_folds,
            }
        )


class EventClassifier:
    """Thin wrapper around a fitted sklearn LogisticRegression."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        model: Any = None,
    ) -> None:
        self._embedder = embedder
        self._model = model  # None until fit() or load()

    @staticmethod
    def available() -> bool:
        try:
            import sklearn  # noqa: F401
        except ImportError:
            return False
        return Embedder.available()

    @property
    def is_fitted(self) -> bool:
        return self._model is not None

    def fit(
        self,
        *,
        texts: list[str],
        labels: list[str],
    ) -> ClassifierMetrics:
        """Fit + cross-validate. Returns the cross-validation metrics.

        Uses stratified 5-fold CV (or min(5, positives, negatives) folds,
        whichever is smaller) so tiny training sets still produce a
        sensible report.
        """
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import precision_recall_fscore_support
        from sklearn.model_selection import StratifiedKFold

        if len(texts) != len(labels):
            raise ValueError("texts / labels length mismatch")
        if not texts:
            raise ValueError("no training data")

        unknown = set(labels) - set(_CLASSES)
        if unknown:
            raise ValueError(f"unexpected labels: {sorted(unknown)}")

        X = self._embedder.encode(texts)
        y = np.array(labels)

        n_pos = int((y == _LABEL_EVENT).sum())
        n_neg = int((y == _LABEL_NEITHER).sum())
        n_folds = max(2, min(5, n_pos, n_neg))
        log.info(
            "classifier_fit_starting",
            n_total=len(texts),
            n_event=n_pos,
            n_neither=n_neg,
            n_folds=n_folds,
        )

        # Cross-validated metrics first — report on held-out folds, not training data.
        skf = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
        y_pred = np.empty_like(y)
        for train_idx, test_idx in skf.split(X, y):
            m = LogisticRegression(
                class_weight="balanced",
                max_iter=1000,
                random_state=42,
            )
            m.fit(X[train_idx], y[train_idx])
            y_pred[test_idx] = m.predict(X[test_idx])

        p, r, _, _ = precision_recall_fscore_support(
            y, y_pred, labels=[_LABEL_EVENT, _LABEL_NEITHER], zero_division=0
        )
        metrics = ClassifierMetrics(
            precision_event=float(p[0]),
            recall_event=float(r[0]),
            precision_neither=float(p[1]),
            recall_neither=float(r[1]),
            n_train=len(texts),
            n_folds=n_folds,
        )

        # Final fit on all data for deployment.
        self._model = LogisticRegression(
            class_weight="balanced",
            max_iter=1000,
            random_state=42,
        )
        self._model.fit(X, y)

        log.info(
            "classifier_fit_done",
            precision_event=metrics.precision_event,
            recall_event=metrics.recall_event,
            precision_neither=metrics.precision_neither,
            recall_neither=metrics.recall_neither,
        )
        return metrics

    def predict_proba_event(self, texts: list[str]) -> list[float]:
        """Return the probability of label 'event' for each input."""
        if self._model is None:
            raise RuntimeError("classifier not fitted or loaded")
        if not texts:
            return []
        X = self._embedder.encode(texts)
        proba = self._model.predict_proba(X)
        classes = list(self._model.classes_)
        event_idx = classes.index(_LABEL_EVENT)
        return [float(row[event_idx]) for row in proba]

    def save(self, path: Path) -> None:
        if self._model is None:
            raise RuntimeError("cannot save an unfitted classifier")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump(
                {
                    "model": self._model,
                    "embed_model": self._embedder._model_name,  # type: ignore[attr-defined]
                    "format_version": 1,
                },
                f,
            )
        log.info("classifier_saved", path=str(path))

    def load(self, path: Path) -> None:
        with path.open("rb") as f:
            blob = pickle.load(f)  # noqa: S301 — trusted local artifact
        self._model = blob["model"]
        log.info(
            "classifier_loaded",
            path=str(path),
            embed_model=blob.get("embed_model"),
            format_version=blob.get("format_version"),
        )
