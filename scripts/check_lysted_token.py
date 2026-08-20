"""Find where the bearer token lives so the scraper can grab it."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import scraper
from patchright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.goto("https://app.lysted.com/sales", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector("table.b-table tbody tr", timeout=15000)
    page.wait_for_timeout(2000)
    storage = page.evaluate(r"""() => {
      const out = {local: {}, session: {}, cookieKeys: document.cookie.split('; ').map(c => c.split('=')[0])};
      for (let i=0; i<localStorage.length; i++) {
        const k = localStorage.key(i);
        const v = localStorage.getItem(k) || '';
        out.local[k] = v.slice(0, 80) + (v.length > 80 ? '...' : '');
      }
      for (let i=0; i<sessionStorage.length; i++) {
        const k = sessionStorage.key(i);
        const v = sessionStorage.getItem(k) || '';
        out.session[k] = v.slice(0, 80) + (v.length > 80 ? '...' : '');
      }
      return out;
    }""")
    print("LOCALSTORAGE keys:")
    for k, v in storage["local"].items():
        print(f"  {k!r}: {v}")
    print("\nSESSIONSTORAGE keys:")
    for k, v in storage["session"].items():
        print(f"  {k!r}: {v}")
    print(f"\nCOOKIE KEYS: {storage['cookieKeys']}")
    page.close()
