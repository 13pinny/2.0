"""Redact identifying info from a Tickchak ticket PDF and split it into
one PDF per ticket (one page = one ticket).

Tickchak hands you a multi-page TCPDF document for a multi-seat purchase.
Before forwarding tickets to a buyer you want to strip:
  - the price block ("מחיר הכרטיס" label + value)
  - the buyer-details cell content (name + phone)
  - the Tickchak logo at the bottom-left

The QR code, event name/date/venue, section, row, seat, and the
bottom-right event poster all stay intact.

Per-page metadata (event, section, row, seat) is parsed out of the text
layer and used for output filenames in the same scheme as kupat_pdf.py
(<EventName>_<Date>_<Section>_Row<R>_Seat<S>.pdf), so all ticket PDFs
land in a single folder per event under KARTIS_TICKETS_DIR.

Approach: PyMuPDF (fitz). Text search finds the label anchors; we add
redact annotations relative to those anchors and apply_redactions()
scrubs both the visible glyphs and the underlying text. The Tickchak
logo is an embedded image — we locate the one in the bottom-left
quadrant by bbox and redact its rect.
"""
import os
import re
from pathlib import Path

import fitz

SOURCE = "tickchak"

# Same default save root as kupat_pdf so the user's KartisTickets/<Event>
# folder gets both tools' output side by side.
_DEFAULT_TICKETS_DIR = (
    Path(os.environ.get("USERPROFILE") or Path.home())
    / "OneDrive" / "KartisTickets"
)


class TickchakPdfError(RuntimeError):
    pass


# --- Filename helpers ---------------------------------------------------
# (Same shape as kupat_pdf._safe_name / _date_for_name — copied rather
# than imported so the two ticket-PDF modules stay independent.)

_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(s, max_len=80):
    s = (s or "").strip()
    s = _FORBIDDEN_CHARS.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s or "untitled"


def _date_iso_from_dotted(text):
    """'12.05.2026' → '2026-05-12'. Returns '' if it doesn't match."""
    if not text:
        return ""
    m = re.match(r"\s*(\d{1,2})[./](\d{1,2})[./](\d{4})", text.strip())
    if not m:
        return ""
    d, mth, y = m.group(1), m.group(2), m.group(3)
    return f"{y}-{int(mth):02d}-{int(d):02d}"


# --- Metadata extraction -----------------------------------------------

# Hebrew labels we anchor on. The values follow each label in the PDF's
# text layer. Keep these centralized so a future Tickchak template tweak
# only needs one place to update.
LABEL_EVENT = "פרטי אירוע"      # event name follows
LABEL_VENUE = "מקום"             # venue follows
LABEL_DATE = "תאריך"             # event date (DD.MM.YYYY) follows
LABEL_SECTION = "איזור"          # section/area follows
LABEL_ROW = "שורה"               # row follows
LABEL_SEAT = "כסא"               # seat follows
LABEL_PRICE = "מחיר הכרטיס"     # price label (to redact)
LABEL_BUYER = "פרטי המזמין"     # buyer-details label (cell content below is redacted)
LABEL_TICKET_NO = "מספר כרטיס"  # ticket number (TCPDF emits it concatenated)


def _extract_page_meta(page):
    """Return {event_name, event_date_iso, venue, section, row, seat,
    ticket_no} parsed from the page's text layer. Missing fields come
    back as empty strings — callers fall back to a generic filename when
    the parse fails."""
    lines = [ln.strip() for ln in page.get_text("text").splitlines() if ln.strip()]
    meta = {
        "event_name": "",
        "event_date_iso": "",
        "venue": "",
        "section": "",
        "row": "",
        "seat": "",
        "ticket_no": "",
    }
    # Label-then-next-non-empty-line for the simple cases.
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ln == LABEL_EVENT:
            meta["event_name"] = nxt
        elif ln == LABEL_VENUE:
            meta["venue"] = nxt
        elif ln == LABEL_DATE:
            meta["event_date_iso"] = _date_iso_from_dotted(nxt)
        elif ln == LABEL_SECTION:
            meta["section"] = nxt
        elif ln == LABEL_ROW:
            meta["row"] = nxt
        elif ln == LABEL_SEAT:
            meta["seat"] = nxt
        elif LABEL_TICKET_NO in ln:
            # TCPDF concatenates: "מספר כרטיס18244064" with no space.
            m = re.search(r"(\d{5,})", ln)
            if m:
                meta["ticket_no"] = m.group(1)
            elif nxt.isdigit():
                meta["ticket_no"] = nxt
    return meta


