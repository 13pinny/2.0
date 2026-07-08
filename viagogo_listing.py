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
import threading
import traceback
from contextlib import contextmanager

from patchright.sync_api import sync_playwright

import scraper

HOME_URL = "https://inv.viagogo.com/"
LISTINGS_URL = "https://inv.viagogo.com/Listings"
NAV_TIMEOUT_MS = 30000
MODAL_TIMEOUT_MS = 15000

# One modal flow at a time: every function here drives the same CDP Chrome,
# and a section-fetch racing an approve (or two approves) trips over the
# shared #modal state. Threads queue here instead of failing flaky.
_BROWSER_LOCK = threading.Lock()


@contextmanager
def _exclusive_browser(timeout=180):
    if not _BROWSER_LOCK.acquire(timeout=timeout):
        raise ViagogoListingError(
            "timed out waiting for another viagogo browser operation to finish"
        )
    try:
        yield
    finally:
        _BROWSER_LOCK.release()

# Empirically confirmed from two real listings on this account (Caesarea
# Orchestra: $232 -> $208.80 proceeds; Middle Tier 1: $200 -> $180 proceeds) —
# viagogo takes a flat 10% seller commission. We compute Proceeds ourselves
# rather than rely on uncertain page auto-calc JS.
PROCEEDS_RATE = 0.90


class ViagogoListingError(RuntimeError):
    pass


