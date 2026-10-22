"""Probe the inv.viagogo magnifier popup for market-wide SALES rows.

Usage:
    .venv/Scripts/python scripts/probe_viagogo_market_sales.py [event_id]

The magnifier on /Listings opens the MarketDataV3 competitor popup (already
consumed by viagogo_pricer for listings). The user reports it also shows
recent SALES for the event. This probe answers, for the vgsales tracker:

  1. Do the sale rows ride the same MarketDataV3 response, or a separate
     lazy XHR fired when a sales tab renders?
  2. Is latestServerStamp an incremental cursor (free dedup)?
  3. Row shape: timestamp resolution, price semantics, feed depth, any
     stable row id.
  4. Does the endpoint answer for an eventId we have NO listing on
     (watchlist support)?

Read-only — clicks the magnifier and tabs, never saves anything. Dumps
bodies to debug/vgsales-*.txt for offline inspection.
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

import viagogo_listing
from patchright.sync_api import sync_playwright

MARKET_DATA_URL = "https://inv.viagogo.com/Listings/MarketDataV3"
SALES_MARK_RE = re.compile(r"sale|sold|transaction|purchas", re.I)


def dump(name, text):
    os.makedirs("debug", exist_ok=True)
    fn = f"debug/vgsales-{name}.txt"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"      saved -> {fn} ({len(text)} b)")
    return fn


def summarize_body(label, body):
    print(f"      {label}: {len(body)} b")
    try:
        data = json.loads(body)
        print(f"      JSON! top-level keys: {list(data)[:20] if isinstance(data, dict) else type(data)}")
    except Exception:
        n_tr = len(re.findall(r"<tr", body))
        print(f"      not JSON; <tr> count={n_tr}")
    hits = {}
    for w in ("sale", "sold", "transaction", "purchas", "latestServerStamp",
              "serverStamp", "recent"):
        n = len(re.findall(w, body, re.I))
        if n:
            hits[w] = n
    print(f"      marker hits: {hits or 'none'}")


def replay_market_data(page, event_id, stamp="0"):
    resp = page.request.post(
        MARKET_DATA_URL,
        form={"eventId": str(event_id), "latestServerStamp": str(stamp)})
    body = ""
    try:
        body = resp.text() or ""
    except Exception:
        pass
    print(f"  POST MarketDataV3 eventId={event_id} stamp={stamp} -> "
          f"HTTP {resp.status}")
    return resp.status, body


def find_stamp(body):
    m = re.search(r'(?:latestServerStamp|serverStamp)["\']?\s*[:=]\s*["\']?(\d+)',
                  body, re.I)
    return m.group(1) if m else None


def visible_panels(page):
    return page.evaluate(r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('#modal, [class*=modal], [class*=popover], [class*=panel], [class*=compare], [class*=market], [class*=dialog]')) {
    if (!el.offsetParent) continue;
    const t = (el.innerText || '').trim();
    if (t.length > 40) out.push({sel: el.tagName + '.' + String(el.className).slice(0, 60),
                                 html: el.outerHTML, text: t.slice(0, 1500)});
  }
  out.sort((a, b) => b.html.length - a.html.length);
  return out.slice(0, 3);
}
""")


