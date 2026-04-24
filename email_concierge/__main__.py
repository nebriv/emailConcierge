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
    sub = parser.add_subparsers(dest="command", required=False)

    sh = sub.add_parser(
        "shell",
        help=(
            "Interactive REPL with the listener running as a background "
            "thread. Default when no subcommand is given. Falls back to "
            "foreground listener when stdin is not a TTY."
        ),
    )
    sh.add_argument(
        "--no-listener",
        action="store_true",
        help="Drop into the REPL without starting the listener thread",
    )

    sub.add_parser("run", help="Start the live IMAP listener (foreground, no REPL)")

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
    bf.add_argument(
        "--account",
        default=None,
        help=(
            "Which configured account to backfill (name from "
            "EMAIL_CONCIERGE_ACCOUNTS). Default: the first account."
        ),
    )

    tr = sub.add_parser("train", help="Fit the Stage 3 event classifier from training_examples")
    tr.add_argument(
        "kind",
        nargs="?",
        default="classifier",
        choices=["classifier"],
        help="What to train (only 'classifier' supported today)",
    )
    tr.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute cross-validation metrics but don't persist the model",
    )

    ev = sub.add_parser(
        "evaluate",
        help="Replay recent training rows through all extractors and log disagreements",
    )
    ev.add_argument("--sample", type=int, default=100, help="Number of rows to sample")
    ev.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for reproducible samples (default: random)",
    )
    ev.add_argument(
        "--require-plugin",
        default=None,
        help="Only sample rows previously handled by this extractor name",
    )

    sub.add_parser(
        "feedback",
        help=(
            "Scan CalDAV for Concierge-written events deleted within the "
            "feedback window and flip the matching training rows to "
            "label='neither' (active learning signal)."
        ),
    )

    lbl = sub.add_parser(
        "label",
        help=(
            "Manually correct training_examples labels. Used to fix "
            "auto-labeled false positives that the feedback loop cannot "
            "see (e.g., rejected extractions that never hit CalDAV)."
        ),
    )
    lbl.add_argument(
        "--message-id",
        action="append",
        dest="message_ids",
        required=True,
        help="Message-ID to update. Repeat the flag to update multiple rows.",
    )
    lbl.add_argument(
        "--label",
        choices=["event", "neither"],
        required=True,
        help="New label to assign.",
    )
    lbl.add_argument(
        "--reason",
        default=None,
        help="Free-text note, logged for audit (not persisted to the row).",
    )
    lbl.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to the DB.",
    )

    wt = sub.add_parser(
        "watch",
        help=(
            "Tail recent pipeline activity from the local DB: one compact "
            "line per processed message, with per-status/stage filters "
            "and an optional summary mode."
        ),
    )
    wt.add_argument(
        "--since",
        default="15m",
        help="Time window: '15m', '2h', '1d', or ISO-8601 (default: 15m).",
    )
    wt.add_argument(
        "--status",
        default=None,
        help="Filter by status (processed, rejected, no_extraction, failed, ...).",
    )
    wt.add_argument(
        "--stage",
        type=int,
        default=None,
        help="Filter by handled_by_stage (1-4).",
    )
    wt.add_argument(
        "--account",
        default=None,
        help=(
            "Filter by configured account name (EMAIL_CONCIERGE_ACCOUNTS[i].name). "
            "Omit to see all accounts; column is auto-added when the window "
            "spans more than one."
        ),
    )
    wt.add_argument(
        "--follow",
        action="store_true",
        help="After the snapshot, poll for new rows every --interval seconds.",
    )
    wt.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Poll interval in seconds when --follow is set (default: 5).",
    )
    wt.add_argument(
        "--summary",
        action="store_true",
        help="Print counts-by-status instead of per-row tail.",
    )
    wt.add_argument(
        "--ids",
        action="store_true",
        dest="show_ids",
        help="Append the full Message-ID to each row (for mark-event/forget/label).",
    )

    me = sub.add_parser(
        "mark-event",
        help=(
            "Shorthand for `label --label=event`: flip a training_examples "
            "row to label='event'/label_source='manual'. Use when the "
            "pipeline missed an email that really was an event (false "
            "negative) so the next classifier train absorbs the fix."
        ),
    )
    me.add_argument(
        "message_ids",
        nargs="+",
        metavar="MESSAGE_ID",
        help="One or more Message-IDs to mark as positive training examples.",
    )
    me.add_argument(
        "--reason",
        default=None,
        help="Free-text note, logged for audit (not persisted to the row).",
    )
    me.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to the DB.",
    )

    fg = sub.add_parser(
        "forget",
        help=(
            "Drop a calendar_events row so the feedback scan won't flip "
            "its training label when the event disappears from CalDAV. "
            "Use when you want to delete an event without it counting as "
            "a negative training signal."
        ),
    )
    fg.add_argument("uid", help="iCal UID of the event to forget")
    fg.add_argument(
        "--delete-remote",
        action="store_true",
        help="Also delete the event from CalDAV (default: local row only)",
    )
    fg.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without writing anything",
    )

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

    # No subcommand → drop into the interactive shell. This is what the
    # Dockerfile's CMD relies on; locally `python -m email_concierge`
    # gets you the REPL too.
    if args.command is None or args.command == "shell":
        from email_concierge.commands.shell import shell_command

        start_listener = not getattr(args, "no_listener", False)
        return shell_command(start_listener=start_listener)

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
            account=args.account,
        )

    if args.command == "train":
        from email_concierge.commands.train import train_command

        return train_command(kind=args.kind, dry_run=args.dry_run)

    if args.command == "evaluate":
        from email_concierge.commands.evaluate import evaluate_command

        return evaluate_command(
            sample=args.sample,
            seed=args.seed,
            require_plugin=args.require_plugin,
        )

    if args.command == "feedback":
        from email_concierge.commands.feedback import feedback_command

        return feedback_command()

    if args.command == "label":
        from email_concierge.commands.label import label_command

        return label_command(
            message_ids=args.message_ids,
            label=args.label,
            reason=args.reason,
            dry_run=args.dry_run,
        )

    if args.command == "watch":
        from email_concierge.commands.watch import watch_command

        return watch_command(
            since=args.since,
            status=args.status,
            stage=args.stage,
            account=args.account,
            follow=args.follow,
            interval=args.interval,
            summary=args.summary,
            show_ids=args.show_ids,
        )

    if args.command == "mark-event":
        from email_concierge.commands.label import label_command

        return label_command(
            message_ids=args.message_ids,
            label="event",
            reason=args.reason,
            dry_run=args.dry_run,
        )

    if args.command == "forget":
        from email_concierge.commands.forget import forget_command

        return forget_command(
            uid=args.uid,
            delete_remote=args.delete_remote,
            dry_run=args.dry_run,
        )

    not_yet = {"metrics", "export-fixtures"}
    if args.command in not_yet:
        print(f"'{args.command}' is not implemented in this phase.", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
