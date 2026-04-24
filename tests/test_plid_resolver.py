"""Tests for the Selenium-backed plid resolver.

No real browser: we patch `undetected_chromedriver.Chrome` and hand back
a MagicMock driver. The point is to cover:
  - the redirect-wait loop terminates when `current_url` changes away
    from the raw plid form,
  - DOM scraping picks the lowest msg-f:<decimal> and converts it to hex,
  - resolution returns None cleanly when no permmsgid is present.

Importing `undetected_chromedriver` at test time is NOT required: the
module imports it lazily inside `_ensure_driver`. To keep tests running
without the optional dep, we patch the import at the import machinery
boundary.
"""

from __future__ import annotations

import random
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from email_concierge.integrations.google.plid_resolver import PlidResolver


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Swap ``time.sleep`` inside the resolver for a recording no-op.

    The resolver now sleeps in three places (inter-call pace, post-nav
    settle, per-action micro-jitter) plus its existing URL-poll loop.
    Making all of those instant keeps the suite fast; tests that care
    about *how much* we slept can inspect this mock directly.
    """
    mock = MagicMock()
    monkeypatch.setattr(
        "email_concierge.integrations.google.plid_resolver.time.sleep",
        mock,
    )
    return mock


@pytest.fixture
def fake_uc(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake `undetected_chromedriver` module and yield its Chrome mock."""
    fake_module = types.ModuleType("undetected_chromedriver")
    chrome_mock = MagicMock()
    fake_module.Chrome = chrome_mock  # type: ignore[attr-defined]
    # PlidResolver constructs `uc.ChromeOptions()` to set page_load_strategy
    # before launching. The fake module has to expose it or the import-graph
    # trick below fails at attribute-lookup time.
    fake_module.ChromeOptions = MagicMock()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "undetected_chromedriver", fake_module)
    return chrome_mock


class _FakeDriver:
    """Stand-in for uc.Chrome. `current_url` walks through `urls` on each read;
    the last entry is sticky for subsequent reads so `resolve` can wrap up.
    """

    def __init__(self, urls: list[str], page_source: str) -> None:
        self._urls = urls
        self._idx = 0
        self.page_source = page_source
        self.get = MagicMock()
        self.quit = MagicMock()
        self.set_page_load_timeout = MagicMock()
        self.execute_script = MagicMock()
        # Single-window default — the window-focus helper iterates handles
        # to find the one on mail.google.com; we just keep one alive.
        self.window_handles = ["win-0"]
        self.switch_to = MagicMock()

    @property
    def current_url(self) -> str:
        i = self._idx
        url = self._urls[min(i, len(self._urls) - 1)]
        self._idx = i + 1
        return url


def _driver_with_urls(urls: list[str], page_source: str) -> _FakeDriver:
    return _FakeDriver(urls, page_source)


def test_resolve_returns_thread_hex_from_permmsgid(fake_uc: MagicMock) -> None:
    # 1758661343626264203 decimal == 1868052c9b0dfe8b hex
    page_source = (
        "<a href='?ik=foo&th=msg-f:1758661343626264203'>download</a>"
        "<div data-msg-id='msg-f:1758661343626264203'>body</div>"
    )
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/?fs=1&source=cal#all/FMfcgzGrcjSVPdmrjgBlrLNSzhpzxHpk",
        ],
        page_source=page_source,
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    result = resolver.resolve("TOKEN", timeout=1.0)

    assert result == "1868052c9b0dfe8b"


def test_resolve_empty_plid_returns_none(fake_uc: MagicMock) -> None:
    """No driver launch should happen for an empty plid."""
    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    assert resolver.resolve("") is None
    fake_uc.assert_not_called()


def test_resolve_no_redirect_returns_none(fake_uc: MagicMock) -> None:
    """URL still carries `plid=` when the timeout elapses → no resolution."""
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
        ],
        page_source="<html>stuck</html>",
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    assert resolver.resolve("TOKEN", timeout=0.1) is None


def test_resolve_redirect_without_permmsgid_returns_none(fake_uc: MagicMock) -> None:
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/?fs=1#inbox",
        ],
        page_source="<html>no permmsgid anywhere</html>",
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    assert resolver.resolve("TOKEN", timeout=1.0) is None