def _open_listings_page(p):
    """Fresh, warmed-up Listings page.

    The inv.viagogo.com SPA goes stale: deep-loading /Listings on a session
    that's been sitting often never renders its data. The manual remedy is
    always "close the tab, open inv.viagogo.com, then click through to
    Listings" — so replicate it: land on the root first, then navigate to
    Listings, verify the page chrome actually rendered (the New Listing
    button), and reload once if it didn't.
    """
    browser = scraper._connect_over_cdp(p, scraper.CDP_URL_VIAGOGO)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(800)
    page.goto(LISTINGS_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    try:
        page.wait_for_selector('text="New Listing"', timeout=MODAL_TIMEOUT_MS)
    except Exception:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector('text="New Listing"', timeout=MODAL_TIMEOUT_MS)
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
    with _exclusive_browser(), sync_playwright() as p:
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


def _select_ticket_type_tile(page, ticket_type, event_id=None, match_row=None):
    """Click the ticket-type tile, auto-waiting for it to be actionable.

    When Chrome is under load (e.g. the hourly scrape driving the same
    browser), the event-row click can silently not register, so the tiles
    never render. If the tile doesn't appear and we know the row, re-click
    it once and wait again (longer) before giving up.
    """
    tile = page.locator(".js-select.tile", has_text=ticket_type).first
    try:
        tile.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
    except Exception:
        if event_id is None:
            raise
        _click_event_row(page, event_id, match_row)
        tile.wait_for(state="visible", timeout=2 * MODAL_TIMEOUT_MS)
    tile.click()


def fetch_sections(event_id, search_query, ticket_type="E-Tickets"):
    """Return the list of section names available for *event_id* on viagogo.

    Navigates to the New Listing modal, picks the event row and ticket-type
    tile, reads the Listing.Section <select> options, then closes the page.
    Takes ~10 s (drives the live browser). Results should be cached by the
    caller — nothing is written.
    """
    with _exclusive_browser(), sync_playwright() as p:
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
            _select_ticket_type_tile(page, ticket_type, event_id, match.get("_row"))
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


def seats_range(seats_str):
    """'117 - 118' → ['117', '118']; '57' → ['57']; '' → []."""
    nums = re.findall(r"\d+", seats_str or "")
    if len(nums) == 2:
        a, b = sorted((int(nums[0]), int(nums[1])))
        if b - a <= 60:
            return [str(n) for n in range(a, b + 1)]
    return nums


def _select_by_seats(items, seats, row=None):
    """Pick each listing's OWN tickets out of a whole-order capture.

    A single order can hold several seat groups that become separate
    listings — the ticket viewer link always shows every ticket in the
    order, so each expected seat number must be matched (as a standalone
    number, optionally requiring the row number too) against the ticket's
    visible text. Any ambiguity or miss raises rather than guessing: wrong
    tickets to a buyer is worse than no tickets.
    `items` are {"pdf": bytes, "text": str} dicts."""
    chosen, used = [], set()
    row_rx = re.compile(r"(?<!\d)" + re.escape(str(row)) + r"(?!\d)") if row else None
    for seat in seats:
        rx = re.compile(r"(?<!\d)" + re.escape(str(seat)) + r"(?!\d)")
        matches = [i for i, it in enumerate(items)
                   if i not in used and rx.search(it["text"])
                   and (row_rx is None or row_rx.search(it["text"]))]
        if len(matches) != 1:
            raise ViagogoListingError(
                f"seat {seat}" + (f" row {row}" if row else "") +
                f": {len(matches)} matching tickets in the order — cannot assign safely"
            )
        used.add(matches[0])
        chosen.append(items[matches[0]]["pdf"])
    return chosen


def download_ticket_pdfs_for(ticket_url, qty=1, seats=None, row=None):
    """Route a ticket-viewer URL to the right per-site renderer.

    - tickchak (either the confirmation email's static emailLink URL or the
      /n/ viewer URL) → official print PDF split per page, with the
      screenshot carousel as fallback.
    - anything else (Kupat share links incl. SendGrid-wrapped, and
      ticketmaster.co.il/t/…) → the Swiper walker below; both sites render
      their e-tickets in a Swiper carousel on a public tokenized link.

    `seats` (list of seat-number strings, optionally with `row`): select
    exactly those tickets out of the order — required when one order was
    split into multiple listings, harmless (an identity check) otherwise.
    """
    low = (ticket_url or "").lower()
    seats = [s for s in (seats or []) if s][:qty] if qty else list(seats or [])
    if "tickchak.co.il" in low:
        try:
            pdfs = download_tickchak_pdfs(ticket_url, qty=None if seats else qty)
        except Exception:
            traceback.print_exc()
            if "/n/" not in low:
                raise
            import tickchak_tickets  # lazy: pulls in PyMuPDF
            cards = tickchak_tickets.scrape_ticket_url(ticket_url)
            pdfs = [c["pdf_bytes"] for c in cards]
        if seats:
            # The official print PDF has a text layer — match on it.
            import fitz
            items = []
            for d in pdfs:
                doc = fitz.open(stream=d, filetype="pdf")
                items.append({"pdf": d, "text": " ".join(pg.get_text() for pg in doc)})
                doc.close()
            pdfs = _select_by_seats(items, seats, row)
        elif qty:
            pdfs = pdfs[:qty]
    else:
        if seats:
            items = download_ticket_pdfs(ticket_url, qty=qty, with_text=True)
            pdfs = _select_by_seats(items, seats, row)
        else:
            pdfs = download_ticket_pdfs(ticket_url, qty=qty)
    if not pdfs:
        raise ViagogoListingError(f"no ticket PDFs captured from {ticket_url[:80]}")
    if qty and len(pdfs) < qty:
        raise ViagogoListingError(
            f"only {len(pdfs)}/{qty} ticket PDFs captured — refusing partial upload"
        )
    # Hard gate: no PDF leaves here without a decodable QR (see the
    # 2026-07-04 blank-upload incident in validate_ticket_pdfs).
    return validate_ticket_pdfs(pdfs)


def _resolve_tickchak_viewer(page):
    """The confirmation email links to a static rendered-email page whose
    'הציגו כרטיס' button chains (via tic.li) into the real /n/ ticket
    viewer. If we're not on /n/ yet, follow it."""
    if "/n/" in page.url:
        return
    href = page.evaluate(
        """() => {
          const a = [...document.querySelectorAll('a')].find(e => (e.innerText || '').includes('הציגו כרטיס'));
          return a ? a.href : null;
        }"""
    )
    if not href:
        raise ViagogoListingError("tickchak: show-ticket link not found on email page")
    page.goto(href, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(2500)


def download_tickchak_pdfs(ticket_url, qty=None):
    """Official per-ticket PDFs for a tickchak order.

    The /n/ viewer's הדפסה button downloads one PDF with a page per ticket
    (probed live 2026-07-05: 8-ticket order → 8-page PDF with ticket numbers
    and order details) — split it into single-page PDFs with PyMuPDF.
    """
    import os
    import tempfile

    import fitz

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844}, locale="he-IL")
            page = ctx.new_page()
            page.goto(ticket_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2500)
            _resolve_tickchak_viewer(page)
            btn = page.locator("text=הדפסה").first
            btn.wait_for(state="visible", timeout=MODAL_TIMEOUT_MS)
            with page.expect_download(timeout=20000) as dl:
                btn.click()
            tmp = tempfile.mktemp(suffix=".pdf")
            dl.value.save_as(tmp)
            try:
                doc = fitz.open(tmp)
                pdfs = []
                for i in range(len(doc)):
                    nd = fitz.open()
                    nd.insert_pdf(doc, from_page=i, to_page=i)
                    pdfs.append(nd.tobytes())
                    nd.close()
                doc.close()
            finally:
                os.unlink(tmp)
            return pdfs[:qty] if qty else pdfs
        finally:
            browser.close()


