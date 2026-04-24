"""Stage 3: classifier-gated NER + heuristic assembler.

Flow:
  1. classifier.predict_proba_event  → if < threshold, return None.
  2. ner.extract_entities(body)      → get a list of labeled spans.
  3. assemble(entities, email)       → build a ParsedEvent if the spans
                                       form a complete enough record.

Each step can fail gracefully. Missing extras → `can_handle` is 0.0 and
the router skips this stage entirely. Classifier missing but NER ok →
runs NER on everything (less efficient, still correct). NER missing →
stage is dead for this run.

The assembler is rule-based on purpose. Generative models would be
overkill; most real bookings have a small number of shapes that map
cleanly to a title + start + optional (end, location).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.ml.classifier import EventClassifier, compose_training_text
from email_concierge.ml.embeddings import Embedder
from email_concierge.ml.ner_entities import Entity, NerExtractor
from email_concierge.models import Email, ExtractionResult, ParsedEvent

log = get_logger(__name__)


_IATA_RE = re.compile(r"\b([A-Z]{3})\b")
_FLIGHT_RE = re.compile(r"\b([A-Z]{1,3})\s?([0-9]{1,4})\b")
_BODY_PREVIEW_LEN = 500
_GATE_FLOOR = 0.5  # below this probability-of-event, skip NER entirely
_MIN_CONFIDENCE = 0.6  # under this, the router will escalate to stage 4 anyway


@dataclass
class _Assembled:
    title: str
    start: datetime
    end: datetime | None
    location: str | None
    confidence: float
    notes: list[str]


class NerEventExtractor:
    """Stage 3 extractor. Dependency-injected for tests; production
    callers should leave the constructor args as None so it wires itself
    up from settings().
    """

    name = "ner"
    stage = 3
    priority = 0

    def __init__(
        self,
        *,
        classifier: EventClassifier | None = None,
        ner: NerExtractor | None = None,
        gate_floor: float = _GATE_FLOOR,
    ) -> None:
        cfg = settings()
        self._gate_floor = gate_floor

        # NER is mandatory for Stage 3 — without it, there's nothing useful to do.
        if ner is not None:
            self._ner: NerExtractor | None = ner
        elif NerExtractor.available():
            self._ner = NerExtractor(model_name=cfg.gliner_model)
        else:
            log.warning("ner_extractor_unavailable_ml_extras_missing")
            self._ner = None

        # Classifier is optional — without it we run NER on everything.
        if classifier is not None:
            self._clf: EventClassifier | None = classifier
        else:
            self._clf = _try_load_classifier(cfg.resolved_classifier_path)

    def can_handle(self, email: Email) -> float:
        if self._ner is None:
            return 0.0
        # Stage 3 applies broadly — the classifier decides whether to spend
        # NER cycles. Return 1.0 so the router passes us, but we may still
        # bail out in extract().
        return 1.0

    def extract(self, email: Email) -> ExtractionResult | None:
        if self._ner is None:
            return None
        t0 = time.perf_counter()

        # Classifier gate (if loaded). This is the cheap filter that keeps
        # NER off newsletters.
        if self._clf is not None and self._clf.is_fitted:
            text = compose_training_text(
                email.sender, email.subject, (email.body_text or "")[:_BODY_PREVIEW_LEN]
            )
            p_event = self._clf.predict_proba_event([text])[0]
            if p_event < self._gate_floor:
                log.debug(
                    "ner_gate_skip",
                    sender=email.sender,
                    subject=email.subject,
                    p_event=p_event,
                )
                return None
        else:
            p_event = None

        body = email.body_text or ""
        if not body.strip():
            return None

        entities = self._ner.extract_entities(body)
        if not entities:
            return None

        assembled = _assemble(entities, email)
        if assembled is None:
            return None

        elapsed = int((time.perf_counter() - t0) * 1000)
        notes = list(assembled.notes)
        if p_event is not None:
            notes.append(f"classifier_p_event={p_event:.3f}")
        return ExtractionResult(
            handled_by_stage=3,
            handled_by_name="ner",
            confidence=assembled.confidence,
            parsed=ParsedEvent(
                title=assembled.title,
                start=assembled.start,
                end=assembled.end,
                location=assembled.location,
            ),
            latency_ms=elapsed,
            notes=notes,
        )


def _try_load_classifier(path: Path) -> EventClassifier | None:
    if not EventClassifier.available():
        log.warning("classifier_unavailable_ml_extras_missing")
        return None
    if not path.exists():
        log.info("classifier_not_trained_yet", path=str(path))
        return None
    clf = EventClassifier(embedder=Embedder())
    try:
        clf.load(path)
    except Exception as e:  # noqa: BLE001 — file may be stale/corrupt; degrade gracefully
        log.warning("classifier_load_failed", path=str(path), error=str(e))
        return None
    return clf


def _assemble(entities: list[Entity], email: Email) -> _Assembled | None:
    """Rule-based composer. Returns None if no rule fires."""
    by_label: dict[str, list[Entity]] = {}
    for e in entities:
        by_label.setdefault(e.label, []).append(e)

    # Flight rule: two IATA codes + at least one date-like entity → flight event.
    iata_entities = by_label.get("iata airport code", [])
    if len(iata_entities) >= 2:
        built = _build_flight(by_label, email)
        if built is not None:
            return built

    # Hotel rule: hotel name + check-in date → stay event.
    if by_label.get("hotel name") and (
        by_label.get("check-in date") or by_label.get("date")
    ):
        built = _build_hotel(by_label, email)
        if built is not None:
            return built

    # Generic rule: event title + date + venue → event.
    if by_label.get("event title") and (by_label.get("date") or by_label.get("time")):
        built = _build_generic(by_label, email)
        if built is not None:
            return built

    return None


def _build_flight(by_label: dict[str, list[Entity]], email: Email) -> _Assembled | None:
    iata = [e.text.upper() for e in by_label.get("iata airport code", [])]
    if len(iata) < 2:
        return None
    origin, dest = iata[0], iata[1]

    start = _first_date(by_label)
    if start is None:
        return None

    flight_number = _extract_flight_number(email)
    title = (
        f"Flight {flight_number}: {origin} \u2192 {dest}"
        if flight_number
        else f"Flight {origin} \u2192 {dest}"
    )

    # Airline flights are well-shaped — most fields we need are usually
    # present, so confidence is moderate-high. Router's min_confidence
    # still gates whether we escalate.
    return _Assembled(
        title=title,
        start=start,
        end=None,
        location=f"{origin} / {dest}",
        confidence=0.75,
        notes=["rule=flight"],
    )


def _build_hotel(by_label: dict[str, list[Entity]], _email: Email) -> _Assembled | None:
    hotels = by_label.get("hotel name", [])
    if not hotels:
        return None
    hotel = hotels[0].text

    check_in = _first_parsed_date(by_label, "check-in date") or _first_date(by_label)
    if check_in is None:
        return None
    check_out = _first_parsed_date(by_label, "check-out date")

    location_parts: list[str] = []
    for label in ("street address", "city"):
        parts = by_label.get(label, [])
        if parts:
            location_parts.append(parts[0].text)
    location = ", ".join(location_parts) or None

    return _Assembled(
        title=f"Stay at {hotel}",
        start=check_in,
        end=check_out,
        location=location,
        confidence=0.7,
        notes=["rule=hotel"],
    )


def _build_generic(by_label: dict[str, list[Entity]], _email: Email) -> _Assembled | None:
    titles = by_label.get("event title", [])
    if not titles:
        return None
    title = titles[0].text

    start = _first_date(by_label)
    if start is None:
        return None

    venue_or_loc = _first_of(by_label, "venue name") or _first_of(by_label, "street address")
    location = venue_or_loc.text if venue_or_loc else None

    return _Assembled(
        title=title,
        start=start,
        end=None,
        location=location,
        confidence=0.6,
        notes=["rule=generic"],
    )


def _first_of(by_label: dict[str, list[Entity]], label: str) -> Entity | None:
    xs = by_label.get(label)
    return xs[0] if xs else None


def _first_parsed_date(by_label: dict[str, list[Entity]], label: str) -> datetime | None:
    for e in by_label.get(label, []):
        dt = _parse_natural_date(e.text)
        if dt is not None:
            return dt
    return None


def _first_date(by_label: dict[str, list[Entity]]) -> datetime | None:
    """Return the first parseable date, falling back to email.received_at
    handling in the caller if nothing works."""
    for label in ("check-in date", "date"):
        for e in by_label.get(label, []):
            dt = _parse_natural_date(e.text)
            if dt is not None:
                return dt
    return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_natural_date(text: str) -> datetime | None:
    """Best-effort date parsing of free-text spans GLiNER produces.

    Deliberately narrow: ISO, `YYYY-MM-DD`, and `Mon D, YYYY`. Anything
    weirder falls through and the event is discarded — preferable to
    guessing wrong and writing a bogus calendar entry.
    """
    text = text.strip()
    if not text:
        return None

    # ISO 8601.
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass

    # "Mon D, YYYY" or "Month D YYYY" etc.
    m = re.match(
        r"(?i)(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})",
        text,
    )
    if m:
        mon = _MONTHS.get(m.group("mon").lower()[:3])
        if mon:
            return datetime(
                int(m.group("y")),
                mon,
                int(m.group("d")),
                0, 0,
                tzinfo=UTC,
            )

    # "YYYY-MM-DD"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)

    return None


def _extract_flight_number(email: Email) -> str | None:
    for source in (email.subject, email.body_text or ""):
        m = _FLIGHT_RE.search(source)
        if m:
            return f"{m.group(1)}{m.group(2)}"
    return None


__all__ = ["NerEventExtractor"]
