"""
ezproxy_login.py
----------------
One-time interactive script to log in to Penn Libraries via EZproxy + PennKey SSO.
Saves session cookies to ezproxy_cookies.json for reuse by fetch_fulltext.py.

Run this whenever your session expires (typically lasts 8-24 hours):

    python ezproxy_login.py
"""

import json
import time
from playwright.sync_api import sync_playwright

COOKIE_FILE = "ezproxy_cookies.json"
TEST_URL = "https://proxy.library.upenn.edu/login?url=https://www.sciencedirect.com"


def main():
    print("Opening browser for Penn EZproxy login...")
    print("Complete the PennKey login + DUO 2FA in the browser window.")
    print("The script will continue automatically once you're logged in.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto(TEST_URL)

        # Wait until we're off all auth/login pages
        print("Waiting for you to complete login...")
        page.wait_for_url(
            lambda url: (
                "pennkey" not in url
                and "okta" not in url
                and "idp." not in url
                and "login" not in url
            ),
            timeout=300_000,  # 5 minutes
        )

        # Wait for the page and all redirects to fully settle
        page.wait_for_load_state("networkidle")

        # Poll for the EZproxy session cookie (up to 10s)
        cookies = []
        for _ in range(20):
            cookies = ctx.cookies()
            if any("ezproxy" in c["name"].lower() for c in cookies):
                break
            time.sleep(0.5)

        if not any("ezproxy" in c["name"].lower() for c in cookies):
            print("\n[!] EZproxy session cookie not detected.")
            print("    The login may not have completed. Try running again.")
            print(f"    Current URL: {page.url}")
            print(f"    Cookies found: {[c['name'] for c in cookies]}")
        else:
            with open(COOKIE_FILE, "w") as f:
                json.dump(cookies, f, indent=2)
            print(f"\nLogged in. Current URL: {page.url}")
            print(f"Saved {len(cookies)} cookies to {COOKIE_FILE}.")
            print(f"EZproxy cookies: {[c['name'] for c in cookies if 'ezproxy' in c['name'].lower()]}")

        browser.close()


if __name__ == "__main__":
    main()
