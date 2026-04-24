from __future__ import annotations

import signal
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from email_concierge.commands.feedback import feedback_command
from email_concierge.config import Account, settings
from email_concierge.extractors.base import Extractor
from email_concierge.imap_readonly import ReadOnlyMailbox
from email_concierge.log import get_logger
from email_concierge.pipeline import Sink, process_email

log = get_logger(__name__)


# IMAP servers typically force-close IDLE after 29 minutes. Stay inside that window.
IDLE_TIMEOUT_SECONDS = 29 * 60

# Exponential backoff caps at 5 minutes per CLAUDE.md section 13.
MAX_RECONNECT_SECONDS = 300


def run(
    extractors: Iterable[Extractor],
    sink: Sink,
    conn: sqlite3.Connection,
    stop_event: threading.Event | None = None,
    account: Account | None = None,
    run_feedback: bool = True,
) -> None:
    """Main loop for one IMAP account. Opens a read-only session, catches
    up on new mail, then enters IDLE. Reconnects with exponential backoff
    on failure.

    Stop by setting `stop_event` (the CLI wires this up to SIGTERM/SIGINT).

    If `account` is not provided, defaults to the first entry in
    `settings().accounts` — which itself synthesizes a single account
    from the legacy `imap_*` env vars if `EMAIL_CONCIERGE_ACCOUNTS` is
    unset. That keeps the single-mailbox call site (`run_command`)
    unchanged.

    When multiple listeners run in parallel (one per account), only one
    should run the CalDAV feedback scan — pass `run_feedback=False` on
    the others so we don't hit CalDAV N times per interval.
    """
    stop_event = stop_event or _install_signal_handler()
    cfg = settings()
    acct = account or cfg.accounts[0]
    backoff = cfg.imap_reconnect_seconds
    feedback_state = _FeedbackState()

    while not stop_event.is_set():
        try:
            with _open_mailbox(acct) as mb:
                mb.examine(acct.folder)
                log.info(
                    "listener_ready",
                    account=acct.name,
                    folder=acct.folder,
                    host=acct.host,
                    user=acct.username,
                )

                _catch_up(mb, extractors, sink, conn, acct)
                if run_feedback:
                    _maybe_run_feedback(feedback_state)

                while not stop_event.is_set():
                    had_activity = mb.idle_wait(IDLE_TIMEOUT_SECONDS)
                    if stop_event.is_set():
                        break
                    if had_activity:
                        log.debug("idle_activity", account=acct.name)
                    # On activity OR timeout, re-check recent mail. A
                    # re-check on timeout is cheap insurance against a
                    # missed IDLE notification.
                    _catch_up(mb, extractors, sink, conn, acct)
                    if run_feedback:
                        _maybe_run_feedback(feedback_state)
            backoff = cfg.imap_reconnect_seconds
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception(
                "listener_error", account=acct.name, backoff_seconds=backoff
            )
            _sleep_with_stop(stop_event, backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_SECONDS)

    log.info("listener_stopped", account=acct.name)


def _open_mailbox(acct: Account) -> ReadOnlyMailbox:
    return ReadOnlyMailbox(
        host=acct.host,
        port=acct.port,
        username=acct.username,
        password=acct.password,
        use_ssl=acct.use_ssl,
    )


def _catch_up(
    mb: ReadOnlyMailbox,
    extractors: Iterable[Extractor],
    sink: Sink,
    conn: sqlite3.Connection,
    account: Account,
) -> None:
    """Fetch messages newer than the last processed one (or last hour on
    first run), run the pipeline on each. Dedup at the pipeline level makes
    this naturally idempotent.

    Resume cursor is per-account: `WHERE account = ?`. Legacy rows with
    NULL account never drive the cursor for a named account — fine,
    message_id dedup handles the rare overlap.
    """
    since = _resume_from(conn, account.name)
    criteria = _since_criteria(since)
    log.debug(
        "catch_up", account=account.name, criteria=criteria, since=since.isoformat()
    )

    extractor_list = list(extractors)
    for email in mb.fetch(criteria=criteria):
        process_email(
            email, conn, extractor_list, sink, source="live", account=account.name
        )


