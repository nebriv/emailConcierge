"""Resolve Google Calendar ``plid`` web-UI tokens to Gmail thread IDs.

Google Calendar's auto-extracted events carry a ``source.url`` of the form
``https://mail.google.com/mail?extsrc=cal&plid=<token>``. The token is a
server-signed, opaque payload — it cannot be decoded offline to recover
the underlying Gmail thread ID. Feeding the plid to the Gmail REST API
returns 400.

The only known way to turn a plid into a REST-addressable ID is to load
the URL in an authenticated Gmail web session and watch where it
redirects. Gmail embeds ``msg-f:<decimal>`` permanent IDs throughout the
rendered thread DOM (download links, reply buttons, permalink anchors);
the decimal converts directly to the Gmail REST thread ID hex via
``format(int(decimal), 'x')``.

This module is the single authorized consumer of Selenium / undetected-
chromedriver. The ruff TID rule forbids browser-automation imports
anywhere else. It is used ONLY by the ``import-training --resolve-plids``
path — not by the live listener. Loading a plid URL in a browser session
marks the underlying email as read server-side, which is why this is
gated behind an explicit opt-in flag on an already-read training corpus.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from email_concierge.log import get_logger

if TYPE_CHECKING:  # pragma: no cover — type-only import
    import undetected_chromedriver as uc

log = get_logger(__name__)

_PERMMSGID_RE = re.compile(r"msg-f[:%3A]+(\d+)")

_GMAIL_LOGIN_URL = "https://mail.google.com/mail/u/0/"
_PLID_URL_TEMPLATE = "https://mail.google.com/mail?extsrc=cal&plid={plid}"

_DEFAULT_TIMEOUT_SECONDS = 20.0
_POLL_INTERVAL_SECONDS = 0.25

# With pageLoadStrategy='none' driver.get() returns the instant the HTTP
# request dispatches, so ensure_logged_in() used to prompt the user
# immediately — sometimes before Chrome had even started navigating,
# leaving them staring at a blank new-tab page with no obvious recourse
# but to Ctrl+C and rerun. This is how long we'll wait for current_url
# to show *any* google.com host (mail.google.com for warm profiles,
# accounts.google.com for the cold-login redirect) before giving up.
_NAV_WAIT_TIMEOUT_SECONDS = 15.0
_NAV_WAIT_POLL_INTERVAL = 0.5
# Gmail's SPA bootstrap does a redirect chain (auth → sync prompt → inbox
# shell → feed hydrate) that is unreliable to pin to any single event. The
# 'normal' strategy waits for document.readyState == 'complete' which never
# fires because Gmail keeps long-poll connections open. 'eager' waits for
# DOMContentLoaded which can fire on an intermediate redirect target,
# leaving Chrome aborted mid-chain when the page-load timeout expires — the
# user sees a blank welcome screen with no error.
#
# 'none' returns from driver.get() the instant the HTTP request dispatches.
# Chrome loads the page on its own, visible to the user. We don't need to
# block on any load event because the polling loop on current_url already
# tolerates partial loads, and the post-nav sleep gives the DOM time to
# hydrate before we scrape.
_PAGE_LOAD_STRATEGY = "none"

# Anti-fingerprinting pacing. Google flags sessions whose request rhythm
# looks mechanical (identical inter-request gaps, zero DOM interaction,
# scraping fires the instant navigation settles). These ranges give us
# human-ish variation without slowing the overall run to a crawl.
#   inter-call: gap between successive resolve() calls.
#   post-nav:   gap between URL stabilising and DOM scrape.
#   micro:      small gaps between individual synthetic actions.
_DEFAULT_INTER_CALL_DELAY_RANGE = (1.0, 5.0)
_DEFAULT_POST_NAV_DELAY_RANGE = (1.0, 3.0)
_MICRO_JITTER_RANGE = (0.2, 0.9)


def _safe_get(driver: Any, url: str) -> None:
    """``driver.get`` that tolerates a page-load timeout.

    With ``pageLoadStrategy='none'`` ``get`` should return instantly, but
    if a future change ever sets a stricter strategy or page-load timeout
    we'd get ``selenium.common.exceptions.TimeoutException`` back. The
    current_url polling loop the caller runs next tolerates partial
    loads, so swallowing the timeout is safe.
    """
    try:
        driver.get(url)
    except Exception as e:  # noqa: BLE001 — selenium dep is optional
        # We don't import selenium.common here (keeps the import graph
        # narrow) — match on class name instead. Only TimeoutException
        # is benign; all other selenium errors (NoSuchWindow, etc.)
        # re-raise and the caller's error boundary decides what to do.
        if type(e).__name__ == "TimeoutException":
            log.debug("plid_resolver_get_timeout", url=url, error=str(e))
            return
        raise


def _wait_for_host(driver: Any, host_fragment: str, timeout: float) -> bool:
    """Poll ``driver.current_url`` until it contains ``host_fragment``.

    Returns True on match, False on timeout. ``current_url`` access is
    wrapped in a broad except because a half-started chromedriver can
    raise WebDriverException briefly during Chrome bootstrap, and we'd
    rather keep polling than surface that to the caller.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = driver.current_url
            if host_fragment in (url or ""):
                return True
        except Exception as e:  # noqa: BLE001 — transient chromedriver noise
            log.debug("plid_resolver_current_url_failed", error=str(e))
        time.sleep(_NAV_WAIT_POLL_INTERVAL)
    return False


