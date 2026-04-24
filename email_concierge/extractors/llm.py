from __future__ import annotations

import json
import re
import time
from datetime import datetime
from importlib import resources
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ValidationError, field_validator

from email_concierge.config import settings
from email_concierge.log import get_logger
from email_concierge.models import Email, ExtractionResult, ParsedEvent

log = get_logger(__name__)


MAX_BODY_CHARS = 8000


class _LlmEventSchema(BaseModel):
    is_event: bool
    confidence: float
    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    location: str | None = None
    description: str | None = None
    commitment_evidence: str | None = None

    @field_validator("start", "end")
    @classmethod
    def _require_tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("datetime must be timezone-aware")
        return v


class LlmExtractor:
    """Stage 4: LLM fallback. Applicability is all-or-nothing (disabled flag);
    output quality is gated by min_confidence at the router.
    """

    name = "llm"
    stage = 4
    priority = 0

    def __init__(self, client: OpenAI | None = None) -> None:
        cfg = settings()
        self._disabled = cfg.disable_llm
        self._model = cfg.llm_model
        self._timeout = cfg.llm_timeout_seconds
        if client is not None:
            self._client = client
        elif not self._disabled:
            self._client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        else:
            self._client = None
        self._prompt_template = _load_prompt()

    def can_handle(self, email: Email) -> float:
        return 0.0 if self._disabled else 1.0

    def extract(self, email: Email) -> ExtractionResult | None:
        if self._disabled or self._client is None:
            return None

        t0 = time.perf_counter()
        body = _prepare_body(email)
        prompt = _render_prompt(
            self._prompt_template,
            sender=email.sender,
            subject=email.subject,
            received_at=email.received_at.isoformat(),
            body=body,
        )

        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": "You output only valid JSON. No prose or code fences.",
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                timeout=self._timeout,
            )
        except Exception:
            log.exception("llm_call_failed", model=self._model)
            return None

        latency_ms = int((time.perf_counter() - t0) * 1000)

        content = _response_content(resp)
        if content is None:
            log.warning("llm_empty_response", model=self._model)
            return None

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            log.warning("llm_invalid_json", content_preview=content[:200])
            return None

        try:
            schema = _LlmEventSchema.model_validate(payload)
        except ValidationError as e:
            log.warning("llm_schema_mismatch", errors=e.errors())
            return None

        if not schema.is_event:
            log.debug("llm_not_event", confidence=schema.confidence)
            return None

        if schema.start is None or not schema.title:
            log.debug("llm_missing_required", has_start=schema.start is not None)
            return None

        parsed = ParsedEvent(
            title=schema.title,
            start=schema.start,
            end=schema.end,
            location=schema.location,
            description=schema.description,
            ical_uid=None,
        )
        return ExtractionResult(
            handled_by_stage=self.stage,
            handled_by_name=self.name,
            confidence=float(schema.confidence),
            parsed=parsed,
            latency_ms=latency_ms,
            commitment_evidence=_clean_evidence(schema.commitment_evidence),
        )


def _load_prompt() -> str:
    return (
        resources.files("email_concierge.prompts")
        .joinpath("event_extract.txt")
        .read_text(encoding="utf-8")
    )


def _render_prompt(template: str, **values: str) -> str:
    """Substitute {name} placeholders without invoking str.format.

    The prompt contains a literal JSON schema block with `{` / `}`
    characters that would confuse str.format, so we do plain replaces
    for the four known slot names.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _prepare_body(email: Email) -> str:
    if email.body_text and email.body_text.strip():
        text = email.body_text
    elif email.body_html:
        text = _TAG_RE.sub(" ", email.body_html)
    else:
        text = ""
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS]
    return text


def _response_content(resp: Any) -> str | None:
    try:
        return resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return None


def _clean_evidence(value: str | None) -> str | None:
    """Trim and cap the LLM's quoted commitment snippet.

    Upstream validator just checks presence + length, so we normalize here
    to keep the stored value compact and loggable.
    """
    if not value:
        return None
    s = _WHITESPACE_RE.sub(" ", value).strip()
    if not s:
        return None
    if len(s) > 200:
        s = s[:200]
    return s
