"""Tests for the Gmail read-only fetcher.

No live API calls — the `googleapiclient.discovery.build` return value
is mocked at the users().messages().get().execute() boundary. Gmail
API payloads are rich MIME trees; these fixtures exercise the shapes
the parser needs to handle.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError

from email_concierge.integrations.google.gmail import GmailSource


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _fake_service(
    message_payload: dict | None,
    *,
    raise_http: HttpError | None = None,
    attachments: dict[str, bytes] | None = None,
):
    """Mock Gmail API. `attachments` maps attachmentId -> raw bytes."""
    service = MagicMock()
    messages_resource = MagicMock()

    get_req = MagicMock()
    if raise_http is not None:
        get_req.execute.side_effect = raise_http
    else:
        get_req.execute.return_value = message_payload
    messages_resource.get.return_value = get_req

    attachments_resource = MagicMock()

    def _att_get(**kw) -> MagicMock:
        att_id = kw.get("id")
        req = MagicMock()
        raw = (attachments or {}).get(att_id, b"")
        req.execute.return_value = {
            "data": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
            "size": len(raw),
        }
        return req

    attachments_resource.get.side_effect = _att_get
    messages_resource.attachments.return_value = attachments_resource

    service.users.return_value.messages.return_value = messages_resource
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


def test_nested_multipart_with_attachment_fetches_bytes() -> None:
    pdf_bytes = b"%PDF-1.4 fake pdf content"
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
                    "body": {"attachmentId": "att-1", "size": len(pdf_bytes)},
                },
            ],
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload, attachments={"att-1": pdf_bytes}),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc")

    assert email is not None
    assert email.body_text == "Ticket body"
    assert email.body_html == "<p>html</p>"
    assert len(email.attachments) == 1
    assert email.attachments[0].filename == "ticket.pdf"
    assert email.attachments[0].content_type == "application/pdf"
    assert email.attachments[0].payload == pdf_bytes


def test_attachment_fetch_disabled_leaves_empty_payload() -> None:
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "x@y.com"},
                {"name": "Subject", "value": "s"},
                {"name": "Message-ID", "value": "<m@y.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("hi")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "t.pdf",
                    "body": {"attachmentId": "att-1", "size": 10},
                },
            ],
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload, attachments={"att-1": b"ignored"}),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc", fetch_attachments=False)

    assert email is not None
    assert len(email.attachments) == 1
    assert email.attachments[0].payload == b""


def test_attachment_oversized_is_skipped() -> None:
    """Large attachments retain metadata but not bytes (memory guardrail)."""
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "x@y.com"},
                {"name": "Subject", "value": "s"},
                {"name": "Message-ID", "value": "<m@y.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("hi")}},
                {
                    "mimeType": "application/octet-stream",
                    "filename": "huge.bin",
                    "body": {"attachmentId": "att-1", "size": 100 * 1024 * 1024},
                },
            ],
        },
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(payload, attachments={"att-1": b"doesnt matter"}),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc")

    assert email is not None
    assert len(email.attachments) == 1
    assert email.attachments[0].filename == "huge.bin"
    assert email.attachments[0].payload == b""


def test_inline_attachment_bytes_not_refetched() -> None:
    """Small attachments come inline in body.data; we shouldn't double-fetch."""
    inline_bytes = b"tiny"
    payload = {
        "internalDate": "1762000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "x@y.com"},
                {"name": "Subject", "value": "s"},
                {"name": "Message-ID", "value": "<m@y.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64url("hi")}},
                {
                    "mimeType": "text/calendar",
                    "filename": "event.ics",
                    "body": {"data": _b64url(inline_bytes.decode()), "size": len(inline_bytes)},
                },
            ],
        },
    }
    service = _fake_service(payload)
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=service,
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("abc")

    assert email is not None
    assert email.attachments[0].payload == inline_bytes
    # attachments().get() should NOT have been called — inline bytes were enough.
    service.users.return_value.messages.return_value.attachments.return_value.get.assert_not_called()


def test_body_not_truncated() -> None:
    """Full body is stored so downstream training has the complete message.

    The Email.body_text column holds the full plain-text body (not a preview);
    callers that want a smaller cap should truncate themselves.
    """
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
    assert len(email.body_text) == 3000


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


def test_400_returns_none() -> None:
    """Gmail REST rejects web-UI plid tokens with a 400 — treat as missing."""
    resp = MagicMock()
    resp.status = 400
    resp.reason = "Bad Request"
    http_err = HttpError(resp=resp, content=b'{"error": "invalid id"}')

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_message("ACUX6DNb_not_a_real_gmail_id")

    assert email is None


def _fake_service_with_search(
    *,
    search_ids: list[str],
    metadata_by_id: dict[str, dict],
) -> MagicMock:
    """Mock for find_best_message: users.messages.list + users.messages.get(metadata)."""
    service = MagicMock()
    messages_resource = MagicMock()

    list_req = MagicMock()
    list_req.execute.return_value = {
        "messages": [{"id": mid} for mid in search_ids]
    }
    messages_resource.list.return_value = list_req

    def get_side_effect(**kwargs):
        mid = kwargs.get("id")
        req = MagicMock()
        req.execute.return_value = metadata_by_id.get(mid, {})
        return req

    messages_resource.get.side_effect = get_side_effect
    service.users.return_value.messages.return_value = messages_resource
    return service