def main():
    want_event = sys.argv[1] if len(sys.argv) > 1 else None

    with viagogo_listing._exclusive_browser(), sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        try:
            page.wait_for_selector("tr.eventRow", timeout=30000)
            page.wait_for_timeout(1500)

            # ---- expand every event so rows + magnifiers exist ----------
            for _ in range(20):
                collapsed = page.query_selector_all(
                    "tr.eventRow:not(.expanded) td.js-expand")
                if not collapsed:
                    break
                for a in collapsed:
                    try:
                        a.click()
                    except Exception:
                        pass
                page.wait_for_timeout(600)

            # ---- step 1: click a real magnifier, capture all fallout ----
            print("\n=== step 1: click the magnifier, capture XHRs ===")
            resps = []
            page.on("response", lambda r: resps.append(r))
            clicked = page.evaluate(
                r"""(wantEvent) => {
  // magnifier candidates anywhere on the table; prefer a row whose text
  // hints at the requested event id (rarely visible), else first found
  const cands = [...document.querySelectorAll('tr td, tr a, tr i, tr span, tr button')].filter(el => {
    const s = (el.className + ' ' + (el.title || '') + ' '
               + (el.getAttribute('aria-label') || '')).toLowerCase();
    return /search|magnif|compar|glass|zoom|spy|market|price.?check/.test(s);
  });
  if (!cands.length) return null;
  const el = cands[0];
  const tr = el.closest('tr');
  el.click();
  return {desc: el.tagName + '.' + String(el.className).trim().replace(/\s+/g, '.'),
          rowClass: tr ? tr.className : '', rowText: tr ? (tr.innerText || '').slice(0, 120) : ''};
}""",
                str(want_event or ""))
            print(f"  clicked: {clicked}")
            if not clicked:
                print("  !! no magnifier found — dumping first eventRow anatomy")
                anat = page.evaluate(r"""
() => {
  const tr = document.querySelector('tr.eventRow');
  if (!tr) return null;
  return [...tr.querySelectorAll('td')].map((td, i) => ({
    i, cls: td.className, title: td.title || '', text: (td.innerText || '').trim().slice(0, 40),
    kids: [...td.querySelectorAll('*')].slice(0, 8).map(el =>
      el.tagName + (el.className ? '.' + String(el.className).trim().replace(/\s+/g, '.') : '')
      + (el.title ? `[title=${el.title}]` : ''))
  }));
}""")
                print(json.dumps(anat, indent=1, ensure_ascii=False)[:4000])
                return
            page.wait_for_timeout(4000)

            clicked_event_id = None
            print("\n  XHRs after magnifier click:")
            for r in resps:
                try:
                    ct = (r.headers.get("content-type") or "").split(";")[0]
                    if any(x in ct for x in ("image", "font", "css", "javascript")):
                        continue
                    body = ""
                    try:
                        body = r.text() or ""
                    except Exception:
                        pass
                    post_data = ""
                    try:
                        post_data = r.request.post_data or ""
                    except Exception:
                        pass
                    mark = " <<<<" if SALES_MARK_RE.search(body[:6000]) else ""
                    print(f"    {r.request.method} {r.status} {ct:18} "
                          f"{len(body):>8}b {r.url[:110]}{mark}")
                    if post_data:
                        print(f"        form: {post_data[:200]}")
                        em = re.search(r"eventId=(\d+)", post_data)
                        if em and "MarketDataV3" in r.url:
                            clicked_event_id = em.group(1)
                    if mark and len(body) < 800000:
                        tag = re.sub(r"\W+", "_", r.url.split("/")[-1])[:40]
                        dump(f"click-{tag}", r.url + "\n\n" + post_data + "\n\n" + body)
                except Exception as e:
                    print(f"    (resp read failed: {e})")

            # ---- step 2: what rendered? dump popup DOM ------------------
            print("\n=== step 2: popup DOM ===")
            panels = visible_panels(page)
            for i, pnl in enumerate(panels):
                print(f"  [{pnl['sel']}]")
                print("  " + pnl["text"][:600].replace("\n", " | "))
                dump(f"panel-{i}", pnl["html"])
            if not panels:
                print("  (no visible modal/panel found)")

            # ---- step 3: click anything sales-tab-ish in the popup ------
            print("\n=== step 3: sales tab inside the popup ===")
            resps2 = []
            page.on("response", lambda r: resps2.append(r))
            tab = page.evaluate(r"""
() => {
  const roots = [...document.querySelectorAll('#modal, [class*=modal], [class*=popover], [class*=panel], [class*=compare], [class*=market], [class*=dialog]')]
    .filter(el => el.offsetParent);
  for (const root of roots) {
    const cands = [...root.querySelectorAll('a, button, li, span, div, label, input')].filter(el => {
      const t = ((el.innerText || el.value || '') + ' ' + el.className + ' '
                 + (el.getAttribute('aria-label') || '')).toLowerCase();
      return /\bsales?\b|\bsold\b|transaction|recent/.test(t) && t.length < 120;
    });
    if (cands.length) {
      // innermost, shortest-text candidate = the actual tab control
      cands.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
      const el = cands[0];
      const desc = el.tagName + '.' + String(el.className).trim().replace(/\s+/g, '.')
                   + ' text=' + (el.innerText || el.value || '').trim().slice(0, 60);
      el.click();
      return desc;
    }
  }
  return null;
}""")
            print(f"  clicked tab: {tab}")
            page.wait_for_timeout(4000)
            for r in resps2:
                try:
                    ct = (r.headers.get("content-type") or "").split(";")[0]
                    if any(x in ct for x in ("image", "font", "css", "javascript")):
                        continue
                    body = ""
                    try:
                        body = r.text() or ""
                    except Exception:
                        pass
                    post_data = ""
                    try:
                        post_data = r.request.post_data or ""
                    except Exception:
                        pass
                    print(f"    {r.request.method} {r.status} {ct:18} "
                          f"{len(body):>8}b {r.url[:110]}")
                    if post_data:
                        print(f"        form: {post_data[:200]}")
                    if len(body) < 800000 and body:
                        tag = re.sub(r"\W+", "_", r.url.split("/")[-1])[:40]
                        dump(f"tab-{tag}", r.url + "\n\n" + post_data + "\n\n" + body)
                except Exception as e:
                    print(f"    (resp read failed: {e})")
            panels2 = visible_panels(page)
            for i, pnl in enumerate(panels2):
                dump(f"panel-after-tab-{i}", pnl["html"])
                print(f"  [{pnl['sel']}] " + pnl["text"][:600].replace("\n", " | "))

            # ---- step 4: raw MarketDataV3 replay + stamp semantics ------
            event_id = want_event or clicked_event_id
            print(f"\n=== step 4: raw MarketDataV3 replay (event {event_id}) ===")
            if event_id:
                status, body = replay_market_data(page, event_id, "0")
                summarize_body("stamp=0 body", body)
                dump(f"raw-{event_id}-stamp0", body)
                stamp = find_stamp(body)
                print(f"  stamp found in body: {stamp}")
                if stamp and stamp != "0":
                    status2, body2 = replay_market_data(page, event_id, stamp)
                    summarize_body(f"stamp={stamp} body", body2)
                    dump(f"raw-{event_id}-stamp{stamp}", body2)
            else:
                print("  (no event id known — skipped)")

            # ---- step 5: unlisted event test ----------------------------
            print("\n=== step 5: unlisted-event test ===")
            listed = set()
            try:
                import sqlite3
                c = sqlite3.connect("kartis.db")
                listed = {r[0] for r in c.execute(
                    "SELECT DISTINCT event_id FROM viagogo_listings "
                    "WHERE event_id IS NOT NULL")}
                c.close()
            except Exception as e:
                print(f"  (db read failed: {e})")
            print(f"  we are listed on {len(listed)} events")

            unlisted_id = None
            search_page = page.context.new_page()
            captured = []
            search_page.on(
                "response",
                lambda r: captured.append(r) if "groupedsearch" in r.url else None)
            try:
                search_page.goto("https://www.viagogo.com",
                                 wait_until="domcontentloaded", timeout=45000)
                search_page.wait_for_timeout(2500)
                box = search_page.query_selector("input[placeholder*=earch]")
                if box:
                    box.click()
                    search_page.keyboard.type("Coldplay", delay=60)
                    search_page.wait_for_timeout(5000)
                for resp in captured:
                    try:
                        body = resp.text()
                    except Exception:
                        continue
                    for m in re.finditer(r"/E-(\d+)", body.replace("\\/", "/")):
                        if m.group(1) not in listed:
                            unlisted_id = m.group(1)
                            break
                    if unlisted_id:
                        break
            except Exception as e:
                print(f"  (public search failed: {e})")
            finally:
                try:
                    search_page.close()
                except Exception:
                    pass

            print(f"  unlisted event id: {unlisted_id}")
            if unlisted_id:
                status, body = replay_market_data(page, unlisted_id, "0")
                summarize_body("unlisted body", body)
                if body:
                    dump(f"unlisted-{unlisted_id}", body)
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
