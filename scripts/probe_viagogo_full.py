"""Dump full Viagogo Listings/Details HTML, looking for any numeric input fields."""
import sys, io, json, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import scraper
from patchright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = next((pg for pg in context.pages if "inv.viagogo.com" in (pg.url or "")), None)
    listing_id = page.evaluate("() => document.querySelector('tr[data-id]')?.getAttribute('data-id')")

    resp = page.request.post(
        "https://inv.viagogo.com/Listings/Details",
        form={"listingId": str(listing_id)},
    )
    body = resp.text()

    # Save full HTML
    with open("debug/viagogo-details.html", "w", encoding="utf-8") as f:
        f.write(body)
    print(f"Saved full HTML to debug/viagogo-details.html ({len(body)} bytes)")

    # Find all <label> tags + any <input> with numeric values nearby
    labels = re.findall(r'<label[^>]*>([^<]+)</label>', body)
    print(f"\n=== ALL LABELS ({len(labels)}) ===")
    for l in labels:
        print(f"  - {l.strip()[:80]}")
