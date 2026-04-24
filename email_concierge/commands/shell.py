"""shell — interactive REPL that also runs the listener as a daemon thread.

Container entrypoint. One process:
  - main thread drives the prompt
  - daemon thread runs the IMAP IDLE listener, sharing the same /data volume

Why a REPL rather than `docker compose run` per command: ops like watch,
mark-event, forget, label, feedback, train, evaluate all operate on the
same SQLite file. Running them in-process against the live listener's
state avoids both spinning up a second container per command and the
WAL-reader lag of a separate sqlite process.

Without a TTY (typical `docker compose up -d`), falls back to
listener-only foreground so the container is still useful in a purely
detached deployment.
"""

from __future__ import annotations

import argparse
import cmd
import shlex
import sys
import threading
from datetime import UTC, datetime

from email_concierge import db
from email_concierge.commands.evaluate import evaluate_command
from email_concierge.commands.feedback import feedback_command
from email_concierge.commands.forget import forget_command
from email_concierge.commands.label import label_command
from email_concierge.commands.run import run_command as _foreground_listener
from email_concierge.commands.train import train_command
from email_concierge.commands.watch import watch_command
from email_concierge.config import settings
from email_concierge.log import get_logger

log = get_logger(__name__)


def shell_command(*, start_listener: bool = True) -> int:
    """Entry point. Drops into the REPL when stdin is a TTY; otherwise
    runs the listener in the foreground (the `run` command's behavior)."""
    if not sys.stdin.isatty():
        log.info("shell_no_tty_foreground_listener")
        return _foreground_listener()

    shell = ConciergeShell(start_listener=start_listener)
    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        # Ctrl+C at the bare prompt — wind down cleanly.
        shell.stdout.write("\n")
        shell._stop_listener_quiet()
    return 0


def _listener_worker(stop_event: threading.Event) -> None:
    """Mirror of commands.run.run_command but driven by an external stop
    event so the shell can pause/resume the listener without exiting."""
    from email_concierge import listener
    from email_concierge.extractors.base import Extractor
    from email_concierge.extractors.discovery import discover_plugins
    from email_concierge.extractors.ics import IcsExtractor
    from email_concierge.extractors.llm import LlmExtractor
    from email_concierge.extractors.ner import NerEventExtractor
    from email_concierge.sinks.caldav_sink import CaldavSink

    cfg = settings()
    log.info(
        "listener_thread_starting",
        dry_run=cfg.dry_run,
        folder=cfg.imap_folder,
        disable_llm=cfg.disable_llm,
    )
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    plugins = discover_plugins()
    extractors: list[Extractor] = [
        IcsExtractor(),
        *plugins,
        NerEventExtractor(),
        LlmExtractor(),
    ]
    sink = CaldavSink(conn)
    try:
        listener.run(extractors, sink, conn, stop_event=stop_event)
    except Exception:  # noqa: BLE001
        log.exception("listener_thread_crashed")
    finally:
        conn.close()
        log.info("listener_thread_exited")


