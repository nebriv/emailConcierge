"""Tests for the Gmail read-only fetcher.

No live API calls — the `googleapiclient.discovery.build` return value
is mocked at the users().messages().get().execute() boundary. Gmail
API payloads are rich MIME trees; these fixtures exercise the shapes
the parser needs to handle.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from email_concierge.integrations.google.gmail import GmailSource


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _fake_service(message_payload: dict | None, *, raise_http: HttpError | None = None):
    service = MagicMock()
    get_req = MagicMock()
    if raise_http is not None:
        get_req.execute.side_effect = raise_http
    else:
        get_req.execute.return_value = message_payload
    service.users.return_value.messages.return_value.get.return_value = get_req
    return service


def test_plain_text_only() -> None:
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "United <receipts@united.com>"},
                {"name": "To", "value": "user@example.com"},
                {"name": "Subject", "value": "Your flight UA123"},
                {"name": "Message-ID", "value": "<abc@mail.united.com>"},
                {"name": "Date", "value": "Fri, 01 May 2026 08:00:00 +0000"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url("Flight confirmation: UA123 SFO -> JFK")},
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("17abc123def456")

    assert email is not None
    assert email.message_id == "<abc@mail.united.com>"
    assert email.sender == "United <receipts@united.com>"
    assert email.recipients == ["user@example.com"]
    assert email.subject == "Your flight UA123"
    assert "UA123 SFO" in email.body_text
    assert email.body_html is None
    assert email.attachments == []


def test_multipart_text_and_html() -> None:
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "hotel@marriott.com"},
                {"name": "To", "value": "user@example.com"},
                {"name": "Subject", "value": "Reservation confirmed"},
                {"name": "Message-ID", "value": "<xyz@marriott.com>"},
                {"name": "Date", "value": "Fri, 01 May 2026 08:00:00 +0000"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Your stay at Marriott.")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>Your stay at <b>Marriott</b>.</p>")},
                },
            ],
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc")

    assert email is not None
    assert email.body_text == "Your stay at Marriott."
    assert email.body_html is not None
    assert "<b>Marriott</b>" in email.body_html


def test_nested_multipart_with_attachment() -> None:
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "eventbrite@eventbrite.com"},
                {"name": "Subject", "value": "Your ticket"},
                {"name": "Message-ID", "value": "<t@eb.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64url("Ticket body")}},
                        {"mimeType": "text/html", "body": {"data": _b64url("<p>html</p>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "ticket.pdf",
                    "body": {"attachmentId": "att-1", "size": 12345},
                },
            ],
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc")

    assert email is not None
    assert email.body_text == "Ticket body"
    assert email.body_html == "<p>html</p>"
    assert len(email.attachments) == 1
    assert email.attachments[0].filename == "ticket.pdf"
    assert email.attachments[0].content_type == "application/pdf"
    # We don't fetch attachment bytes — empty payload is the contract.
    assert email.attachments[0].payload == b""


def test_body_preview_truncated_at_2000_chars() -> None:
    big = "A" * 3000
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "foo@bar.com"},
                {"name": "Subject", "value": "big"},
                {"name": "Message-ID", "value": "<big@bar.com>"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url(big)},
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc")

    assert email is not None
    assert len(email.body_text) == 2000


def test_404_returns_none() -> None:
    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"
    http_err = HttpError(resp=resp, content=b'{"error": "not found"}')

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("missing")

    assert email is None


def test_410_returns_none() -> None:
    """Gone — Gmail sometimes serves this for long-deleted messages."""
    resp = MagicMock()
    resp.status = 410
    resp.reason = "Gone"
    http_err = HttpError(resp=resp, content=b'{"error": "gone"}')

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("missing")

    assert email is None


def test_other_http_error_propagates() -> None:
    resp = MagicMock()
    resp.status = 500
    resp.reason = "Server Error"
    http_err = HttpError(resp=resp, content=b'{"error": "boom"}')

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        with pytest.raises(HttpError):
            src.fetch_message("any")


def test_missing_message_id_header_falls_back_to_internal_id() -> None:
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "x@y.com"},
                {"name": "Subject", "value": "s"},
                # No Message-ID header at all
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url("hi")},
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("internal-42")

    assert email is not None
    assert email.message_id == "gmail-internal-internal-42"


def test_received_at_from_internal_date_when_date_header_missing() -> None:
    payload = {
        "internalDate": "1704067200000",  # 2024-01-01T00:00:00Z
        "payload": {
            "headers": [
                {"name": "From", "value": "x@y.com"},
                {"name": "Subject", "value": "s"},
                {"name": "Message-ID", "value": "<m@y.com>"},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url("hi")},
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("internal-42")

    assert email is not None
    assert email.received_at.year == 2024
    assert email.received_at.month == 1
    assert email.received_at.day == 1
