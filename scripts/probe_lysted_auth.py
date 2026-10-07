"""Capture the Authorization header Lysted's frontend sends to api.lysted.com."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import scraper
from patchright.sync_api import sync_playwright

captured = []
def handler(req):
    if "api.lysted.com/api/sales" in req.url:
        captured.append({"url": req.url, "headers": dict(req.headers)})

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.on("request", handler)
    page.goto("https://app.lysted.com/sales", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector("table.b-table tbody tr", timeout=15000)
    page.wait_for_timeout(2500)
    for c in captured:
        print(f"URL: {c['url']}")
        for k, v in c["headers"].items():
            if k.lower() in ("authorization", "x-api-key", "cookie", "accept", "x-requested-with"):
                print(f"  {k}: {v[:120]}{'...' if len(v) > 120 else ''}")
    page.close()
