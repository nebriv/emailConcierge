"""Tests for ml/classifier.py.

Uses a fake Embedder (returns deterministic float vectors from text
hashes) + real sklearn — no torch/sentence-transformers dependency
required. Skipped if scikit-learn isn't installed in this env.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

import numpy as np  # noqa: E402

from email_concierge.ml.classifier import (  # noqa: E402
    EventClassifier,
    compose_training_text,
)


class FakeEmbedder:
    """Deterministic embedder: maps each unique text to a stable random vector."""

    def __init__(self, dim: int = 16) -> None:
        self._dim = dim
        self._model_name = "fake"  # matches real Embedder attribute for .save()

    def encode(self, texts: list[str]) -> np.ndarray:
        rows = []
        for t in texts:
            h = int(hashlib.sha1(t.encode()).hexdigest(), 16)
            rng = np.random.default_rng(h & 0xFFFFFFFF)
            rows.append(rng.standard_normal(self._dim).astype(np.float32))
        return np.stack(rows) if rows else np.zeros((0, self._dim), dtype=np.float32)


def _synthetic_data(n_per_class: int = 50):
    """Build a clean 2-class dataset so the classifier can actually learn.

    Using tokens the FakeEmbedder distinguishes reliably.
    """
    texts: list[str] = []
    labels: list[str] = []
    for i in range(n_per_class):
        texts.append(
            compose_training_text(
                sender="flights@airline.com",
                subject=f"Your flight #{i}",
                body_preview="Departure Gate Arrival Airport Seat",
            )
        )
        labels.append("event")
    for i in range(n_per_class):
        texts.append(
            compose_training_text(
                sender="news@substack.com",
                subject=f"Weekly digest #{i}",
                body_preview="Read more Subscribe Unsubscribe Newsletter",
            )
        )
        labels.append("neither")
    return texts, labels


def test_classifier_fits_and_reports_metrics():
    texts, labels = _synthetic_data(n_per_class=30)
    clf = EventClassifier(embedder=FakeEmbedder())
    metrics = clf.fit(texts=texts, labels=labels)
    assert metrics.n_train == 60
    assert metrics.precision_event >= 0.0
    assert metrics.recall_event >= 0.0


def test_classifier_predicts_probabilities_after_fit():
    texts, labels = _synthetic_data(n_per_class=30)
    clf = EventClassifier(embedder=FakeEmbedder())
    clf.fit(texts=texts, labels=labels)

    preds = clf.predict_proba_event(
        [
            compose_training_text("flights@airline.com", "Your flight #X", "Gate Seat"),
            compose_training_text("news@substack.com", "Weekly digest", "Unsubscribe"),
        ]
    )
    assert len(preds) == 2
    assert all(0.0 <= p <= 1.0 for p in preds)


def test_classifier_raises_on_predict_before_fit():
    clf = EventClassifier(embedder=FakeEmbedder())
    with pytest.raises(RuntimeError):
        clf.predict_proba_event(["anything"])


def test_classifier_raises_on_unknown_label():
    clf = EventClassifier(embedder=FakeEmbedder())
    with pytest.raises(ValueError):
        clf.fit(texts=["a", "b"], labels=["event", "bogus"])


def test_classifier_raises_on_empty_training_set():
    clf = EventClassifier(embedder=FakeEmbedder())
    with pytest.raises(ValueError):
        clf.fit(texts=[], labels=[])


def test_classifier_save_and_load_roundtrip(tmp_path: Path):
    texts, labels = _synthetic_data(n_per_class=20)
    clf = EventClassifier(embedder=FakeEmbedder())
    clf.fit(texts=texts, labels=labels)

    path = tmp_path / "clf.pkl"
    clf.save(path)
    assert path.exists()

    probe = [compose_training_text("flights@airline.com", "Your flight", "Gate")]
    before = clf.predict_proba_event(probe)

    loaded = EventClassifier(embedder=FakeEmbedder())
    loaded.load(path)
    after = loaded.predict_proba_event(probe)
    assert before == pytest.approx(after, abs=1e-6)


def test_classifier_save_raises_when_unfitted(tmp_path: Path):
    clf = EventClassifier(embedder=FakeEmbedder())
    with pytest.raises(RuntimeError):
        clf.save(tmp_path / "nope.pkl")