def tickchak_order_details(ticket_url):
    """Order facts parsed from the official print PDF (one page/ticket):
    qty (page count), per-ticket price (מחיר הכרטיס — the emails carry no
    pricing, but the tickets do), total cost, and seating. Open-seating
    orders read "ישיבה לא מסומן" and get no section; seated orders are
    parsed best-effort with the same אזור/שורה/כיסא patterns as Kupat."""
    import fitz

    pdfs = download_tickchak_pdfs(ticket_url)
    out = {"qty": len(pdfs) or None, "cost_per_unit": None, "cost": None,
           "section": "", "row_label": "", "seats": "", "seating_text": ""}
    if not pdfs:
        return out
    prices = []
    for i, data in enumerate(pdfs):
        doc = fitz.open(stream=data, filetype="pdf")
        txt = doc[0].get_text()
        doc.close()
        m = re.search(r"מחיר הכרטיס\s*\n?\s*([\d,.]+)", txt)
        if m:
            try:
                prices.append(float(m.group(1).replace(",", "")))
            except ValueError:
                pass
        if i == 0:
            m = re.search(r"פרטי הכרטיס\s*\n(.*?)\n\s*מידע נוסף", txt, re.S)
            if m:
                out["seating_text"] = " ".join(m.group(1).split())
            if "לא מסומן" not in out["seating_text"]:
                m = re.search(r"אזור[:\s]+([^\n,]+)", txt)
                if m:
                    out["section"] = m.group(1).strip()
                m = re.search(r"שורה[:\s]+([^\s,]+)", txt)
                if m:
                    out["row_label"] = m.group(1).strip()
                m = re.search(r"(?:כיסא(?:ות)?|מושב(?:ים)?)[:\s]+(\d+(?:\s*[-–]\s*\d+)?)", txt)
                if m:
                    out["seats"] = re.sub(r"\s*([-–])\s*", r" \1 ", m.group(1).strip())
    if prices:
        out["cost_per_unit"] = prices[0]
        out["cost"] = round(sum(prices), 2)
    return out


def _dismiss_overlays(page):
    """Best-effort: clear cookie/consent overlays before printing tickets —
    TM IL's banner covers the bottom of every printed page otherwise.
    Decline non-essential where a reject button exists; then hide any
    remaining cookie/consent fixed elements outright."""
    try:
        btn = page.locator('button:has-text("Reject All"), button:has-text("Decline")').first
        if btn.count() and btn.is_visible():
            btn.click()
            page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        page.evaluate(
            """() => document.querySelectorAll(
                 '[class*="cookie" i], [id*="cookie" i], [class*="consent" i], [id*="consent" i]'
               ).forEach(e => { e.style.display = 'none'; })"""
        )
    except Exception:
        pass


def _png_to_pdf(png_bytes):
    """Wrap a PNG screenshot as a single-page PDF sized to the image."""
    import fitz

    img = fitz.open(stream=png_bytes, filetype="png")
    rect = img[0].rect
    img.close()
    doc = fitz.open()
    pg = doc.new_page(width=rect.width, height=rect.height)
    pg.insert_image(rect, stream=png_bytes)
    data = doc.tobytes()
    doc.close()
    return data


def _shoot_slides_direct(page, want, seen, with_text=False):
    """Fallback for carousels that can't be advanced (TM IL's Angular viewer
    locks the next button and hides the Swiper JS API): every slide is
    already rendered side by side, so element-screenshot each barcode-bearing
    slide directly. One card per capture — never the whole page."""
    out = []
    slides = page.query_selector_all(".swiper-slide")
    for i, s in enumerate(slides):
        if str(i) in seen or len(out) >= want:
            continue
        if not s.query_selector(".qr-code-box, canvas, .barcode"):
            continue
        sl = page.locator(".swiper-slide").nth(i)
        try:
            sl.scroll_into_view_if_needed()
        except Exception:
            pass
        pdf = _png_to_pdf(sl.screenshot(timeout=15000))
        out.append({"pdf": pdf, "text": s.inner_text() or ""} if with_text else pdf)
        seen.add(str(i))
    return out


