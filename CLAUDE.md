# emailConcierge

A self-hosted Python service that watches an IMAP inbox and extracts events from incoming mail through a tiered pipeline: deterministic `.ics` parsing, user-defined vendor plugins, a zero-shot NER stage, and an LLM fallback. Results are written to a CalDAV calendar.

This document is the single source of truth for the v1.0 and v1.5 scope. It is intended to be handed to Claude Code as the implementation brief.

**v2.0 (AdventureLog integration) is deliberately scoped to a separate document: `docs/v2-adventurelog.md`.** That document is not in scope for the initial build and should not influence v1.x decisions. Do not implement any AdventureLog-related code until v1.5 is shipped and stable.

---

## First session instructions

1. Read this document in full before writing any code.
2. Do not read `docs/v2-adventurelog.md` yet. It will be brought into scope explicitly later.
3. Start with Phase 0 (section 11). Confirm the plan with me before implementing Phase 1.
4. When uncertain about a scope decision, ask rather than guess.
5. If any of the tech stack in section 5 is unfamiliar or has changed since the plan was written (e.g., library versions), flag it before using it.

## 1. Goal

Replicate the "automatically added to your calendar" experience that Google Mail used to provide, but fully self-hosted and cost-aware: cheap/deterministic extraction is always tried first, LLM calls are the last resort.

The service should be:

- A single Docker container running in the user's existing docker-compose VM.
- Stateful only via SQLite (single file in a mounted volume).
- Configured entirely through environment variables.
- Resilient to restarts and IMAP disconnects.
- Conservative: a missed event is acceptable, a wrong event on the calendar is not.
- Cost-aware: routes each email to the cheapest stage that can handle it; LLM inference should drop sharply over time as plugins and the trained classifier mature.
- **Strictly read-only against the user's mailbox.** See section 4 for the enforcement model.

---

## 2. Scope

### v1.0 — Email to Calendar (in scope)

- IMAP IDLE listener, read-only. One thread per configured account; single
  folder per account (see section 4a on multi-account).
- Four-stage extraction pipeline (detailed in section 6):
  1. Deterministic `.ics` attachment parser.
  2. Vendor plugin registry (auto-discovered Python modules with `can_handle` / `extract` methods).
  3. Heuristic + zero-shot NER stage (GLiNER + a trained email-vs-not classifier).
  4. LLM fallback via OpenAI-compatible API.
- CalDAV writer (Nextcloud Calendar verified; any RFC 4791 server should work).
- SQLite-backed deduplication keyed on Message-ID.
- Update handling via iCalendar UID: re-emitted bookings update the existing event rather than creating a new one.
- `backfill` mode: ingest an archive folder, run everything through the LLM, save input/output pairs as labeled training data.
- `export-fixtures` command for extracting redacted sample emails (used during plugin authoring).
- Configurable sender allow-list / deny-list.
- Structured JSON logging with per-email stage attribution (which stage handled it, confidence, latency).
- Dockerfile + docker-compose example.

### v1.0 — Out of scope

- Outbound email of any kind.
- **Any IMAP operation that modifies server state.** See section 4.
- Multi-user support (one SQLite file, one CalDAV calendar, one set of
  trained models — but multiple mailboxes belonging to the *same* user
  are supported; see section 4a).
- A web UI for reviewing or approving extractions (CLI / logs only).
- OAuth-based IMAP (XOAUTH2). App passwords only.
- Calendar event editing, deletion, or two-way sync.
- Fine-tuning a generative model. Zero-shot NER + small fine-tuned classifier only.
- AdventureLog integration. See separate v2.0 document (docs/v2-adventurelog.md).

### v1.5 — Trained classifier release (in scope)

- `train` command produces a persisted classifier artifact.
- Active learning loop: when a user deletes a Concierge-created event from CalDAV within a configurable window, that signal gets logged as a negative label.
- Cross-stage agreement metrics: `evaluate` command samples emails, runs through all stages, logs disagreements for review.

### Deferred to v3.0+

- Fine-tuned small generative model (Qwen 0.5B-class) trained on bootstrapped labels for fully-local extraction.
- Web UI.
- Slack / Discord / ntfy notifications of newly-created events.
- Multi-*user* support (separate tenants with separate DBs / CalDAV
  calendars / models). Multi-*mailbox* for a single user is already
  shipped (section 4a).

---

## 3. Architecture

```
┌────────────────────┐
│ IMAP IDLE listener │  Read-only wrapper around imap-tools.
│ (long-running)     │  See section 4 for the safety contract.
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Dedup check        │  SQLite, lookup by Message-ID
└─────────┬──────────┘
          │ new
          ▼
┌────────────────────┐
│ Sender filter      │  allow-list / deny-list
└─────────┬──────────┘
          │ candidate
          ▼
╔════════════════════════════════════════════════════════╗
║  Extraction router (see section 6 for detail)          ║
║                                                        ║
║  Stage 1: .ics attachment?        ─→ ics parser        ║
║  Stage 2: plugin can_handle > 0.8 ─→ matching plugin   ║
║  Stage 3: classifier says event?  ─→ NER + assembly    ║
║  Stage 4: (fallback)              ─→ LLM extractor     ║
║                                                        ║
║  Each stage returns ExtractionResult or None.          ║
║  Router accepts first result meeting confidence floor. ║
╚═══════════════════════════╦════════════════════════════╝
                            │
                            ▼
                  ┌───────────────────┐
                  │ Sink: CalDAV      │
                  └─────────┬─────────┘
                            │
                            ▼
                  ┌───────────────────┐
                  │ Mark processed    │  SQLite: message_id, stage,
                  │                   │  confidence, outputs, status
                  └───────────────────┘
```

