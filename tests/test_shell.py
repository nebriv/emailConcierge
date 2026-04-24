"""Tests for the interactive shell.

Focus is the REPL mechanics — dispatch, arg parsing, listener thread
lifecycle — not re-testing the command implementations (those are
covered individually). The listener worker is stubbed throughout so no
IMAP/CalDAV/LLM calls are made.
"""

from __future__ import annotations

import io
import threading
from unittest.mock import patch

import pytest

from email_concierge.commands.shell import ConciergeShell, _build_parsers


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_CONCIERGE_DB_PATH", str(tmp_path / "shell.db"))
    monkeypatch.setenv("EMAIL_CONCIERGE_DRY_RUN", "true")
    monkeypatch.setenv("EMAIL_CONCIERGE_DISABLE_LLM", "true")
    from email_concierge.config import settings as settings_fn
    settings_fn.cache_clear()
    yield tmp_path
    settings_fn.cache_clear()


def _make_shell(**kwargs) -> ConciergeShell:
    """Always disable the listener in tests — we don't want to touch IMAP."""
    kwargs.setdefault("start_listener", False)
    s = ConciergeShell(**kwargs)
    s.stdout = io.StringIO()
    return s


# ---- argparse shim --------------------------------------------------


def test_parsers_reject_unknown_flag_without_exiting(isolated_settings):
    s = _make_shell()
    # An unparseable arg should NOT call sys.exit — that would kill the REPL.
    ok = s.onecmd("forget --nope")
    assert ok is False  # False = "don't exit the shell"
    out = s.stdout.getvalue()
    assert "forget" in out.lower()


def test_parsers_accept_valid_args(isolated_settings):
    # Verify the parsers are well-formed: watch --follow flag is bool, etc.
    parsers = _build_parsers()
    args = parsers["watch"].parse_args(["--since=5m", "--follow", "--ids"])
    assert args.since == "5m"
    assert args.follow is True
    assert args.show_ids is True


# ---- dispatch -------------------------------------------------------


def test_hyphen_and_underscore_both_resolve(isolated_settings):
    s = _make_shell()
    calls: list[str] = []

    def fake_label(*, message_ids, label, reason, dry_run):
        calls.append(label)
        return 0

    with patch("email_concierge.commands.shell.label_command", side_effect=fake_label):
        s.onecmd("mark-event '<foo@x>'")
        s.onecmd("mark_event '<bar@x>'")
    assert calls == ["event", "event"]


def test_unknown_command_prints_hint(isolated_settings):
    s = _make_shell()
    s.onecmd("nonsense --flag")
    assert "unknown command: nonsense" in s.stdout.getvalue()


def test_empty_line_does_not_repeat_last(isolated_settings):
    s = _make_shell()
    assert s.emptyline() is False
    # and doesn't call any command
    assert s.stdout.getvalue() == ""


# ---- commands -------------------------------------------------------


def test_watch_forwards_args(isolated_settings):
    s = _make_shell()
    seen: dict = {}

    def fake_watch(**kwargs):
        seen.update(kwargs)
        return 0

    with patch("email_concierge.commands.shell.watch_command", side_effect=fake_watch):
        s.onecmd("watch --since=30m --stage=2 --ids")
    assert seen["since"] == "30m"
    assert seen["stage"] == 2
    assert seen["show_ids"] is True
    assert seen["follow"] is False


def test_forget_forwards_args(isolated_settings):
    s = _make_shell()
    seen: dict = {}

    def fake_forget(**kwargs):
        seen.update(kwargs)
        return 0

    with patch("email_concierge.commands.shell.forget_command", side_effect=fake_forget):
        s.onecmd("forget some-uid --delete-remote")
    assert seen["uid"] == "some-uid"
    assert seen["delete_remote"] is True
    assert seen["dry_run"] is False


def test_mark_event_always_passes_label_event(isolated_settings):
    s = _make_shell()
    seen: dict = {}

    def fake_label(**kwargs):
        seen.update(kwargs)
        return 0

    with patch("email_concierge.commands.shell.label_command", side_effect=fake_label):
        s.onecmd("mark-event '<a@x>' '<b@x>'")
    assert seen["label"] == "event"
    assert seen["message_ids"] == ["<a@x>", "<b@x>"]


def test_label_requires_message_id_and_label(isolated_settings):
    s = _make_shell()
    with patch("email_concierge.commands.shell.label_command") as mock_label:
        # Missing required flags: argparse error, no call made.
        s.onecmd("label")
        mock_label.assert_not_called()
        # Valid form: call through.
        s.onecmd("label --message-id='<a@x>' --label=neither")
        mock_label.assert_called_once()


# ---- listener lifecycle ---------------------------------------------


def test_start_and_stop_listener_uses_daemon_thread(isolated_settings):
    """The listener worker must not block the REPL — daemon thread, and
    the stop_event must wire through."""
    start_signal = threading.Event()
    stop_seen = threading.Event()

    def fake_worker(stop_event):
        start_signal.set()
        # Block until told to stop; test the stop path.
        stop_event.wait(timeout=5)
        stop_seen.set()

    with patch("email_concierge.commands.shell._listener_worker", side_effect=fake_worker):
        s = _make_shell(start_listener=False)
        s._start_listener()
        assert start_signal.wait(timeout=2), "worker should start promptly"
        assert s._listener_thread is not None
        assert s._listener_thread.daemon is True
        assert s._listener_thread.is_alive()

        s._stop_listener_quiet()
        assert stop_seen.wait(timeout=2), "stop_event should propagate"
        assert s._listener_thread is None


def test_status_shows_running_state(isolated_settings):
    from email_concierge import db

    # Ensure the DB exists so the status query doesn't fail.
    conn = db.connect(isolated_settings / "shell.db")
    db.init_schema(conn)
    conn.close()

    s = _make_shell(start_listener=False)
    # Fake a running thread so status reports RUNNING.
    s._listener_thread = threading.Thread(target=lambda: threading.Event().wait(1), daemon=True)
    s._listener_thread.start()
    s.onecmd("status")
    out = s.stdout.getvalue()
    assert "RUNNING" in out
    assert "processed_messages:" in out


def test_status_with_no_listener_reports_stopped(isolated_settings):
    from email_concierge import db
    conn = db.connect(isolated_settings / "shell.db")
    db.init_schema(conn)
    conn.close()

    s = _make_shell(start_listener=False)
    s.onecmd("status")
    assert "STOPPED" in s.stdout.getvalue()


def test_exit_stops_listener_and_returns_true(isolated_settings):
    s = _make_shell(start_listener=False)
    stopped = threading.Event()

    def fake_worker(stop_event):
        stop_event.wait(timeout=5)
        stopped.set()

    with patch("email_concierge.commands.shell._listener_worker", side_effect=fake_worker):
        s._start_listener()
        assert s.do_exit("") is True
        assert stopped.wait(timeout=2)


def test_listener_restart_spawns_new_thread(isolated_settings):
    s = _make_shell(start_listener=False)

    def fake_worker(stop_event):
        stop_event.wait(timeout=5)

    with patch("email_concierge.commands.shell._listener_worker", side_effect=fake_worker):
        s._start_listener()
        t1 = s._listener_thread
        s.onecmd("listener restart")
        t2 = s._listener_thread
        assert t1 is not t2
        assert t2 is not None and t2.is_alive()
        s._stop_listener_quiet()
