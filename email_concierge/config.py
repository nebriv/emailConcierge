from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str | list[str] | None) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [v.strip() for v in value if v.strip()]
    return [v.strip() for v in value.split(",") if v.strip()]


class Account(BaseModel):
    """One IMAP mailbox the listener should watch.

    `name` is a short, stable identifier (e.g., "personal", "work") that
    tags rows in `processed_messages` and `training_examples`. Keep it
    short — it appears in logs and in the status table.
    """

    name: str
    host: str
    port: int = 993
    username: str
    password: str
    folder: str = "INBOX"
    use_ssl: bool = True


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMAIL_CONCIERGE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IMAP (single-account — preserved for backward compatibility; the
    # `accounts` property below synthesizes a one-element list from these
    # fields when EMAIL_CONCIERGE_ACCOUNTS is not set).
    imap_host: str = "mail.example.com"
    imap_port: int = 993
    imap_username: str = "user@example.com"
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_use_ssl: bool = True
    imap_reconnect_seconds: int = 30

    # Multi-account: JSON array of {name, host, port, username, password,
    # folder, use_ssl} objects. When set, this overrides the single-account
    # imap_* fields and the listener spawns one thread per account.
    # Shape documented on the Account model above.
    accounts_json: str = Field(
        default="",
        validation_alias=AliasChoices("EMAIL_CONCIERGE_ACCOUNTS"),
    )

    # Sender filtering
    sender_allow: str = ""
    sender_deny: str = ""

    # Pipeline
    min_confidence: float = 0.7
    can_handle_floor: float = 0.5
    disabled_plugins: str = ""
    disable_llm: bool = False

    # LLM (stage 4)
    llm_base_url: str = "http://ollama:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "llama3.2:3b"
    llm_timeout_seconds: int = 60

    # NER / classifier (reserved for Phase 5).
    # classifier_path defaults to `<models_dir>/classifier.pkl`. Users
    # overriding `models_dir` (host-mode deployments) don't need to
    # override this separately; only set the env var if you want to
    # pin a versioned artifact elsewhere.
    gliner_model: str = "urchade/gliner_small-v2.1"
    classifier_path: Path | None = None
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # CalDAV
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    caldav_calendar: str = "auto-imported"

    # Behavior
    user_timezone: str = "America/New_York"
    dry_run: bool = False
    feedback_window_hours: int = 24
    # How often the listener runs the feedback scan (CalDAV deletes →
    # negative training labels). Set to 0 to disable in-process scans
    # and run `email_concierge feedback` from cron instead.
    feedback_scan_interval_minutes: int = 15

    # Storage
    db_path: Path = Path("/data/email-concierge.db")
    models_dir: Path = Path("/data/models")

    # Google integration (Phase 2.5 — training-data import only, read-only scopes)
    # Either supply the OAuth client JSON inline as an env var or as a file path.
    # JSON takes precedence. The env var accepts two names so users can pick a
    # shorter one outside the EMAIL_CONCIERGE_ prefix if preferred.
    google_client_secrets_json: str = Field(
        default="",
        validation_alias=AliasChoices(
            "EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON",
            "GOOGLE_CALENDAR_OAUTH_JSON",
        ),
    )
    google_client_secrets_path: Path = Path("/data/google_client_secrets.json")
    google_token_path: Path = Path("/data/google_token.json")
    google_calendar_id: str = "primary"

    # plid-resolver (opt-in; only used by `import-training --resolve-plids`).
    # Profile dir is a persistent Chrome user-data directory so the user
    # only has to log in once. chrome_major pins undetected-chromedriver
    # to the installed Chrome's major version (look at chrome://version).
    # 0 means "let undetected-chromedriver auto-detect". The shorter env
    # var aliases (`EMAIL_CONCIERGE_CHROME_PROFILE`, `..._CHROME_MAJOR`)
    # match what the one-shot spike script used, so ops muscle-memory
    # keeps working once users move from the spike to the real flag.
    google_chrome_profile_path: Path = Field(
        default=Path("/data/chrome-profile"),
        validation_alias=AliasChoices(
            "EMAIL_CONCIERGE_GOOGLE_CHROME_PROFILE_PATH",
            "EMAIL_CONCIERGE_CHROME_PROFILE",
        ),
    )
    google_chrome_major: int = Field(
        default=0,
        validation_alias=AliasChoices(
            "EMAIL_CONCIERGE_GOOGLE_CHROME_MAJOR",
            "EMAIL_CONCIERGE_CHROME_MAJOR",
        ),
    )

    # Logging
    log_level: str = "INFO"
    log_json: bool = True

    @property
    def resolved_classifier_path(self) -> Path:
        """Classifier artifact path, defaulting under models_dir."""
        return self.classifier_path or (self.models_dir / "classifier.pkl")

    @property
    def accounts(self) -> list[Account]:
        """Parsed list of accounts to watch.

        Precedence:
        1. If `EMAIL_CONCIERGE_ACCOUNTS` is set, parse the JSON array.
        2. Otherwise, synthesize a single-element list from the legacy
           `imap_*` fields so existing single-mailbox deployments keep
           working with zero config change. The synthesized account's
           name is the username (so DB tagging is stable across restarts).
        """
        raw = (self.accounts_json or "").strip()
        if raw:
            try:
                items = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"EMAIL_CONCIERGE_ACCOUNTS is not valid JSON: {e}"
                ) from e
            if not isinstance(items, list) or not items:
                raise ValueError(
                    "EMAIL_CONCIERGE_ACCOUNTS must be a non-empty JSON array"
                )
            parsed = [Account(**item) for item in items]
            seen: set[str] = set()
            for acct in parsed:
                if acct.name in seen:
                    raise ValueError(
                        f"EMAIL_CONCIERGE_ACCOUNTS: duplicate account name {acct.name!r}"
                    )
                seen.add(acct.name)
            return parsed

        return [
            Account(
                name=self.imap_username,
                host=self.imap_host,
                port=self.imap_port,
                username=self.imap_username,
                password=self.imap_password,
                folder=self.imap_folder,
                use_ssl=self.imap_use_ssl,
            )
        ]

    @property
    def sender_allow_list(self) -> list[str]:
        return _csv(self.sender_allow)

    @property
    def sender_deny_list(self) -> list[str]:
        return _csv(self.sender_deny)

    @property
    def disabled_plugins_list(self) -> list[str]:
        return _csv(self.disabled_plugins)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
