from __future__ import annotations

from typing import Protocol, runtime_checkable

from email_concierge.models import Email, ExtractionResult

__all__ = ["Extractor", "ExtractionResult"]


@runtime_checkable
class Extractor(Protocol):
    """The contract every stage implements.

    Router orders extractors by (stage, priority) and picks the first whose
    can_handle() >= can_handle_floor AND whose extract() returns a result
    with confidence >= min_confidence.
    """

    name: str
    stage: int

    def can_handle(self, email: Email) -> float:
        """Return 0.0-1.0 confidence this extractor applies to this email.

        Must be cheap (< 5 ms). The router skips extractors below
        can_handle_floor.
        """
        ...

    def extract(self, email: Email) -> ExtractionResult | None:
        """Perform extraction. Return None if extraction fails or the
        extractor changes its mind after deeper inspection.
        """
        ...