def _focus_window_matching(driver: Any, url_fragment: str) -> bool:
    """Switch to whichever window handle currently holds ``url_fragment``.

    undetected-chromedriver sometimes launches Chrome in a configuration
    that opens a secondary onboarding / welcome tab on a fresh profile.
    The user sees that tab — Chrome promotes it to front — while our
    automation is still pointing at the original tab behind it. If no
    handle matches, leave the current focus as-is and let the caller
    decide what to do.
    """
    try:
        handles = list(driver.window_handles)
    except Exception as e:  # noqa: BLE001 — defensive
        log.debug("plid_resolver_window_enum_failed", error=str(e))
        return False
    for handle in handles:
        try:
            driver.switch_to.window(handle)
            if url_fragment in driver.current_url:
                return True
        except Exception as e:  # noqa: BLE001 — handle can die mid-iter
            log.debug("plid_resolver_window_switch_failed", error=str(e))
            continue
    return False


class PlidResolver:
    """Selenium-backed plid → Gmail thread ID resolver.

    The resolver launches one persistent Chrome session against a user-
    owned profile directory. Login happens ONCE, interactively, the
    first time that profile is used; subsequent runs reuse the cookies
    the profile carries on disk.

    Usage:
        resolver = PlidResolver(profile_path=Path("/path/to/profile"))
        resolver.ensure_logged_in()  # one-time, prompts on stdin
        for plid in plids:
            thread_id = resolver.resolve(plid)
        resolver.close()
    """

    def __init__(
        self,
        profile_path: Path,
        *,
        chrome_major: int | None = None,
        headless: bool = False,
        inter_call_delay_range: tuple[float, float] = _DEFAULT_INTER_CALL_DELAY_RANGE,
        post_nav_delay_range: tuple[float, float] = _DEFAULT_POST_NAV_DELAY_RANGE,
        rng: random.Random | None = None,
    ) -> None:
        self._profile_path = Path(profile_path).resolve()
        self._chrome_major = chrome_major
        self._headless = headless
        self._driver: uc.Chrome | None = None
        self._inter_call_delay_range = inter_call_delay_range
        self._post_nav_delay_range = post_nav_delay_range
        # Injectable RNG so tests / ops can pin the sequence for
        # reproducibility without touching module-global state.
        self._rng = rng if rng is not None else random.Random()
        self._last_resolve_at: float | None = None

    def _ensure_driver(self) -> uc.Chrome:
        if self._driver is not None:
            return self._driver
        import undetected_chromedriver as uc  # noqa: PLC0415 — lazy import (optional dep)

        self._profile_path.mkdir(parents=True, exist_ok=True)
        log.info(
            "plid_resolver_launching",
            profile=str(self._profile_path),
            chrome_major=self._chrome_major,
            headless=self._headless,
        )
        options = uc.ChromeOptions()
        # See _PAGE_LOAD_STRATEGY note. This MUST be set before uc.Chrome
        # launches; setting it on the running driver has no effect.
        options.page_load_strategy = _PAGE_LOAD_STRATEGY
        self._driver = uc.Chrome(
            options=options,
            user_data_dir=str(self._profile_path),
            headless=self._headless,
            use_subprocess=True,
            version_main=self._chrome_major,
        )
        return self._driver

    def ensure_logged_in(self, *, prompt: bool = True) -> None:
        """Open Gmail and, if prompt=True, block on stdin until the user confirms.

        Call this ONCE at the start of a resolver session. On a fresh
        profile Chrome will land on the Google sign-in page; the user
        completes 2FA / unusual-sign-in challenges manually, then hits
        Enter in the terminal. On a warm profile the inbox loads
        straight away and the user can press Enter immediately.
        """
        driver = self._ensure_driver()
        log.info("plid_resolver_opening_inbox")
        for attempt in range(1, 3):
            _safe_get(driver, _GMAIL_LOGIN_URL)
            _focus_window_matching(driver, "mail.google.com")
            if _wait_for_host(driver, "google.com", _NAV_WAIT_TIMEOUT_SECONDS):
                log.info("plid_resolver_inbox_navigated", attempt=attempt)
                break
            log.warning(
                "plid_resolver_inbox_nav_stalled",
                attempt=attempt,
                timeout_seconds=_NAV_WAIT_TIMEOUT_SECONDS,
            )
        else:
            log.warning("plid_resolver_inbox_nav_failed_all_attempts")
        if prompt:
            input(
                "[plid-resolver] If Chrome shows the signed-in Gmail inbox, "
                "press Enter. Otherwise log in first, then press Enter."
            )

    def resolve(
        self, plid: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
    ) -> str | None:
        """Navigate to the plid URL; return the Gmail thread ID hex, or None.

        Returns None if the URL never redirects away from the raw plid
        form (expired token, signed-out session, network hiccup) or if
        no ``msg-f:<decimal>`` permanent ID appears in the resulting DOM.
        """
        if not plid:
            return None
        self._pace()
        driver = self._ensure_driver()
        plid_url = _PLID_URL_TEMPLATE.format(plid=plid)
        log.debug("plid_resolver_navigating", plid=plid)
        _safe_get(driver, plid_url)
        _focus_window_matching(driver, "mail.google.com")

        deadline = time.time() + timeout
        resolved = driver.current_url
        while time.time() < deadline and (
            "plid=" in resolved or resolved == plid_url
        ):
            time.sleep(_POLL_INTERVAL_SECONDS)
            resolved = driver.current_url

        if "plid=" in resolved:
            self._last_resolve_at = time.time()
            log.warning("plid_resolver_no_redirect", plid=plid, url=resolved)
            return None

        # Let Gmail finish rendering, then fake a little interaction before
        # scraping. Order matters: scroll/mousemove on a half-rendered DOM
        # can no-op, and scraping the DOM the instant redirect lands looks
        # like a bot. Humanise → sleep → scrape.
        self._humanize(driver)
        self._sleep_range(self._post_nav_delay_range)

        html = driver.page_source
        matches = _PERMMSGID_RE.findall(html)
        if not matches:
            self._last_resolve_at = time.time()
            log.warning(
                "plid_resolver_no_permmsgid",
                plid=plid,
                resolved_url=resolved,
            )
            return None

        # Gmail usually repeats the same msg-f:<id> many times. Multiple
        # distinct IDs only occur in multi-message threads; the thread ID
        # we want is the oldest (lowest decimal) one, which is the
        # thread's anchor message.
        decimals = sorted({int(m) for m in matches})
        thread_hex = format(decimals[0], "x")
        log.info(
            "plid_resolver_resolved",
            plid=plid,
            thread_id=thread_hex,
            distinct_permmsgids=len(decimals),
        )
        self._last_resolve_at = time.time()
        return thread_hex

    def _sleep_range(self, span: tuple[float, float]) -> None:
        low, high = span
        if high <= 0:
            return
        time.sleep(self._rng.uniform(max(low, 0.0), high))

    def _pace(self) -> None:
        """Block until the per-call minimum gap has elapsed.

        First call is a no-op (nothing to pace against). After that, we
        sleep out the remainder of a fresh random target drawn from
        ``inter_call_delay_range``. Measured gap is *wall clock*, so time
        spent inside the previous ``resolve`` counts — a slow plid won't
        cause a double wait.
        """
        if self._last_resolve_at is None:
            return
        low, high = self._inter_call_delay_range
        if high <= 0:
            return
        target = self._rng.uniform(max(low, 0.0), high)
        elapsed = time.time() - self._last_resolve_at
        remaining = target - elapsed
        if remaining > 0:
            log.debug("plid_resolver_pace", sleep_seconds=round(remaining, 2))
            time.sleep(remaining)

    def _humanize(self, driver: Any) -> None:
        """Dispatch a few organic-looking input events before DOM scrape.

        Random scroll, mousemove, another tiny scroll. Failures are
        deliberately swallowed — we're decorating, not gating: if the
        page doesn't accept a script eval (CSP, dead frame, whatever)
        we still want to fall through to the scrape.
        """
        try:
            scroll_y = self._rng.randint(80, 600)
            driver.execute_script("window.scrollBy(0, arguments[0]);", scroll_y)
            self._sleep_range(_MICRO_JITTER_RANGE)

            mouse_x = self._rng.randint(120, 900)
            mouse_y = self._rng.randint(120, 700)
            driver.execute_script(
                "document.dispatchEvent(new MouseEvent('mousemove', "
                "{clientX: arguments[0], clientY: arguments[1], bubbles: true}));",
                mouse_x,
                mouse_y,
            )
            self._sleep_range(_MICRO_JITTER_RANGE)

            # Short counter-scroll — matches a human skim-then-reread
            # pattern better than a single monotonic scroll.
            driver.execute_script(
                "window.scrollBy(0, arguments[0]);",
                -self._rng.randint(20, 150),
            )
        except Exception as e:  # noqa: BLE001 — best-effort humanisation
            log.debug("plid_resolver_humanize_failed", error=str(e))

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as e:  # noqa: BLE001 — teardown is best-effort
                log.warning("plid_resolver_close_failed", error=str(e))
            finally:
                self._driver = None

    def __enter__(self) -> PlidResolver:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
