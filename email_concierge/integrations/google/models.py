"""Pydantic shapes for Google Calendar / Gmail data we care about.

Only the fields needed for training-data pairing are modeled. Everything
else from the Google API response is ignored (`extra="ignore"`).
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict

# Gmail message URLs take several forms. We've seen:
#   https://mail.google.com/mail/u/0/#inbox/17f2e3a9b1c4d5e6
#   https://mail.google.com/mail/u/0/#all/FMfcgxwHMvqZpjKLwXTRQvbbHnPkCRrG
#   https://mail.google.com/mail/#inbox/17f2e3a9b1c4d5e6
#   https://mail.google.com/mail/u/0/?tab=rm#search/subject/FMfcg.../17f...
#   https://mail.google.com/mail?extsrc=cal&plid=ACUX6DNbghpY3oV27XhjLmK0cCjD2epfA3s_ljQ
#
# The first few embed the Gmail internal ID (16-hex or base62) in the
# fragment; the Calendar `fromGmail` form stuffs a web-UI `plid`
# permalink token into the query string. `plid` is NOT directly usable
# as a Gmail REST API message ID — the command layer treats it as a
# signal that heuristic search is needed, then re-keys by internal ID.
_GMAIL_ID_RE = re.compile(r"[0-9A-Za-z_-]{12,}")
# Query-string keys that carry REST-addressable IDs. Note: `plid` is
# deliberately absent — plid is a server-signed web-UI permalink token
# that Gmail's REST API rejects with 400. It's surfaced via the separate
# `plid` property so the command layer can route it through the
# browser-backed plid_resolver instead.
_ID_QUERY_KEYS = ("th", "msgid", "ik")


def _extract_gmail_id(url: str) -> str | None:
    """Return the best-effort Gmail message handle from a mail.google.com URL.

    Checked in order: fragment path segments (modern web UI), path
    segments (older legacy form), then query params. Returns None if
    nothing matches. Does NOT read the `plid` query param — callers
    that want the plid use `GoogleEvent.plid`.
    """
    if "mail.google.com" not in url:
        return None
    parsed = urlparse(url)
    if parsed.fragment:
        candidates = [
            seg for seg in parsed.fragment.split("/") if _GMAIL_ID_RE.fullmatch(seg)
        ]
        if candidates:
            return max(candidates, key=len)
    if parsed.path:
        candidates = [
            seg for seg in parsed.path.split("/") if _GMAIL_ID_RE.fullmatch(seg)
        ]
        if candidates:
            return max(candidates, key=len)
    qs = parse_qs(parsed.query)
    for key in _ID_QUERY_KEYS:
        vals = qs.get(key)
        if vals and _GMAIL_ID_RE.fullmatch(vals[0]):
            return vals[0]
    return None


def _extract_plid(url: str) -> str | None:
    """Return the `plid` query-param token, if present. Otherwise None."""
    if "mail.google.com" not in url:
        return None
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    vals = qs.get("plid")
    if vals:
        return vals[0]
    return None


class GoogleEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str
    summary: str
    start: datetime
    end: datetime | None = None
    location: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    event_type: str | None = None
    updated: datetime | None = None

    @property
    def gmail_message_id(self) -> str | None:
        if not self.source_url:
            return None
        return _extract_gmail_id(self.source_url)

    @property
    def plid(self) -> str | None:
        """Google Calendar web-UI permalink token, if present in source_url.

        Present on events whose `source.url` is of the form
        `mail.google.com/mail?extsrc=cal&plid=<token>`. Must be resolved
        to a Gmail thread ID via a browser session — the Gmail REST API
        rejects plids directly.
        """
        if not self.source_url:
            return None
        return _extract_plid(self.source_url)

    @property
    def is_from_gmail(self) -> bool:
        """True if Google flagged this as auto-extracted from a Gmail message."""
        if self.event_type == "fromGmail":
            return True
        return bool(self.source_url and "mail.google.com" in self.source_url)
