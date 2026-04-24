"""Tests for email_concierge.integrations.google.auth.

All external OAuth flow calls are mocked; no live consent and no
network. We verify: scope allowlist, cache hit, refresh-on-expired,
fresh flow when token missing, secrets-source selection (path vs
inline JSON), Settings wrapper prefers inline JSON.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from email_concierge.config import Settings
from email_concierge.integrations.google.auth import (
    CALENDAR_READONLY,
    GMAIL_READONLY,
    ClientSecretsMissing,
    ScopeNotAllowed,
    load_credentials,
    load_credentials_from_settings,
)

ALL_SCOPES = [CALENDAR_READONLY, GMAIL_READONLY]

FAKE_CLIENT_CONFIG = {
    "installed": {
        "client_id": "fake-client.apps.googleusercontent.com",
        "client_secret": "fake-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def _fake_token_json(scopes: list[str], expired: bool = False) -> str:
    return json.dumps(
        {
            "token": "expired-or-not",
            "refresh_token": "refresh-token-abc",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake-client-id",
            "client_secret": "fake-client-secret",
            "scopes": scopes,
            "expiry": "2020-01-01T00:00:00Z" if expired else "2099-01-01T00:00:00Z",
        }
    )


def test_rejects_scope_outside_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ScopeNotAllowed):
        load_credentials(
            token_path=tmp_path / "token.json",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            client_secrets_path=tmp_path / "secrets.json",
        )


def test_raises_when_no_client_source_available(tmp_path: Path) -> None:
    with pytest.raises(ClientSecretsMissing):
        load_credentials(
            token_path=tmp_path / "token.json",
            scopes=ALL_SCOPES,
        )


def test_raises_when_client_secrets_path_missing(tmp_path: Path) -> None:
    with pytest.raises(ClientSecretsMissing, match="client secrets not found"):
        load_credentials(
            token_path=tmp_path / "token.json",
            scopes=ALL_SCOPES,
            client_secrets_path=tmp_path / "nope.json",
        )


def test_cache_hit_returns_without_flow(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(_fake_token_json(ALL_SCOPES, expired=False))

    fake_creds = MagicMock()
    fake_creds.valid = True
    fake_creds.expired = False

    with (
        patch(
            "email_concierge.integrations.google.auth.Credentials.from_authorized_user_file",
            return_value=fake_creds,
        ) as from_file,
        patch(
            "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config"
        ) as flow_from_config,
        patch(
            "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_secrets_file"
        ) as flow_from_file,
    ):
        creds = load_credentials(
            token_path=token_path,
            scopes=ALL_SCOPES,
            client_config=FAKE_CLIENT_CONFIG,
        )

    assert creds is fake_creds
    from_file.assert_called_once()
    flow_from_config.assert_not_called()
    flow_from_file.assert_not_called()


def test_refresh_when_expired_with_refresh_token(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(_fake_token_json(ALL_SCOPES, expired=True))

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "refresh-token-abc"
    fake_creds.to_json.return_value = _fake_token_json(ALL_SCOPES, expired=False)

    with (
        patch(
            "email_concierge.integrations.google.auth.Credentials.from_authorized_user_file",
            return_value=fake_creds,
        ),
        patch(
            "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config"
        ) as flow_from_config,
    ):
        creds = load_credentials(
            token_path=token_path,
            scopes=ALL_SCOPES,
            client_config=FAKE_CLIENT_CONFIG,
        )

    assert creds is fake_creds
    fake_creds.refresh.assert_called_once()
    flow_from_config.assert_not_called()
    assert token_path.read_text() == _fake_token_json(ALL_SCOPES, expired=False)


def test_fresh_flow_from_file_when_token_missing(tmp_path: Path) -> None:
    client_secrets = tmp_path / "secrets.json"
    client_secrets.write_text(json.dumps(FAKE_CLIENT_CONFIG))
    token_path = tmp_path / "token.json"

    fresh_creds = MagicMock()
    fresh_creds.to_json.return_value = _fake_token_json(ALL_SCOPES, expired=False)
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fresh_creds

    with patch(
        "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_secrets_file",
        return_value=fake_flow,
    ) as flow_ctor:
        creds = load_credentials(
            token_path=token_path,
            scopes=ALL_SCOPES,
            client_secrets_path=client_secrets,
        )

    assert creds is fresh_creds
    flow_ctor.assert_called_once_with(str(client_secrets), ALL_SCOPES)
    fake_flow.run_local_server.assert_called_once_with(port=0)
    assert token_path.exists()


def test_fresh_flow_from_inline_config_when_token_missing(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"

    fresh_creds = MagicMock()
    fresh_creds.to_json.return_value = _fake_token_json(ALL_SCOPES, expired=False)
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fresh_creds

    with patch(
        "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config",
        return_value=fake_flow,
    ) as flow_ctor:
        creds = load_credentials(
            token_path=token_path,
            scopes=ALL_SCOPES,
            client_config=FAKE_CLIENT_CONFIG,
        )

    assert creds is fresh_creds
    flow_ctor.assert_called_once_with(FAKE_CLIENT_CONFIG, ALL_SCOPES)
    fake_flow.run_local_server.assert_called_once_with(port=0)
    assert token_path.exists()


def test_persisted_token_has_owner_only_perms_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX perms not honored on this platform")

    token_path = tmp_path / "token.json"

    fresh_creds = MagicMock()
    fresh_creds.to_json.return_value = _fake_token_json(ALL_SCOPES)
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fresh_creds

    with patch(
        "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config",
        return_value=fake_flow,
    ):
        load_credentials(
            token_path=token_path,
            scopes=ALL_SCOPES,
            client_config=FAKE_CLIENT_CONFIG,
        )

    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_invalid_cached_token_falls_through_to_flow(tmp_path: Path) -> None:
    """Token exists but creds.valid is False and no refresh_token → fresh flow."""
    token_path = tmp_path / "token.json"
    token_path.write_text(_fake_token_json(ALL_SCOPES, expired=True))

    bad_creds = MagicMock()
    bad_creds.valid = False
    bad_creds.expired = True
    bad_creds.refresh_token = None

    fresh_creds = MagicMock()
    fresh_creds.to_json.return_value = _fake_token_json(ALL_SCOPES)
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fresh_creds

    with (
        patch(
            "email_concierge.integrations.google.auth.Credentials.from_authorized_user_file",
            return_value=bad_creds,
        ),
        patch(
            "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config",
            return_value=fake_flow,
        ),
    ):
        creds = load_credentials(
            token_path=token_path,
            scopes=ALL_SCOPES,
            client_config=FAKE_CLIENT_CONFIG,
        )

    assert creds is fresh_creds
    fake_flow.run_local_server.assert_called_once_with(port=0)


class TestSettingsAliasesAndWrapper:
    def test_inline_json_env_via_google_calendar_oauth_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The user-requested alias GOOGLE_CALENDAR_OAUTH_JSON must populate the field."""
        monkeypatch.delenv("EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON", raising=False)
        monkeypatch.setenv("GOOGLE_CALENDAR_OAUTH_JSON", json.dumps(FAKE_CLIENT_CONFIG))
        cfg = Settings()
        assert cfg.google_client_secrets_json == json.dumps(FAKE_CLIENT_CONFIG)

    def test_inline_json_env_via_prefixed_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOOGLE_CALENDAR_OAUTH_JSON", raising=False)
        monkeypatch.setenv(
            "EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON", json.dumps(FAKE_CLIENT_CONFIG)
        )
        cfg = Settings()
        assert cfg.google_client_secrets_json == json.dumps(FAKE_CLIENT_CONFIG)

    def test_from_settings_prefers_inline_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GOOGLE_CALENDAR_OAUTH_JSON", json.dumps(FAKE_CLIENT_CONFIG))
        token_path = tmp_path / "token.json"
        token_path.write_text(_fake_token_json(ALL_SCOPES, expired=False))

        cfg = Settings(google_token_path=token_path)

        fake_creds = MagicMock()
        fake_creds.valid = True
        fake_creds.expired = False
        with (
            patch(
                "email_concierge.integrations.google.auth.Credentials.from_authorized_user_file",
                return_value=fake_creds,
            ),
            patch(
                "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config"
            ) as flow_from_config,
            patch(
                "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_secrets_file"
            ) as flow_from_file,
        ):
            creds = load_credentials_from_settings(ALL_SCOPES, cfg=cfg)

        # Cache-hit path; neither flow ctor is called, but the setting is
        # resolved as inline JSON (not path).
        assert creds is fake_creds
        flow_from_config.assert_not_called()
        flow_from_file.assert_not_called()

    def test_from_settings_falls_back_to_path_when_no_inline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("GOOGLE_CALENDAR_OAUTH_JSON", raising=False)
        monkeypatch.delenv("EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON", raising=False)

        client_secrets = tmp_path / "secrets.json"
        client_secrets.write_text(json.dumps(FAKE_CLIENT_CONFIG))
        token_path = tmp_path / "token.json"

        # _env_file=None skips .env so a user-local .env that defines
        # GOOGLE_CALENDAR_OAUTH_JSON doesn't leak into this test.
        cfg = Settings(
            _env_file=None,
            google_client_secrets_path=client_secrets,
            google_token_path=token_path,
        )

        fresh_creds = MagicMock()
        fresh_creds.to_json.return_value = _fake_token_json(ALL_SCOPES)
        fake_flow = MagicMock()
        fake_flow.run_local_server.return_value = fresh_creds

        with (
            patch(
                "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_secrets_file",
                return_value=fake_flow,
            ) as flow_ctor,
            patch(
                "email_concierge.integrations.google.auth.InstalledAppFlow.from_client_config"
            ) as flow_from_config,
        ):
            load_credentials_from_settings(ALL_SCOPES, cfg=cfg)

        flow_ctor.assert_called_once_with(str(client_secrets), ALL_SCOPES)
        flow_from_config.assert_not_called()
