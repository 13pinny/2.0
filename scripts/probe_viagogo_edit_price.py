"""Probe editing an EXISTING inv.viagogo listing's price.

Usage:
    .venv\\Scripts\\python scripts\\probe_viagogo_edit_price.py <listing_id>            # inspect only
    .venv\\Scripts\\python scripts\\probe_viagogo_edit_price.py <listing_id> <price>    # set + verify + tell you to revert

Auto-pricer Phase 0. Two candidate mechanisms:
  A) form replay — POST /Listings/Details {listingId} returns the edit form
     HTML; enumerate its action / hidden fields / anti-forgery token, then
     replay the save POST with only WebsitePrice + Proceeds changed.
  B) UI — expand the event on /Listings, click the row's edit pencil
     (td.editCol / td.deets), fill() the modal price inputs, save.

This probe does A's inspection always; with a <price> arg it attempts the
save (A first, falling back to B) and verifies by re-reading the row.
"""
import io
import json
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

import scraper
import viagogo_listing
from patchright.sync_api import sync_playwright

DETAILS_URL = "https://inv.viagogo.com/Listings/Details"


def _parse_form_fields(html):
    """Return (form_actions, ordered [(name, value, type)]) from the edit-form
    HTML. Order and duplicates preserved — ASP.NET MVC checkboxes render as a
    checkbox + hidden 'false' pair with the same name, and the checked state
    only survives a replay if both entries are posted."""
    fields = []
    for m in re.finditer(r'<(input|select|textarea)\b[^>]*>', html, re.I):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        typ = re.search(r'type="([^"]*)"', tag)
        checked = re.search(r'\bchecked\b', tag, re.I)
        fields.append({
            "name": name.group(1),
            "value": value.group(1) if value else "",
            "type": typ.group(1) if typ else m.group(1).lower(),
            "checked": bool(checked),
        })
    forms = re.findall(r'<form\b[^>]*action="([^"]*)"[^>]*>', html, re.I)
    print(f"form actions: {forms}")
    for f in fields:
        marker = " <==" if re.search(r"price|proceed|currency|token|publish", f["name"], re.I) else ""
        chk = " [checked]" if f["checked"] else ""
        print(f"  {f['name']} = {f['value']!r} ({f['type']}){chk}{marker}")
    return forms, fields


