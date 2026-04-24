"""Layer-4 safety tests for the IMAP read-only wrapper.

CLAUDE.md section 4 requires defense in depth. These tests are the final
enforcement layer. If they fail, no change merges.

Three independent assertions:
1. Static: `imap_tools` is imported in exactly one file (imap_readonly.py).
2. Structural: ReadOnlyMailbox exposes no mutating method names.
3. Behavioral: the wrapper always calls upstream imap-tools with
   read-only flags (examine readonly=True, fetch mark_seen=False).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent / "email_concierge"

# Method names that imap-tools exposes for mutation. If any of these
# appear on ReadOnlyMailbox, the safety contract is broken.
MUTATING_METHODS = {
    "delete", "move", "copy", "append", "expunge",
    "seen", "flag", "unflag", "store",
}

# IMAP command verbs that modify server state. If the wrapper ever
# issued one of these at the protocol level, that's a failure.
MUTATING_IMAP_COMMANDS = {
    "STORE", "COPY", "MOVE", "APPEND", "EXPUNGE",
    "DELETE", "CREATE", "RENAME", "SUBSCRIBE", "UNSUBSCRIBE", "SETACL",
}


# ---------- Layer-4, part 1: static import check ----------

def test_imap_tools_imported_only_in_wrapper():
    """Belt-and-suspenders alongside the ruff TID rule in pyproject.toml.

    Scans every .py file in the package. `imap_tools` must appear as an
    import in exactly one file: imap_readonly.py.
    """
    pattern = re.compile(r"^\s*(from\s+imap_tools|import\s+imap_tools)", re.MULTILINE)
    offenders = []
    for py in PACKAGE_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(py.relative_to(PACKAGE_ROOT.parent))
    assert len(offenders) == 1, (
        f"imap_tools must only be imported from email_concierge/imap_readonly.py; "
        f"found in {offenders}"
    )
    assert offenders[0].name == "imap_readonly.py"


# ---------- Layer-4, part 2: structural check ----------

def test_readonly_mailbox_has_no_mutating_methods():
    """ReadOnlyMailbox must not expose any method that could mutate server state.

    This is the `class deliberately does NOT define...` guarantee from
    CLAUDE.md section 4.1.
    """
    from email_concierge.imap_readonly import ReadOnlyMailbox

    public_attrs = {
        name for name in dir(ReadOnlyMailbox) if not name.startswith("_")
    }
    leaked = public_attrs & MUTATING_METHODS
    assert not leaked, f"ReadOnlyMailbox must not expose mutating methods; found {leaked}"


def test_readonly_mailbox_public_surface_is_minimal():
    """Whitelist the public API. Any new public method must be considered
    carefully and added here deliberately.
    """
    from email_concierge.imap_readonly import ReadOnlyMailbox

    allowed = {"examine", "folder_list", "fetch", "idle_wait", "logout"}
    public = {
        name for name in dir(ReadOnlyMailbox)
        if not name.startswith("_") and callable(getattr(ReadOnlyMailbox, name))
    }
    extras = public - allowed
    assert not extras, (
        f"New public method on ReadOnlyMailbox detected: {extras}. "
        f"Every additional method expands the safety surface; add it to the allow-list "
        f"in tests/test_imap_readonly.py only with explicit review."
    )


# ---------- Layer-4, part 3: behavioral check ----------

class _FakeMailBox:
    """Stand-in for imap_tools.MailBox that records every method call.

    Upstream imap-tools exposes MailBox with attributes like `folder`,
    `fetch`, `idle`, `login`, `logout`. We mirror the shape just enough
    for ReadOnlyMailbox to drive it, and capture every invocation.
    """

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.calls: list[tuple[str, tuple, dict]] = []
        self.folder = MagicMock()
        self.folder.set = self._folder_set
        self.folder.list = self._folder_list
        self.idle = MagicMock()
        self.idle.wait = self._idle_wait

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def login(self, user, password, initial_folder=None):
        self._record("login", user, password, initial_folder=initial_folder)
        return self

    def _folder_set(self, folder, readonly=False):
        self._record("folder.set", folder, readonly=readonly)

    def _folder_list(self):
        self._record("folder.list")
        return []

    def fetch(self, criteria="ALL", mark_seen=True, bulk=False, limit=None, reverse=False):
        self._record(
            "fetch",
            criteria,
            mark_seen=mark_seen,
            bulk=bulk,
            limit=limit,
            reverse=reverse,
        )
        return iter(())

    def _idle_wait(self, timeout=None):
        self._record("idle.wait", timeout=timeout)
        return False

    def logout(self):
        self._record("logout")


@pytest.fixture
def fake_mailbox(monkeypatch):
    """Replace MailBox/MailBoxUnencrypted with recording fakes."""
    instances: list[_FakeMailBox] = []

    def _factory(host, port):
        fm = _FakeMailBox(host, port)
        instances.append(fm)
        return fm

    monkeypatch.setattr("email_concierge.imap_readonly.MailBox", _factory)
    monkeypatch.setattr("email_concierge.imap_readonly.MailBoxUnencrypted", _factory)
    return instances


def test_examine_always_uses_readonly_true(fake_mailbox):
    from email_concierge.imap_readonly import ReadOnlyMailbox

    mb = ReadOnlyMailbox("host", 993, "user", "pass", use_ssl=True)
    mb.examine("INBOX")
    mb.examine("Archive")

    fake = fake_mailbox[0]
    examine_calls = [c for c in fake.calls if c[0] == "folder.set"]
    assert len(examine_calls) == 2
    for _, _args, kwargs in examine_calls:
        assert kwargs.get("readonly") is True, (
            "folder.set must always be called with readonly=True "
            "(this is how imap-tools issues EXAMINE instead of SELECT)"
        )


def test_fetch_always_passes_mark_seen_false(fake_mailbox):
    from email_concierge.imap_readonly import ReadOnlyMailbox

    mb = ReadOnlyMailbox("host", 993, "user", "pass", use_ssl=True)
    list(mb.fetch("ALL"))
    list(mb.fetch("SINCE 01-Jan-2020", limit=10))

    fake = fake_mailbox[0]
    fetch_calls = [c for c in fake.calls if c[0] == "fetch"]
    assert len(fetch_calls) == 2
    for _, _args, kwargs in fetch_calls:
        assert kwargs.get("mark_seen") is False, (
            "fetch must always pass mark_seen=False to prevent the IMAP server "
            "from setting the \\Seen flag (which would mutate mailbox state)"
        )


def test_login_uses_initial_folder_none(fake_mailbox):
    """initial_folder=None prevents imap-tools from auto-SELECTing a folder on login."""
    from email_concierge.imap_readonly import ReadOnlyMailbox

    ReadOnlyMailbox("host", 993, "user", "pass", use_ssl=True)
    fake = fake_mailbox[0]
    login_calls = [c for c in fake.calls if c[0] == "login"]
    assert login_calls, "login should have been called exactly once"
    _, _args, kwargs = login_calls[0]
    assert kwargs.get("initial_folder") is None


def test_full_session_issues_no_mutating_methods(fake_mailbox):
    """Drive every ReadOnlyMailbox method and verify the recorded upstream calls
    contain nothing that could mutate server state."""
    from email_concierge.imap_readonly import ReadOnlyMailbox

    mb = ReadOnlyMailbox("host", 993, "user", "pass", use_ssl=True)
    mb.examine("INBOX")
    mb.folder_list()
    list(mb.fetch("ALL"))
    mb.idle_wait(1)
    mb.logout()

    fake = fake_mailbox[0]
    observed_method_names = {c[0].split(".")[-1].lower() for c in fake.calls}
    mutating = observed_method_names & MUTATING_METHODS
    assert not mutating, (
        f"ReadOnlyMailbox caused mutating upstream calls: {mutating}. "
        f"Full call log: {fake.calls}"
    )