def event_folder_name(meta):
    name = _safe_name(meta.get("event_name") or "Unknown event")
    date = _safe_name(meta.get("event_date_iso") or "")
    return f"{name}_{date}".rstrip("_")


def ticket_filename(meta, page_index):
    """<EventName>_<Date>_<Section>_Row<R>_Seat<S>.pdf, with a
    page-number fallback if seat info is missing."""
    name = _safe_name(meta.get("event_name") or "Ticket")
    date = _safe_name(meta.get("event_date_iso") or "")
    section = _safe_name(meta.get("section") or "")
    row = _safe_name(str(meta.get("row") or ""))
    seat = _safe_name(str(meta.get("seat") or ""))
    parts = [name]
    if date:
        parts.append(date)
    if section:
        parts.append(section)
    if row:
        parts.append(f"Row{row}")
    if seat:
        parts.append(f"Seat{seat}")
    if not (row or seat):
        # Fallback so partial-parse pages still get unique filenames.
        parts.append(f"page{page_index + 1}")
        if meta.get("ticket_no"):
            parts.append(meta["ticket_no"])
    return "_".join(parts) + ".pdf"


# --- Redaction ---------------------------------------------------------

# Padding (PDF points) applied around each label/image bbox before
# redacting, so we cover ascenders/descenders and any drop-shadow on
# the logo without nibbling into adjacent content.
_LABEL_PAD = 2
_LOGO_PAD = 4


def _redact_page(page, on_progress=None):
    """Add redact annotations for the buyer cell content, the price
    block, and the Tickchak logo. Caller is responsible for calling
    page.apply_redactions() after all annotations are added.

    Returns a dict describing what was found so the caller can warn if
    the layout drifted.
    """
    page_w = page.rect.width
    page_h = page.rect.height
    found = {"buyer": False, "price": False, "logo": False}

    # --- Buyer cell ---
    # Anchor on the "פרטי המזמין" label, redact the rectangle below it
    # that contains the name + phone. The label sits in the top header
    # at y≈54 in the sample (page 595×842 pt). The values directly
    # below span y≈76–106. Use a rect anchored to label.y1 + small pad,
    # extending down 45pt — covers a 2-line cell with breathing room.
    buyer_hits = page.search_for(LABEL_BUYER)
    if buyer_hits:
        # Take the topmost hit (the cell header), ignore any incidental
        # later hits if a future template puts the same string elsewhere.
        lbl = min(buyer_hits, key=lambda r: r.y0)
        # Cell spans roughly from x≈220 (where phone digits start) to
        # x≈300 (right edge of header). Use the label's x as right
        # anchor and back off ~75pt for the left edge — sample shows
        # the longest value (the phone) starts ~58pt left of the label.
        rect = fitz.Rect(
            lbl.x0 - 75,
            lbl.y1 + _LABEL_PAD,
            lbl.x1 + 5,
            lbl.y1 + 45,
        )
        page.add_redact_annot(rect, fill=(1, 1, 1))
        found["buyer"] = True

    # --- Price block ---
    # Anchor on "מחיר הכרטיס" label, redact label + value below. The ₪
    # symbol sits left of the value, so extend the rect leftward.
    price_hits = page.search_for(LABEL_PRICE)
    if price_hits:
        lbl = min(price_hits, key=lambda r: r.y0)
        rect = fitz.Rect(
            lbl.x0 - 60,
            lbl.y0 - _LABEL_PAD,
            lbl.x1 + 5,
            lbl.y0 + 60,
        )
        page.add_redact_annot(rect, fill=(1, 1, 1))
        found["price"] = True

    # --- Tickchak logo (bottom-left image) ---
    # Iterate embedded images and find the one whose placed bbox is in
    # the bottom-left quadrant of the page. Tickchak puts only the
    # logo there; the event poster is bottom-right and the venue
    # diagram is mid-right.
    for img in page.get_images(full=True):
        # img[7] is the image name (e.g. "I1"); get_image_rects returns
        # every placement of that XObject on the page.
        try:
            placements = page.get_image_rects(img[7])
        except Exception:
            continue
        for bbox in placements:
            if not bbox or bbox.is_empty:
                continue
            if bbox.x0 < page_w * 0.30 and bbox.y0 > page_h * 0.75:
                rect = fitz.Rect(
                    bbox.x0 - _LOGO_PAD,
                    bbox.y0 - _LOGO_PAD,
                    bbox.x1 + _LOGO_PAD,
                    bbox.y1 + _LOGO_PAD,
                )
                # Clamp so we don't overflow page edges.
                rect &= page.rect
                page.add_redact_annot(rect, fill=(1, 1, 1))
                found["logo"] = True

    if on_progress and not all(found.values()):
        missing = [k for k, v in found.items() if not v]
        on_progress("warning", {"page": page.number + 1, "missing": missing})

    return found