def _resume_from(conn: sqlite3.Connection, account_name: str) -> datetime:
    row = conn.execute(
        "SELECT MAX(received_at) AS last FROM processed_messages WHERE account = ?",
        (account_name,),
    ).fetchone()
    raw = row["last"] if row else None
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            pass
    return datetime.now(tz=UTC) - timedelta(hours=1)


def _since_criteria(since: datetime) -> str:
    # IMAP SINCE takes DD-MMM-YYYY (day resolution). Widen by one day to
    # avoid time-of-day rounding cutting off recent messages; dedup
    # handles the overlap.
    d = (since - timedelta(days=1)).date()
    return f'SINCE {d.strftime("%d-%b-%Y")}'


def run_all_accounts(
    extractors: Iterable[Extractor],
    sink: Sink,
    conn: sqlite3.Connection,
    stop_event: threading.Event | None = None,
    accounts: Iterable[Account] | None = None,
) -> None:
    """Spawn one listener thread per configured account and block until
    the shared `stop_event` is set (or SIGTERM/SIGINT arrives).

    Used by both the foreground `run` command and the shell. Each thread
    calls `run(account=...)`; the first account also runs the CalDAV
    feedback scan so we don't hit CalDAV N times per interval.

    The threads are NOT daemons here — we join them on shutdown so pending
    writes can finish. Callers that need hard-exit semantics (e.g. the
    REPL) should wrap this differently; see `shell._listener_worker`.
    """
    stop_event = stop_event or _install_signal_handler()
    cfg = settings()
    accounts = list(accounts) if accounts is not None else cfg.accounts
    if not accounts:
        raise ValueError("no accounts configured")

    extractor_list = list(extractors)

    threads: list[threading.Thread] = []
    for i, acct in enumerate(accounts):
        t = threading.Thread(
            target=_run_one,
            args=(extractor_list, sink, conn, stop_event, acct, i == 0),
            name=f"concierge-listener-{acct.name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("listener_thread_spawned", account=acct.name)

    # Block the caller until stop_event fires. Thread join with a short
    # timeout so KeyboardInterrupt in the main thread is responsive.
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        stop_event.set()

    for t in threads:
        t.join(timeout=2.0)


def _run_one(
    extractors: list[Extractor],
    sink: Sink,
    conn: sqlite3.Connection,
    stop_event: threading.Event,
    account: Account,
    run_feedback: bool,
) -> None:
    try:
        run(
            extractors,
            sink,
            conn,
            stop_event=stop_event,
            account=account,
            run_feedback=run_feedback,
        )
    except Exception:  # noqa: BLE001
        log.exception("listener_thread_crashed", account=account.name)


def _install_signal_handler() -> threading.Event:
    stop_event = threading.Event()

    def _handler(_signum, _frame):
        log.info("shutdown_requested")
        stop_event.set()

    # Only install in the main thread; tests can pass their own event.
    try:
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        # Not running in main thread; caller should provide its own event.
        pass
    return stop_event


def _sleep_with_stop(stop_event: threading.Event, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not stop_event.is_set() and time.monotonic() < deadline:
        stop_event.wait(timeout=min(1.0, deadline - time.monotonic()))


class _FeedbackState:
    """Tracks when the feedback scan last ran, so it fires on an interval
    rather than on every IDLE wake.
    """

    __slots__ = ("last_run_monotonic",)

    def __init__(self) -> None:
        # None means "not yet run in this process" — first catch-up triggers it.
        self.last_run_monotonic: float | None = None


def _maybe_run_feedback(state: _FeedbackState) -> None:
    cfg = settings()
    interval_s = cfg.feedback_scan_interval_minutes * 60
    if interval_s <= 0:
        return  # disabled; operator prefers cron
    now = time.monotonic()
    if state.last_run_monotonic is not None:
        if (now - state.last_run_monotonic) < interval_s:
            return
    try:
        feedback_command()
    except Exception:  # noqa: BLE001 — feedback failure should never kill the listener
        log.exception("feedback_scan_failed")
    state.last_run_monotonic = now