def _barcode_decoder():
    """Best available barcode decoder as fn(np_image) -> payload|None.

    zxing-cpp reads every format the Israeli sites use — QR (Kupat, TM IL)
    AND Data Matrix (tickchak's official print PDFs; OpenCV's QR-only
    detector rejected those as 'no decodable QR', incident 2026-07-07).
    OpenCV stays as a QR-only fallback; None means no decoder installed.
    """
    try:
        import zxingcpp

        def _zx(arr):
            if arr.ndim == 3 and arr.shape[2] == 4:
                arr = arr[:, :, :3]
            res = zxingcpp.read_barcodes(arr)
            return res[0].text if res else None
        return _zx
    except ImportError:
        pass
    try:
        import cv2

        det = cv2.QRCodeDetector()

        def _cv(arr):
            if arr.ndim == 3:
                arr = cv2.cvtColor(
                    arr, cv2.COLOR_RGB2GRAY if arr.shape[2] == 3 else cv2.COLOR_RGBA2GRAY)
            txt, _, _ = det.detectAndDecode(arr)
            return txt or None
        return _cv
    except ImportError:
        return None


def validate_ticket_pdfs(pdfs):
    """Refuse to pass along any ticket PDF without a machine-decodable
    barcode (QR / Data Matrix / Aztec), and require distinct payloads.

    Guards against the 2026-07-04 incident: a capture taken mid-swipe
    printed only the page background — two blank maroon pages were uploaded
    to a live listing and a buyer would have been turned away at the gate.
    Raises ViagogoListingError naming the bad ticket. If no decoder is
    installed the check is skipped with a loud log line rather than
    blocking the pipeline.
    """
    decode = _barcode_decoder()
    if decode is None:
        print("[kartis] WARNING: zxing-cpp/opencv missing — ticket barcode validation SKIPPED")
        return pdfs
    import fitz
    import numpy as np

    payloads = []
    for i, data in enumerate(pdfs):
        doc = fitz.open(stream=data, filetype="pdf")
        payload = None
        for pg in doc:
            for dpi in (200, 300):
                pix = pg.get_pixmap(dpi=dpi)
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, pix.n)
                payload = decode(arr)
                if payload:
                    break
            if payload:
                break
        doc.close()
        if not payload:
            raise ViagogoListingError(
                f"ticket {i + 1}/{len(pdfs)} has no decodable barcode — refusing to use it"
            )
        payloads.append(payload)
    # Each buyer must get a DIFFERENT ticket — duplicate payloads mean the
    # capture shot the same slide twice.
    if len(set(payloads)) != len(payloads):
        raise ViagogoListingError(
            "duplicate barcode payloads across ticket PDFs — same ticket captured twice"
        )
    return pdfs


def _advance_carousel(page):
    """Advance a Swiper carousel one slide; True if it advanced.

    Kupat renders clickable .swiper-button-next arrows; TM IL's viewer has
    the same Swiper DOM but no clickable next button (that's why the walker
    used to stop after one TM ticket) — fall back to driving the Swiper
    instance directly via its JS API.
    """
    nxt = page.query_selector(".swiper-button-next")
    if nxt and nxt.is_visible() and "swiper-button-disabled" not in (nxt.get_attribute("class") or ""):
        nxt.click()
        return True
    return bool(page.evaluate(
        """() => {
          for (const el of document.querySelectorAll('.swiper, .swiper-container')) {
            const sw = el.swiper;
            if (!sw) continue;
            if (sw.isEnd && !(sw.params && sw.params.loop)) return false;
            sw.slideNext();
            return true;
          }
          return false;
        }"""
    ))


