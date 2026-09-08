"""Probe network requests on Lysted /sales to find the sales API endpoint."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import sys as _sys
_sys.path.insert(0, ".")
import scraper
from patchright.sync_api import sync_playwright

requests_seen = []

def handler(req):
    url = req.url
    if "lysted" in url and req.method in ("GET", "POST") and any(k in url.lower() for k in ("sale", "order", "transact", "graphql", "api")):
        requests_seen.append({
            "method": req.method,
            "url": url,
            "headers": dict(req.headers) if hasattr(req, "headers") else {},
            "post_data": req.post_data if req.post_data else None,
        })

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.on("request", handler)
    page.goto("https://app.lysted.com/sales", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector("table.b-table tbody tr", timeout=15000)
    page.wait_for_timeout(3000)
    print(f"Captured {len(requests_seen)} relevant requests:")
    for r in requests_seen:
        print(f"  [{r['method']}] {r['url'][:200]}")
        if r["post_data"]:
            print(f"     body: {r['post_data'][:300]}")
    page.close()
