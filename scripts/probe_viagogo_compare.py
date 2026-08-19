"""Probe the inv.viagogo.com per-listing "magnifying glass" competitor view.

Usage:
    .venv/bin/python scripts/probe_viagogo_compare.py [listing_id]

Auto-pricer follow-up: the user reports a magnifier icon on each Listings
row that shows competitor pricing directly inside inv.viagogo — if its
data source is reachable (XHR or modal DOM), the pricer can drop the whole
public-page scrape (URL discovery, fx calibration, buyer-currency math).

Steps: warm up /Listings, expand events, dump the target row's clickable
anatomy, click the magnifier-looking cell, then report every XHR fired and
whatever modal/panel rendered. Read-only — clicks the icon, never saves.
"""
import io
import json
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

import viagogo_listing
from patchright.sync_api import sync_playwright


def main():
    listing_id = sys.argv[1] if len(sys.argv) > 1 else None

    with viagogo_listing._exclusive_browser(), sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        try:
            page.wait_for_selector("tr.eventRow", timeout=30000)
            page.wait_for_timeout(1500)
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

            if not listing_id:
                listing_id = page.evaluate(
                    "() => document.querySelector('tr[data-id]')?.getAttribute('data-id')")
            print(f"target listing: {listing_id}")

            # 1) anatomy of the row: every cell + icon + clickable bit
            anatomy = page.evaluate(
                r"""(id) => {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return null;
  const out = [];
  tr.querySelectorAll('td').forEach((td, i) => {
    const kids = [...td.querySelectorAll('*')].slice(0, 6).map(el =>
      el.tagName + (el.className ? '.' + String(el.className).trim().replace(/\s+/g, '.') : '')
      + (el.title ? `[title=${el.title}]` : '')
      + (el.getAttribute('aria-label') ? `[aria=${el.getAttribute('aria-label')}]` : ''));
    out.push({i, cls: td.className, title: td.title || '',
              text: (td.innerText || '').trim().slice(0, 30), kids});
  });
  return out;
}""",
                str(listing_id),
            )
            print("\n=== row anatomy ===")
            for cell in anatomy or []:
                print(f"  td[{cell['i']}] .{cell['cls']} title={cell['title']!r} "
                      f"text={cell['text']!r}")
                for k in cell["kids"]:
                    print(f"      {k}")

            # 2) click the magnifier-looking thing and capture the fallout
            resps = []
            page.on("response", lambda r: resps.append(r))
            clicked = page.evaluate(
                r"""(id) => {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return null;
  const cands = [...tr.querySelectorAll('td, a, i, span, button')].filter(el => {
    const s = (el.className + ' ' + (el.title || '') + ' '
               + (el.getAttribute('aria-label') || '')).toLowerCase();
    return /search|magnif|compar|glass|zoom|spy|market|price.?check/.test(s);
  });
  if (!cands.length) return null;
  const el = cands[0];
  const desc = el.tagName + '.' + String(el.className).trim().replace(/\s+/g, '.');
  el.click();
  return desc;
}""",
                str(listing_id),
            )
            print(f"\nclicked: {clicked}")
            if not clicked:
                print("no magnifier-ish element found on the row — see anatomy above")
                return
            page.wait_for_timeout(4000)

            print("\n=== XHRs after click ===")
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
                    mark = " <<<<" if re.search(
                        r"price|listing|section|compet|market", body[:4000], re.I) else ""
                    print(f"  {r.request.method} {r.status} {ct:20} "
                          f"{len(body):>8}b {r.url[:120]}{mark}")
                    if mark and len(body) < 500000:
                        fn = f"debug/compare-{listing_id}-{abs(hash(r.url)) % 10000}.txt"
                        with open(fn, "w", encoding="utf-8") as f:
                            f.write(r.url + "\n\n" + body)
                        print(f"      saved -> {fn}")
                except Exception as e:
                    print(f"  (resp read failed: {e})")

            # 3) whatever rendered: modal / expanded panel with prices
            panel = page.evaluate(r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('#modal, [class*=modal], [class*=popover], [class*=panel], [class*=compare], [class*=market]')) {
    if (!el.offsetParent) continue;
    const t = (el.innerText || '').trim().replace(/\s+/g, ' ');
    if (t && /[$€₪]|price/i.test(t)) out.push({sel: el.tagName + '.' + String(el.className).slice(0, 60), text: t.slice(0, 800)});
  }
  return out.slice(0, 5);
}
""")
            print("\n=== visible panels with prices ===")
            for pnl in panel:
                print(f"  [{pnl['sel']}]")
                print(f"  {pnl['text']}")
            if not panel:
                print("  (nothing obvious rendered — check saved XHR bodies)")
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