def test_find_best_message_picks_closest_internal_date() -> None:
    around = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    search_ids = ["msg-far", "msg-close", "msg-medium"]

    def _ms(dt: datetime) -> str:
        return str(int(dt.timestamp() * 1000))

    metadata = {
        "msg-far": {"internalDate": _ms(datetime(2026, 4, 15, tzinfo=UTC))},
        "msg-close": {"internalDate": _ms(datetime(2026, 4, 20, 13, tzinfo=UTC))},
        "msg-medium": {"internalDate": _ms(datetime(2026, 4, 18, tzinfo=UTC))},
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_search(
            search_ids=search_ids, metadata_by_id=metadata
        ),
    ):
        src = GmailSource(credentials=MagicMock())
        best = src.find_best_message(summary="Snowshoe Lodge", around=around)

    assert best == "msg-close"


def test_find_best_message_empty_result() -> None:
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_search(search_ids=[], metadata_by_id={}),
    ):
        src = GmailSource(credentials=MagicMock())
        best = src.find_best_message(
            summary="does not match anything",
            around=datetime(2026, 4, 20, tzinfo=UTC),
        )

    assert best is None


def test_find_best_message_empty_summary_returns_none() -> None:
    """No point searching without a query term."""
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=MagicMock(),
    ):
        src = GmailSource(credentials=MagicMock())
        best = src.find_best_message(
            summary="   ", around=datetime(2026, 4, 20, tzinfo=UTC)
        )

    assert best is None


def test_find_best_message_query_includes_date_range() -> None:
    """Date range should bracket `around` so we don't scan the whole inbox."""
    around = datetime(2026, 4, 20, 12, 0, tzinfo=UTC)
    service = _fake_service_with_search(search_ids=[], metadata_by_id={})
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=service,
    ):
        src = GmailSource(credentials=MagicMock())
        src.find_best_message(summary="Flight UA123", around=around, window_hours=48)

    list_call = service.users.return_value.messages.return_value.list.call_args
    q = list_call.kwargs["q"]
    assert '"Flight UA123"' in q
    assert "after:2026/04/18" in q
    assert "before:" in q


def _fake_service_with_threads(
    thread_payload: dict | None,
    *,
    raise_http: HttpError | None = None,
) -> MagicMock:
    """Mock Gmail API at users().threads().get().execute()."""
    service = MagicMock()
    threads_resource = MagicMock()

    get_req = MagicMock()
    if raise_http is not None:
        get_req.execute.side_effect = raise_http
    else:
        get_req.execute.return_value = thread_payload
    threads_resource.get.return_value = get_req

    service.users.return_value.threads.return_value = threads_resource
    # Also wire a minimal messages resource so attachment fetches (if any) don't NPE.
    service.users.return_value.messages.return_value = MagicMock()
    return service


def test_fetch_first_in_thread_returns_oldest_message() -> None:
    thread_payload = {
        "id": "1868052c9b0dfe8b",
        "messages": [
            {
                "id": "1868052c9b0dfe8b",
                "internalDate": "1762000000000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "bookings@united.com"},
                        {"name": "Subject", "value": "Your reservation is confirmed"},
                        {"name": "Message-ID", "value": "<first@united.com>"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Trip confirmation body")},
                },
            },
            {
                "id": "18680543ca1c000b",
                "internalDate": "1762100000000",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "bookings@united.com"},
                        {"name": "Subject", "value": "Re: Your reservation (update)"},
                        {"name": "Message-ID", "value": "<followup@united.com>"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Update body")},
                },
            },
        ],
    }
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_threads(thread_payload),
    ):
        src = GmailSource(credentials=MagicMock())
        email = src.fetch_first_in_thread("1868052c9b0dfe8b")

    assert email is not None
    assert email.message_id == "<first@united.com>"
    assert email.subject == "Your reservation is confirmed"
    assert "Trip confirmation body" in email.body_text


def test_fetch_first_in_thread_empty_messages_returns_none() -> None:
    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_threads({"id": "t", "messages": []}),
    ):
        src = GmailSource(credentials=MagicMock())
        assert src.fetch_first_in_thread("t") is None


def test_fetch_first_in_thread_404_returns_none() -> None:
    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"
    http_err = HttpError(resp=resp, content=b'{"error": "not found"}')

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_threads(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        assert src.fetch_first_in_thread("missing") is None


def test_fetch_first_in_thread_400_returns_none() -> None:
    """400 on a thread ID happens if the hex we hand over isn't actually a thread."""
    resp = MagicMock()
    resp.status = 400
    resp.reason = "Bad Request"
    http_err = HttpError(resp=resp, content=b'{"error": "invalid id"}')

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_threads(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        assert src.fetch_first_in_thread("bogus") is None


def test_fetch_first_in_thread_other_http_error_propagates() -> None:
    resp = MagicMock()
    resp.status = 500
    resp.reason = "Server Error"
    http_err = HttpError(resp=resp, content=b"boom")

    with patch(
        "email_concierge.integrations.google.gmail.build",
        return_value=_fake_service_with_threads(None, raise_http=http_err),
    ):
        src = GmailSource(credentials=MagicMock())
        with pytest.raises(HttpError):
            src.fetch_first_in_thread("t")


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
