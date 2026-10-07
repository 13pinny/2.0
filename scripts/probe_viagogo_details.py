"""Hit /Listings/Details directly and dump full payload to find cost field."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import scraper
from patchright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = next((pg for pg in context.pages if "inv.viagogo.com" in (pg.url or "")), None)
    if not page:
        page = context.new_page()
        page.goto("https://inv.viagogo.com/Listings", wait_until="domcontentloaded", timeout=15000)

    listing_id = page.evaluate("() => document.querySelector('tr[data-id]')?.getAttribute('data-id')")
    print("Probing listing:", listing_id)

    resp = page.request.post(
        "https://inv.viagogo.com/Listings/Details",
        form={"listingId": str(listing_id)},
    )
    print("Status:", resp.status)
    print("Content-Type:", resp.headers.get("content-type", ""))
    body = resp.text()
    if "json" in resp.headers.get("content-type", "").lower() or body.strip().startswith("{"):
        try:
            d = json.loads(body)
            print("\nFULL JSON (first 3000 chars):")
            print(json.dumps(d, indent=2)[:3000])
            print("\n\nSEARCH for any field with 'cost' or 'paid' or 'invoice':")
            def walk(obj, path=""):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if any(t in k.lower() for t in ("cost", "paid", "purchase", "invoice", "acqui", "basis")):
                            print(f"  {path}/{k}: {v}")
                        walk(v, f"{path}/{k}")
                elif isinstance(obj, list):
                    for i, x in enumerate(obj):
                        walk(x, f"{path}[{i}]")
            walk(d)
        except Exception as e:
            print("Parse failed:", e)
            print(body[:2000])
    else:
        # Probably HTML — search for cost-ish words
        print("\nHTML body — searching for cost/paid keywords:")
        import re
        for m in re.finditer(r'[\w\-]*(?:cost|paid|invoice|purchase|acquir)[\w\-]*[^<>"\n]{0,80}', body, re.I):
            print(f"  ...{m.group(0)[:120]}...")
        print("\nFirst 2000 chars of HTML:")
        print(body[:2000])
