"""GLiNER wrapper + the entity types Stage 3 asks about.

Kept deliberately small: the zero-shot model is good but sensitive to
label choice. Labels here are biased toward booking emails — if we want
to extract other event shapes later, add labels rather than rephrasing
existing ones.

Graceful degradation: `NerExtractor.available()` returns False when
GLiNER isn't installed (ml extras); Stage 3's extractor detects that at
init and returns None from every extract() call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from email_concierge.log import get_logger

log = get_logger(__name__)

_DEFAULT_MODEL = "urchade/gliner_small-v2.1"

ENTITY_LABELS: list[str] = [
    "date",
    "time",
    "iata airport code",
    "city",
    "confirmation number",
    "flight number",
    "hotel name",
    "venue name",
    "event title",
    "street address",
    "check-in date",
    "check-out date",
]


@dataclass
class Entity:
    label: str
    text: str
    start: int
    end: int
    score: float


class NerExtractor:
    """Zero-shot entity extractor over a GLiNER model.

    Minimal surface: construct once, call `extract_entities(text)` as
    needed. GLiNER loads the model on first use — cache the instance.
    """

    def __init__(
        self,
        *,
        model_name: str = _DEFAULT_MODEL,
        model: Any = None,
        labels: list[str] | None = None,
        threshold: float = 0.35,
    ) -> None:
        self._model_name = model_name
        self._model = model
        self._labels = labels or ENTITY_LABELS
        self._threshold = threshold

    @staticmethod
    def available() -> bool:
        try:
            import gliner  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from gliner import GLiNER
        except ImportError as e:
            raise RuntimeError(
                "gliner is not installed. Install with: pip install -e '.[ml]'"
            ) from e
        log.info("ner_model_loading", model=self._model_name)
        self._model = GLiNER.from_pretrained(self._model_name)
        return self._model

    def extract_entities(self, text: str) -> list[Entity]:
        """Run the model and return the raw entity spans."""
        if not text:
            return []
        model = self._ensure_model()
        raw = model.predict_entities(text, self._labels, threshold=self._threshold)
        return [
            Entity(
                label=r["label"],
                text=r["text"],
                start=int(r["start"]),
                end=int(r["end"]),
                score=float(r.get("score", 0.0)),
            )
            for r in raw
        ]
