"""Probe the PUBLIC viagogo event page for competitor listings.

Usage:
    .venv\\Scripts\\python scripts\\probe_viagogo_public.py <event_id>
    .venv\\Scripts\\python scripts\\probe_viagogo_public.py "hanan ben ari"

Numeric arg = inv.viagogo event id (from viagogo_listings.event_id / the
Listings page row). Non-numeric arg = search the New Listing picker for the
event first and use its data-eventlink.

Goals (auto-pricer Phase 0):
  1. Resolve a public buyer-side URL for an inv event id.
  2. Capture the JSON XHR the event page uses to hydrate its listing list
     (dumped to debug/viagogo-public-<event_id>*.json).
  3. DOM-extraction fallback: section / price / qty per rendered row.
  4. Calibration: locate OUR OWN listing(s) (matched from kartis.db
     viagogo_listings for this event) and report shown_price vs our USD
     WebsitePrice, so the pricer knows which price field/currency to trust.

Read-only: opens one tab, never logs in, never writes to the DB.
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

import db
import scraper
from patchright.sync_api import sync_playwright

DEBUG_DIR = "debug"
JSON_URL_RE = re.compile(r"grid|categoryitems|listings|inventory|items|tickets", re.I)


def _resolve_event(page_factory, arg):
    """Return (event_id, candidate_urls). Numeric arg -> direct guesses;
    otherwise search the picker for a data-eventlink."""
    if re.fullmatch(r"\d+", arg):
        eid = arg
        return eid, [
            f"https://www.viagogo.com/E-{eid}",
            f"https://www.viagogo.com/ww/E-{eid}",
        ]
    import viagogo_listing
    print(f"searching New Listing picker for: {arg!r}")
    rows = viagogo_listing.search_event(arg, limit=5)
    for r in rows:
        print(f"  candidate: {r['event_id']}  {r['event_name']}  {r['venue']}  {r['date']}")
    if not rows:
        raise SystemExit("no picker matches — try the numeric event id")
    eid = rows[0]["event_id"]
    return eid, [
        f"https://www.viagogo.com/E-{eid}",
        f"https://www.viagogo.com/ww/E-{eid}",
    ]


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        raise SystemExit(__doc__)

    os.makedirs(DEBUG_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = scraper._connect_over_cdp(p, scraper.CDP_URL_VIAGOGO)
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        event_id, urls = _resolve_event(context.new_page, arg)
        print(f"event_id={event_id}")

        # Our own listings for calibration (scrape mirror is fine here).
        ours = [r for r in db.all_viagogo() if str(r.get("event_id")) == str(event_id)]
        print(f"our listings in DB for this event: {len(ours)}")
        for r in ours:
            print(f"  ours: listing={r['id']} section={r['section']!r} "
                  f"price={r['price']} proceeds={r['proceeds']} avail={r['available']}")

        page = context.new_page()
        captured = []

        def on_response(resp):
            try:
                url = resp.url
                ctype = (resp.headers.get("content-type") or "").lower()
                if "json" not in ctype:
                    return
                if not JSON_URL_RE.search(url):
                    return
                body = resp.text()
                captured.append({"url": url, "status": resp.status, "body": body})
            except Exception:
                pass

        page.on("response", on_response)

        final_url = None
        for url in urls:
            print(f"\ntrying {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"  goto failed: {e}")
                continue
            page.wait_for_timeout(6000)  # let the SPA hydrate + fire XHRs
            final_url = page.url
            title = page.title()
            print(f"  landed on: {final_url}")
            print(f"  title: {title}")
            not_found = page.evaluate(
                "() => /not found|page you requested|404/i.test(document.body?.innerText?.slice(0,2000) || '')"
            )
            if not_found:
                print("  looks like a not-found page; trying next candidate")
                continue
            break

        # Scroll a little — some grids lazy-load.
        try:
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(2500)
        except Exception:
            pass

        print(f"\n=== captured JSON responses: {len(captured)} ===")
        for i, c in enumerate(captured):
            path = os.path.join(DEBUG_DIR, f"viagogo-public-{event_id}-{i}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(c["body"])
            print(f"  [{i}] {c['status']} {c['url'][:180]}")
            print(f"      -> {path} ({len(c['body'])} bytes)")
            # Peek: does it contain price-ish keys?
            try:
                d = json.loads(c["body"])
                keys = set()

                def walk(o, depth=0):
                    if depth > 6:
                        return
                    if isinstance(o, dict):
                        for k, v in o.items():
                            if re.search(r"price|amount|section|ticket|quantity|currency|row", k, re.I):
                                keys.add(k)
                            walk(v, depth + 1)
                    elif isinstance(o, list):
                        for x in o[:20]:
                            walk(x, depth + 1)

                walk(d)
                if keys:
                    print(f"      price-ish keys: {sorted(keys)[:25]}")
            except Exception as e:
                print(f"      (json parse failed: {e})")

        # DOM fallback: dump anything that looks like a listing row.
        print("\n=== DOM extraction fallback ===")
        rows = page.evaluate(r"""
() => {
  const out = [];
  // viagogo grid rows have historically used [data-testid] / listing-item-ish
  // containers; fall back to any element that shows a currency amount.
  const sels = [
    '[data-testid*="listing"]', '[class*="listing-item"]', '[class*="ticket-list"] li',
    '[id*="listing"] li', 'li[class*="ticket"]',
  ];
  const seen = new Set();
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      const txt = (el.innerText || '').trim().replace(/\s+/g, ' ');
      if (!txt || seen.has(txt) || txt.length > 400) continue;
      if (!/[$₪€£]\s?\d|\d\s?[$₪€£]|US\$|ILS|NIS/.test(txt)) continue;
      seen.add(txt);
      out.push({sel, text: txt.slice(0, 220)});
      if (out.length >= 40) return out;
    }
  }
  return out;
}
""")
        for r in rows:
            print(f"  [{r['sel']}] {r['text']}")
        if not rows:
            print("  (no obvious listing rows found in DOM)")
            # Save full HTML for offline inspection.
            html_path = os.path.join(DEBUG_DIR, f"viagogo-public-{event_id}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"  full HTML saved -> {html_path}")

        # Calibration hints.
        if ours:
            print("\n=== calibration ===")
            print("compare the JSON/DOM prices above against our WebsitePrice values:")
            for r in ours:
                print(f"  ours: section={r['section']!r} USD price={r['price']}")
            print("look for our section+qty in the payload; note shown price, currency "
                  "field, and whether fees are baked in.")

        try:
            page.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