def _read_row(page, listing_id):
    """Find the listing row on /Listings and return its rendered price cells."""
    page.wait_for_selector("tr.eventRow", timeout=30000)
    page.wait_for_timeout(1500)
    # expand everything first
    for _ in range(20):
        collapsed = page.query_selector_all("tr.eventRow:not(.expanded) td.js-expand")
        if not collapsed:
            break
        for arrow in collapsed:
            try:
                arrow.click()
            except Exception:
                pass
        page.wait_for_timeout(600)
    return page.evaluate(
        r"""(id) => {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return null;
  const tds = tr.querySelectorAll('td');
  const txt = (i) => tds[i]?.innerText?.trim() ?? null;
  return {section: txt(1), price: txt(5), proceeds: txt(6)};
}""",
        str(listing_id),
    )


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    listing_id = sys.argv[1]
    new_price = float(sys.argv[2]) if len(sys.argv) > 2 else None

    with sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        try:
            before = _read_row(page, listing_id)
            print(f"row before: {before}")
            if before is None:
                raise SystemExit("listing row not found on /Listings — check the id")

            # --- inspection: what does /Listings/Details return? ---
            resp = page.request.post(DETAILS_URL, form={"listingId": str(listing_id)})
            ctype = resp.headers.get("content-type", "")
            body = resp.text()
            print(f"\nDetails POST: {resp.status} {ctype} ({len(body)} bytes)")
            forms, fields = ([], [])
            if "json" in ctype.lower() or body.strip().startswith("{"):
                d = json.loads(body)
                print(json.dumps(d, indent=2)[:3000])
                # some MVC apps wrap HTML in a JSON envelope
                html = d.get("html") or d.get("Html") or d.get("content") or ""
                if html:
                    forms, fields = _parse_form_fields(html)
            else:
                forms, fields = _parse_form_fields(body)

            if new_price is None:
                print("\n(inspection only — pass a price to attempt an edit)")
                return

            proceeds = round(new_price * viagogo_listing.PROCEEDS_RATE, 2)
            print(f"\nattempting price change -> {new_price} (proceeds {proceeds})")

            # --- Path A: replay the save POST ---
            saved = False
            if fields:
                pairs = []
                for f in fields:
                    if f["type"] in ("submit", "button"):
                        continue
                    if f["type"] == "checkbox" and not f["checked"]:
                        # unchecked checkboxes don't post; their hidden
                        # 'false' twin (a separate entry) still does
                        continue
                    val = f["value"]
                    if re.search(r"WebsitePrice", f["name"]):
                        val = f"{new_price:.2f}"
                    elif re.search(r"Proceeds", f["name"]):
                        val = f"{proceeds:.2f}"
                    elif f["name"] == "Listing.IsPriceModified":
                        val = "True"
                    pairs.append((f["name"], val))
                # Keep the listing published: the form's own JS flips the
                # hidden IsPublishToViagogo to match the toggle before submit.
                orig_pub = next((f["value"] for f in fields
                                 if f["name"] == "Listing.OriginalIsPublishToViagogo"), "")
                if orig_pub.lower() == "true":
                    pairs = [(k, "true" if k == "Listing.IsPublishToViagogo" else v)
                             for k, v in pairs]
                action = forms[0] if forms else "/Listings/SaveListing"
                if action.startswith("/"):
                    action = "https://inv.viagogo.com" + action
                import urllib.parse
                encoded = urllib.parse.urlencode(pairs)
                print(f"Path A: POST {action} with {len(pairs)} fields")
                print("  publish fields:", [(k, v) for k, v in pairs if "Publish" in k])
                try:
                    r2 = page.request.post(
                        action, data=encoded,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    print(f"  -> {r2.status} {r2.headers.get('content-type','')}")
                    print(f"  body head: {r2.text()[:400]!r}")
                    saved = r2.ok
                except Exception as e:
                    print(f"  Path A failed: {e}")

            # verify A before falling back
            if saved:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("tr.eventRow", timeout=30000)
                page.wait_for_timeout(1500)
                after = _read_row(page, listing_id)
                print(f"row after A: {after}")
                if after and after.get("price") != before.get("price"):
                    print("PATH A WORKS — price changed via form replay")
                    return
                print("Path A POST returned ok but the row did not change; trying B")

            # --- Path B: UI modal ---
            print("\nPath B: clicking the row's edit pencil")
            clicked = page.evaluate(
                r"""(id) => {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return false;
  const cell = tr.querySelector('td.editCol, td.deets');
  if (!cell) return false;
  cell.click();
  return true;
}""",
                str(listing_id),
            )
            if not clicked:
                raise SystemExit("could not find the edit cell on the row")
            page.wait_for_timeout(2500)
            # dump visible price inputs in whatever modal opened
            inputs = page.evaluate(r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('input, select')) {
    if (!el.offsetParent) continue;
    const name = el.name || el.id || '';
    if (/price|proceed|currency/i.test(name)) {
      out.push({name, value: el.value, tag: el.tagName});
    }
  }
  return out;
}
""")
            print(f"visible price fields in modal: {inputs}")
            price_sel = None
            proceeds_sel = None
            for cand in ('input[name="Listing.WebsitePrice"]', "#WebsitePrice",
                         'input[name*="WebsitePrice"]'):
                if page.query_selector(cand):
                    price_sel = cand
                    break
            for cand in ('input[name="Listing.Proceeds"]', "#Proceeds",
                         'input[name*="Proceeds"]'):
                if page.query_selector(cand):
                    proceeds_sel = cand
                    break
            print(f"selectors: price={price_sel} proceeds={proceeds_sel}")
            if not price_sel:
                raise SystemExit("no visible WebsitePrice input — inspect the modal manually")
            page.fill(price_sel, f"{new_price:.2f}")
            if proceeds_sel:
                page.fill(proceeds_sel, f"{proceeds:.2f}")
            # find a save button
            for sel in ("#btnSaveDetails", 'button:has-text("Save")', 'input[type="submit"]'):
                btn = page.query_selector(sel)
                if btn:
                    print(f"clicking save: {sel}")
                    btn.click()
                    break
            page.wait_for_timeout(3000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("tr.eventRow", timeout=30000)
            page.wait_for_timeout(1500)
            after = _read_row(page, listing_id)
            print(f"row after B: {after}")
            if after and after.get("price") != before.get("price"):
                print("PATH B WORKS — price changed via edit modal")
            else:
                print("no change detected — inspect debug output above")
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
