#!/usr/bin/env python3
"""Run Flask with SQLite, then open /businesses in Playwright and report what we see."""
import os
import sys
import time
import threading

# Force SQLite and known password before any app import
os.environ["DATABASE_URL"] = "sqlite:///haro_test.db"
os.environ["ADMIN_PASSWORD"] = "changeme"

def run_app():
    from run import app
    app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)

if __name__ == "__main__":
    # Start Flask in background
    t = threading.Thread(target=run_app, daemon=True)
    t.start()
    time.sleep(2)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Install with: pip install playwright && playwright install chromium")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console", lambda msg: print("CONSOLE:", msg.text))
        try:
            # Go to businesses (will redirect to login)
            page.goto("http://127.0.0.1:5001/businesses", wait_until="networkidle", timeout=10000)
            print("URL after load:", page.url)
            print("Title:", page.title())
            print("Status: checking content...")

            # If we're on login, log in
            if "login" in page.url:
                page.fill('input[name="password"]', "changeme")
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")
                time.sleep(0.5)

            # Now go to businesses
            page.goto("http://127.0.0.1:5001/businesses", wait_until="networkidle", timeout=10000)
            print("URL now:", page.url)
            content = page.content()
            if "Internal Server Error" in content or "500" in content:
                print("ERROR: 500 or Internal Server Error on page!")
                # Get the body text to see traceback
                pre = page.query_selector("pre")
                if pre:
                    print(pre.inner_text()[:3000])
                else:
                    print(content[:2500])
                sys.exit(1)
            if "Error loading" in content or "Error saving" in content:
                print("Flash error on page - checking message")
                flashes = page.query_selector_all(".flash")
                for f in flashes:
                    print("Flash:", f.inner_text())
            # Try to save a business
            page.fill('input[name="name"]', "Test Business")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)
            content2 = page.content()
            if "Internal Server Error" in content2:
                print("ERROR: 500 after submitting business form!")
                pre = page.query_selector("pre")
                if pre:
                    print(pre.inner_text()[:3000])
                sys.exit(1)
            if "Business saved" in content2 or "Error saving" in content2:
                print("Submit result: flash message present")
            # Check table has a row
            rows = page.query_selector_all("table.data-table tbody tr")
            print("Table rows (excluding empty state):", len([r for r in rows if "empty-state" not in (r.get_attribute("class") or "") and "No businesses" not in (r.inner_text() or "")]))
            print("SUCCESS: Businesses page works.")
        except Exception as e:
            print("EXCEPTION:", e)
            import traceback
            traceback.print_exc()
            sys.exit(1)
        finally:
            browser.close()
