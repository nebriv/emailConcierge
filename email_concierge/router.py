from __future__ import annotations

import time
from collections.abc import Iterable

from email_concierge.config import settings
from email_concierge.extractors.base import Extractor
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult

log = get_logger(__name__)


def route(
    email: Email,
    extractors: Iterable[Extractor],
    *,
    can_handle_floor: float | None = None,
    min_confidence: float | None = None,
) -> ExtractionResult | None:
    """Try each extractor in stage/priority order; return the first result
    with confidence at or above min_confidence.

    Logs every attempt (name, duration_ms, confidence, outcome) so operators
    can see stage attribution.
    """
    cfg = settings()
    handle_floor = cfg.can_handle_floor if can_handle_floor is None else can_handle_floor
    conf_floor = cfg.min_confidence if min_confidence is None else min_confidence

    ordered = sorted(extractors, key=lambda e: (e.stage, getattr(e, "priority", 0)))

    for extractor in ordered:
        t0 = time.perf_counter()
        try:
            applicability = extractor.can_handle(email)
        except Exception:
            log.exception(
                "can_handle_failed",
                extractor=extractor.name,
                stage=extractor.stage,
            )
            continue

        if applicability < handle_floor:
            log.debug(
                "extractor_skipped",
                extractor=extractor.name,
                stage=extractor.stage,
                applicability=applicability,
                floor=handle_floor,
            )
            continue

        try:
            result = extractor.extract(email)
        except Exception:
            log.exception(
                "extractor_failed",
                extractor=extractor.name,
                stage=extractor.stage,
                message_id=email.message_id,
            )
            continue

        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if result is None:
            log.debug(
                "extractor_none",
                extractor=extractor.name,
                stage=extractor.stage,
                elapsed_ms=elapsed_ms,
            )
            continue

        if result.confidence < conf_floor:
            log.debug(
                "extractor_below_confidence",
                extractor=extractor.name,
                stage=extractor.stage,
                confidence=result.confidence,
                floor=conf_floor,
                elapsed_ms=elapsed_ms,
            )
            continue

        log.info(
            "extractor_accepted",
            extractor=extractor.name,
            stage=extractor.stage,
            confidence=result.confidence,
            elapsed_ms=elapsed_ms,
            message_id=email.message_id,
        )
        return result

    return None
