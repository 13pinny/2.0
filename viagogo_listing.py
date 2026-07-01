"""Drives the user's logged-in viagogo Inventory Manager session (over CDP)
to search for a matching event and create a draft (unpublished) listing from
a Kupat ticket-purchase confirmation email.

No login automation by design — same reasoning as scraper.py: Cloudflare
blocks headless browsers, so this attaches to the Chrome the user already
signed into manually (KARTIS_CDP_URL_VIAGOGO, falling back to KARTIS_CDP_URL).

There is no direct URL into the "New Listing" flow for a specific event —
inv.viagogo.com is JS-routed and rejects deep links ("Page Not Found"), so
every call here drives the in-app search modal via real clicks/keystrokes.
Raw JS DOM manipulation (setting .value + dispatching synthetic events) was
tried during discovery and did not reliably trigger the app's own filter/
validation bindings — Playwright's fill()/click() (real input events) does.

create_draft_listing() ALWAYS forces the Publish toggle off before saving —
the form defaults it on, and an accidental live listing is the one mistake
this module must never make.
"""
import re

from patchright.sync_api import sync_playwright

import scraper

LISTINGS_URL = "https://inv.viagogo.com/Listings"
NAV_TIMEOUT_MS = 30000
MODAL_TIMEOUT_MS = 15000

# Empirically confirmed from two real listings on this account (Caesarea
# Orchestra: $232 -> $208.80 proceeds; Middle Tier 1: $200 -> $180 proceeds) —
# viagogo takes a flat 10% seller commission. We compute Proceeds ourselves
# rather than rely on uncertain page auto-calc JS.
PROCEEDS_RATE = 0.90


class ViagogoListingError(RuntimeError):
    pass


