"""Hit api.lysted.com/api/sales directly and see the JSON shape."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import scraper
from datetime import datetime, timedelta
from patchright.sync_api import sync_playwright

yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
url = f"https://api.lysted.com/api/sales?search=saledate.min:{yesterday}&dateOffset=240&force=false"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # Fetch using page.request which carries cookies
    resp = page.request.get(url)
    print(f"status: {resp.status}")
    print(f"headers: {dict(resp.headers)}")
    body = resp.text()
    try:
        data = json.loads(body)
        print(f"top-level keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"  {k}: {len(v)} items")
                    if v:
                        print(f"  sample[0]: {json.dumps(v[0], indent=2)[:1500]}")
                else:
                    print(f"  {k}: {v}")
        elif isinstance(data, list):
            print(f"list len: {len(data)}")
            if data:
                print(f"sample[0]: {json.dumps(data[0], indent=2)[:1500]}")
    except Exception as e:
        print(f"parse fail: {e}")
        print(f"body[:1000]: {body[:1000]}")
    page.close()
