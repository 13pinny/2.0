"""Throw-away inspection: load CrowdVolt /selling, click Completed tab, dump
the rendered DOM so we can write proper selectors for the new page layout.
Delete this file once the scraper is updated."""
from playwright.sync_api import sync_playwright
from pathlib import Path

CDP_URL = "http://localhost:9222"
OUT = Path("debug/crowdvolt-completed-inspect.html")
SCREENSHOT = Path("debug/crowdvolt-completed-inspect.png")
OUT.parent.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    page = context.new_page()
    try:
        page.goto("https://www.crowdvolt.com/selling", wait_until="domcontentloaded", timeout=20000)
        # Let the SPA render
        page.wait_for_timeout(2500)
        # Click the "Completed" tab
        try:
            page.click("text=Completed", timeout=5000)
        except Exception as e:
            print(f"Couldn't click Completed: {e}")
        page.wait_for_timeout(3000)
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        OUT.write_text(page.content(), encoding="utf-8")
        print(f"Wrote DOM to {OUT}")
        print(f"Wrote screenshot to {SCREENSHOT}")
        print(f"Final URL: {page.url}")
    finally:
        page.close()
