"""Probe the ticketmaster.co.il seat-selection / cart flow.

Usage:
    .venv\\Scripts\\python scripts\\probe_tm_cart.py EVENT/PERF
    .venv\\Scripts\\python scripts\\probe_tm_cart.py https://www.ticketmaster.co.il/performance/...

Attaches to the CDP Chrome (login.py's window), opens the performance's
seat map, and RECORDS every ismapi/wbtxapi request while YOU pick a seat by
hand and push it as far toward checkout as you're willing to go. It clicks
nothing itself — the whole point is to capture what the real SPA does.

Answers, for the auto-cart module:

  1. Which endpoint actually reserves a seat? (candidates highlighted by
     URL keyword: select/reserve/cart/basket/order/hold/lock/book)
  2. Is the reserve anonymous, or does it need a logged-in session? TM's
     seat feeds are anonymous (CHANNEL/CPU headers only) — if the hold is
     too, the module needs no credentials at all, which is a much smaller
     blast radius than driving checkout.
  3. What identifies the seat in the reserve payload — the raw `id` from
     getAllSelectableSeats, or a (block,row,seat) triple? The watcher
     already stores `id` on every seat dict, so if it's the id we can cart
     straight from a drop ping with no re-fetch.
  4. What's the hold TTL, and does the response carry a cart/session token
     needed to release it again?
  5. Which headers beyond CHANNEL/CPU does the write need (CSRF, session)?

Everything lands in debug/tm-cart-*.json for offline reading. Nothing is
purchased: a hold expires on its own, and step 5 of the printed script
tells you how to drop it manually.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

import ticketmaster
from patchright.sync_api import sync_playwright

CDP_URL = os.environ.get("KARTIS_CDP_URL", "http://localhost:9222")

# Requests we care about: TM's own API surfaces, not analytics/CDN noise.
API_MARK = ("ismapi", "wbtxapi", "/api/")
NOISE = ("google", "facebook", "doubleclick", "hotjar", "clarity",
         "gtm.js", "analytics", "sentry", "newrelic")
# A reserve/hold call almost certainly says one of these in its path.
CART_WORDS = ("select", "reserve", "cart", "basket", "order", "hold",
              "lock", "book", "seatplan/set", "addticket")

RECORD_SECONDS = int(os.environ.get("KARTIS_TM_PROBE_SECONDS") or 240)


def interesting(url):
    low = url.lower()
    if any(n in low for n in NOISE):
        return False
    return any(m in low for m in API_MARK)


def is_cart_candidate(url, method):
    low = url.lower()
    if method.upper() in ("POST", "PUT", "PATCH"):
        return True
    return any(w in low for w in CART_WORDS)


def dump(rows, ev, pf):
    os.makedirs("debug", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fn = f"debug/tm-cart-{ev}-{pf}-{stamp}.json"
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n[probe] saved {len(rows)} call(s) -> {fn}")
    return fn


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    ev, pf = ticketmaster.parse_url(sys.argv[1])
    if pf == ticketmaster.EVENT_LEVEL_PERF:
        print("Give a PERFORMANCE, not an event — carting needs one perf.")
        print("Pick a date off the event page, e.g. MBP19/001")
        sys.exit(2)
    url = ticketmaster.perf_url(ev, pf)
    print(f"[probe] event={ev} perf={pf}")
    print(f"[probe] {url}")

    rows = []

    with sync_playwright() as p:
        print(f"[probe] connecting to Chrome on {CDP_URL} ...")
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()

        def on_request(req):
            if not interesting(req.url):
                return
            try:
                body = req.post_data
            except Exception:
                body = None
            rows.append({
                "t": time.time(),
                "phase": "request",
                "method": req.method,
                "url": req.url,
                "headers": dict(req.headers),
                "post_data": body,
                "candidate": is_cart_candidate(req.url, req.method),
            })
            if is_cart_candidate(req.url, req.method):
                print(f"  >> {req.method} {req.url[:120]}")
                if body:
                    print(f"     body: {body[:300]}")

        def on_response(resp):
            if not interesting(resp.url):
                return
            try:
                text = resp.text()[:20000]
            except Exception as e:
                text = f"<unreadable: {type(e).__name__}: {e}>"
            rows.append({
                "t": time.time(),
                "phase": "response",
                "status": resp.status,
                "url": resp.url,
                "body": text,
            })
            if is_cart_candidate(resp.url, "GET") or resp.request.method != "GET":
                print(f"  << {resp.status} {resp.url[:110]}")
                print(f"     {text[:300]}")

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        print("\n" + "=" * 68)
        print("RECORDING. Do this in the Chrome window, slowly:")
        print("  1. wait for the seat map to render")
        print("  2. click ONE seat  <- the reserve call should fire here")
        print("  3. continue to the cart / basket step")
        print("  4. STOP before paying. Do not complete a purchase.")
        print("  5. release it: remove the seat from the cart, or just")
        print("     leave it — TM drops the hold when it times out.")
        print(f"\nRecording for {RECORD_SECONDS}s. Ctrl-C when you're done.")
        print("=" * 68 + "\n")

        try:
            page.wait_for_timeout(RECORD_SECONDS * 1000)
        except KeyboardInterrupt:
            print("\n[probe] stopped by user")
        finally:
            fn = dump(rows, ev, pf)
            cands = [r for r in rows
                     if r.get("phase") == "request" and r.get("candidate")]
            print(f"\n[probe] {len(cands)} cart-candidate call(s):")
            for r in cands:
                print(f"   {r['method']:6} {r['url']}")
                if r.get("post_data"):
                    print(f"          {r['post_data'][:200]}")
            if not cands:
                print("   (none — the SPA may reserve over websocket, or the")
                print("    seat click didn't register. Check the dump.)")
            print(f"\n[probe] full trace: {fn}")
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