Every stage produces the same `ExtractionResult` shape, making the router trivial: it picks the first stage that produces a result meeting the confidence floor. Each stage is independently testable and replaceable.

---

## 4. Safety: IMAP is read-only, guaranteed

This is a hard requirement. The service must never cause any change to the user's mailbox under any circumstance. Not "mark as read," not "move to folder," not "flag," not "delete," not "append," never. A bug in plugin code, a malformed email, an LLM hallucination, or any other failure mode must be incapable of modifying server state.

### 4.1 Enforcement model

The IMAP integration is enforced at **four layers**. All four must be in place. Defense in depth.

**Layer 1: Open mailboxes in read-only mode.**
Use IMAP `EXAMINE` instead of `SELECT`. This causes the server itself to reject any write attempt with a protocol error. Even if application code is buggy, the server won't execute it.

**Layer 2: Use `.PEEK` fetch variants exclusively.**
A plain `FETCH BODY[...]` implicitly sets the `\Seen` flag on the message, which mutates server state. `FETCH BODY.PEEK[...]` does not. This is the single most common footgun in IMAP clients. The `imap-tools` library exposes `mark_seen=False` on its fetch methods; it must be set explicitly on every call.

**Layer 3: A `ReadOnlyMailbox` wrapper class.**
`imap-tools`' `MailBox` exposes write methods (`delete`, `move`, `copy`, `flag`, `append`, `expunge`, `seen`). The service does not use `MailBox` directly. Instead, all IMAP access goes through a wrapper:

```python
# email_concierge/imap_readonly.py
class ReadOnlyMailbox:
    """A deliberately narrow IMAP interface. Exposes only read
    operations. Impossible to call a mutating method by construction
    because they simply are not defined on this class."""

    def __init__(self, host, port, username, password, use_ssl=True):
        self._mb = MailBox(host, port) if use_ssl else MailBoxUnencrypted(host, port)
        self._mb.login(username, password, initial_folder=None)

    def folder_list(self) -> list[str]: ...
    def examine(self, folder: str) -> None:
        """Open a folder in EXAMINE (read-only) mode.
        SELECT is never used."""
        ...
    def fetch(self, criteria: str, limit: int | None = None) -> Iterator[Email]:
        """Fetch matching messages. Always uses mark_seen=False."""
        ...
    def idle(self, timeout_seconds: int) -> Iterator[Email]: ...
    def logout(self) -> None: ...

    # Deliberately NOT exposed:
    # - delete, move, copy, append, expunge
    # - seen, flag, unflag
    # - any STORE-equivalent method
```

