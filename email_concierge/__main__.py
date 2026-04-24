from __future__ import annotations

import argparse
import sys

from email_concierge import log as logmod
from email_concierge.config import settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email_concierge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Start the live IMAP listener")
    sub.add_parser("backfill", help="(not implemented in this phase)")
    sub.add_parser("train", help="(not implemented in this phase)")
    sub.add_parser("evaluate", help="(not implemented in this phase)")
    sub.add_parser("metrics", help="(not implemented in this phase)")
    sub.add_parser("export-fixtures", help="(not implemented in this phase)")

    args = parser.parse_args(argv)

    cfg = settings()
    logmod.configure(level=cfg.log_level, json_output=cfg.log_json)

    if args.command == "run":
        from email_concierge.commands.run import run_command

        return run_command()

    not_yet = {"backfill", "train", "evaluate", "metrics", "export-fixtures"}
    if args.command in not_yet:
        print(f"'{args.command}' is not implemented in this phase.", file=sys.stderr)
        return 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
