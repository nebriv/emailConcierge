from __future__ import annotations

from email_concierge import db, listener
from email_concierge.config import settings
from email_concierge.extractors.ics import IcsExtractor
from email_concierge.extractors.llm import LlmExtractor
from email_concierge.log import get_logger
from email_concierge.sinks.caldav_sink import CaldavSink

log = get_logger(__name__)


def run_command() -> int:
    cfg = settings()
    log.info(
        "starting",
        dry_run=cfg.dry_run,
        folder=cfg.imap_folder,
        disable_llm=cfg.disable_llm,
    )

    conn = db.connect(cfg.db_path)
    db.init_schema(conn)

    # Phase 1: hard-coded extractor list. Phase 2 swaps this for
    # discover_plugins() + [IcsExtractor(), LlmExtractor()].
    extractors = [IcsExtractor(), LlmExtractor()]
    sink = CaldavSink(conn)

    try:
        listener.run(extractors, sink, conn)
    finally:
        conn.close()

    return 0