def _open_listings_page(p):
    browser = scraper._connect_over_cdp(p, scraper.CDP_URL_VIAGOGO)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.goto(LISTINGS_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    return page


def _open_new_listing_modal(page):
    page.click('text="New Listing"')
    page.wait_for_selector("#modal #txtSearch", timeout=MODAL_TIMEOUT_MS)


def _search_rows(page, query):
    """Types `query` into the New Listing event picker (real keystrokes —
    raw value-setting doesn't trigger the picker's filter binding) and
    returns the matching rows as both data dicts and live element handles.
    """
    search_box = page.locator("#modal #txtSearch")
    search_box.click()
    search_box.fill("")
    page.keyboard.type(query, delay=40)
    page.wait_for_timeout(700)  # debounce — the picker filters async
    rows = page.query_selector_all("#modal tr.pointer")
    out = []
    for r in rows:
        link = r.get_attribute("data-eventlink") or ""
        m = re.search(r"(\d+)$", link)
        if not m:
            continue
        tds = r.query_selector_all("td")
        if len(tds) < 2:
            continue
        date_kids = tds[0].query_selector_all(":scope > *")
        name_kids = tds[1].query_selector_all(":scope > *")

        def _t(kids, i):
            return kids[i].inner_text().strip() if len(kids) > i else ""

        out.append({
            "event_id": m.group(1),
            "event_name": _t(name_kids, 0),
            "venue": _t(name_kids, 1),
            "city": _t(name_kids, 2),
            "weekday": _t(date_kids, 0),
            "date": _t(date_kids, 1),
            "time": _t(date_kids, 2),
            "_row": r,
        })
    return out


def search_event(query, limit=10):
    """Search viagogo's New Listing event picker for `query`.

    Returns up to `limit` candidate dicts: {event_id, event_name, venue,
    city, weekday, date, time}. Read-only — opens its own page, never
    creates or modifies anything, and closes the page before returning.
    """
    with sync_playwright() as p:
        page = _open_listings_page(p)
        try:
            _open_new_listing_modal(page)
            rows = _search_rows(page, query)
            return [{k: v for k, v in r.items() if k != "_row"} for r in rows[:limit]]
        finally:
            try:
                page.close()
            except Exception:
                pass


def _fill_row(page, row_value):
    """Best-effort: some events expose a known-rows <select>, others a free
    text <input>, both named Listing.Row (only one is visible at a time).
    Row isn't a required field, so failures here are swallowed."""
    if not row_value:
        return
    try:
        select = page.locator('select[name="Listing.Row"]')
        if select.count() and select.first.is_visible():
            select.first.select_option(label=str(row_value))
            return
    except Exception:
        pass
    try:
        text_input = page.locator('input[name="Listing.Row"]')
        if text_input.count() and text_input.first.is_visible():
            text_input.first.fill(str(row_value))
    except Exception:
        pass


def create_draft_listing(event_id, search_query, ticket_type, section,
                          available_tickets, website_price, face_value,
                          currency="USD", proceeds=None, row=None,
                          seat_from=None, seat_to=None, max_display_quantity=None):
    """Creates a viagogo listing in DRAFT (unpublished) state and saves it.

    event_id      — numeric viagogo event id from a prior search_event() call.
    search_query  — re-run against the picker to locate the row; there's no
                    deep link into the create flow so this replays the search.
    ticket_type   — "E-Tickets" or "Paper Tickets" (exact tile text).
    section       — must be an exact <option> value on this event's Section
                    dropdown (see db.viagogo_section_map_get for the Kupat ->
                    viagogo section-name translation).
    proceeds      — defaults to website_price * PROCEEDS_RATE if not given.

    Caller is responsible for getting explicit user approval before calling
    this — this function actually saves the listing (as a draft).
    """
    with sync_playwright() as p:
        page = _open_listings_page(p)
        try:
            _open_new_listing_modal(page)
            rows = _search_rows(page, search_query)
            match = next((r for r in rows if r["event_id"] == str(event_id)), None)
            if not match:
                raise ViagogoListingError(
                    f"event {event_id} not found re-searching '{search_query}' "
                    f"(picker returned {[r['event_id'] for r in rows]})"
                )
            match["_row"].click()
            page.wait_for_timeout(400)

            tile = page.locator(".js-select.tile", has_text=ticket_type).first
            tile.click()
            page.wait_for_selector('input[name="Listing.AvailableTickets"]', timeout=MODAL_TIMEOUT_MS)

            page.fill('input[name="Listing.AvailableTickets"]', str(available_tickets))
            page.select_option('select[name="Listing.Section"]', section)
            _fill_row(page, row)
            if seat_from:
                page.fill('input[name="Listing.SeatFrom"]', str(seat_from))
            if seat_to:
                page.fill('input[name="Listing.SeatTo"]', str(seat_to))

            resolved_proceeds = proceeds if proceeds is not None else round(website_price * PROCEEDS_RATE, 2)
            page.fill('input[name="Listing.WebsitePrice"]', f"{website_price:.2f}")
            page.fill('input[name="Listing.Proceeds"]', f"{resolved_proceeds:.2f}")
            page.select_option('select[name="Listing.CurrencyCode"]', currency)
            page.fill('input[name="Listing.FaceValue"]', f"{face_value:.2f}")
            if max_display_quantity is not None:
                page.fill('input[name="Listing.MaxDisplayQuantity"]', str(max_display_quantity))

            # Hard safety: force draft/unpublished regardless of the form's
            # default-on toggle, every single time, no exceptions. The real
            # checkbox is visually hidden behind a styled toggle (zero-size,
            # so Playwright can't click it directly) — click its <label>
            # instead and verify state on the checkbox itself.
            publish_checkbox = page.locator("#IsPublishToViagogo")
            publish_toggle = page.locator('label[for="IsPublishToViagogo"]')
            if publish_checkbox.is_checked():
                publish_toggle.click()
            if publish_checkbox.is_checked():
                raise ViagogoListingError("failed to uncheck Publish toggle — refusing to save")

            page.click("#btnSaveDetails")
            # Save doesn't commit the listing by itself — viagogo inserts a
            # legal attestation step ("I confirm I own these tickets...")
            # that must be explicitly accepted before the listing exists.
            # If validation errors block the form, this selector never
            # appears and wait_for_selector raises — surfacing the failure
            # instead of silently returning a listing that was never saved.
            page.wait_for_selector("#modal a.js-ok", timeout=MODAL_TIMEOUT_MS)
            page.click("#modal a.js-ok")
            page.wait_for_timeout(2000)

            return {
                "event_id": str(event_id),
                "event_name": match["event_name"],
                "venue": match["venue"],
                "section": section,
                "website_price": website_price,
                "proceeds": resolved_proceeds,
                "face_value": face_value,
                "published": False,
            }
        finally:
            try:
                page.close()
            except Exception:
                pass


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("usage: python viagogo_listing.py <search query>")
        sys.exit(2)
    results = search_event(sys.argv[1])
    print(json.dumps(results, ensure_ascii=False, indent=2))
