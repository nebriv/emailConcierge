"""plid → Gmail internal ID resolution spike.

Run this ONCE to verify that opening a Google Calendar plid URL in a
real Chrome session redirects to a URL containing the Gmail internal
message ID we can extract and hand to the Gmail REST API.

Usage:
    # 1. Pick a profile dir somewhere private (NOT in the repo)
    export EMAIL_CONCIERGE_CHROME_PROFILE=/path/to/email-concierge-chrome-profile

    # 2. Pick ONE plid you know about (e.g. from the debug log)
    export EMAIL_CONCIERGE_SPIKE_PLID=<plid-token-from-source-url>

    # 3. First run will open Chrome. Log in to Gmail normally.
    #    (2FA, "unusual sign-in" challenges, whatever — do it all.)
    #    Once Gmail inbox is visible, come back to the terminal and press Enter.

    # 4. Script will navigate to the plid URL and print the resolved URL.
    #    Rerunning later should skip login (profile is persistent).
    python scripts/plid_spike.py

Requires:  pip install undetected-chromedriver
           plus Chrome installed on the machine. undetected-chromedriver
           downloads its own patched chromedriver on first run.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

_PERMMSGID_RE = re.compile(r"msg-f[:%3A]+(\d+)")

try:
    import undetected_chromedriver as uc
except ImportError as e:
    sys.exit(f"failed to import undetected_chromedriver: {e!r}")


def main() -> int:
    profile = os.environ.get("EMAIL_CONCIERGE_CHROME_PROFILE")
    plid = os.environ.get("EMAIL_CONCIERGE_SPIKE_PLID")
    if not profile or not plid:
        print(
            "Set EMAIL_CONCIERGE_CHROME_PROFILE and EMAIL_CONCIERGE_SPIKE_PLID env vars.",
            file=sys.stderr,
        )
        return 2

    profile_path = Path(profile).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    # Non-headless on purpose — you need to be able to log in, solve
    # 2FA, and look at what's happening.
    # version_main: match your installed Chrome's major version.
    #   Check: chrome://version in the browser.
    #   Set EMAIL_CONCIERGE_CHROME_MAJOR=147 (or whatever) to override.
    chrome_major_env = os.environ.get("EMAIL_CONCIERGE_CHROME_MAJOR")
    chrome_major = int(chrome_major_env) if chrome_major_env else None

    print(
        f"[spike] launching undetected Chrome with profile: {profile_path} "
        f"(version_main={chrome_major})"
    )
    driver = uc.Chrome(
        user_data_dir=str(profile_path),
        headless=False,
        use_subprocess=True,
        version_main=chrome_major,
    )

    try:
        # Step 1: make sure we're logged in. Go to Gmail and wait for
        # the user to confirm login is complete.
        print("[spike] navigating to Gmail inbox…")
        driver.get("https://mail.google.com/mail/u/0/")
        input(
            "[spike] If Chrome opened to the signed-in inbox, press Enter. "
            "Otherwise log in first, then press Enter."
        )

        # Step 2: navigate to the plid URL and see what happens.
        plid_url = f"https://mail.google.com/mail?extsrc=cal&plid={plid}"
        print(f"[spike] navigating to: {plid_url}")
        driver.get(plid_url)

        # Wait up to 20s for the URL to change away from the raw plid
        # form. The redirect chain sets window.location.hash to
        # '#inbox/<id>' (or similar) once the thread is selected.
        deadline = time.time() + 20
        resolved = driver.current_url
        while time.time() < deadline and ("plid=" in resolved or resolved == plid_url):
            time.sleep(0.25)
            resolved = driver.current_url

        print(f"[spike] resolved URL: {resolved}")
        fragment = resolved.split("#", 1)[-1] if "#" in resolved else "(none)"
        print(f"[spike] URL fragment: {fragment}")

        # Scrape the DOM for `msg-f:<decimal>` — Gmail embeds this in
        # numerous places (download links, reply forms, permalink
        # buttons). The decimal converts directly to the REST API's
        # hex message ID.
        html = driver.page_source
        matches = _PERMMSGID_RE.findall(html)
        unique = sorted(set(matches), key=int)
        print(f"[spike] msg-f permmsgids found in DOM: {len(unique)}")
        for decimal_str in unique:
            decimal_id = int(decimal_str)
            hex_id = format(decimal_id, "x")
            print(f"[spike]   msg-f:{decimal_str}  ->  REST id: {hex_id}")

        # Let the user eyeball the browser state before we close it.
        input("[spike] Inspect Chrome if you like, then press Enter to close.")
    finally:
        driver.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
