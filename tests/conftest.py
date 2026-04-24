from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from email_concierge import db
from email_concierge.models import Attachment, Email, ExtractionResult, ParsedEvent

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_db(tmp_path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def make_email():
    def _make(
        *,
        message_id: str = "<test@example.com>",
        sender: str = "test@example.com",
        subject: str = "Test Email",
        body_text: str = "Hello",
        body_html: str | None = None,
        attachments: list[Attachment] | None = None,
        received_at: datetime | None = None,
    ) -> Email:
        return Email(
            message_id=message_id,
            sender=sender,
            recipients=["user@example.com"],
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments or [],
            received_at=received_at or datetime.now(tz=UTC),
        )

    return _make


@pytest.fixture
def make_result():
    def _make(
        *,
        stage: int = 1,
        name: str = "stub",
        confidence: float = 1.0,
        title: str = "Test Event",
        start: datetime | None = None,
        ical_uid: str | None = None,
    ) -> ExtractionResult:
        return ExtractionResult(
            handled_by_stage=stage,
            handled_by_name=name,
            confidence=confidence,
            parsed=ParsedEvent(
                title=title,
                start=start or datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                ical_uid=ical_uid,
            ),
            latency_ms=1,
        )

    return _make


class StubExtractor:
    def __init__(
        self,
        name: str,
        stage: int,
        result: ExtractionResult | None,
        applicability: float = 1.0,
        priority: int = 0,
        raise_in_extract: bool = False,
    ):
        self.name = name
        self.stage = stage
        self.priority = priority
        self._result = result
        self._applicability = applicability
        self._raise = raise_in_extract

    def can_handle(self, email: Email) -> float:
        return self._applicability

    def extract(self, email: Email):
        if self._raise:
            raise RuntimeError("boom")
        return self._result


class RecordingSink:
    def __init__(self) -> None:
        self.writes: list[tuple[ExtractionResult, str]] = []

    def write(self, result: ExtractionResult, message_id: str) -> str:
        self.writes.append((result, message_id))
        return result.parsed.ical_uid or f"stub-uid:{message_id}"


@pytest.fixture
def stub_extractor():
    return StubExtractor


@pytest.fixture
def recording_sink():
    return RecordingSink
