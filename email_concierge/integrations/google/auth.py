"""Google OAuth — the one module in the codebase that touches google.oauth2.

Ruff's TID rule forbids importing google-auth libraries outside this package,
mirroring the `imap_tools` → `imap_readonly.py` read-only enforcement pattern.

Scopes are pinned to `.readonly` variants; there is no code path that can
request a write scope. The caller passes a scopes list but `load_credentials`
raises if any scope isn't in the read-only allowlist.

Client secrets can be supplied either as a file path (`client_secrets_path`)
or as an in-memory dict (`client_config`) — the latter is used when the
OAuth JSON lives in an env var rather than on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from email_concierge.config import Settings, settings
from email_concierge.log import get_logger

log = get_logger(__name__)

CALENDAR_READONLY = "https://www.googleapis.com/auth/calendar.readonly"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

_ALLOWED_SCOPES = frozenset({CALENDAR_READONLY, GMAIL_READONLY})


class ScopeNotAllowed(ValueError):
    """Raised when a caller asks for a scope that isn't on the read-only allowlist."""


class ClientSecretsMissing(FileNotFoundError):
    """Raised when neither a secrets path nor an inline config was provided."""


def load_credentials(
    token_path: Path,
    scopes: list[str],
    *,
    client_secrets_path: Path | None = None,
    client_config: dict[str, Any] | None = None,
) -> Credentials:
    """Load cached OAuth creds, refreshing or running the consent flow as needed.

    - Cache hit: parse `token_path`, return if still valid.
    - Expired with refresh_token: refresh in place, rewrite `token_path`.
    - Missing or unrefreshable: run the InstalledAppFlow loopback consent
      flow (opens the user's browser), then persist the token.

    Exactly one of `client_secrets_path` or `client_config` must be
    supplied when a fresh consent flow is needed. If both are provided,
    `client_config` wins (it's the in-memory form; the caller has
    already decided what to use).

    The token file is written with owner-only permissions (0o600) — it's a
    credential, not config.
    """
    bad = [s for s in scopes if s not in _ALLOWED_SCOPES]
    if bad:
        raise ScopeNotAllowed(
            f"Scopes {bad} are not on the read-only allowlist: {sorted(_ALLOWED_SCOPES)}"
        )

    requested = set(scopes)
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    # The cached token might have been granted a narrower set of scopes
    # than we need now (common when a user first consented before Gmail
    # API was enabled in their project). Detect that and force
    # re-consent instead of silently returning permissionless creds.
    granted = set(creds.scopes or []) if creds else set()
    scope_widened = creds is not None and not requested.issubset(granted)

    if creds and creds.valid and not scope_widened:
        log.info("google_auth_loaded", source="cache")
        return creds

    if creds and creds.expired and creds.refresh_token and not scope_widened:
        log.info("google_auth_refreshing")
        creds.refresh(Request())
        _persist_token(token_path, creds)
        return creds

    if scope_widened:
        log.info(
            "google_auth_scope_widened",
            granted=sorted(granted),
            requested=sorted(requested),
        )

    flow = _build_flow(
        scopes=scopes,
        client_secrets_path=client_secrets_path,
        client_config=client_config,
    )
    log.info("google_auth_flow_started", scopes=scopes)
    creds = flow.run_local_server(port=0)
    _persist_token(token_path, creds)
    return creds


def load_credentials_from_settings(
    scopes: list[str],
    cfg: Settings | None = None,
) -> Credentials:
    """Convenience wrapper that reads client-secret source + token path from Settings.

    Prefers the inline JSON env var (`EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON`
    or `GOOGLE_CALENDAR_OAUTH_JSON`) over the on-disk file. This is the
    entry point every command-layer caller should use.
    """
    cfg = cfg or settings()
    inline = cfg.google_client_secrets_json.strip()
    if inline:
        return load_credentials(
            token_path=cfg.google_token_path,
            scopes=scopes,
            client_config=json.loads(inline),
        )
    return load_credentials(
        token_path=cfg.google_token_path,
        scopes=scopes,
        client_secrets_path=cfg.google_client_secrets_path,
    )


def _build_flow(
    *,
    scopes: list[str],
    client_secrets_path: Path | None,
    client_config: dict[str, Any] | None,
) -> InstalledAppFlow:
    if client_config is not None:
        return InstalledAppFlow.from_client_config(client_config, scopes)
    if client_secrets_path is not None:
        if not client_secrets_path.exists():
            raise ClientSecretsMissing(
                f"Google OAuth client secrets not found at {client_secrets_path}. "
                "Either set EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON (or "
                "GOOGLE_CALENDAR_OAUTH_JSON) with the OAuth client JSON inline, "
                "or create a Desktop-app OAuth client in Google Cloud Console, "
                "download the JSON, and place it at this path."
            )
        return InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), scopes)
    raise ClientSecretsMissing(
        "No Google OAuth client secrets available. Provide either "
        "client_secrets_path or client_config."
    )


def _persist_token(token_path: Path, creds: Credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    try:
        os.chmod(token_path, 0o600)
    except (OSError, NotImplementedError):
        # Windows FS doesn't honor POSIX perms the same way; skip silently.
        pass
