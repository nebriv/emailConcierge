from __future__ import annotations

import signal
import sqlite3
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from email_concierge.commands.feedback import feedback_command
from email_concierge.config import settings
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
) -> None:
    """Main loop. Opens a read-only IMAP session, catches up on new mail,
    then enters IDLE. Reconnects with exponential backoff on failure.

    Stop by setting `stop_event` (the CLI wires this up to SIGTERM/SIGINT).
    """
    stop_event = stop_event or _install_signal_handler()
    cfg = settings()
    backoff = cfg.imap_reconnect_seconds
    feedback_state = _FeedbackState()

    while not stop_event.is_set():
        try:
            with _open_mailbox(cfg) as mb:
                mb.examine(cfg.imap_folder)
                log.info(
                    "listener_ready",
                    folder=cfg.imap_folder,
                    host=cfg.imap_host,
                    user=cfg.imap_username,
                )

                _catch_up(mb, extractors, sink, conn)
                _maybe_run_feedback(feedback_state)

                while not stop_event.is_set():
                    had_activity = mb.idle_wait(IDLE_TIMEOUT_SECONDS)
                    if stop_event.is_set():
                        break
                    if had_activity:
                        log.debug("idle_activity")
                    # On activity OR timeout, re-check recent mail. A
                    # re-check on timeout is cheap insurance against a
                    # missed IDLE notification.
                    _catch_up(mb, extractors, sink, conn)
                    _maybe_run_feedback(feedback_state)
            backoff = cfg.imap_reconnect_seconds
        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("listener_error", backoff_seconds=backoff)
            _sleep_with_stop(stop_event, backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_SECONDS)

    log.info("listener_stopped")


def _open_mailbox(cfg) -> ReadOnlyMailbox:
    return ReadOnlyMailbox(
        host=cfg.imap_host,
        port=cfg.imap_port,
        username=cfg.imap_username,
        password=cfg.imap_password,
        use_ssl=cfg.imap_use_ssl,
    )


def _catch_up(
    mb: ReadOnlyMailbox,
    extractors: Iterable[Extractor],
    sink: Sink,
    conn: sqlite3.Connection,
) -> None:
    """Fetch messages newer than the last processed one (or last hour on
    first run), run the pipeline on each. Dedup at the pipeline level makes
    this naturally idempotent.
    """
    since = _resume_from(conn)
    criteria = _since_criteria(since)
    log.debug("catch_up", criteria=criteria, since=since.isoformat())

    extractor_list = list(extractors)
    for email in mb.fetch(criteria=criteria):
        process_email(email, conn, extractor_list, sink, source="live")


def _resume_from(conn: sqlite3.Connection) -> datetime:
    row = conn.execute(
        "SELECT MAX(received_at) AS last FROM processed_messages"
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
