"""Pydantic shapes for Google Calendar / Gmail data we care about.

Only the fields needed for training-data pairing are modeled. Everything
else from the Google API response is ignored (`extra="ignore"`).
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict

# Gmail message URLs take several forms over the years. The ID we want
# is the last hex-like segment in the URL fragment or path. Examples:
#   https://mail.google.com/mail/u/0/#inbox/17f2e3a9b1c4d5e6
#   https://mail.google.com/mail/u/0/#all/FMfcgxwHMvqZpjKLwXTRQvbbHnPkCRrG
#   https://mail.google.com/mail/#inbox/17f2e3a9b1c4d5e6
#   https://mail.google.com/mail/u/0/?tab=rm#search/subject/FMfcg.../17f...
_GMAIL_ID_RE = re.compile(r"[0-9A-Za-z]{12,}")


def _extract_gmail_id(url: str) -> str | None:
    """Return the Gmail internal message ID embedded in a mail.google.com URL.

    We split on '/' and scan segments for the longest alphanumeric blob —
    Gmail IDs are 16-hex or ~24-char base62. If nothing matches we return
    None so the caller can skip the pairing.
    """
    if "mail.google.com" not in url:
        return None
    # Take the fragment (after #) if present; otherwise the path tail.
    tail = url.split("#", 1)[1] if "#" in url else url.split("?", 1)[0]
    candidates = [seg for seg in tail.split("/") if _GMAIL_ID_RE.fullmatch(seg)]
    if not candidates:
        return None
    return max(candidates, key=len)


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
    def is_from_gmail(self) -> bool:
        """True if Google flagged this as auto-extracted from a Gmail message."""
        if self.event_type == "fromGmail":
            return True
        return bool(self.source_url and "mail.google.com" in self.source_url)
