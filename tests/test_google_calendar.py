"""Tests for Google Calendar source wrapper and GoogleEvent model.

No live API calls — the `googleapiclient.discovery.build` return value
is mocked at the events().list().execute() boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from email_concierge.integrations.google.calendar import GoogleCalendarSource
from email_concierge.integrations.google.models import (
    GoogleEvent,
    _extract_gmail_id,
    _extract_plid,
)


class TestGmailIdExtraction:
    def test_modern_inbox_fragment(self) -> None:
        url = "https://mail.google.com/mail/u/0/#inbox/17f2e3a9b1c4d5e6"
        assert _extract_gmail_id(url) == "17f2e3a9b1c4d5e6"

    def test_legacy_all_fragment(self) -> None:
        url = "https://mail.google.com/mail/#all/FMfcgxwHMvqZpjKLwXTRQvbbHnPkCRrG"
        assert _extract_gmail_id(url) == "FMfcgxwHMvqZpjKLwXTRQvbbHnPkCRrG"

    def test_threaded_form_picks_longest_id(self) -> None:
        url = "https://mail.google.com/mail/u/0/#inbox/thread-id/17f2e3a9b1c4d5e6aaaa"
        assert _extract_gmail_id(url) == "17f2e3a9b1c4d5e6aaaa"

    def test_non_gmail_url_returns_none(self) -> None:
        assert _extract_gmail_id("https://example.com/some/page") is None

    def test_malformed_gmail_url_returns_none(self) -> None:
        assert _extract_gmail_id("https://mail.google.com/mail/u/0/#inbox/") is None

    def test_plid_query_param_not_treated_as_rest_id(self) -> None:
        """plid tokens are web-UI permalinks; the Gmail REST API rejects them.

        _extract_gmail_id deliberately does NOT surface them — the command
        layer routes plids through the separate `plid` property and the
        browser-backed resolver.
        """
        url = (
            "https://mail.google.com/mail?extsrc=cal&"
            "plid=ACUX6DNbghpY3oV27XhjLmK0cCjD2epfA3s_ljQ"
        )
        assert _extract_gmail_id(url) is None

    def test_fragment_wins_over_query(self) -> None:
        """Fragment ID is a real REST ID; prefer it when a fragment is present."""
        url = (
            "https://mail.google.com/mail?plid=ACUX6DNb_not_a_rest_id_XYZ"
            "#inbox/17f2e3a9b1c4d5e6"
        )
        assert _extract_gmail_id(url) == "17f2e3a9b1c4d5e6"


class TestPlidExtraction:
    def test_extracts_plid_query_param(self) -> None:
        url = (
            "https://mail.google.com/mail?extsrc=cal&"
            "plid=ACUX6DNbghpY3oV27XhjLmK0cCjD2epfA3s_ljQ"
        )
        assert _extract_plid(url) == "ACUX6DNbghpY3oV27XhjLmK0cCjD2epfA3s_ljQ"

    def test_no_plid_returns_none(self) -> None:
        url = "https://mail.google.com/mail/u/0/#inbox/17f2e3a9b1c4d5e6"
        assert _extract_plid(url) is None

    def test_non_gmail_returns_none(self) -> None:
        assert _extract_plid("https://example.com/?plid=foo") is None

    def test_event_plid_property(self) -> None:
        ev = GoogleEvent(
            event_id="e",
            summary="Flight",
            start="2026-05-01T10:00:00+00:00",
            source_url="https://mail.google.com/mail?extsrc=cal&plid=TOKENVALUE",
            event_type="fromGmail",
        )
        assert ev.plid == "TOKENVALUE"
        assert ev.gmail_message_id is None


class TestGoogleEventModel:
    def test_is_from_gmail_via_event_type(self) -> None:
        ev = GoogleEvent(
            event_id="abc",
            summary="Flight",
            start="2026-05-01T10:00:00+00:00",
            source_url=None,
            event_type="fromGmail",
        )
        assert ev.is_from_gmail is True
        assert ev.gmail_message_id is None

    def test_is_from_gmail_via_source_url(self) -> None:
        ev = GoogleEvent(
            event_id="abc",
            summary="Flight",
            start="2026-05-01T10:00:00+00:00",
            source_url="https://mail.google.com/mail/u/0/#inbox/17abc123def456ab",
        )
        assert ev.is_from_gmail is True
        assert ev.gmail_message_id == "17abc123def456ab"

    def test_manually_created_event(self) -> None:
        ev = GoogleEvent(
            event_id="abc",
            summary="Coffee with Bob",
            start="2026-05-01T10:00:00+00:00",
        )
        assert ev.is_from_gmail is False
        assert ev.gmail_message_id is None


def _fake_events_resource(pages: list[dict]) -> MagicMock:
    """Build a mock that returns the given pages on successive list().execute() calls."""
    events = MagicMock()
    call_args: list[dict] = []

    def list_side_effect(**kwargs):
        call_args.append(kwargs)
        req = MagicMock()
        page_idx = min(len(call_args) - 1, len(pages) - 1)
        req.execute.return_value = pages[page_idx]
        return req

    events.list.side_effect = list_side_effect
    events._call_args = call_args
    return events


def _fake_service(events_resource: MagicMock) -> MagicMock:
    service = MagicMock()
    service.events.return_value = events_resource
    return service


def test_list_auto_events_filters_non_gmail(tmp_path) -> None:
    pages = [
        {
            "items": [
                {
                    "id": "evt-from-gmail",
                    "summary": "Flight UA123",
                    "start": {"dateTime": "2026-05-01T08:00:00Z"},
                    "end": {"dateTime": "2026-05-01T11:00:00Z"},
                    "source": {
                        "url": "https://mail.google.com/mail/u/0/#inbox/17abc123def456ab",
                        "title": "Your flight",
                    },
                    "eventType": "fromGmail",
                    "updated": "2026-04-20T12:00:00.000Z",
                },
                {
                    "id": "evt-manual",
                    "summary": "Coffee with Bob",
                    "start": {"dateTime": "2026-05-02T09:00:00Z"},
                    "end": {"dateTime": "2026-05-02T10:00:00Z"},
                    "updated": "2026-04-20T12:00:00.000Z",
                },
                {
                    "id": "evt-bad-source",
                    "summary": "Some event",
                    "start": {"dateTime": "2026-05-03T09:00:00Z"},
                    "source": {"url": "https://example.com/foo", "title": "Other"},
                    "updated": "2026-04-20T12:00:00.000Z",
                },
            ],
            # no nextPageToken
        }
    ]
    events = _fake_events_resource(pages)
    with patch(
        "email_concierge.integrations.google.calendar.build",
        return_value=_fake_service(events),
    ):
        src = GoogleCalendarSource(credentials=MagicMock(), calendar_id="primary")
        results = list(src.list_auto_events())

    assert [e.event_id for e in results] == ["evt-from-gmail"]
    assert results[0].gmail_message_id == "17abc123def456ab"


def test_list_auto_events_paginates(tmp_path) -> None:
    pages = [
        {
            "items": [
                {
                    "id": "e1",
                    "summary": "Flight",
                    "start": {"dateTime": "2026-05-01T08:00:00Z"},
                    "source": {"url": "https://mail.google.com/mail/u/0/#inbox/aaaaaaaaaaaaaaaa"},
                    "eventType": "fromGmail",
                }
            ],
            "nextPageToken": "tok2",
        },
        {
            "items": [
                {
                    "id": "e2",
                    "summary": "Hotel",
                    "start": {"dateTime": "2026-05-05T14:00:00Z"},
                    "source": {"url": "https://mail.google.com/mail/u/0/#inbox/bbbbbbbbbbbbbbbb"},
                    "eventType": "fromGmail",
                }
            ],
        },
    ]
    events = _fake_events_resource(pages)
    with patch(
        "email_concierge.integrations.google.calendar.build",
        return_value=_fake_service(events),
    ):
        src = GoogleCalendarSource(credentials=MagicMock(), calendar_id="primary")
        results = list(src.list_auto_events(page_size=1))

    assert [e.event_id for e in results] == ["e1", "e2"]
    assert events.list.call_count == 2
    # Second call should have picked up the page token.
    assert events._call_args[1].get("pageToken") == "tok2"


def test_list_auto_events_skips_items_without_start(tmp_path) -> None:
    pages = [
        {
            "items": [
                {"id": "no-start", "summary": "weird"},
                {
                    "id": "ok",
                    "summary": "Flight",
                    "start": {"dateTime": "2026-05-01T08:00:00Z"},
                    "source": {"url": "https://mail.google.com/mail/u/0/#inbox/ccccccccccccccccc"},
                    "eventType": "fromGmail",
                },
            ]
        }
    ]
    events = _fake_events_resource(pages)
    with patch(
        "email_concierge.integrations.google.calendar.build",
        return_value=_fake_service(events),
    ):
        src = GoogleCalendarSource(credentials=MagicMock(), calendar_id="primary")
        results = list(src.list_auto_events())

    assert [e.event_id for e in results] == ["ok"]


def test_all_day_event_parsed(tmp_path) -> None:
    pages = [
        {
            "items": [
                {
                    "id": "allday",
                    "summary": "Snowshoe Lodge",
                    "start": {"date": "2026-05-01"},
                    "end": {"date": "2026-05-04"},
                    "source": {"url": "https://mail.google.com/mail/u/0/#inbox/ddddddddddddddd1"},
                    "eventType": "fromGmail",
                }
            ]
        }
    ]
    events = _fake_events_resource(pages)
    with patch(
        "email_concierge.integrations.google.calendar.build",
        return_value=_fake_service(events),
    ):
        src = GoogleCalendarSource(credentials=MagicMock(), calendar_id="primary")
        results = list(src.list_auto_events())

    assert len(results) == 1
    assert results[0].start.date().isoformat() == "2026-05-01"


@pytest.mark.parametrize(
    "since_kw, expected_key",
    [
        ({"since": None, "updated_min": None}, None),
    ],
)
def test_list_auto_events_passes_no_filters_when_none(since_kw, expected_key) -> None:
    pages = [{"items": []}]
    events = _fake_events_resource(pages)
    with patch(
        "email_concierge.integrations.google.calendar.build",
        return_value=_fake_service(events),
    ):
        src = GoogleCalendarSource(credentials=MagicMock(), calendar_id="primary")
        list(src.list_auto_events(**since_kw))

    call = events._call_args[0]
    assert "timeMin" not in call
    assert "updatedMin" not in call
