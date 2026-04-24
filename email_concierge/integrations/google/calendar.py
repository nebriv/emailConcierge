"""Google Calendar read-only wrapper.

Streams events from a calendar, yielding only those that look like they
were auto-extracted from Gmail (so they can be paired with the source
message for training data).

Read-only: we only call `events().list()`. Never `insert`, `update`,
`delete`, `move`, `patch`. The Google Calendar API client exposes those
methods but this module does not use them; the ruff TID rule enforces
that no other module can import `googleapiclient` to bypass us.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from googleapiclient.discovery import build

from email_concierge.integrations.google.models import GoogleEvent
from email_concierge.log import get_logger

log = get_logger(__name__)


def _parse_event_datetime(node: dict[str, Any] | None) -> datetime | None:
    """Google returns either {dateTime: ...} or {date: YYYY-MM-DD} for all-day."""
    if not node:
        return None
    if "dateTime" in node:
        return datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00"))
    if "date" in node:
        # All-day event — midnight in event's TZ (or naive). Upstream code
        # uses this only for pairing/filtering, not CalDAV writes.
        return datetime.fromisoformat(node["date"])
    return None


def _to_google_event(raw: dict[str, Any]) -> GoogleEvent | None:
    """Coerce a raw events.list item into our GoogleEvent. Returns None if
    required fields are missing (cancelled events, recurring exceptions, etc.)."""
    start = _parse_event_datetime(raw.get("start"))
    if start is None:
        return None
    source = raw.get("source") or {}
    return GoogleEvent(
        event_id=raw["id"],
        summary=raw.get("summary", ""),
        start=start,
        end=_parse_event_datetime(raw.get("end")),
        location=raw.get("location"),
        source_url=source.get("url"),
        source_title=source.get("title"),
        event_type=raw.get("eventType"),
        updated=datetime.fromisoformat(raw["updated"].replace("Z", "+00:00"))
        if "updated" in raw
        else None,
    )


class GoogleCalendarSource:
    """Read-only view over a single Google Calendar."""

    def __init__(self, credentials: Any, calendar_id: str = "primary") -> None:
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._calendar_id = calendar_id

    def list_auto_events(
        self,
        *,
        since: datetime | None = None,
        updated_min: datetime | None = None,
        page_size: int = 250,
    ) -> Iterator[GoogleEvent]:
        """Yield events that look auto-extracted from Gmail.

        Skips manually-created events and recurring instances without a
        Gmail source URL. Paginates transparently via `nextPageToken`.
        """
        events = self._service.events()
        page_token: str | None = None
        seen_pages = 0
        while True:
            request_kwargs: dict[str, Any] = {
                "calendarId": self._calendar_id,
                "maxResults": page_size,
                "singleEvents": True,
                "showDeleted": False,
                # Chronological order so the log reads oldest-first. The
                # default ("unspecified, stable") mixes eras, which makes
                # a --since flag feel like it's skipping old events when
                # really they're on later pages.
                "orderBy": "startTime",
            }
            if since is not None:
                request_kwargs["timeMin"] = since.isoformat()
            if updated_min is not None:
                request_kwargs["updatedMin"] = updated_min.isoformat()
            if page_token:
                request_kwargs["pageToken"] = page_token

            resp = events.list(**request_kwargs).execute()
            seen_pages += 1
            items = resp.get("items", [])
            log.debug("google_calendar_page", n=len(items), page=seen_pages)
            for raw in items:
                ev = _to_google_event(raw)
                if ev is None:
                    continue
                if not ev.is_from_gmail:
                    continue
                log.debug(
                    "google_calendar_raw_event",
                    event_id=ev.event_id,
                    keys=sorted(raw.keys()),
                    source=raw.get("source"),
                    summary=raw.get("summary"),
                    description=raw.get("description"),
                    attendees=raw.get("attendees"),
                    organizer=raw.get("organizer"),
                    creator=raw.get("creator"),
                    extended_properties=raw.get("extendedProperties"),
                    ical_uid=raw.get("iCalUID"),
                )
                yield ev
            page_token = resp.get("nextPageToken")
            if not page_token:
                return
