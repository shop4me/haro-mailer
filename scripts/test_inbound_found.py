#!/usr/bin/env python3
"""Test inbound found page: login, open /inbound-emails/7/found, verify query-only view and expand works."""
import os
import sys
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///haro.db")
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
BASE_URL = os.getenv("TEST_BASE_URL", "http://127.0.0.1:5000")
INBOUND_ID = int(os.getenv("INBOUND_ID", "7"))
HEADLESS = os.getenv("HEADLESS", "0").strip().lower() in ("1", "true", "yes")


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    url = "%s/inbound-emails/%s/found" % (BASE_URL, INBOUND_ID)
    print("Browser: %s" % ("headless" if HEADLESS else "visible (headed)"))
    print("URL:", url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        try:
            print("Loading login...")
            page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=15000)
            if "login" in page.url:
                print("Logging in...")
                page.fill('input[name="password"]', ADMIN_PASSWORD)
                page.click('button[type="submit"]')
                page.wait_for_url("**/", timeout=5000)
                time.sleep(0.5)
            print("Opening inbound found page...")
            page.goto(url, wait_until="networkidle", timeout=15000)
            time.sleep(0.5)

            # 1) Only query should be visible: .found-details must be hidden
            details = page.query_selector_all(".found-details")
            for i, el in enumerate(details):
                hidden = el.get_attribute("hidden")
                is_visible = el.is_visible()
                if is_visible:
                    print("FAIL: found-details[%s] is visible on load (should be hidden)." % i)
                    sys.exit(1)
            print("OK: All .found-details are hidden on load (query-only view).")

            # 2) Expand first button: click + and check details become visible
            expand_btns = page.query_selector_all(".found-expand-btn")
            if not expand_btns:
                print("WARN: No expand buttons found (no requests on this email?).")
            else:
                first_btn = expand_btns[0]
                first_block = page.query_selector(".found-request")
                first_details = first_block.query_selector(".found-details") if first_block else None
                if not first_details:
                    print("FAIL: No .found-details inside first .found-request")
                    sys.exit(1)
                first_btn.click()
                time.sleep(0.3)
                if first_details.get_attribute("hidden") is not None:
                    print("FAIL: After clicking +, .found-details still has hidden.")
                    sys.exit(1)
                if not first_details.is_visible():
                    print("FAIL: After clicking +, .found-details is not visible.")
                    sys.exit(1)
                print("OK: Click + expands details.")

                # Collapse again
                first_btn.click()
                time.sleep(0.2)
                if first_details.get_attribute("hidden") is None:
                    print("FAIL: After clicking -, .found-details should have hidden again.")
                    sys.exit(1)
                print("OK: Click - collapses details.")

            # 3) "Relevant & replied" section should appear before "Not relevant" if present
            body = page.content()
            idx_relevant = body.find("Relevant &amp; replied")
            idx_not = body.find("Not relevant")
            if idx_relevant >= 0 and idx_not >= 0 and idx_relevant > idx_not:
                print("FAIL: 'Not relevant' section appears before 'Relevant & replied' in HTML.")
                sys.exit(1)
            if idx_relevant >= 0:
                print("OK: 'Relevant & replied' section present and before 'Not relevant'.")
            print("Test passed.")
        finally:
            time.sleep(0.5)
            browser.close()


if __name__ == "__main__":
    main()