def download_ticket_pdfs(ticket_url, qty=1, with_text=False):
    """Render each e-ticket in a Swiper-carousel viewer to its own PDF using
    headless Chromium (public tokenized links — no login; used for Kupat and
    Ticketmaster IL). Kupat's first slide is an intro ("N tickets — swipe to
    scan", no barcode); TM IL's first slide is already ticket 1. We advance
    the carousel and element-screenshot each slide that actually carries a
    barcode, up to `qty`. Returns a list of PDF bytes (one per ticket).

    with_text=True captures EVERY barcode slide in the order (qty ignored)
    and returns [{"pdf": bytes, "text": slide innerText}] so the caller can
    match tickets to seats (multi-listing orders).
    """
    if with_text:
        qty = 99
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            # device_scale_factor=2: screenshots are the deliverable now, so
            # render crisp enough that the QR scans from a phone screen.
            ctx = browser.new_context(device_scale_factor=2)
            page = ctx.new_page()
            page.goto(ticket_url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(2500)
            _dismiss_overlays(page)

            slides = page.query_selector_all(".swiper-slide")
            if not slides:
                raise ViagogoListingError(
                    f"no ticket carousel found at {page.url[:100]}"
                )

            pdfs = []
            seen = set()
            # Walk the carousel a bounded number of steps (loop mode never
            # disables next). Each ticket is captured as an ELEMENT screenshot
            # of its slide — Playwright waits for the element to stop moving
            # first, which kills the mid-swipe race that once produced blank
            # full-page prints (2026-07-04 incident), and the output is the
            # ticket card alone rather than an A4 page of background.
            for _ in range(len(slides) + 3):
                active = page.locator(".swiper-slide-active").first
                has_code = False
                try:
                    active.locator(".qr-code-box, canvas, .barcode").first.wait_for(
                        state="visible", timeout=8000)
                    has_code = True
                except Exception:
                    pass
                # Key on DOM position, not text: both real-ticket slides
                # share the same leading order-number text, so a text key
                # collides and drops the 2nd ticket.
                key = active.evaluate(
                    "e => String([...document.querySelectorAll('.swiper-slide')].indexOf(e))"
                )
                if has_code and key not in seen:
                    seen.add(key)
                    pdf = _png_to_pdf(active.screenshot(timeout=15000))
                    pdfs.append({"pdf": pdf, "text": active.inner_text() or ""}
                                if with_text else pdf)
                    if len(pdfs) >= qty:
                        break
                if not _advance_carousel(page):
                    if len(pdfs) < qty:
                        # Locked pre-rendered carousel (TM IL): slides can't
                        # be activated, so shoot each one directly.
                        pdfs.extend(_shoot_slides_direct(
                            page, qty - len(pdfs), seen, with_text=with_text))
                    break
                page.wait_for_timeout(800)
            return pdfs
        finally:
            browser.close()


_MARKET_ROW_RE = re.compile(r'<tr data-id="(\d*)"[^>]*class="([^"]*)"[^>]*>(.*?)</tr>', re.S)
_MARKET_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _find_owned_listing_id(page, event_id, section, row=None, qty=None):
    """Resolve OUR listing's id on an event via /Listings/MarketDataV3 —
    server-truth, immune to the /Listings page's lagging event-card list.
    Owned rows carry class "owned"; cells are [_, section, row, qty, price,
    proceeds]. `row` and `qty` disambiguate same-section listings (a split
    order can put two Orchestra listings on one event; a GA event can hold
    several of our listings); newest id wins a tie."""
    resp = page.request.post(
        "https://inv.viagogo.com/Listings/MarketDataV3",
        form={"eventId": str(event_id), "latestServerStamp": "0"},
    )
    if not resp.ok:
        raise ViagogoListingError(f"MarketDataV3 {resp.status} for event {event_id}")

    def _clean(cell):
        txt = re.sub(r"<[^>]+>", " ", cell)
        txt = txt.replace("&nbsp;", " ")
        return re.sub(r"\s+", " ", txt).strip()

    sec_norm = re.sub(r"\s+", " ", section or "").strip().lower()
    cand = []
    for rid, cls, body in _MARKET_ROW_RE.findall(resp.text()):
        if "owned" not in cls or not rid:
            continue
        tds = [_clean(c) for c in _MARKET_TD_RE.findall(body)]
        if len(tds) < 4:
            continue
        if sec_norm and sec_norm not in tds[1].lower():
            continue
        if row is not None and str(row).strip() and tds[2] != str(row).strip():
            continue
        if qty is not None and tds[3] != str(qty):
            continue
        cand.append(int(rid))
    if not cand:
        raise ViagogoListingError(
            f"no owned listing found on event {event_id} matching "
            f"section '{section}'" + (f" row {row}" if row else "")
            + (f" qty {qty}" if qty else "")
        )
    return str(max(cand))


def _upload_ticket_pdfs(page, ticket_pdfs, event_id, section, row=None):
    """Attach ticket PDFs to a just-created listing via its E-Tickets flow:
    resolve the listing id (MarketDataV3), deep-link its UploadETickets
    page, set the PDFs on the file input, assign each to a seat slot, and
    save. Non-fatal — the listing already exists; the caller surfaces
    failures. Cleans up temp files regardless.
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

        # Resolve the listing id server-side, then deep-link its upload form.
        # NEVER hunt the /Listings DOM for it: the event card list lags —
        # the first listing on a brand-new event has no card for a while
        # after creation (missed uploads: Shlomo Artzi 2026-07-07, Ben Tzur
        # 2026-07-08). MarketDataV3 shows our own rows (class "owned")
        # immediately, and /Listings/UploadETickets?listingId=<id> renders
        # the upload form directly (probe-confirmed 2026-07-08).
        target_id = _find_owned_listing_id(page, event_id, section, row,
                                           qty=len(ticket_pdfs) or None)
        page.goto(
            "https://inv.viagogo.com/Listings/UploadETickets?listingId="
            + target_id + "&returnUrl=%2FListings",
            wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS,
        )
        try:
            page.wait_for_selector("#js-preUploadInput, #js-activeUploadInput",
                                   timeout=MODAL_TIMEOUT_MS)
        except Exception:
            # The UploadETickets page renders EMPTY for a listing that
            # already has tickets attached (probe-confirmed 2026-07-08) —
            # far more likely than a genuine render failure.
            raise ViagogoListingError(
                f"upload form did not render for listing {target_id} — "
                f"it probably already has tickets attached"
            )
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

        # Assign + commit WITHOUT the drag. The UI wants each uploaded page
        # (a .js-ticketThumb in #js-excViewport) dragged onto a seat slot
        # (.js-incDropTarget), but eticketupload.js's own save is just a form
        # post: it writes the comma-separated page data-ids into the hidden
        # #eTicketSelectionPageIds field, sets #eTicketSelectionSubmitMode=1,
        # and submits form.js-eticket-state. jQuery-UI 1.9.2 drags via
        # synthetic mouse events proved hopelessly flaky over CDP (the pool
        # thumbs sit below the fold, and the module-scoped jQuery isn't
        # reachable to call includePage directly), so we replicate the save
        # post directly — the server takes the pageIds field, not the DOM.
        page.wait_for_selector("#js-excViewport .js-ticketThumb", timeout=MODAL_TIMEOUT_MS)
        ids = page.evaluate(
            "() => [...document.querySelectorAll('#js-excViewport .js-ticketThumb')]"
            ".map(t => t.getAttribute('data-id')).filter(Boolean)"
        )
        # Seat listings expose one .js-incDropTarget per required page; GA /
        # non-allocation listings expose none, in which case every uploaded
        # page counts.
        required = page.evaluate(
            "() => document.querySelectorAll('#js-incViewport .js-incDropTarget').length"
        )
        if required and len(ids) < required:
            raise ViagogoListingError(
                f"uploaded {len(ids)} page(s) but listing needs {required}"
            )
        use = ids[:required] if required else ids
        if not use:
            raise ViagogoListingError("no uploaded ticket pages to assign")
        page.evaluate(
            "(v) => { document.querySelector('#eTicketSelectionPageIds').value = v;"
            " document.querySelector('#eTicketSelectionSubmitMode').value = '1'; }",
            ",".join(use),
        )
        page.evaluate(
            "() => { const f = document.querySelector('.js-eticket-state');"
            " (f.submit ? f.submit() : f.requestSubmit()); }"
        )
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
    with _exclusive_browser(), sync_playwright() as p:
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
            _select_ticket_type_tile(page, ticket_type, event_id, match.get("_row"))
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

            tickets_uploaded = False
            ticket_upload_error = None
            if ticket_pdfs:
                try:
                    _upload_ticket_pdfs(page, ticket_pdfs, event_id, section, row=row)
                    tickets_uploaded = True
                except Exception as e:
                    # Non-fatal — the listing already exists — but the caller
                    # must surface it: a listing without tickets is a seller
                    # default waiting to happen.
                    ticket_upload_error = f"{type(e).__name__}: {e}"
                    traceback.print_exc()

            return {
                "event_id": str(event_id),
                "event_name": match["event_name"],
                "venue": match["venue"],
                "section": section,
                "website_price": website_price,
                "proceeds": resolved_proceeds,
                "face_value": face_value,
                "published": bool(publish),
                "tickets_uploaded": tickets_uploaded,
                "ticket_upload_error": ticket_upload_error,
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
