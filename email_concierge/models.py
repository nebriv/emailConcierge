from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Attachment(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    filename: str
    content_type: str
    payload: bytes


class Email(BaseModel):
    message_id: str
    sender: str
    recipients: list[str] = Field(default_factory=list)
    subject: str
    body_text: str = ""
    body_html: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    received_at: datetime

    @field_validator("received_at")
    @classmethod
    def _require_tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        return v


class ParsedEvent(BaseModel):
    title: str
    start: datetime
    end: datetime | None = None
    location: str | None = None
    description: str | None = None
    ical_uid: str | None = None

    @field_validator("start")
    @classmethod
    def _start_tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        return v

    @field_validator("end")
    @classmethod
    def _end_tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("end must be timezone-aware")
        return v


class ExtractionResult(BaseModel):
    handled_by_stage: int
    handled_by_name: str
    confidence: float
    parsed: ParsedEvent
    latency_ms: int = 0
    notes: list[str] = Field(default_factory=list)
    # Verbatim snippet from the email proving personal commitment — an order/
    # confirmation/reservation number, or a direct greeting + confirming verb.
    # The pipeline validator rejects Stage 3/4 results without this. Stages
    # 1 (.ics) and 2 (plugins) leave it None; their commitment proof is
    # structural (a real calendar attachment, a matched vendor template).
    commitment_evidence: str | None = None

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "handled_by_stage": self.handled_by_stage,
            "handled_by_name": self.handled_by_name,
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "title": self.parsed.title,
            "start": self.parsed.start.isoformat(),
            "ical_uid": self.parsed.ical_uid,
        }