def test_resolve_picks_lowest_decimal_when_multiple(fake_uc: MagicMock) -> None:
    """Multi-message threads sprinkle many IDs; the anchor (oldest) is the lowest."""
    page_source = (
        "msg-f:1758661343626264203 "  # the thread's first/oldest
        "msg-f:1758999999999999999 "  # a later reply
    )
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
        ],
        page_source=page_source,
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    # 1758661343626264203 → hex 1868052c9b0dfe8b
    assert resolver.resolve("TOKEN", timeout=1.0) == "1868052c9b0dfe8b"


def test_resolve_handles_url_encoded_permmsgid(fake_uc: MagicMock) -> None:
    """Gmail sometimes URL-encodes the colon as %3A in link hrefs."""
    page_source = "<a href='...msg-f%3A1758661343626264203...'>x</a>"
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
        ],
        page_source=page_source,
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    assert resolver.resolve("TOKEN", timeout=1.0) == "1868052c9b0dfe8b"


def _navigated_driver() -> MagicMock:
    """MagicMock driver whose current_url immediately satisfies the
    post-get() navigation wait (must contain 'google.com')."""
    driver = MagicMock()
    driver.current_url = "https://mail.google.com/mail/u/0/#inbox"
    return driver


def test_close_quits_driver_and_is_idempotent(fake_uc: MagicMock) -> None:
    driver = _navigated_driver()
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    resolver.ensure_logged_in(prompt=False)
    resolver.close()
    resolver.close()  # second call no-ops

    driver.quit.assert_called_once()


def test_close_swallows_driver_exception(fake_uc: MagicMock) -> None:
    driver = _navigated_driver()
    driver.quit.side_effect = RuntimeError("browser crashed")
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    resolver.ensure_logged_in(prompt=False)
    # Must not raise — teardown is best-effort.
    resolver.close()


def test_context_manager_closes(fake_uc: MagicMock) -> None:
    driver = _navigated_driver()
    fake_uc.return_value = driver

    with PlidResolver(profile_path=Path("/tmp/fake-profile")) as resolver:
        resolver.ensure_logged_in(prompt=False)

    driver.quit.assert_called_once()


def test_ensure_logged_in_opens_inbox(fake_uc: MagicMock) -> None:
    driver = _navigated_driver()
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    resolver.ensure_logged_in(prompt=False)

    # First attempt should succeed (current_url already matches), so get()
    # fires exactly once — the retry path is covered by a separate test.
    driver.get.assert_called_once()
    called_url = driver.get.call_args.args[0]
    assert "mail.google.com" in called_url


@patch("builtins.input", return_value="")
def test_ensure_logged_in_with_prompt_reads_stdin(
    mock_input: MagicMock, fake_uc: MagicMock
) -> None:
    driver = _navigated_driver()
    fake_uc.return_value = driver

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    resolver.ensure_logged_in(prompt=True)

    mock_input.assert_called_once()


