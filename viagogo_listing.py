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

create_draft_listing() defaults to forcing the Publish toggle OFF before
saving (draft) — the form defaults it on, and an accidental live listing is
the one mistake this module must never make. Pass publish=True to explicitly
go live; the toggle state is then asserted rather than assumed.
"""
import re
import traceback

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
    # The picker filters async. Wait for at least one row to render (up to
    # 5s — a laggy page can take well over the old flat 700ms) rather than
    # sleeping a fixed amount and reading an empty, still-filtering list.
    # Don't fail hard if the query legitimately has no matches.
    try:
        page.wait_for_selector("#modal tr.pointer", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(300)  # brief settle after the first row appears
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


def _click_event_row(page, event_id, match_row=None):
    """Open an event's ticket-type step by clicking its picker row.

    Uses a FRESH locator keyed on the event id rather than the ElementHandle
    captured during the initial search: the picker re-renders as its async
    filter settles, which detaches the old handle — and clicking a detached
    handle hangs until the full timeout instead of failing fast (the "stall
    that a page refresh fixes"). A Playwright locator re-resolves the element
    at click time and auto-waits for it to be actionable. Falls back to the
    captured handle only if the locator can't find the row.
    """
    row = page.locator(f'#modal tr.pointer[data-eventlink$="{event_id}"]').first
    try:
        row.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
        row.scroll_into_view_if_needed()
        row.click()
        return
    except Exception:
        if match_row is None:
            raise
    match_row.click()


def _select_ticket_type_tile(page, ticket_type):
    """Click the ticket-type tile, auto-waiting for it to be actionable
    (replaces a fixed post-click sleep that was too short on a laggy page)."""
    tile = page.locator(".js-select.tile", has_text=ticket_type).first
    tile.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
    tile.click()


def fetch_sections(event_id, search_query, ticket_type="E-Tickets"):
    """Return the list of section names available for *event_id* on viagogo.

    Navigates to the New Listing modal, picks the event row and ticket-type
    tile, reads the Listing.Section <select> options, then closes the page.
    Takes ~10 s (drives the live browser). Results should be cached by the
    caller — nothing is written.
    """
    with sync_playwright() as p:
        page = _open_listings_page(p)
        try:
            _open_new_listing_modal(page)
            rows = _search_rows(page, search_query)
            match = next((r for r in rows if r["event_id"] == str(event_id)), None)
            if not match:
                raise ViagogoListingError(
                    f"event {event_id} not found re-searching '{search_query}'"
                )
            _click_event_row(page, event_id, match.get("_row"))
            _select_ticket_type_tile(page, ticket_type)
            page.wait_for_selector('select[name="Listing.Section"]', timeout=MODAL_TIMEOUT_MS)
            options = page.query_selector_all('select[name="Listing.Section"] option')
            sections = []
            for o in options:
                val = (o.get_attribute("value") or "").strip()
                text = (o.inner_text() or "").strip()
                label = val or text
                if label:
                    sections.append(label)
            return sections
        finally:
            try:
                page.close()
            except Exception:
                pass


def download_ticket_pdfs(ticket_url, qty=1):
    """Render each Kupat e-ticket to its own PDF using headless Chromium.

    The Kupat viewer is a public link (no login), so this uses a fresh
    headless Chromium rather than the CDP session. The tickets live in a
    Swiper carousel: the first slide is an intro ("N tickets — swipe to
    scan", no barcode) and each remaining slide is one real ticket with a
    QR/barcode. page.pdf() only renders the *active* slide, so we advance
    the carousel and PDF each slide that actually carries a barcode, up to
    `qty`. Returns a list of PDF bytes (one per ticket).
    """
    _margin = {"top": "8mm", "bottom": "8mm", "left": "8mm", "right": "8mm"}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(ticket_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2500)

            slides = page.query_selector_all(".swiper-slide")
            if not slides:
                # Not the expected carousel — best effort: one full-page PDF.
                return [page.pdf(print_background=True, format="A4", margin=_margin)]

            pdfs = []
            seen = set()
            # Walk the carousel a bounded number of steps (loop mode never
            # disables next), PDF-ing each distinct barcode-bearing slide.
            for _ in range(len(slides) + 3):
                active = page.query_selector(".swiper-slide-active")
                if active:
                    # Key on DOM position, not text: both real-ticket slides
                    # share the same leading order-number text, so a text key
                    # collides and drops the 2nd ticket.
                    key = active.evaluate(
                        "e => String([...document.querySelectorAll('.swiper-slide')].indexOf(e))"
                    )
                    has_code = active.query_selector(".qr-code-box, canvas, .barcode")
                    if has_code and key not in seen:
                        seen.add(key)
                        pdfs.append(page.pdf(print_background=True, format="A4", margin=_margin))
                        if len(pdfs) >= qty:
                            break
                nxt = page.query_selector(".swiper-button-next")
                if not nxt or "swiper-button-disabled" in (nxt.get_attribute("class") or ""):
                    break
                nxt.click()
                page.wait_for_timeout(1200)
            return pdfs
        finally:
            browser.close()


def _upload_ticket_pdfs(page, ticket_pdfs, event_id, section):
    """Attach ticket PDFs to a just-created listing via its E-Tickets flow.

    On the Listings page each event card expands to per-section rows; the
    row for a listing that still needs tickets shows an 'Upload Now' link
    (class js-upload-etickets) that opens /Listings/UploadETickets. We set
    the PDFs on that page's file input and click its 'continue' save button
    (class js-save). Non-fatal — the listing already exists; the caller
    swallows failures. Cleans up temp files regardless.
    """
    import tempfile, os, shutil
    tmp_dir = tempfile.mkdtemp(prefix="kartis_tickets_")
    try:
        paths = []
        for i, data in enumerate(ticket_pdfs):
            pth = os.path.join(tmp_dir, f"ticket_{i + 1}.pdf")
            with open(pth, "wb") as fh:
                fh.write(data)
            paths.append(pth)

        page.goto(LISTINGS_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(2500)

        # Expand the event's listing card so its per-section E-Ticket rows
        # (each with an 'Upload Now' link) render. The card shows the event
        # id in a [<id>] span; climb to the clickable card ancestor.
        try:
            page.wait_for_selector(
                f"xpath=//span[contains(text(),'{event_id}')]", timeout=MODAL_TIMEOUT_MS
            )
        except Exception:
            pass
        node = page.query_selector(f"xpath=//span[contains(text(),'{event_id}')]")
        if node is None:
            raise ViagogoListingError(
                f"listing card for event {event_id} not found on Listings page"
            )
        card = node
        for _ in range(6):
            parent = card.query_selector("xpath=..")
            if not parent:
                break
            card = parent
            box = card.bounding_box()
            if box and box.get("height", 0) > 60:
                break
        card.click()
        page.wait_for_timeout(2500)

        # Pick the 'Upload Now' link in the row matching our section (a fresh
        # listing is the one still showing Upload Now rather than View).
        # Normalize whitespace both sides — the stored section can carry
        # doubled/odd spacing that won't substring-match the rendered row.
        sec_norm = re.sub(r"\s+", " ", section or "").strip()
        upload_link = None
        for h in page.query_selector_all(".js-upload-etickets"):
            rowtext = h.evaluate(
                "e => { let n=e; for (let i=0;i<5&&n.parentElement;i++) n=n.parentElement; return n.innerText; }"
            )
            if sec_norm and sec_norm in re.sub(r"\s+", " ", rowtext):
                upload_link = h
                break
        if upload_link is None:
            upload_link = page.query_selector(".js-upload-etickets")
        if upload_link is None:
            raise ViagogoListingError("no 'Upload Now' e-ticket link found for this listing")

        upload_link.click()
        page.wait_for_selector("#js-preUploadInput, #js-activeUploadInput", timeout=MODAL_TIMEOUT_MS)
        file_input = (page.query_selector("#js-preUploadInput")
                      or page.query_selector("#js-activeUploadInput"))
        file_input.set_input_files(paths)  # the input is multiple — set all at once
        # Let the uploads finish before committing.
        page.wait_for_timeout(2000 + 2500 * len(paths))

        # If the exact ticket was uploaded before, viagogo stacks an "already
        # uploaded — do you still want to upload?" confirm dialog per file.
        # Dismiss from the top of the stack so a lower one can't intercept the
        # next click. A fresh listing normally shows none of these.
        for _ in range(2 * len(paths) + 2):
            confirms = [c for c in page.query_selector_all(".js-vgDialog-button-confirm")
                        if c.is_visible()]
            if not confirms:
                break
            confirms[-1].click()
            page.wait_for_timeout(1500)

        save = page.locator(".js-save").first
        save.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
        save.click()
        # Commit navigates back to the Listings page — treat that as success;
        # if we're still on UploadETickets after the wait the commit didn't take.
        try:
            page.wait_for_url("**/Listings", timeout=MODAL_TIMEOUT_MS)
        except Exception:
            page.wait_for_timeout(3000)
            if "UploadETickets" in page.url:
                raise ViagogoListingError(
                    "ticket upload did not commit (still on UploadETickets)"
                )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def create_draft_listing(event_id, search_query, ticket_type, section,
                          available_tickets, website_price, face_value,
                          currency="USD", proceeds=None, row=None,
                          seat_from=None, seat_to=None, max_display_quantity=None,
                          ticket_pdfs=None, publish=False):
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
            _click_event_row(page, event_id, match.get("_row"))
            _select_ticket_type_tile(page, ticket_type)
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

            # Drive the Publish toggle to the requested state and ASSERT it —
            # never assume. The real checkbox is visually hidden behind a
            # styled toggle (zero-size, so Playwright can't click it directly)
            # — click its <label> instead and verify state on the checkbox.
            # Default (publish=False) forces draft/unpublished, the safe path.
            publish_checkbox = page.locator("#IsPublishToViagogo")
            publish_toggle = page.locator('label[for="IsPublishToViagogo"]')
            if publish:
                if not publish_checkbox.is_checked():
                    publish_toggle.click()
                if not publish_checkbox.is_checked():
                    raise ViagogoListingError("failed to enable Publish toggle — refusing to save")
            else:
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

            if ticket_pdfs:
                try:
                    _upload_ticket_pdfs(page, ticket_pdfs, event_id, section)
                except Exception:
                    traceback.print_exc()
                    # Non-fatal — draft listing already saved

            return {
                "event_id": str(event_id),
                "event_name": match["event_name"],
                "venue": match["venue"],
                "section": section,
                "website_price": website_price,
                "proceeds": resolved_proceeds,
                "face_value": face_value,
                "published": bool(publish),
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
