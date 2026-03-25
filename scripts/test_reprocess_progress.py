#!/usr/bin/env python3
"""Test reprocess progress: login, click Reprocess, verify progress moves from 0%. Run with headed browser by default."""
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
HEADLESS = os.getenv("HEADLESS", "0").strip().lower() in ("1", "true", "yes")

def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    print("Browser: %s" % ("headless" if HEADLESS else "visible (headed)"))
    print("URL:", BASE_URL)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()
        try:
            print("Loading login/dashboard...")
            page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=15000)
            if "login" in page.url:
                print("Logging in...")
                page.fill('input[name="password"]', ADMIN_PASSWORD)
                page.click('button[type="submit"]')
                page.wait_for_url("**/", timeout=5000)
                time.sleep(0.5)
            page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=10000)
            btn = page.query_selector('#reprocess-form button[type="submit"]')
            if not btn:
                print("ERROR: Reprocess form/button not found")
                sys.exit(1)
            print("Clicking Reprocess requests...")
            btn.click()
            time.sleep(1.2)
            toast = page.query_selector('#reprocess-toast')
            if not toast:
                print("ERROR: Toast element not found")
                sys.exit(1)
            print("Polling for progress (0%% -> 1%%+ or Finished)...")
            last_pct = -1
            last_msg = ""
            for i in range(90):
                time.sleep(0.8)
                pct_el = page.query_selector('#reprocess-toast-pct')
                msg_el = page.query_selector('#reprocess-toast-message')
                pct_text = pct_el.inner_text() if pct_el else ""
                msg_text = msg_el.inner_text() if msg_el else ""
                try:
                    pct = int(pct_text.replace("%", "").strip())
                except (ValueError, AttributeError):
                    pct = 0
                if "Finished" in msg_text or "finished" in msg_text:
                    print("OK: Reprocess finished. Message:", msg_text[:80])
                    break
                if pct > 0:
                    print("OK: Progress moved to %s%%" % pct)
                    break
                if msg_text != last_msg or pct != last_pct:
                    print("  [%ds] %s | %s" % (i, pct_text, msg_text[:70]))
                    last_msg = msg_text
                    last_pct = pct
            else:
                print("TIMEOUT: Progress did not move from 0%% or finish in 90 polls")
                print("Last message:", last_msg[:100] if last_msg else "(none)")
                sys.exit(1)
            print("Test passed.")
        finally:
            time.sleep(1)
            browser.close()

if __name__ == "__main__":
    main()