def _parse_date(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


class _ShellArgparseError(Exception):
    """Raised by our no-exit argparse subclass instead of sys.exit()."""


class _ShellArgParser(argparse.ArgumentParser):
    """argparse that raises instead of calling sys.exit — the REPL must
    survive bad arguments without taking the whole process down."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ShellArgparseError(message)


def _build_parsers() -> dict[str, _ShellArgParser]:
    parsers: dict[str, _ShellArgParser] = {}

    p = _ShellArgParser(prog="watch", add_help=True)
    p.add_argument("--since", default="15m")
    p.add_argument("--status", default=None)
    p.add_argument("--stage", type=int, default=None)
    p.add_argument("--follow", action="store_true")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--ids", action="store_true", dest="show_ids")
    parsers["watch"] = p

    p = _ShellArgParser(prog="forget", add_help=True)
    p.add_argument("uid")
    p.add_argument("--delete-remote", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    parsers["forget"] = p

    p = _ShellArgParser(prog="mark-event", add_help=True)
    p.add_argument("message_ids", nargs="+", metavar="MESSAGE_ID")
    p.add_argument("--reason", default=None)
    p.add_argument("--dry-run", action="store_true")
    parsers["mark_event"] = p

    p = _ShellArgParser(prog="label", add_help=True)
    p.add_argument("--message-id", action="append", dest="message_ids", required=True)
    p.add_argument("--label", choices=["event", "neither"], required=True)
    p.add_argument("--reason", default=None)
    p.add_argument("--dry-run", action="store_true")
    parsers["label"] = p

    p = _ShellArgParser(prog="train", add_help=True)
    p.add_argument("kind", nargs="?", default="classifier", choices=["classifier"])
    p.add_argument("--dry-run", action="store_true")
    parsers["train"] = p

    p = _ShellArgParser(prog="evaluate", add_help=True)
    p.add_argument("--sample", type=int, default=100)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--require-plugin", default=None)
    parsers["evaluate"] = p

    p = _ShellArgParser(prog="feedback", add_help=True)
    parsers["feedback"] = p

    return parsers


class ConciergeShell(cmd.Cmd):
    """Interactive REPL. Commands map 1:1 to the CLI subcommands so
    muscle memory transfers both ways."""

    intro = (
        "email-concierge interactive shell.\n"
        "  listener runs in the background — type `status` to check on it.\n"
        "  type `help` for commands, `help <cmd>` for usage, `exit` to quit.\n"
    )
    prompt = "emc> "

    def __init__(self, *, start_listener: bool = True) -> None:
        super().__init__()
        self._stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._parsers = _build_parsers()
        if start_listener:
            self._start_listener()

    # ---- lifecycle -------------------------------------------------

    def _start_listener(self) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            self.stdout.write("listener already running\n")
            return
        self._stop_event = threading.Event()
        self._listener_thread = threading.Thread(
            target=_listener_worker,
            args=(self._stop_event,),
            name="concierge-listener",
            daemon=True,
        )
        self._listener_thread.start()
        self.stdout.write("listener started\n")

    def _stop_listener(self) -> None:
        if not self._listener_thread:
            self.stdout.write("no listener running\n")
            return
        self.stdout.write("stopping listener...\n")
        self._stop_listener_quiet()
        self.stdout.write("listener stopped\n")

    def _stop_listener_quiet(self) -> None:
        if not self._listener_thread:
            return
        self._stop_event.set()
        self._listener_thread.join(timeout=10)
        if self._listener_thread.is_alive():
            # IDLE timeout can be up to 29min; we use daemon=True so it
            # dies with the process. 10s wait is a courtesy, not required.
            pass
        self._listener_thread = None

    # ---- dispatch shims --------------------------------------------

    def onecmd(self, line: str) -> bool:
        """Translate hyphens to underscores so `mark-event` and
        `mark_event` both resolve to do_mark_event."""
        stripped = line.lstrip()
        if stripped and not stripped.startswith("?"):
            parts = stripped.split(None, 1)
            parts[0] = parts[0].replace("-", "_")
            line = " ".join(parts)
        try:
            return super().onecmd(line)
        except KeyboardInterrupt:
            # A long-running command (e.g., watch --follow) was interrupted.
            self.stdout.write("\n")
            return False

    def emptyline(self) -> bool:
        # Default cmd.Cmd behavior is to repeat the last command, which
        # is surprising in an ops shell.
        return False

    def default(self, line: str) -> bool:
        self.stdout.write(f"unknown command: {line.split()[0]}. type 'help'.\n")
        return False

    def _parse(self, name: str, line: str):
        parser = self._parsers[name]
        try:
            return parser.parse_args(shlex.split(line))
        except _ShellArgparseError as e:
            self.stdout.write(f"{name}: {e}\n")
            self.stdout.write(parser.format_usage())
            return None
        except SystemExit:
            # --help prints and calls sys.exit; swallow it in the REPL.
            return None

    # ---- commands --------------------------------------------------

    def do_watch(self, line: str) -> bool:
        """Tail recent pipeline activity. Use --follow to stream (Ctrl+C to stop)."""
        args = self._parse("watch", line)
        if args is None:
            return False
        watch_command(
            since=args.since,
            status=args.status,
            stage=args.stage,
            follow=args.follow,
            interval=args.interval,
            summary=args.summary,
            show_ids=args.show_ids,
        )
        return False

    def do_forget(self, line: str) -> bool:
        """Drop a calendar_events row (optional --delete-remote)."""
        args = self._parse("forget", line)
        if args is None:
            return False
        forget_command(
            uid=args.uid,
            delete_remote=args.delete_remote,
            dry_run=args.dry_run,
        )
        return False

    def do_mark_event(self, line: str) -> bool:
        """Flip training rows to label='event' — for fixing false negatives."""
        args = self._parse("mark_event", line)
        if args is None:
            return False
        label_command(
            message_ids=args.message_ids,
            label="event",
            reason=args.reason,
            dry_run=args.dry_run,
        )
        return False

    def do_label(self, line: str) -> bool:
        """Manually set a training label (event|neither)."""
        args = self._parse("label", line)
        if args is None:
            return False
        label_command(
            message_ids=args.message_ids,
            label=args.label,
            reason=args.reason,
            dry_run=args.dry_run,
        )
        return False

    def do_train(self, line: str) -> bool:
        """Train the Stage 3 classifier from training_examples."""
        args = self._parse("train", line)
        if args is None:
            return False
        train_command(kind=args.kind, dry_run=args.dry_run)
        return False

    def do_evaluate(self, line: str) -> bool:
        """Replay recent training rows through all extractors, log disagreements."""
        args = self._parse("evaluate", line)
        if args is None:
            return False
        evaluate_command(
            sample=args.sample,
            seed=args.seed,
            require_plugin=args.require_plugin,
        )
        return False

    def do_feedback(self, line: str) -> bool:
        """Scan CalDAV for deletions within the feedback window."""
        args = self._parse("feedback", line)
        if args is None:
            return False
        feedback_command()
        return False

    def do_status(self, _line: str) -> bool:
        """Show listener thread state and DB counters."""
        alive = bool(self._listener_thread and self._listener_thread.is_alive())
        cfg = settings()
        conn = db.connect(cfg.db_path)
        try:
            db.init_schema(conn)
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM processed_messages"
            ).fetchone()["c"]
            last = conn.execute(
                "SELECT MAX(processed_at) AS t FROM processed_messages"
            ).fetchone()["t"]
            by_status = conn.execute(
                """SELECT status, COUNT(*) AS c
                     FROM processed_messages
                    WHERE processed_at > datetime('now', '-1 day')
                 GROUP BY status"""
            ).fetchall()
        finally:
            conn.close()
        self.stdout.write(f"listener:           {'RUNNING' if alive else 'STOPPED'}\n")
        self.stdout.write(f"db:                 {cfg.db_path}\n")
        self.stdout.write(f"processed_messages: {total} rows total\n")
        self.stdout.write(f"last processed:     {last or '(none)'}\n")
        if by_status:
            self.stdout.write("last 24h by status:\n")
            for row in by_status:
                self.stdout.write(f"  {row['status']:16s} {row['c']:5d}\n")
        return False

    def do_listener(self, line: str) -> bool:
        """listener [start|stop|restart] — manage the background listener thread."""
        action = (line or "").strip().lower()
        if action == "stop":
            self._stop_listener()
        elif action == "start":
            self._start_listener()
        elif action == "restart":
            self._stop_listener_quiet()
            self._start_listener()
        else:
            self.stdout.write("usage: listener [start|stop|restart]\n")
        return False

    def do_exit(self, _line: str) -> bool:
        """Stop the listener and exit the shell (terminates the container)."""
        self._stop_listener_quiet()
        return True

    def do_quit(self, line: str) -> bool:
        """Alias for exit."""
        return self.do_exit(line)

    def do_EOF(self, line: str) -> bool:  # noqa: N802 — cmd.Cmd protocol name
        """Ctrl+D — same as exit."""
        self.stdout.write("\n")
        return self.do_exit(line)


__all__ = ["shell_command", "ConciergeShell"]
