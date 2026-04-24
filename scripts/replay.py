"""Replay a single .eml file through the pipeline.

This is the primary dev loop for iterating on plugins and the LLM prompt.
It does NOT touch IMAP.

Usage:
    python scripts/replay.py tests/fixtures/eml/sample.eml [--no-dry-run]

By default runs in dry-run mode: CalDAV is not touched, SQLite writes go
to a throwaway in-memory DB.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

# Make the package importable when running from the repo root without install.
sys.path.insert(0, str(Path(__file__).parent.parent))

from email_concierge import db  # noqa: E402
from email_concierge import log as logmod
from email_concierge.extractors.discovery import discover_plugins  # noqa: E402
from email_concierge.extractors.ics import IcsExtractor  # noqa: E402
from email_concierge.extractors.llm import LlmExtractor  # noqa: E402
from email_concierge.models import Attachment, Email  # noqa: E402
from email_concierge.router import route  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay an .eml through the pipeline.")
    parser.add_argument("eml_path", type=Path)
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually write to CalDAV. Default is dry-run (no-op sink).",
    )
    parser.add_argument("--disable-llm", action="store_true")
    args = parser.parse_args()

    logmod.configure(level="INFO", json_output=False)

    email = _load_eml(args.eml_path)
    print(
        f"[replay] loaded: from={email.sender!r} subject={email.subject!r} "
        f"attachments={len(email.attachments)}",
        file=sys.stderr,
    )

    plugins = discover_plugins()
    print(f"[replay] plugins: {[p.name for p in plugins]}", file=sys.stderr)
    extractors = [IcsExtractor(), *plugins]
    if not args.disable_llm:
        try:
            extractors.append(LlmExtractor())
        except Exception as e:  # pragma: no cover - happens if LLM not reachable
            print(f"[replay] skipping LLM extractor: {e}", file=sys.stderr)

    result = route(email, extractors)
    if result is None:
        print(json.dumps({"result": None}, indent=2))
        return 0

    payload = {
        "handled_by_stage": result.handled_by_stage,
        "handled_by_name": result.handled_by_name,
        "confidence": result.confidence,
        "latency_ms": result.latency_ms,
        "parsed": result.parsed.model_dump(mode="json"),
    }
    print(json.dumps(payload, indent=2, default=str))

    if args.no_dry_run:
        # Write via real sink with a throwaway in-memory DB.
        from email_concierge.sinks.caldav_sink import CaldavSink

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_schema(conn)
        sink = CaldavSink(conn)
        sink.write(result, email)
        print("[replay] written to CalDAV", file=sys.stderr)
    return 0


def _load_eml(path: Path) -> Email:
    with path.open("rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)

    message_id = (msg["Message-ID"] or f"<replay-{path.name}@local>").strip().strip("<>")
    sender = str(msg["From"] or "")
    recipients = [str(a) for a in (msg.get_all("To") or [])]
    subject = str(msg["Subject"] or "")
    date = msg["Date"]
    try:
        received_at = date.datetime if date is not None else datetime.now(tz=UTC)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)
    except Exception:
        received_at = datetime.now(tz=UTC)

    body_text = ""
    body_html: str | None = None
    attachments: list[Attachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            is_attachment = (
                "attachment" in disposition
                or ctype.startswith(("image/", "application/"))
                or ctype == "text/calendar"
            )
            if is_attachment:
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    Attachment(
                        filename=part.get_filename() or "attachment",
                        content_type=ctype,
                        payload=payload,
                    )
                )
            elif ctype == "text/plain" and not body_text:
                body_text = part.get_content()
            elif ctype == "text/html" and body_html is None:
                body_html = part.get_content()
    else:
        body_text = msg.get_content() if msg.get_content_type() == "text/plain" else ""
        if msg.get_content_type() == "text/html":
            body_html = msg.get_content()

    return Email(
        message_id=message_id,
        sender=sender,
        recipients=recipients,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        received_at=received_at,
    )


if __name__ == "__main__":
    raise SystemExit(main())
