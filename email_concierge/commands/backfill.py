"""backfill — run the pipeline over an IMAP archive folder.

Points the same router at a historical folder (e.g. "Archive") instead
of the live-inbox IDLE loop. Each message runs through stages 1-4,
writing a `training_examples` row as it goes. Those rows are what
Phase 5 trains the classifier on.

Strictly read-only — uses the same `ReadOnlyMailbox` wrapper the live
listener does, so the mailbox cannot be modified regardless of how the
pipeline behaves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from email_concierge import db
from email_concierge.config import Account, settings
from email_concierge.extractors.base import Extractor
from email_concierge.extractors.discovery import discover_plugins
from email_concierge.extractors.ics import IcsExtractor
from email_concierge.extractors.llm import LlmExtractor
from email_concierge.extractors.ner import NerEventExtractor
from email_concierge.imap_readonly import ReadOnlyMailbox
from email_concierge.log import get_logger
from email_concierge.pipeline import Sink, process_email
from email_concierge.sinks.caldav_sink import CaldavSink

log = get_logger(__name__)


class _NullSink:
    """Drop-in Sink used when --no-write is set. Records nothing, logs
    the would-be UID so backfill runs can be dry without touching CalDAV."""

    def write(self, result, email, *, account=None) -> str:  # type: ignore[no-untyped-def]
        uid = result.parsed.ical_uid or f"backfill-{email.message_id}"
        log.info(
            "backfill_would_write",
            uid=uid,
            title=result.parsed.title,
            message_id=email.message_id,
            account=account,
        )
        return uid


def backfill_command(
    *,
    folder: str,
    since: datetime | None = None,
    max_messages: int | None = None,
    write_to_caldav: bool = False,
    account: str | None = None,
) -> int:
    """Run the live pipeline over a historical IMAP folder.

    Args:
      folder: IMAP folder name (e.g. "Archive", "INBOX").
      since:  only fetch messages received on-or-after this datetime.
              Default: 2 years ago (matches the import-training default).
      max_messages: cap total messages processed (safety valve for
              first-time runs on huge archives).
      write_to_caldav: if True, write extracted events to CalDAV via
              the same sink the live listener uses. Default False — we
              usually only care about labeled training rows, not about
              backfilling the calendar with years of old events.
    """
    cfg = settings()
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    acct = _resolve_account(cfg, account)

    if since is None:
        since = datetime.now(tz=UTC) - timedelta(days=365 * 2)

    plugins = discover_plugins()
    log.info("plugins_loaded", names=[p.name for p in plugins])
    extractors: list[Extractor] = [
        IcsExtractor(),
        *plugins,
        NerEventExtractor(),  # stage 3 — no-op if ml extras missing
        LlmExtractor(),
    ]

    sink: Sink
    if write_to_caldav:
        sink = CaldavSink(conn)
    else:
        sink = _NullSink()

    criteria = _since_criteria(since)
    log.info(
        "backfill_started",
        account=acct.name,
        folder=folder,
        since=since.isoformat(),
        criteria=criteria,
        max_messages=max_messages,
        write_to_caldav=write_to_caldav,
        disable_llm=cfg.disable_llm,
    )
    if not cfg.disable_llm and (max_messages is None or max_messages > 500):
        # Huge archives + LLM stage 4 = many thousands of API calls. Flag it
        # loudly; users who actually want that can ignore the warning.
        log.warning(
            "backfill_llm_enabled_and_large",
            hint=(
                "Stage 4 LLM fallback is enabled. Set EMAIL_CONCIERGE_DISABLE_LLM=true "
                "or pass --max to limit LLM call volume on big archives."
            ),
        )

    n_seen = 0
    n_processed = 0
    n_dedup = 0
    try:
        with _open_mailbox(acct) as mb:
            mb.examine(folder)
            for email in mb.fetch(criteria=criteria):
                n_seen += 1
                status = process_email(
                    email, conn, extractors, sink, source="backfill", account=acct.name
                )
                if status == "skipped_dedup":
                    n_dedup += 1
                else:
                    n_processed += 1
                if max_messages is not None and n_seen >= max_messages:
                    log.info("backfill_max_reached", max_messages=max_messages)
                    break
    finally:
        log.info(
            "backfill_done",
            account=acct.name,
            seen=n_seen,
            processed=n_processed,
            skipped_dedup=n_dedup,
        )

    return 0


def _resolve_account(cfg, name: str | None) -> Account:  # type: ignore[no-untyped-def]
    accounts = cfg.accounts
    if name is None:
        return accounts[0]
    for a in accounts:
        if a.name == name:
            return a
    raise ValueError(
        f"account {name!r} not found; configured accounts: "
        f"{[a.name for a in accounts]}"
    )


def _open_mailbox(acct: Account) -> ReadOnlyMailbox:
    return ReadOnlyMailbox(
        host=acct.host,
        port=acct.port,
        username=acct.username,
        password=acct.password,
        use_ssl=acct.use_ssl,
    )


def _since_criteria(since: datetime) -> str:
    # IMAP SINCE only supports day resolution; widen by one day so we
    # don't miss messages at the boundary. The pipeline's Message-ID
    # dedup handles the overlap.
    d = (since - timedelta(days=1)).date()
    return f'SINCE {d.strftime("%d-%b-%Y")}'