No other module in the codebase imports `imap_tools` directly. Enforced by a linter rule (`ruff`'s `TID` flake-8-tidy-imports) in `pyproject.toml`. If a future change tries to `from imap_tools import MailBox` outside of `imap_readonly.py`, CI fails.

**Layer 4: Integration test that proves read-only-ness.**
A test fixture runs a local Dovecot (or uses a Python fake IMAP server like `aioimaplib`'s test harness) that records every protocol command received. The test exercises the full listener loop against a seeded mailbox, then asserts: only `CAPABILITY`, `LOGIN`, `LIST`, `EXAMINE`, `SEARCH`, `FETCH` (with `BODY.PEEK`), `IDLE`, `DONE`, `LOGOUT` commands were observed. Any other command is a test failure.

This is the ultimate safety net. Even if the wrapper gets a bug, even if a plugin does something crazy, CI catches any write attempt before the change ships.

### 4.2 Credentials guidance

The README must instruct the user to generate a dedicated app password for the service, separate from their main email credentials, and revoke it if the service is removed. Where the email provider supports scoped app passwords (Fastmail, some Microsoft accounts), the README recommends scoping to read-only if available. This is advisory since not all providers offer it, but every little bit helps.

### 4.3 What this looks like in logs

Every IMAP command issued is logged at DEBUG level with the full command string (minus credentials). This is off by default in production but flip-on-able via `EMAIL_CONCIERGE_LOG_LEVEL=DEBUG` for troubleshooting. Makes it trivially verifiable that the service is only ever issuing read operations.

---

## 4a. Multi-account (one user, several mailboxes)

The v1.0 scope originally called for a single mailbox; shipped behavior is now *N mailboxes belonging to the same user*. Two users with two inboxes is still out of scope — the DB, CalDAV calendar, and trained models are all shared. "Multi-account" here means "one person, their personal IMAP *and* their Gmail," not "SaaS tenancy."

**Configuration.** Either leave the legacy `EMAIL_CONCIERGE_IMAP_*` env vars set (one account) or set one `EMAIL_CONCIERGE_ACCOUNT_<N>` env var per mailbox, each holding an IMAP URL: `imaps://<user>:<password>@<host>[:<port>]/<folder>#<name>` (use `imap://` for plaintext; default port 993 for imaps, 143 for imap; folder defaults to INBOX when path is empty). The URL fragment `#<name>` is a short stable identifier tagged onto every DB row — keep it under ~16 chars and unique across all configured accounts. User/password/folder must be percent-encoded if they contain `@`, `:`, `/`, or `#`. When any `EMAIL_CONCIERGE_ACCOUNT_<N>` is set the legacy `IMAP_*` fields are ignored; indices need not be contiguous and sort numerically (`_2` before `_10`).

**Runtime.** `listener.run_all_accounts` spawns one daemon thread per account. Each thread owns its own `ReadOnlyMailbox` and its own IMAP IDLE session; they share the SQLite connection (WAL mode makes multi-writer safe) and the single `CaldavSink`. Resume cursor is per-account: `MAX(received_at) WHERE account = ?`. Only the first account in the list runs the CalDAV feedback scan so we don't multiply-hit CalDAV.

**Gmail caveat.** Gmail IMAP does not implement server-initiated IDLE notifications the way most servers do — `idle_wait` will block for the full 29-minute timeout and catch-up runs on the timeout cycle. That's fine for personal-inbox cadence but don't expect sub-second latency.

**DB tagging.** `processed_messages.account` and `training_examples.account` are nullable TEXT columns added via an idempotent `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` migration (not a migration framework — just a guarded one-shot). Legacy rows predating multi-account remain NULL and are preserved unchanged.

**Why not one DB per account.** Cross-account dedup matters (both inboxes may CC you on a booking) and the trained classifier should see the union of examples, not N partitions. Tagging rather than partitioning is the right call.

---

## 5. Tech stack

| Concern | Library | Reason |
|---|---|---|
| IMAP | `imap-tools`, wrapped by `ReadOnlyMailbox` | Modern wrapper, supports IDLE, sane Message objects, `mark_seen=False` is explicit |
| iCalendar parsing | `icalendar` | De facto standard |
| HTML parsing (plugins) | `selectolax` | Fast CSS selector-based HTML parsing |
| Zero-shot NER | `gliner` | Extracts custom entity types with no training; CPU-fast |
| Classifier | `scikit-learn` + `sentence-transformers` | MiniLM sentence embedding + logistic regression; tiny, CPU-inferable, easy to retrain |
| Embeddings | `sentence-transformers` (all-MiniLM-L6-v2) | 80MB, CPU-friendly |
| LLM client | `openai` (official SDK) | `base_url` makes Ollama/OpenRouter/Anthropic gateways drop-in |
| Schema | `pydantic` v2 | Schema-as-code, used by openai SDK for structured outputs |
| CalDAV | `caldav` | Most mature CalDAV client in Python |
| State | `sqlite3` (stdlib) | Atomic writes, one file, no deps |
| Logging | `structlog` | JSON output, structured context |
| Config | `pydantic-settings` | Env var loading with validation |
| Tests | `pytest` + `pytest-asyncio` | Standard |
| Container | `python:3.12-slim` | Current, small |

No web framework. No background job queue. No ORM. No generative-model fine-tuning infrastructure.

---

## 6. The extraction pipeline (detailed)

### 6.1 Shared interface

```python
# email_concierge/extractors/base.py
from typing import Protocol, runtime_checkable
from email_concierge.models import Email, ExtractionResult

@runtime_checkable
class Extractor(Protocol):
    name: str            # for logging and metrics
    stage: int           # 1..4; router tries lower numbers first

    def can_handle(self, email: Email) -> float:
        """Return 0.0-1.0 confidence this extractor applies to this email.
        Cheap check only (sender match, subject regex, etc.).
        Must return quickly (< 5 ms). The router skips extractors
        that return < 0.5."""
        ...

    def extract(self, email: Email) -> ExtractionResult | None:
        """Do the actual extraction. Return None if extraction fails
        (extractor changes its mind or required fields are missing).
        Allowed to be slow."""
        ...
```

`ExtractionResult` wraps a `ParsedEvent` plus a confidence score and stage attribution. The router accepts the first result whose confidence meets `EMAIL_CONCIERGE_MIN_CONFIDENCE`.

### 6.2 Stage 1: `.ics` attachment parser

One extractor, ships built-in. Iterates attachments looking for `text/calendar` or `.ics`, passes through `icalendar.Calendar.from_ical`, returns `ParsedEvent` with `confidence=1.0` and the original `UID` preserved. `can_handle` returns 1.0 if any attachment qualifies, else 0.0.

### 6.3 Stage 2: vendor plugin registry

Plugins live in `email_concierge/extractors/plugins/` as standalone Python files. Auto-discovered at startup via `pkgutil.iter_modules` and registered in a list sorted by `stage` then `priority`.

Each plugin is typically a single class:

```python
# email_concierge/extractors/plugins/united_airlines.py
import re
from email_concierge.extractors.base import Extractor
from email_concierge.models import Email, ExtractionResult

SENDER_PATTERN = re.compile(r"@(united\.com|uafrequentflyer\.com)$", re.I)
SUBJECT_HINTS = ("e-Ticket", "Your flight", "Flight confirmation")

class UnitedAirlinesExtractor:
    name = "united_airlines"
    stage = 2
    priority = 10

    def can_handle(self, email: Email) -> float:
        if not SENDER_PATTERN.search(email.sender):
            return 0.0
        if any(hint in email.subject for hint in SUBJECT_HINTS):
            return 1.0
        return 0.5

    def extract(self, email: Email) -> ExtractionResult | None:
        # Parse the HTML body for flight details.
        # Return ExtractionResult with high confidence, or None if parsing failed.
        ...
```

**Plugin conventions:**

- One file per vendor, named by vendor (`united_airlines.py`, `marriott.py`, `ticketmaster.py`).
- `can_handle` is a cheap regex/string check. It must not parse the body.
- `extract` is allowed to do heavy parsing. If it fails (page format changed, required field missing), return `None` and the router falls through.
- Every plugin ships with at least one fixture email in `tests/fixtures/emails/<plugin_name>/` and a test that asserts expected output.
- Plugins can be disabled individually via `EMAIL_CONCIERGE_DISABLED_PLUGINS`.
- **Plugins must never raise for expected failure modes.** They may raise for programming bugs. The router catches all exceptions but logs them loudly.

**Initial plugin wave** — chosen because they represent the long tail of common senders and format their emails consistently:

- `united_airlines`, `american_airlines`, `delta`, `southwest`
- `marriott`, `hilton`, `ihg`, `airbnb`
- `eventbrite`, `ticketmaster`, `axs`, `stubhub`
- `opentable`, `resy`
- `calendly`
- `amtrak`

The actual wave is determined by looking at the user's own email distribution once backfill has run. See section 14 for how Claude Code builds and adds plugins autonomously.

### 6.4 Stage 3: heuristic + zero-shot NER

Runs when stages 1 and 2 miss. Internally three components:

**Classifier (fine-tuned):** logistic regression over sentence-embedding features of (sender + subject + first ~500 chars of body). Outputs `event` / `neither`. If `neither`, return `None`. This is the gate: it saves running NER on every newsletter.

**NER extractor (zero-shot, GLiNER):** given the body text and a prompt listing entity types (dates, times, IATA codes, cities, confirmation numbers, flight numbers, hotel names, venue names, addresses), returns spans with labels. No training needed.

**Heuristic assembler:** rule-based combiner that takes the NER output and composes a `ParsedEvent`. Example rules:
- Two IATA codes plus a flight number pattern → event title "Flight XXX: ORIG → DEST", start from departure time.
- Check-in date + hotel name → event title "Stay at $hotel", start on check-in date.
- Single date + venue + event title → event.

The assembler emits a confidence based on how cleanly the entities map to a complete record. Missing required fields → lower confidence → router escalates to LLM.

### 6.5 Stage 4: LLM fallback

OpenAI-compatible client with structured output, Pydantic schema validation. Every LLM call is expensive relative to the other stages, so the system tracks per-sender LLM hit rates and surfaces a "top senders that keep falling to LLM" list. Those are the candidates Claude Code uses to prioritize new plugins.

### 6.6 Router logic

```python
def route(email: Email) -> ExtractionResult | None:
    for extractor in extractors_sorted_by_stage():
        if extractor.can_handle(email) < CAN_HANDLE_FLOOR:
            continue
        try:
            result = extractor.extract(email)
        except Exception:
            logger.exception("extractor_failed", name=extractor.name)
            continue
        if result is None:
            continue
        if result.confidence < min_confidence:
            continue
        return result
    return None
```

Every stage attempt is logged with name, duration, confidence, outcome. The logs are the primary feedback mechanism for figuring out what to improve next.

---

## 7. Training and data bootstrapping

The classifier is the only trained model in v1.0/v1.5. Training data comes from three sources:

**A. Backfill mode (recommended first step):**

```bash
python -m email_concierge backfill --folder=Archive --since=2015-01-01 --max=5000
```

Iterates historical email, runs each message through the full pipeline, saves results to a `training_examples` table. LLM-extracted examples become positive labels, emails classified as `neither` become negative labels. Backfill is **strictly read-only** against IMAP (same safety guarantees as live listening).

**B. Live operation (ongoing):**

Every email processed during normal operation also gets logged as a training example.

**C. User feedback (active learning):**

When the user deletes a Concierge-created event from CalDAV within `EMAIL_CONCIERGE_FEEDBACK_WINDOW_HOURS`, a background sync detects it and marks that example as a negative label. The book-keeping is thin: compare calendar UIDs currently present against UIDs Concierge wrote in the last N days; anything missing is assumed deleted.

### Training command

```bash
python -m email_concierge train classifier
```

1. Loads all rows from `training_examples` where `label IS NOT NULL`.
2. Computes sentence embeddings for `sender + subject + body_preview` using MiniLM (cached).
3. Fits a logistic regression with class weighting.
4. Runs 5-fold cross-validation; prints precision/recall per class.
5. Saves model + embedding cache to `/data/models/classifier.pkl`.
6. Writes a record to `model_versions` so the pipeline knows which artifact to load.

Training time for 5000 examples: under 2 minutes on modest CPU.

### Evaluation

```bash
python -m email_concierge evaluate --sample=200
```

Samples N recent emails, runs each through every stage in order (even if an earlier stage matched), logs disagreements. Surfaces:

- Plugins silently breaking when a vendor changes their email template.
- Classifier drifting because email patterns have shifted.
- LLM disagreeing with plugin output (almost always means the plugin is subtly wrong).

---

## 8. Project structure

Repo name on disk: `email-concierge`. Python package: `email_concierge`.

```
email-concierge/
├── README.md
├── LICENSE
├── pyproject.toml              # includes ruff TID rules that forbid importing imap_tools outside imap_readonly.py
├── Dockerfile
├── docker-compose.yaml
├── .env.example
├── .dockerignore
├── email_concierge/
│   ├── __init__.py
│   ├── __main__.py             # CLI: run | backfill | train | evaluate | metrics | export-fixtures
│   ├── config.py
│   ├── log.py
│   ├── db.py
│   ├── models.py               # Pydantic: Email, ExtractionResult, ParsedEvent
│   ├── pipeline.py
│   ├── imap_readonly.py        # ONLY file that imports imap_tools. See section 4.
│   ├── listener.py             # uses ReadOnlyMailbox
│   ├── router.py
│   ├── extractors/
│   │   ├── __init__.py
│   │   ├── base.py             # Extractor protocol, ExtractionResult
│   │   ├── discovery.py
│   │   ├── ics.py              # stage 1
│   │   ├── ner.py              # stage 3
│   │   ├── llm.py              # stage 4
│   │   └── plugins/            # stage 2, auto-discovered
│   │       ├── __init__.py
│   │       └── ... (vendor-specific files)
│   ├── ml/
│   │   ├── __init__.py
│   │   ├── classifier.py
│   │   ├── embeddings.py
│   │   └── ner_entities.py
│   ├── sinks/
│   │   ├── __init__.py
│   │   └── caldav_sink.py
│   ├── commands/
│   │   ├── __init__.py
│   │   ├── run.py
│   │   ├── backfill.py
│   │   ├── train.py
│   │   ├── evaluate.py
│   │   ├── metrics.py
│   │   └── export_fixtures.py  # redacted email export for plugin authoring
│   ├── redaction.py            # PII redaction for export_fixtures
│   └── prompts/
│       └── event_extract.txt
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── emails/
│   │   │   └── <plugin_name>/
│   │   └── ics/
│   ├── test_imap_readonly.py   # asserts no write commands ever issued; section 4, layer 4
│   ├── test_ics_parser.py
│   ├── test_plugin_discovery.py
│   ├── test_plugins/
│   ├── test_ner_extractor.py
│   ├── test_llm_parser.py
│   ├── test_classifier.py
│   ├── test_router.py
│   ├── test_caldav_sink.py
│   ├── test_redaction.py
│   └── test_pipeline.py
└── scripts/
    └── replay.py
```

---

## 9. Data model

### SQLite schema

```sql
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id          TEXT PRIMARY KEY,
    received_at         TEXT NOT NULL,
    sender              TEXT NOT NULL,
    subject             TEXT NOT NULL,
    handled_by_stage    INTEGER,
    handled_by_name     TEXT,
    confidence          REAL,
    status              TEXT NOT NULL,
    error               TEXT,
    processed_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pm_handled_by ON processed_messages(handled_by_stage, handled_by_name);
CREATE INDEX IF NOT EXISTS idx_pm_received ON processed_messages(received_at);

CREATE TABLE IF NOT EXISTS calendar_events (
    ical_uid            TEXT PRIMARY KEY,
    message_id          TEXT NOT NULL,
    caldav_url          TEXT NOT NULL,
    summary             TEXT,
    starts_at           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES processed_messages(message_id)
);

CREATE TABLE IF NOT EXISTS training_examples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT NOT NULL UNIQUE,
    sender              TEXT NOT NULL,
    subject             TEXT NOT NULL,
    body_preview        TEXT NOT NULL,
    label               TEXT,
    label_source        TEXT,
    extracted_json      TEXT,
    created_at          TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES processed_messages(message_id)
);

CREATE INDEX IF NOT EXISTS idx_te_label ON training_examples(label);

CREATE TABLE IF NOT EXISTS model_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT NOT NULL,
    version             TEXT NOT NULL,
    artifact_path       TEXT NOT NULL,
    training_n_examples INTEGER NOT NULL,
    metrics_json        TEXT NOT NULL,
    trained_at          TEXT NOT NULL,
    is_active           INTEGER NOT NULL DEFAULT 0
);
```

### Pydantic models

```python
class Email(BaseModel):
    message_id: str
    sender: str
    recipients: list[str]
    subject: str
    body_text: str
    body_html: str | None
    attachments: list[Attachment]
    received_at: datetime

class ExtractionResult(BaseModel):
    handled_by_stage: int
    handled_by_name: str
    confidence: float
    parsed: ParsedEvent
    latency_ms: int
    notes: list[str] = Field(default_factory=list)

class ParsedEvent(BaseModel):
    title: str
    start: datetime        # must be timezone-aware
    end: datetime | None
    location: str | None
    description: str | None
    ical_uid: str | None   # set only by stage 1; other stages leave None and the sink generates one
```

---

## 10. Configuration

Prefix `EMAIL_CONCIERGE_`. Loaded by `pydantic-settings`. A `.env.example` lives in the repo.

```bash
# IMAP (read-only, enforced in code)
EMAIL_CONCIERGE_IMAP_HOST=mail.example.com
EMAIL_CONCIERGE_IMAP_PORT=993
EMAIL_CONCIERGE_IMAP_USERNAME=user@example.com
EMAIL_CONCIERGE_IMAP_PASSWORD=app-password-here
EMAIL_CONCIERGE_IMAP_FOLDER=INBOX
EMAIL_CONCIERGE_IMAP_USE_SSL=true
EMAIL_CONCIERGE_IMAP_RECONNECT_SECONDS=30

# Sender filtering (comma-separated)
EMAIL_CONCIERGE_SENDER_ALLOW=
EMAIL_CONCIERGE_SENDER_DENY=newsletter@,noreply@substack.com

# Pipeline
EMAIL_CONCIERGE_MIN_CONFIDENCE=0.7
EMAIL_CONCIERGE_CAN_HANDLE_FLOOR=0.5
EMAIL_CONCIERGE_DISABLED_PLUGINS=
EMAIL_CONCIERGE_DISABLE_LLM=false

# LLM (stage 4)
EMAIL_CONCIERGE_LLM_BASE_URL=http://ollama:11434/v1
EMAIL_CONCIERGE_LLM_API_KEY=ollama
EMAIL_CONCIERGE_LLM_MODEL=llama3.2:3b
EMAIL_CONCIERGE_LLM_TIMEOUT_SECONDS=60

# NER (stage 3)
EMAIL_CONCIERGE_GLINER_MODEL=urchade/gliner_small-v2.1
EMAIL_CONCIERGE_CLASSIFIER_PATH=/data/models/classifier.pkl
EMAIL_CONCIERGE_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2

# CalDAV
EMAIL_CONCIERGE_CALDAV_URL=https://nextcloud.home.nebriv.com/remote.php/dav/calendars/ben/
EMAIL_CONCIERGE_CALDAV_USERNAME=ben
EMAIL_CONCIERGE_CALDAV_PASSWORD=app-password
EMAIL_CONCIERGE_CALDAV_CALENDAR=auto-imported

# Behavior
EMAIL_CONCIERGE_USER_TIMEZONE=America/New_York
EMAIL_CONCIERGE_DRY_RUN=false
EMAIL_CONCIERGE_FEEDBACK_WINDOW_HOURS=24

# Storage
EMAIL_CONCIERGE_DB_PATH=/data/email-concierge.db
EMAIL_CONCIERGE_MODELS_DIR=/data/models

# Logging
EMAIL_CONCIERGE_LOG_LEVEL=INFO
EMAIL_CONCIERGE_LOG_JSON=true
```

---

## 11. Phased implementation plan

### Phase 0 — Skeleton (half day)

- Project structure, `pyproject.toml` (including the ruff TID rule for `imap_tools`), Dockerfile, `.env.example`.
- `config.py`, `log.py`, `db.py` (with `CREATE TABLE IF NOT EXISTS`).
- `__main__.py` CLI dispatch (only `run` implemented).
- Container builds and runs.

**Done when:** `docker compose up` starts the container, connects to IMAP, logs success, SQLite file created.

### Phase 1 — Read-only IMAP wrapper + router + stages 1 & 4 (1-2 days)

- `imap_readonly.py`: the wrapper described in section 4. This is the first real code written.
- `test_imap_readonly.py`: section 4 layer 4 test. Fails CI if any write command is observed.
- `extractors/base.py`: `Extractor` protocol, `ExtractionResult`.
- `extractors/ics.py` and `extractors/llm.py`.
- `router.py`.
- `sinks/caldav_sink.py` with update-by-UID.
- `listener.py` using `ReadOnlyMailbox`.
- `scripts/replay.py`.

**Done when:** an airline confirmation with `.ics` writes to CalDAV via stage 1; a Ticketmaster email (no `.ics`) writes via stage 4; the layer-4 safety test passes.

### Phase 2 — Plugin framework + fixture export (half to one day)

- `extractors/discovery.py`.
- One reference plugin (`united_airlines.py`) end-to-end.
- Per-plugin test harness.
- `redaction.py` + `commands/export_fixtures.py`.

**Done when:** (a) adding a plugin is a single-file drop-in with a fixture, and (b) `python -m email_concierge export-fixtures --sender-domain=united.com --limit=3` produces redacted `.eml` files in `tests/fixtures/emails/united_airlines/`.

### Phase 3 — First plugin wave, driven by real data (iterative)

Claude Code runs `python -m email_concierge metrics --top-llm-senders=20`, identifies the heaviest LLM users in the actual inbox, writes plugins for them one at a time using the workflow in section 14.

**Done when:** LLM hit rate on recent emails drops meaningfully from the Phase 1 baseline.

### Phase 4 — v1.0 hardening (half day)

- Idempotency tests (replay same email; no duplicate).
- Update-by-UID tests.
- `backfill` command implemented.
- README with setup walkthrough, plugin authoring guide, `docker-compose.yaml` example, observability section, **a prominent read-only safety section**.
- Tag v1.0.

### Phase 5 — Stage 3 scaffolding (1-2 days)

- `ml/embeddings.py`, `ml/classifier.py`, `ml/ner_entities.py`.
- `extractors/ner.py`.
- Initial classifier trained on backfill data.
- Stage 3 registered; starts handling emails where classifier is confident and assembler succeeds.

**Done when:** after backfill and one training pass, stage 3 handles a non-trivial fraction of emails that previously went to stage 4.

### Phase 6 — Active learning + v1.5 (half to one day)

- Feedback detector: periodic job reads CalDAV to detect user-deleted events, writes negative labels.
- `train` and `evaluate` commands polished; `evaluate` produces a report.
- README section on how the system gets smarter over time.
- Tag v1.5.

---

## 12. Testing strategy

**Unit tests** for every parser, plugin, matcher. Fixtures in `tests/fixtures/`.

**The section 4 layer 4 test is mandatory and non-negotiable.** It runs in CI and must pass before any change merges.

**Plugin test harness:** parameterized pytest walks `tests/fixtures/emails/<plugin_name>/`, runs each `.eml` through the plugin, compares against `expected.json` sibling.

**Integration tests** (opt-in, via env var):
- A local Radicale CalDAV server in CI for caldav_sink tests.
- A local Dovecot (or `aioimaplib` test harness) for IMAP read-only verification.

**Replay testing** is the primary development loop for new senders:
1. Save email as `tests/fixtures/emails/<sender>/<scenario>.eml` (via `export-fixtures`).
2. Run `python scripts/replay.py <fixture> --dry-run`.
3. Iterate plugin or prompt.
4. Add assertion.

**No live LLM calls in CI.** All LLM stage tests use recorded responses.

---

## 13. Logging, observability, and failure modes

Every message produces one INFO log line with: sender, subject, message_id, handled_by_stage, handled_by_name, confidence, total_duration_ms, per-stage breakdown. Structured JSON.

LLM calls additionally log: model, prompt tokens, response tokens, latency.

Failures log with `exc_info=True` plus message_id. Full email body logged only at DEBUG.

`python -m email_concierge metrics` prints: stage hit rates over last 7/30/90 days, top senders hitting the LLM (plugin candidates), classifier performance over time.

| Failure | Behavior |
|---|---|
| IMAP connection drops | Reconnect with exponential backoff, cap 5 min. Last-seen Message-ID persists in SQLite. |
| Plugin throws | Caught by router, logged, treated as `None`. No single plugin can take down the service. |
| GLiNER model unavailable | Stage 3 logs warning, returns `None`. Pipeline falls to stage 4. |
| Classifier file missing | Stage 3 falls back to NER-only (no gate). Still functional. |
| LLM unavailable | Mark message `failed`, log loudly. Retry on next poll. |
| LLM returns invalid JSON | Pydantic catches it. Log, skip. No retry loop. |
| CalDAV unavailable | Mark failed, retry on restart. |
| Same email twice | Message-ID dedup; logged at DEBUG. |
| Same booking re-sent (modified) | iCal UID match → update existing event. |
| User manually edits CalDAV event | Next re-send from source overwrites. Documented; users disable Concierge for that sender via deny-list. |

---

## 14. Claude Code autonomy and workflow

This is the operating procedure for Claude Code working on Concierge after the initial build is complete. Claude Code should be able to improve the system independently within the guardrails below.

### 14.1 What Claude Code is authorized to do

- Read from SQLite: `processed_messages`, `training_examples`, `model_versions`, `calendar_events`.
- Run CLI commands: `metrics`, `export-fixtures`, `train`, `evaluate`, `replay`.
- Create new plugin files in `email_concierge/extractors/plugins/`.
- Create new fixtures in `tests/fixtures/emails/<plugin_name>/`.
- Modify tests, prompts, ML code, heuristic rules.
- Open PRs against the repo.

### 14.2 What Claude Code must never do

- Read emails from the live IMAP server. All email content it works with must come from the `training_examples.body_preview` column or from `.eml` files already in `tests/fixtures/` (produced via `export-fixtures` and already redacted).
- Write code that touches IMAP outside of `imap_readonly.py`. The ruff TID rule enforces this; CI will reject any such change.
- Add any IMAP method that mutates state (move, copy, delete, flag, seen, expunge, append).
- Disable or weaken the section 4 layer 4 test.
- Commit unredacted email fixtures. The `redaction.py` module handles this; if a fixture contains obvious PII, it was bypassed and the commit should be rejected.
- Modify the `ReadOnlyMailbox` class to add new methods without an accompanying test and explicit human review.

### 14.3 Workflow: adding a new plugin

Trigger: `metrics --top-llm-senders=20` shows a sender consistently hitting stage 4.

1. `python -m email_concierge export-fixtures --sender-domain=<domain> --limit=5`. Produces redacted `.eml` files in `tests/fixtures/emails/<proposed_plugin_name>/`.
2. Inspect the fixtures. Identify the HTML structure or text patterns that reliably encode the event fields.
3. Create `email_concierge/extractors/plugins/<plugin_name>.py`. Implement `can_handle` (cheap sender/subject check) and `extract` (parse HTML/text, return `ExtractionResult` or `None`).
4. Create `tests/test_plugins/test_<plugin_name>.py` that walks the fixture directory and asserts expected extraction against `expected.json` siblings.
5. Create an `expected.json` for each fixture with the ground-truth extraction.
6. Run `pytest tests/test_plugins/test_<plugin_name>.py`. Iterate until green.
7. Run `python -m email_concierge evaluate --sample=100 --require-plugin=<plugin_name>` to verify the plugin doesn't disagree with the LLM on previously-seen emails from the same sender.
8. If agreement is high, open a PR.

### 14.4 Workflow: retraining the classifier

Trigger: `metrics --classifier` shows precision or recall has drifted below threshold, OR `training_examples` has grown by more than 20% since the last training run.

1. `python -m email_concierge train classifier --dry-run`. Prints expected cross-validation metrics without writing the model.
2. If metrics are better than the currently active model in `model_versions`, run without `--dry-run`. This writes the new artifact and marks it active.
3. Run `python -m email_concierge evaluate --sample=200` to check real-world behavior hasn't regressed. Classifier decisions should be similar to before except where there's a justified reason (more training data, etc.).
4. If the new model is clearly worse on real samples, revert by setting `is_active=1` on the prior row in `model_versions`.

### 14.5 Workflow: improving heuristic assembly

Trigger: `evaluate` shows stage 3 producing low-confidence results where stage 4 succeeds.

1. Identify the specific pattern the heuristic assembler is missing (e.g., "hotel name not being extracted when the address contains a comma").
2. Add a test case in `test_ner_extractor.py` that captures the failure.
3. Update the heuristic rules in `extractors/ner.py` or the entity prompt in `ml/ner_entities.py`.
4. Run the full test suite and `evaluate` before merging.

### 14.6 Safe-by-default mentality

When in doubt, Claude Code should assume an operation is forbidden and ask. The read-only guarantee is the single most important property of this system; it is worth more than any feature.

---

## 15. Deployment

Single-service `docker-compose.yaml` in the repo. Runs on the user's existing docker-compose VM.

```yaml
services:
  email-concierge:
    image: ghcr.io/<user>/email-concierge:latest   # or build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/data            # SQLite + trained models
    depends_on:
      - ollama                  # only include if Ollama is in this same compose file; otherwise use the LLM_BASE_URL to point at its network address
```

---

## 16. Open questions to resolve early

1. **Default LLM model.** `llama3.2:3b` is a reasonable default; `qwen2.5:7b` noticeably better on edge cases. Document the tradeoff in README.
2. **Calendar isolation.** Recommendation: write to a dedicated `auto-imported` calendar in Nextcloud, separate from primary. Makes bulk cleanup trivial during tuning.
3. **Inbox folder strategy.** Watch INBOX with a deny-list, or a dedicated folder? Support both; README recommends INBOX + deny-list.
4. **Backfill scope.** 15-year archive with an LLM-per-email is tens of thousands of calls. README should recommend a smaller initial window, scale up once the classifier is working.

---

## 17. Out-of-scope reminders for Claude Code

- Do not add a web UI.
- Do not add a job queue.
- Do not add a database migration framework. Idempotent `CREATE TABLE IF NOT EXISTS` only.
- Do not add user accounts or inbound HTTP.
- Do not depend on any LLM-vendor-specific SDK other than `openai` with custom `base_url`.
- Do not fine-tune a generative model in v1.0/v1.5.
- Do not invent iCal UIDs in stages other than stage 1. Let the CalDAV sink generate them.
- Do not implement any AdventureLog code. That belongs to the separate v2.0 document (docs/v2-adventurelog.md) and should only be considered after v1.5 ships.
- Do not import `imap_tools` outside of `imap_readonly.py`. Enforced by the ruff TID rule.
- Do not weaken, delete, or skip `tests/test_imap_readonly.py`.