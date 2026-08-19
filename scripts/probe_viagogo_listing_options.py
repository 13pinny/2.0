"""Probe the New Listing form for listing-notes / restrictions / split controls.

Usage:
    .venv\\Scripts\\python scripts\\probe_viagogo_listing_options.py "<search query>" [event_id]

Reaches the New Listing details form (via the picker, E-Tickets tile) for the
first matching event (or the given event_id), then dumps — WITHOUT saving:
  - every checkbox: id / name / label text / checked / visible
  - every <select>: name + all option values/texts
  - outerHTML of regions whose text matches note|restrict|split|separation
  - any collapsed "more options" expanders

Goal: discover the selectors/values for listing notes (Standing Only, Aisle
seat, Actual 1st/2nd row of section), Restrictions on Use (21+/18+), and the
Ticket Separation (split) dropdown, so create_draft_listing can set them.
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

import viagogo_listing
from patchright.sync_api import sync_playwright


DUMP_JS = r"""
() => {
  const lab = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.innerText.trim();
    }
    const p = el.closest('label');
    if (p) return p.innerText.trim();
    // sibling text fallback
    const sib = el.nextElementSibling;
    return sib ? (sib.innerText || '').trim().slice(0, 120) : '';
  };
  const vis = (el) => !!(el.offsetParent || el.getClientRects().length);
  const checkboxes = [];
  for (const el of document.querySelectorAll('input[type=checkbox]')) {
    checkboxes.push({id: el.id, name: el.name, label: lab(el),
                     checked: el.checked, visible: vis(el)});
  }
  const selects = [];
  for (const el of document.querySelectorAll('select')) {
    selects.push({id: el.id, name: el.name, visible: vis(el),
                  value: el.value,
                  options: [...el.options].map(o => ({value: o.value, text: o.text.trim()}))});
  }
  const regions = [];
  const re = /note|restrict|split|separat/i;
  for (const el of document.querySelectorAll('fieldset, .form-group, .row, section, div[class*=note i], div[class*=split i]')) {
    const t = (el.innerText || '');
    if (re.test(t) && t.length < 1500 && !regions.some(r => r.includes(el.outerHTML.slice(0, 80)))) {
      // only keep leaf-ish regions to avoid dumping the whole form
      if (![...el.querySelectorAll('*')].some(c => re.test(c.innerText || '') && (c.innerText || '').length < t.length * 0.5 && c.querySelector('input,select')))
        regions.push(el.outerHTML.slice(0, 2500));
    }
  }
  const expanders = [];
  for (const el of document.querySelectorAll('a, button, [class*=expand], [class*=collapse], [class*=more], [class*=toggle]')) {
    const t = (el.innerText || '').trim();
    if (t && t.length < 60 && /more|option|advanced|note|restrict|detail/i.test(t))
      expanders.push({tag: el.tagName, id: el.id, cls: el.className, text: t, visible: vis(el)});
  }
  return {checkboxes, selects, regions, expanders};
}
"""


def dump(page, label):
    d = page.evaluate(DUMP_JS)
    print(f"\n================ DUMP: {label} ================")
    print("\n--- checkboxes ---")
    for c in d["checkboxes"]:
        print(f"  id={c['id']!r} name={c['name']!r} checked={c['checked']} "
              f"visible={c['visible']} label={c['label']!r}")
    print("\n--- selects ---")
    for s in d["selects"]:
        print(f"  id={s['id']!r} name={s['name']!r} visible={s['visible']} value={s['value']!r}")
        for o in s["options"]:
            print(f"      option value={o['value']!r} text={o['text']!r}")
    print("\n--- note/restrict/split regions ---")
    for r in d["regions"]:
        print("  ----")
        print("  " + r.replace("\n", "\n  "))
    print("\n--- candidate expanders ---")
    for e in d["expanders"]:
        print(f"  <{e['tag']}> id={e['id']!r} cls={e['cls']!r} text={e['text']!r} visible={e['visible']}")
    return d


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    query = sys.argv[1]
    want_id = sys.argv[2] if len(sys.argv) > 2 else None

    with viagogo_listing._exclusive_browser(), sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        try:
            viagogo_listing._open_new_listing_modal(page)
            rows = viagogo_listing._search_rows(page, query)
            if not rows:
                raise SystemExit(f"no picker rows for {query!r}")
            match = rows[0] if not want_id else next(
                (r for r in rows if r["event_id"] == str(want_id)), None)
            if not match:
                raise SystemExit(
                    f"event {want_id} not in {[r['event_id'] for r in rows]}")
            print(f"using event {match['event_id']}: {match['event_name']} @ {match['venue']}")
            viagogo_listing._click_event_row(page, match["event_id"], match.get("_row"))
            viagogo_listing._select_ticket_type_tile(
                page, "E-Tickets", match["event_id"], match.get("_row"))
            page.wait_for_selector('input[name="Listing.AvailableTickets"]',
                                   timeout=viagogo_listing.MODAL_TIMEOUT_MS)
            page.wait_for_timeout(1000)

            d = dump(page, "initial form")

            # click any promising collapsed expanders and re-dump
            clicked = []
            for e in d["expanders"]:
                if not e["visible"]:
                    continue
                if not any(k in e["text"].lower() for k in
                           ("more", "option", "advanced", "note", "restrict")):
                    continue
                try:
                    sel = f"#{e['id']}" if e["id"] else None
                    loc = (page.locator(sel) if sel
                           else page.locator(f"{e['tag'].lower()}", has_text=e["text"]).first)
                    loc.click(timeout=3000)
                    clicked.append(e["text"])
                    page.wait_for_timeout(800)
                except Exception as ex:
                    print(f"  (couldn't click expander {e['text']!r}: {ex})")
            if clicked:
                print(f"\nclicked expanders: {clicked}")
                dump(page, "after expanding")

            print("\nNOT SAVING — closing page. (No listing was created.)")
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
