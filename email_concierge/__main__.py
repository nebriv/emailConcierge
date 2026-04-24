from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from email_concierge import log as logmod
from email_concierge.config import settings


def _parse_date(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email_concierge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Start the live IMAP listener")

    bf = sub.add_parser(
        "backfill",
        help="Run the full pipeline over a historical IMAP folder (read-only)",
    )
    bf.add_argument(
        "--folder",
        required=True,
        help="IMAP folder to scan (e.g. INBOX, Archive)",
    )
    bf.add_argument(
        "--since",
        type=_parse_date,
        default=None,
        help="Only fetch messages received on-or-after this ISO date (default: 2 years ago)",
    )
    bf.add_argument(
        "--max",
        type=int,
        default=None,
        dest="max_messages",
        help="Stop after processing N messages (safety valve for huge archives)",
    )
    bf.add_argument(
        "--write-to-caldav",
        action="store_true",
        help="Write extracted events to CalDAV (off by default — training rows only)",
    )

    sub.add_parser("train", help="(not implemented in this phase)")
    sub.add_parser("evaluate", help="(not implemented in this phase)")
    sub.add_parser("metrics", help="(not implemented in this phase)")
    sub.add_parser("export-fixtures", help="(not implemented in this phase)")

    imp = sub.add_parser(
        "import-training",
        help="Import labeled (email, event) pairs from external sources",
    )
    imp.add_argument(
        "--from-google",
        action="store_true",
        required=True,
        help="Pull auto-extracted Google Calendar events + source Gmail messages",
    )
    imp.add_argument(
        "--since",
        type=_parse_date,
        default=None,
        help="Only import events starting after this ISO date (default: 2 years ago)",
    )
    imp.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N paired rows have been written",
    )
    imp.add_argument(
        "--resolve-plids",
        action="store_true",
        help=(
            "Resolve Google Calendar `plid=` web-UI permalinks via a browser "
            "session (requires the plid-resolver extras). Opens Chrome with a "
            "persistent profile; log in to Gmail once on first use. Note: "
            "loading a plid URL marks the email as read on the server, which "
            "is why this is opt-in and scoped to already-read training data."
        ),
    )

    args = parser.parse_args(argv)

    cfg = settings()
    logmod.configure(level=cfg.log_level, json_output=cfg.log_json)

    if args.command == "run":
        from email_concierge.commands.run import run_command

        return run_command()

    if args.command == "import-training":
        from email_concierge.commands.import_training import import_training_command

        return import_training_command(
            source="google",
            since=args.since,
            limit=args.limit,
            resolve_plids=args.resolve_plids,
        )

    if args.command == "backfill":
        from email_concierge.commands.backfill import backfill_command

        return backfill_command(
            folder=args.folder,
            since=args.since,
            max_messages=args.max_messages,
            write_to_caldav=args.write_to_caldav,
        )

    not_yet = {"train", "evaluate", "metrics", "export-fixtures"}
    if args.command in not_yet:
        print(f"'{args.command}' is not implemented in this phase.", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
