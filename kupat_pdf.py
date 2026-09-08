"""Print each ticket from a Kupat e-document URL into its own PDF.

Kupat shares a single document URL of the shape
    https://tickets.kupat.co.il/edocument/<uuid>/?id=<numeric>
The page is a Swiper.js carousel — N tickets, all the same event but
different seats. Each "slide" is a printable ticket. We:

1. Hit /api/edocument/<uuid>?id=<numeric> directly (anonymous JSON, no
   Cloudflare blocking) to get the ticket array up front. That gives us
   featureName / presentationDateTime / venueName / sectionName / row /
   seat / customerName for every ticket — used for filenames AND to
   sanity-check the count before launching the browser.

2. Open the URL via patchright Chromium. For each ticket index:
     - jump the swiper to that slide via the bullet-pagination button
       ('[aria-label="Go to slide N"]') — the carousel is RTL but the
       aria-labels stay 1-based LTR
     - page.pdf() with print_background=True
     - write the PDF to <base_dir>/<EventName_Date>/Section_Row_Seat.pdf

3. Return a manifest the Flask route can hand to the dashboard so the
   user sees what landed where.

Filenames are sanitized via _safe_name — Hebrew is preserved (folders
and PDFs stay readable in Explorer) but slashes / quotes / control
chars get scrubbed so the path is always a legal Windows path.

The browser stays headless (default). Set KUPAT_PDF_HEADLESS=0 to watch.
"""
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import gzip
from pathlib import Path
from datetime import datetime

SOURCE = "kupat"
SITE_BASE = "https://tickets.kupat.co.il"
REQUEST_TIMEOUT = 20
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip",
    "Referer": SITE_BASE + "/",
}

# Default save root if KARTIS_TICKETS_DIR isn't set. The repo lives in
# OneDrive so this gives the user free cloud-backup of their ticket PDFs
# for the same reason KARTIS_ATTACHMENTS_DIR did. Override by setting
# KARTIS_TICKETS_DIR in .env.
_DEFAULT_TICKETS_DIR = (
    Path(os.environ.get("USERPROFILE") or Path.home())
    / "OneDrive" / "KartisTickets"
)


class KupatPdfError(RuntimeError):
    pass


# --- URL parsing --------------------------------------------------------

_EDOC_RE = re.compile(
    r"^https?://tickets\.kupat\.co\.il/edocument/"
    r"(?P<uuid>[0-9a-f-]{36})/?(?:\?(?P<query>.*))?$",
    re.IGNORECASE,
)


def parse_url(url):
    """Return (uuid, doc_id) or raise. doc_id is the numeric `id` query param."""
    s = (url or "").strip()
    m = _EDOC_RE.match(s)
    if not m:
        raise KupatPdfError(
            "URL must look like https://tickets.kupat.co.il/edocument/<uuid>/?id=<numeric>"
        )
    uuid = m.group("uuid")
    query = m.group("query") or ""
    doc_id = None
    for part in query.split("&"):
        if part.startswith("id="):
            doc_id = part[3:]
            break
    if not doc_id:
        raise KupatPdfError("URL is missing the ?id=<numeric> parameter")
    return uuid, doc_id


# --- JSON metadata fetch ------------------------------------------------

def fetch_metadata(uuid, doc_id):
    """Return the parsed /api/edocument/<uuid>?id=<id> document.

    Anonymous endpoint — no Cloudflare gate on this one (unlike the
    seats-status endpoint which is browser-only).
    """
    api = f"{SITE_BASE}/api/edocument/{uuid}?id={doc_id}&_={int(time.time()*1000)}"
    req = urllib.request.Request(api, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            data = json.loads(body)
    except urllib.error.HTTPError as e:
        raise KupatPdfError(f"Kupat API returned {e.code}") from e
    except urllib.error.URLError as e:
        raise KupatPdfError(f"Kupat API unreachable: {e}") from e
    except (json.JSONDecodeError, KeyError) as e:
        raise KupatPdfError(f"Kupat API returned malformed JSON: {e}") from e

    doc = (data or {}).get("document") or {}
    tickets = doc.get("tickets") or []
    if not tickets:
        raise KupatPdfError("No tickets found in document")
    if doc.get("reservationIsCancelled"):
        raise KupatPdfError("Reservation is cancelled — no tickets to print")
    return doc


# --- Filename / folder name helpers ------------------------------------

# Anything Windows refuses + control chars + leading/trailing dots/spaces.
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(s, max_len=80):
    s = (s or "").strip()
    s = _FORBIDDEN_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "untitled"


def _date_for_name(dt_str):
    """'2026-02-26 21:00:00' → '2026-02-26'. Falls back to the raw string."""
    if not dt_str:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str.strip())
    return m.group(1) if m else _safe_name(dt_str)


