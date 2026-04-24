from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from email_concierge.commands import backfill as backfill_mod
from email_concierge.commands.backfill import _NullSink, _since_criteria


class FakeMailbox:
    """Context-manager stand-in for ReadOnlyMailbox.

    Records every call so tests can assert read-only behavior — no mutating
    methods exist here, mirroring the real wrapper's shape.
    """

    def __init__(self, emails: list, *, raise_on_fetch: bool = False) -> None:
        self._emails = emails
        self._raise = raise_on_fetch
        self.examined: list[str] = []
        self.fetch_calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def examine(self, folder: str) -> None:
        self.examined.append(folder)

    def fetch(self, *, criteria: str):
        self.fetch_calls.append(criteria)
        if self._raise:
            raise RuntimeError("boom")
        yield from self._emails


@pytest.fixture
def patched_mailbox(monkeypatch):
    """Swap _open_mailbox for a factory returning the caller's FakeMailbox."""

    holder: dict[str, FakeMailbox] = {}

    def _install(emails, **kwargs):
        mb = FakeMailbox(emails, **kwargs)
        holder["mb"] = mb

        @contextmanager
        def _fake_open(cfg):
            yield mb

        monkeypatch.setattr(backfill_mod, "_open_mailbox", lambda cfg: mb)
        return mb

    return _install


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point config at a tmp db + safe dummy IMAP/CalDAV values and reset cache."""
    monkeypatch.setenv("EMAIL_CONCIERGE_DB_PATH", str(tmp_path / "backfill.db"))
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_USERNAME", "user")
    monkeypatch.setenv("EMAIL_CONCIERGE_IMAP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_URL", "http://caldav.invalid/")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_USERNAME", "user")
    monkeypatch.setenv("EMAIL_CONCIERGE_CALDAV_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_CONCIERGE_DRY_RUN", "true")
    from email_concierge.config import settings

    settings.cache_clear()  # type: ignore[attr-defined]
    yield
    settings.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture
def stub_plugins(monkeypatch):
    """Replace plugin discovery with a no-op so backfill only runs ICS + LLM."""
    monkeypatch.setattr(backfill_mod, "discover_plugins", lambda: [])


@pytest.fixture
def stub_ics_extractor(monkeypatch, make_result):
    """Force IcsExtractor to return a canned result for every email.

    Swaps the symbol that `backfill.backfill_command` imports, not the class
    in extractors.ics — tests don't care about ICS parsing, only pipeline
    behavior.
    """
    class _AlwaysIcs:
        name = "ics"
        stage = 1
        priority = 0

        def can_handle(self, email) -> float:
            return 1.0

        def extract(self, email):
            return make_result(
                stage=1,
                name="ics",
                ical_uid=f"uid-{email.message_id}",
                title=f"Event for {email.subject}",
            )

    monkeypatch.setattr(backfill_mod, "IcsExtractor", _AlwaysIcs)
    return _AlwaysIcs


@pytest.fixture
def stub_llm_extractor(monkeypatch):
    """Stub out LLM extractor (never actually invoked in these tests, but its
    import requires an API key at construction time, so give it a harmless
    shape)."""
    class _NullLlm:
        name = "llm"
        stage = 4
        priority = 0

        def can_handle(self, email) -> float:
            return 0.0

        def extract(self, email):
            return None

    monkeypatch.setattr(backfill_mod, "LlmExtractor", _NullLlm)
    return _NullLlm


def test_since_criteria_formats_imap_date_and_widens_by_one_day():
    # 2025-06-15 → SINCE 14-Jun-2025 (widened so we don't miss boundary messages)
    since = datetime(2025, 6, 15, 12, 0, tzinfo=UTC)
    assert _since_criteria(since) == "SINCE 14-Jun-2025"


def test_null_sink_logs_and_returns_uid(make_result, make_email):
    sink = _NullSink()
    result = make_result(stage=1, name="ics", ical_uid="explicit-uid")
    email = make_email(message_id="<msg-1@x>")
    assert sink.write(result, email) == "explicit-uid"


def test_null_sink_generates_uid_when_result_has_none(make_result, make_email):
    sink = _NullSink()
    result = make_result(stage=1, ical_uid=None)
    email = make_email(message_id="<msg-2@x>")
    assert sink.write(result, email) == "backfill-<msg-2@x>"


def test_backfill_processes_emails_and_records_training_rows(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_ics_extractor,
    stub_llm_extractor,
    make_email,
):
    emails = [
        make_email(message_id="<a@x>", subject="A"),
        make_email(message_id="<b@x>", subject="B"),
    ]
    mb = patched_mailbox(emails)

    from email_concierge.commands.backfill import backfill_command

    rc = backfill_command(folder="Archive")
    assert rc == 0
    assert mb.examined == ["Archive"]
    assert len(mb.fetch_calls) == 1

    from email_concierge import db
    from email_concierge.config import settings

    conn = db.connect(settings().db_path)
    pm = conn.execute(
        "SELECT message_id, status FROM processed_messages ORDER BY message_id"
    ).fetchall()
    assert [row["message_id"] for row in pm] == ["<a@x>", "<b@x>"]
    assert all(row["status"] == "processed" for row in pm)

    te = conn.execute(
        "SELECT message_id, label, label_source FROM training_examples"
    ).fetchall()
    assert len(te) == 2
    assert all(row["label"] == "event" for row in te)
    assert all(row["label_source"] == "auto" for row in te)


def test_backfill_is_idempotent_on_rerun(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_ics_extractor,
    stub_llm_extractor,
    make_email,
):
    emails = [make_email(message_id="<dup@x>", subject="Only one")]
    patched_mailbox(emails)

    from email_concierge.commands.backfill import backfill_command

    assert backfill_command(folder="Archive") == 0
    # Rerun with the same data — expect no duplicate rows.
    patched_mailbox(emails)
    assert backfill_command(folder="Archive") == 0

    from email_concierge import db
    from email_concierge.config import settings

    conn = db.connect(settings().db_path)
    pm_count = conn.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"]
    te_count = conn.execute("SELECT COUNT(*) AS n FROM training_examples").fetchone()["n"]
    assert pm_count == 1
    assert te_count == 1


def test_backfill_respects_max_messages(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_ics_extractor,
    stub_llm_extractor,
    make_email,
):
    emails = [make_email(message_id=f"<m{i}@x>", subject=f"S{i}") for i in range(5)]
    patched_mailbox(emails)

    from email_concierge.commands.backfill import backfill_command

    assert backfill_command(folder="Archive", max_messages=2) == 0

    from email_concierge import db
    from email_concierge.config import settings

    conn = db.connect(settings().db_path)
    pm_count = conn.execute("SELECT COUNT(*) AS n FROM processed_messages").fetchone()["n"]
    assert pm_count == 2


def test_backfill_defaults_to_two_year_lookback(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_ics_extractor,
    stub_llm_extractor,
):
    mb = patched_mailbox([])

    from email_concierge.commands.backfill import backfill_command

    backfill_command(folder="Archive")
    assert len(mb.fetch_calls) == 1
    criteria = mb.fetch_calls[0]
    # Format is SINCE DD-Mon-YYYY; we don't pin the exact date (depends on
    # wall clock at test time) but it should look like a SINCE criterion.
    assert criteria.startswith("SINCE ")
    # Year should be ~today minus 2 years, minus the 1-day widen.
    expected_year = datetime.now(tz=UTC).year - 2
    assert str(expected_year) in criteria or str(expected_year - 1) in criteria


def test_backfill_defaults_to_null_sink(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_ics_extractor,
    stub_llm_extractor,
    monkeypatch,
    make_email,
):
    """Without --write-to-caldav, CaldavSink must never be constructed.

    Important: CaldavSink's __init__ hits the network even in dry-run mode
    unless dry_run is set; guarding this here makes backfill safe to run on
    machines with no reachable CalDAV server.
    """
    def _boom(*a, **kw):
        raise AssertionError("CaldavSink should not be constructed without --write-to-caldav")

    monkeypatch.setattr(backfill_mod, "CaldavSink", _boom)
    patched_mailbox([make_email(message_id="<x@x>")])

    from email_concierge.commands.backfill import backfill_command

    assert backfill_command(folder="Archive") == 0


def test_backfill_with_caldav_update_by_uid(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_ics_extractor,
    stub_llm_extractor,
    monkeypatch,
    make_email,
):
    """Second pass over the same UID should UPDATE, not duplicate.

    Simulates what would happen if the same booking re-lands in the folder —
    the CalDAV sink's update-by-UID path keeps calendar_events stable.
    """
    written: list[tuple[str, str]] = []

    class FakeCaldavSink:
        """In-memory update-by-UID sink. Deliberately avoids SQL so the test
        isolates the sink-level dedup behavior from the FK ordering between
        pipeline.sink_write and _record_processed."""

        def __init__(self, conn) -> None:
            self.by_uid: dict[str, str] = {}

        def write(self, result, email, *, account=None) -> str:
            uid = result.parsed.ical_uid or f"gen-{email.message_id}"
            self.by_uid[uid] = result.parsed.title  # overwrites on re-UID
            written.append((uid, email.message_id))
            return uid

    holder: dict[str, FakeCaldavSink] = {}

    def _make_sink(conn):
        sink = FakeCaldavSink(conn)
        holder["sink"] = sink
        return sink

    monkeypatch.setattr(backfill_mod, "CaldavSink", _make_sink)

    # Same Message-ID → pipeline-level dedup short-circuits, so we use two
    # Message-IDs but extractor returns the same UID. That's the exact
    # scenario update-by-UID is for: resent booking with a new Message-ID.
    class _FixedUid:
        name = "ics"
        stage = 1
        priority = 0

        def can_handle(self, email) -> float:
            return 1.0

        def extract(self, email):
            from email_concierge.models import ExtractionResult, ParsedEvent
            return ExtractionResult(
                handled_by_stage=1,
                handled_by_name="ics",
                confidence=1.0,
                parsed=ParsedEvent(
                    title=f"v-{email.subject}",
                    start=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                    ical_uid="same-uid",
                ),
                latency_ms=1,
            )

    monkeypatch.setattr(backfill_mod, "IcsExtractor", _FixedUid)

    emails = [
        make_email(message_id="<first@x>", subject="first"),
        make_email(message_id="<second@x>", subject="second"),
    ]
    patched_mailbox(emails)

    from email_concierge.commands.backfill import backfill_command

    assert backfill_command(folder="Archive", write_to_caldav=True) == 0

    sink = holder["sink"]
    assert list(sink.by_uid.keys()) == ["same-uid"]
    # Second write's title wins (update, not duplicate).
    assert sink.by_uid["same-uid"] == "v-second"
    assert len(written) == 2


def test_backfill_keeps_going_after_per_message_failure(
    isolated_settings,
    patched_mailbox,
    stub_plugins,
    stub_llm_extractor,
    monkeypatch,
    make_email,
):
    """A single exploding extractor result must not abort the whole run.

    The pipeline catches per-message exceptions and marks them 'failed';
    backfill must keep iterating the mailbox cursor after that.
    """
    call_count = {"n": 0}

    class _FlakyIcs:
        name = "ics"
        stage = 1
        priority = 0

        def can_handle(self, email) -> float:
            return 1.0

        def extract(self, email):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated parser crash")
            from email_concierge.models import ExtractionResult, ParsedEvent
            return ExtractionResult(
                handled_by_stage=1,
                handled_by_name="ics",
                confidence=1.0,
                parsed=ParsedEvent(
                    title="ok",
                    start=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
                ),
                latency_ms=1,
            )

    monkeypatch.setattr(backfill_mod, "IcsExtractor", _FlakyIcs)
    patched_mailbox([
        make_email(message_id="<crash@x>", subject="crash"),
        make_email(message_id="<ok@x>", subject="ok"),
    ])

    from email_concierge.commands.backfill import backfill_command

    assert backfill_command(folder="Archive") == 0

    from email_concierge import db
    from email_concierge.config import settings

    conn = db.connect(settings().db_path)
    rows = {
        r["message_id"]: r["status"]
        for r in conn.execute(
            "SELECT message_id, status FROM processed_messages"
        ).fetchall()
    }
    # Both messages reached the DB — crashed one is marked, healthy one processed.
    assert "<crash@x>" in rows
    assert "<ok@x>" in rows
    assert rows["<ok@x>"] == "processed"
