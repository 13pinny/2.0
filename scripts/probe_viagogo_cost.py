"""Probe Viagogo Listings edit modal for cost field."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import scraper
from patchright.sync_api import sync_playwright

captured = []
def handler(req):
    if "inv.viagogo.com" in req.url and req.method in ("GET","POST") and "pageevents" not in req.url and "keepAlive" not in req.url:
        captured.append({"method": req.method, "url": req.url, "post": (req.post_data or "")[:400]})

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(scraper.CDP_URL)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = next((pg for pg in context.pages if "inv.viagogo.com" in (pg.url or "")), None)
    if not page:
        page = context.new_page()
        page.goto("https://inv.viagogo.com/Listings", wait_until="domcontentloaded", timeout=15000)
    page.on("request", handler)

    # Make sure events are expanded
    page.evaluate("""() => { const a = document.querySelector('tr.eventRow:not(.expanded) td.js-expand'); if (a) a.click(); }""")
    page.wait_for_timeout(800)
    captured.clear()

    # Click the pencil edit cell
    listing_id = page.evaluate(r"""
() => {
  const tr = document.querySelector('tr[data-id]');
  if (!tr) return null;
  const editCol = tr.querySelector('td.editCol, td.deets');
  if (editCol) editCol.click();
  return tr.getAttribute('data-id');
}
""")
    print("Clicked edit on listing:", listing_id)
    page.wait_for_timeout(3000)

    print("\n=== REQUESTS ===")
    for c in captured[:20]:
        print(f"  [{c['method']}] {c['url'][:200]}")
        if c["post"]:
            print(f"     body: {c['post']}")

    # Inspect any opened dialog/modal for cost-related fields
    fields = page.evaluate(r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('input, select, textarea, label')) {
    const tag = el.tagName;
    const value = el.value || el.placeholder || '';
    const text = (el.innerText || '').trim();
    const name = el.name || el.id || '';
    let lbl = '';
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l) lbl = (l.innerText || '').trim();
    }
    const probe = (name + ' ' + text + ' ' + lbl + ' ' + (el.placeholder || '')).toLowerCase();
    if (probe.includes('cost') || probe.includes('purchase') || probe.includes('paid')) {
      out.push({tag, name, value, label: lbl, text: text.slice(0, 60)});
    }
  }
  return out;
}
""")
    print("\n=== COST-RELATED FIELDS ===")
    for f in fields:
        print(f"  {f}")

    # Show full edit URL if present
    edit_urls = [c for c in captured if "edit" in c["url"].lower() or "details" in c["url"].lower() or "listing" in c["url"].lower()]
    print(f"\n=== potentially-relevant URLs ({len(edit_urls)}) ===")
    for c in edit_urls[:5]:
        print(f"  [{c['method']}] {c['url'][:240]}")