def event_folder_name(ticket):
    name = _safe_name(ticket.get("featureName") or "Unknown event")
    date = _date_for_name(ticket.get("presentationDateTime") or "")
    return f"{name}_{date}".rstrip("_")


def ticket_filename(ticket):
    """<EventName>_<Date>_<Section>_Row<row>_Seat<seat>.pdf"""
    name = _safe_name(ticket.get("featureName") or "Ticket")
    date = _date_for_name(ticket.get("presentationDateTime") or "")
    section = _safe_name(ticket.get("sectionName") or "")
    row = _safe_name(str(ticket.get("row") or ""))
    seat = _safe_name(str(ticket.get("seat") or ""))
    parts = [name, date]
    if section:
        parts.append(section)
    if row:
        parts.append(f"Row{row}")
    if seat:
        parts.append(f"Seat{seat}")
    return "_".join(parts) + ".pdf"


# --- The print loop -----------------------------------------------------

def render_pdfs(url, base_dir=None, on_progress=None, headless=None):
    """Drive the browser, print each ticket. Returns a manifest dict.

    Parameters:
      url         – the Kupat e-document URL (any of /edocument/<uuid>/?id=…).
      base_dir    – where to drop the event folder. Defaults to
                    KARTIS_TICKETS_DIR or ~/OneDrive/KartisTickets.
      on_progress – optional callable(stage:str, info:dict) for status pings;
                    used by the Flask route to stream a tiny progress log.
      headless    – override headlessness. Falls back to KUPAT_PDF_HEADLESS
                    env var (default headless=True).

    Returns:
      {
        "ok": True,
        "url": <url>,
        "uuid": <uuid>, "doc_id": <doc_id>,
        "event_name": <featureName>, "event_date": <date>,
        "venue": <venueName>,
        "folder": "<absolute path>",
        "tickets": [ {seat, row, section, file, bytes}, ... ],
      }
    """
    uuid, doc_id = parse_url(url)
    if on_progress:
        on_progress("parse", {"uuid": uuid, "doc_id": doc_id})

    doc = fetch_metadata(uuid, doc_id)
    tickets = doc["tickets"]
    n = len(tickets)
    first = tickets[0]

    if base_dir is None:
        base_dir = os.environ.get("KARTIS_TICKETS_DIR") or str(_DEFAULT_TICKETS_DIR)
    base = Path(base_dir)
    folder = base / event_folder_name(first)
    folder.mkdir(parents=True, exist_ok=True)

    if on_progress:
        on_progress("metadata", {
            "ticket_count": n,
            "event_name": first.get("featureName"),
            "event_date": first.get("presentationDateTime"),
            "folder": str(folder),
        })

    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        raise KupatPdfError(
            "Need patchright + Chromium. "
            "`.venv\\Scripts\\python -m patchright install chromium`"
        ) from e

    if headless is None:
        headless = os.environ.get("KUPAT_PDF_HEADLESS", "1") != "0"

    saved = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        # 2x device scale → retina-quality screenshots so the embedded PNG
        # in each PDF stays sharp at print resolution.
        ctx = browser.new_context(
            locale="he-IL",
            viewport={"width": 1100, "height": 1700},
            device_scale_factor=2,
        )
        page = ctx.new_page()

        if on_progress:
            on_progress("navigate", {"url": url})
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # Wait for the carousel + at least one canvas (barcode) to render.
        try:
            page.wait_for_selector(".swiper-slide canvas", timeout=20000)
        except Exception:
            # Some layouts swap canvas for img/svg — fall back to a plain timeout.
            page.wait_for_timeout(4000)

        # The Kupat e-document carousel has a leading "cover" slide that
        # introduces the order ("N כרטיסים · החליקו לסריקה"), then one
        # slide per ticket. Detect by comparing slide count to JSON ticket
        # count — if there's exactly one extra slide we offset by 1, else
        # we trust 1:1 mapping (futureproof if Kupat drops the cover).
        slide_count = page.evaluate("() => document.querySelectorAll('.swiper-slide').length")
        slide_offset_known = (slide_count == n + 1)
        if on_progress:
            on_progress("layout", {
                "slide_count": slide_count,
                "ticket_count": n,
                "cover_slide": slide_offset_known,
            })

        # Also wait for accessibility-pane noise to quiet down so it doesn't
        # leak into the print. The vee-crm accessibility widget injects a
        # large <aside> at startup; we hide it via inline CSS so the PDF is
        # just the ticket.
        page.add_style_tag(content="""
            aside._vp_open_false, aside[class*=_vp_], #vplugin-toggler,
            #vplugin-interface { display: none !important; }
            html, body { background: white !important; }
            /* swiper bullets are decorative for a single-ticket print */
            .swiper-pagination, .swiper-button-next, .swiper-button-prev {
                display: none !important;
            }
        """)

        for i in range(n):
            t = tickets[i]
            if on_progress:
                on_progress("ticket", {
                    "index": i + 1, "total": n,
                    "section": t.get("sectionName"), "row": t.get("row"), "seat": t.get("seat"),
                })

            # Show only the slide we want. Just clicking the swiper bullet
            # doesn't help — page.pdf() renders the entire DOM, and the
            # carousel positions all slides via translate3d so they all
            # land in the printable area regardless of which is "active".
            # Hide every other slide and unset the wrapper's transform so
            # the chosen slide flows into the printable rectangle alone.
            #
            # Note: slide[0] is a "cover" intro slide ("8 כרטיסים · swipe to
            # scan"), tickets are slides[1..n]. The JSON `tickets` array is
            # 0-indexed so ticket i maps to slide i+1. We assume the cover
            # is always present; if it ever isn't we fall back to slide i.
            slide_target = i + 1 if slide_offset_known else i
            # Hide other slides + force the active one to flow at the top of
            # the page. Also bake every canvas inside the active slide into
            # an <img src=data:image/png>: Chromium's print-to-PDF path
            # serializes <canvas> as inline vector ops which some PDF
            # viewers/printer drivers silently drop ("blank ticket"
            # symptom). A real <img> lands as a regular Image XObject,
            # rendered reliably anywhere. Then return the active slide's
            # pixel size so the PDF call below can use a page format that
            # matches — without this, page.pdf("A4") emits a sheet with the
            # ticket pinned in the middle and 70% blank space around it.
            box = page.evaluate("""
(idx) => {
  // Strip padding/margin from the document so the ticket flows from (0,0).
  document.documentElement.style.margin = '0';
  document.documentElement.style.padding = '0';
  document.body.style.margin = '0';
  document.body.style.padding = '0';
  document.body.style.minHeight = 'auto';

  document.querySelectorAll('.swiper-slide').forEach((el, i) => {
    el.style.display = (i === idx) ? 'block' : 'none';
    el.style.transform = 'none';
    el.style.width = '100%';
    el.style.margin = '0';
    el.style.padding = '0';
  });
  document.querySelectorAll('.swiper-wrapper').forEach(w => {
    w.style.transform = 'none';
    w.style.width = '100%';
    w.style.display = 'block';
    w.style.margin = '0';
    w.style.padding = '0';
  });
  document.querySelectorAll('.swiper-container, .swiper').forEach(c => {
    c.style.width = '100%';
    c.style.height = 'auto';
    c.style.overflow = 'visible';
    c.style.margin = '0';
    c.style.padding = '0';
  });
  // Hoist the active slide all the way to the top — its ancestors may have
  // app-shell padding/margins (the ticket sat in the lower half of an A4
  // page before this fix because of those).
  const active = document.querySelectorAll('.swiper-slide')[idx];
  if (active) {
    let p = active.parentElement;
    while (p && p !== document.body) {
      p.style.margin = '0';
      p.style.padding = '0';
      p.style.minHeight = 'auto';
      p = p.parentElement;
    }
    // Repaint canvases that may have been culled while offscreen.
    window.dispatchEvent(new Event('resize'));
    // Bake each canvas → <img> so the QR code lands in the PDF as a real
    // raster image (Image XObject), not as fragile inline vector paint.
    active.querySelectorAll('canvas').forEach(c => {
      try {
        const dataUrl = c.toDataURL('image/png');
        const img = document.createElement('img');
        img.src = dataUrl;
        img.width = c.width;
        img.height = c.height;
        // Match canvas's box so the layout doesn't shift.
        const cs = getComputedStyle(c);
        img.style.cssText = c.style.cssText;
        img.style.width = cs.width;
        img.style.height = cs.height;
        c.parentNode.replaceChild(img, c);
      } catch (e) { /* tainted canvas — leave the canvas in place */ }
    });
    const r = active.getBoundingClientRect();
    return { w: Math.ceil(r.width), h: Math.ceil(r.height) };
  }
  return null;
}
""", slide_target)
            page.wait_for_timeout(350)  # let lazy canvases repaint

            # Capture the active slide as a PNG screenshot, then wrap it
            # in a PDF via Pillow. Why not page.pdf()? Chromium's print-to-
            # PDF path emits content Adobe Acrobat Reader sometimes
            # renders as a blank page (canvas-as-vector + tagged-pdf
            # quirks). A raster PNG embedded as an Image XObject in a
            # one-page PDF renders identically in every viewer (Adobe,
            # Edge, Chrome, Preview, mobile) and prints reliably to any
            # printer. Tradeoff: text isn't selectable in the PDF — for a
            # ticket that gets scanned at the venue, that's a fine trade.
            from PIL import Image
            if box and box.get("w") and box.get("h"):
                # Locate the active slide via the same selector trick we
                # used during DOM manipulation, get its element handle,
                # take a screenshot of just that element. element.screenshot()
                # respects device_scale_factor so we get retina pixel density.
                handle = page.evaluate_handle(
                    "(idx) => document.querySelectorAll('.swiper-slide')[idx]",
                    slide_target,
                ).as_element()
                if handle:
                    png_bytes = handle.screenshot(type="png", omit_background=False)
                else:
                    png_bytes = page.screenshot(type="png", full_page=False)
            else:
                png_bytes = page.screenshot(type="png", full_page=True)

            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            pdf_buf = io.BytesIO()
            # resolution=144 — the screenshot is 2x DPI; declaring 144 in
            # the PDF means each px maps to 1/144 inch on a printed page,
            # so the printed ticket comes out at its natural size (~5.5×8").
            img.save(pdf_buf, format="PDF", resolution=144.0)
            pdf_bytes = pdf_buf.getvalue()
            fname = ticket_filename(t)
            target = folder / fname
            target.write_bytes(pdf_bytes)
            saved.append({
                "index": i + 1,
                "section": t.get("sectionName"),
                "row": t.get("row"),
                "seat": t.get("seat"),
                "file": str(target),
                "filename": fname,
                "bytes": len(pdf_bytes),
            })
            if on_progress:
                on_progress("saved", {"file": str(target), "bytes": len(pdf_bytes)})

        browser.close()

    return {
        "ok": True,
        "url": url,
        "uuid": uuid,
        "doc_id": doc_id,
        "event_name": first.get("featureName"),
        "event_date": first.get("presentationDateTime"),
        "venue": first.get("venueName"),
        "folder": str(folder),
        "tickets": saved,
    }


# --- CLI entry point ---------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: kupat_pdf.py <kupat-edocument-url> [base_dir]")
        sys.exit(2)
    url = sys.argv[1]
    base_dir = sys.argv[2] if len(sys.argv) > 2 else None

    def _log(stage, info):
        print(f"[{stage}] {info}")

    out = render_pdfs(url, base_dir=base_dir, on_progress=_log)
    print()
    print(f"saved {len(out['tickets'])} ticket PDF(s) to {out['folder']}")
    for t in out["tickets"]:
        print(f"  {t['filename']}  ({t['bytes']:,} bytes)")