# --- Main entry point --------------------------------------------------

def redact_and_split(src_path, base_dir=None, on_progress=None):
    """Open `src_path`, redact identifying info on every page, save each
    page as its own one-page PDF in <base_dir>/<EventFolder>/. Returns a
    manifest dict matching kupat_pdf.render_pdfs()'s shape so the Flask
    layer and the dashboard can render either tool's output uniformly.
    """
    src_path = str(src_path)
    if not os.path.exists(src_path):
        raise TickchakPdfError(f"file not found: {src_path}")

    try:
        src = fitz.open(src_path)
    except Exception as e:
        raise TickchakPdfError(f"cannot open PDF: {e}") from e

    if src.needs_pass:
        src.close()
        raise TickchakPdfError("PDF is encrypted")

    n = src.page_count
    if n == 0:
        src.close()
        raise TickchakPdfError("PDF has no pages")

    if on_progress:
        on_progress("open", {"pages": n})

    # First-page metadata drives the event folder name.
    first_meta = _extract_page_meta(src[0])
    if base_dir is None:
        base_dir = os.environ.get("KARTIS_TICKETS_DIR") or str(_DEFAULT_TICKETS_DIR)
    folder = Path(base_dir) / event_folder_name(first_meta)
    folder.mkdir(parents=True, exist_ok=True)

    if on_progress:
        on_progress("metadata", {
            "ticket_count": n,
            "event_name": first_meta.get("event_name"),
            "event_date": first_meta.get("event_date_iso"),
            "venue": first_meta.get("venue"),
            "folder": str(folder),
        })

    saved = []
    for i in range(n):
        page = src[i]
        meta = _extract_page_meta(page)
        # Inherit event-level fields from page 0 if a later page failed
        # to parse them (shouldn't happen on a normal Tickchak doc, but
        # cheap insurance).
        for k in ("event_name", "event_date_iso", "venue"):
            if not meta.get(k):
                meta[k] = first_meta.get(k, "")

        _redact_page(page, on_progress=on_progress)
        page.apply_redactions()

        # Pull this single redacted page into a fresh document so we can
        # save it independently — saving the full src would write all
        # pages.
        out = fitz.open()
        out.insert_pdf(src, from_page=i, to_page=i)
        fname = ticket_filename(meta, i)
        target = folder / fname
        # If the same seat appears twice (shouldn't happen with valid
        # Tickchak data), suffix the page index to avoid clobbering.
        if target.exists():
            stem = target.stem
            target = folder / f"{stem}_p{i + 1}.pdf"
        out.save(str(target), garbage=4, deflate=True)
        out.close()
        size = target.stat().st_size
        saved.append({
            "index": i + 1,
            "section": meta.get("section"),
            "row": meta.get("row"),
            "seat": meta.get("seat"),
            "ticket_no": meta.get("ticket_no"),
            "file": str(target),
            "filename": fname,
            "bytes": size,
        })
        if on_progress:
            on_progress("saved", {"file": str(target), "bytes": size})

    src.close()
    return {
        "ok": True,
        "event_name": first_meta.get("event_name"),
        "event_date": first_meta.get("event_date_iso"),
        "venue": first_meta.get("venue"),
        "folder": str(folder),
        "tickets": saved,
    }


# --- CLI ---------------------------------------------------------------

if __name__ == "__main__":
    import sys
    # Hebrew labels in metadata blow up Windows' default cp1252 stdout —
    # force UTF-8 so the CLI status lines print cleanly.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print("usage: tickchak_pdf.py <path/to/ticket.pdf> [base_dir]")
        sys.exit(2)
    src = sys.argv[1]
    base = sys.argv[2] if len(sys.argv) > 2 else None

    def _log(stage, info):
        print(f"[{stage}] {info}")

    try:
        out = redact_and_split(src, base_dir=base, on_progress=_log)
    except TickchakPdfError as e:
        print(f"error: {e}")
        sys.exit(1)
    print()
    print(f"saved {len(out['tickets'])} ticket PDF(s) to {out['folder']}")
    for t in out["tickets"]:
        print(f"  {t['filename']}  ({t['bytes']:,} bytes)")