def test_ensure_logged_in_retries_when_url_stalled(
    fake_uc: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If current_url never shows google.com, get() retries once and logs."""
    driver = MagicMock()
    driver.current_url = "about:blank"  # sticky — never navigates
    fake_uc.return_value = driver

    # Collapse the nav-wait deadline so the test doesn't spin on wall clock.
    monkeypatch.setattr(
        "email_concierge.integrations.google.plid_resolver._NAV_WAIT_TIMEOUT_SECONDS",
        0.01,
    )

    resolver = PlidResolver(profile_path=Path("/tmp/fake-profile"))
    resolver.ensure_logged_in(prompt=False)

    # Two attempts, both against the same URL.
    assert driver.get.call_count == 2
    assert all(
        c.args[0] == "https://mail.google.com/mail/u/0/"
        for c in driver.get.call_args_list
    )


def test_driver_launched_with_expected_args(
    fake_uc: MagicMock, tmp_path: Path
) -> None:
    driver = _navigated_driver()
    fake_uc.return_value = driver

    profile = tmp_path / "chrome-prof"
    resolver = PlidResolver(profile_path=profile, chrome_major=147, headless=False)
    resolver.ensure_logged_in(prompt=False)

    call = fake_uc.call_args
    assert call.kwargs["user_data_dir"] == str(profile.resolve())
    assert call.kwargs["version_main"] == 147
    assert call.kwargs["headless"] is False
    assert call.kwargs["use_subprocess"] is True
    assert profile.exists()


def test_resolve_humanize_fires_scroll_and_mousemove(fake_uc: MagicMock) -> None:
    """Resolver should inject organic interaction events before scraping."""
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
        ],
        page_source="msg-f:1758661343626264203",
    )
    fake_uc.return_value = driver

    # Deterministic RNG so we can assert on the exact pixel offsets.
    resolver = PlidResolver(
        profile_path=Path("/tmp/fake-profile"),
        rng=random.Random(0),
        inter_call_delay_range=(0.0, 0.0),
        post_nav_delay_range=(0.0, 0.0),
    )
    resolver.resolve("TOKEN", timeout=1.0)

    scripts = [call.args[0] for call in driver.execute_script.call_args_list]
    assert any("scrollBy" in s for s in scripts)
    assert any("MouseEvent" in s and "mousemove" in s for s in scripts)
    # Two scrolls (main + counter-scroll) and one mousemove = 3 scripts.
    assert len(scripts) == 3


def test_resolve_humanize_failure_does_not_block_resolution(
    fake_uc: MagicMock,
) -> None:
    """execute_script blowing up must not prevent the DOM scrape."""
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
        ],
        page_source="msg-f:1758661343626264203",
    )
    driver.execute_script.side_effect = RuntimeError("no frame")
    fake_uc.return_value = driver

    resolver = PlidResolver(
        profile_path=Path("/tmp/fake-profile"),
        inter_call_delay_range=(0.0, 0.0),
        post_nav_delay_range=(0.0, 0.0),
    )
    assert resolver.resolve("TOKEN", timeout=1.0) == "1868052c9b0dfe8b"


def test_pace_skipped_on_first_call(fake_uc: MagicMock, fast_sleep: MagicMock) -> None:
    """No prior timestamp → pace() must not sleep."""
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
        ],
        page_source="msg-f:1758661343626264203",
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(
        profile_path=Path("/tmp/fake-profile"),
        inter_call_delay_range=(5.0, 5.0),  # would be 5s if pace ran
        post_nav_delay_range=(0.0, 0.0),
    )
    resolver.resolve("TOKEN", timeout=1.0)

    # Any sleeps we see are poll-loop / micro-jitter only, never >=5s.
    assert all(call.args[0] < 5.0 for call in fast_sleep.call_args_list if call.args)


def test_pace_sleeps_between_consecutive_resolves(
    fake_uc: MagicMock, fast_sleep: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second resolve() should sleep the remainder of the inter-call gap."""
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN2",
            "https://mail.google.com/mail/u/0/#inbox/y",
        ],
        page_source="msg-f:1758661343626264203",
    )
    fake_uc.return_value = driver

    # Freeze wall clock so the "elapsed since last resolve" delta is 0
    # and pace has to sleep the full target.
    monkeypatch.setattr(
        "email_concierge.integrations.google.plid_resolver.time.time",
        lambda: 1000.0,
    )

    resolver = PlidResolver(
        profile_path=Path("/tmp/fake-profile"),
        inter_call_delay_range=(4.0, 4.0),  # pinned
        post_nav_delay_range=(0.0, 0.0),
    )
    resolver.resolve("TOKEN", timeout=1.0)
    fast_sleep.reset_mock()
    resolver.resolve("TOKEN2", timeout=1.0)

    pace_sleeps = [c.args[0] for c in fast_sleep.call_args_list if c.args and c.args[0] >= 3.5]
    assert pace_sleeps, f"expected pace() to sleep ~4s, saw: {fast_sleep.call_args_list}"
    assert pace_sleeps[0] == pytest.approx(4.0)


def test_pace_zero_range_disables_delay(
    fake_uc: MagicMock, fast_sleep: MagicMock
) -> None:
    """Tests/ops can disable pacing by passing (0, 0)."""
    driver = _driver_with_urls(
        urls=[
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN",
            "https://mail.google.com/mail/u/0/#inbox/x",
            "https://mail.google.com/mail?extsrc=cal&plid=TOKEN2",
            "https://mail.google.com/mail/u/0/#inbox/y",
        ],
        page_source="msg-f:1758661343626264203",
    )
    fake_uc.return_value = driver

    resolver = PlidResolver(
        profile_path=Path("/tmp/fake-profile"),
        inter_call_delay_range=(0.0, 0.0),
        post_nav_delay_range=(0.0, 0.0),
    )
    resolver.resolve("TOKEN", timeout=1.0)
    resolver.resolve("TOKEN2", timeout=1.0)

    # Only sub-second poll-loop sleeps should remain.
    assert all(
        (not c.args) or c.args[0] < 1.0 for c in fast_sleep.call_args_list
    )
