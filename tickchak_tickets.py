"""tickchak_tickets.py

Tickchak sends a link (https://app.tickchak.co.il/n/<token>) that opens a
carousel of ticket cards — all tickets are rendered in the DOM at once and
navigated with "Card N" / Prev / Next buttons.

This script:
  1. Scans Gmail for tickchak ticket emails containing an /n/ link.
  2. Uses patchright (headless Chromium) to visit each link.
  3. Scrolls through the carousel, screenshotting each ticket card.
  4. Saves each as a PDF named:
       "tickchak ticket 1 of 10 - עידן עמדי 2026-08-13 - 19129974.pdf"

Run:
    .venv\\Scripts\\python tickchak_tickets.py
    .venv\\Scripts\\python tickchak_tickets.py --url https://app.tickchak.co.il/n/TOKEN
    .venv\\Scripts\\python tickchak_tickets.py --days 30 --dest C:\\Tickets
    .venv\\Scripts\\python tickchak_tickets.py --dry-run
    .venv\\Scripts\\python tickchak_tickets.py --debug   # screenshot each card to debug/

Auth reuses GMAIL_USER / GMAIL_APP_PASSWORD env vars (same as mail_intake).
IMAP fetch uses BODY.PEEK — emails are never marked read.
"""
import argparse
import imaplib
import io
import os
import re
import zipfile
from datetime import date as _date, datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path

import fitz  # PyMuPDF — converts screenshots to PDFs
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from mail_intake import (
    GMAIL_HOST,
    GMAIL_PORT,
    _decode,
    _email_body_text,
)

# ── constants ────────────────────────────────────────────────────────────────

DEFAULT_LOOKBACK_DAYS = int(os.getenv("KARTIS_TICKCHAK_TICKET_DAYS") or 60)
DEFAULT_FOLDER = os.getenv("KARTIS_TICKCHAK_TICKET_FOLDER") or "INBOX"

_TICKET_URL_RX = re.compile(
    r"https://app\.tickchak\.co\.il/n/([A-Za-z0-9_\-]+)", re.I
)
# "1/10 tickets • 19129974"  or  "3/10 tickets • 19129976"
_COUNTER_RX = re.compile(r"(\d+)/(\d+)\s+tickets?\s*[•·]\s*(\d+)", re.I)

_DATE_RX = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_OWNED_FILE_RX = re.compile(r"^tickchak ticket \d+ of \d+ - .*\.pdf$", re.I)

# ── helpers ──────────────────────────────────────────────────────────────────

def _sanitize(name, fallback="Unknown"):
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or fallback


def _default_dest():
    env = os.getenv("KARTIS_TICKCHAK_TICKET_DEST")
    if env:
        return Path(env)
    return Path.home() / "OneDrive" / "Desktop" / "Tickchak Tickets"


def _image_to_pdf(png_bytes):
    """Wrap a PNG screenshot as a single-page PDF (lossless, no raster change)."""
    img_doc = fitz.open(stream=png_bytes, filetype="png")
    out = fitz.open()
    rect = img_doc[0].rect
    page = out.new_page(width=rect.width, height=rect.height)
    page.insert_image(rect, stream=png_bytes)
    pdf_bytes = out.tobytes(garbage=4, deflate=True)
    out.close()
    img_doc.close()
    return pdf_bytes


# ── Gmail ────────────────────────────────────────────────────────────────────

def _connect_gmail():
    user = os.getenv("GMAIL_USER")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pw:
        raise RuntimeError("Set GMAIL_USER + GMAIL_APP_PASSWORD in .env")
    M = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
    M.login(user, pw.replace(" ", ""))
    return M


def _get_html(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            except Exception:
                pass
    return ""


def fetch_tickchak_messages(days=DEFAULT_LOOKBACK_DAYS, folder=DEFAULT_FOLDER):
    """Return [(uid, parsed_msg, received_dt)] for tickchak emails with /n/ links."""
    since = (_date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    M = _connect_gmail()
    out = []
    try:
        M.select(f'"{folder}"' if " " in folder else folder)
        raw = f"tickchak.co.il newer_than:{days}d"
        typ, data = M.search(None, "X-GM-RAW", f'"{raw}"')
        if typ != "OK":
            typ, data = M.search(None, "FROM", "tickchak", "SINCE", since)
        if typ != "OK" or not data or not data[0]:
            return []
        for uid in data[0].split():
            typ, msg_data = M.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = message_from_bytes(msg_data[0][1])
            body = _email_body_text(msg) or ""
            html_part = _get_html(msg)
            if not _TICKET_URL_RX.search(body + "\n" + html_part):
                continue
            received = None
            try:
                received = parsedate_to_datetime(msg.get("Date"))
                if received and received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
            except Exception:
                pass
            out.append((uid, msg, received))
    finally:
        try:
            M.close()
        except Exception:
            pass
        M.logout()
    return out


def parse_email_meta(msg):
    """Extract {event_name, date_iso, tokens} from a tickchak email."""
    subject = _decode(msg.get("Subject") or "")
    sender = _decode(msg.get("From") or "")
    body = _email_body_text(msg) or ""

    event_name = ""
    m = re.match(r'\s*"?([^<"]+?)"?\s*<', sender or "")
    if m:
        candidate = m.group(1).strip()
        if "@" not in candidate:
            event_name = candidate
    if not event_name and subject:
        event_name = re.sub(r"^(re:|fwd?:|fw:)\s*", "", subject, flags=re.I).strip()

    date_iso = ""
    m = _DATE_RX.search(body)
    if m:
        d, mo, y = m.groups()
        date_iso = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    html = _get_html(msg)
    tokens = list(dict.fromkeys(
        m.group(1) for m in _TICKET_URL_RX.finditer(body + "\n" + html)
    ))
    return {"event_name": event_name, "date_iso": date_iso, "tokens": tokens}


# ── patchright scraping ───────────────────────────────────────────────────────

def _launch_browser(headless=True):
    from patchright.sync_api import sync_playwright
    pw = sync_playwright().start()
    # Mobile viewport — tickchak /n/ is a mobile-first PWA
    browser = pw.chromium.launch(headless=headless)
    return pw, browser


def scrape_ticket_url(url, debug=False, debug_dir=None):
    """Visit a tickchak /n/ URL and capture each ticket card as a PNG→PDF.

    Page structure (observed 2026-07-01):
      - All ticket cards are rendered in the DOM at once (carousel).
      - Counter text: "N/M tickets • TICKET_ID"
      - Navigation: "Card N" buttons + "Previous"/"Next" buttons.
      - No seat/row info for standing-room events.

    Returns list of {pdf_bytes, index, total, ticket_id, event_name, date_iso}.
    """
    pw, browser = _launch_browser(headless=not debug)
    results = []
    try:
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            locale="he-IL",
        )
        page = ctx.new_page()
        page.goto(url, wait_until="networkidle", timeout=30_000)

        # --- discover tickets from the counter elements in the DOM ---
        # All cards are rendered; we read their counter text to get ticket list.
        full_text = page.inner_text("body")
        tickets_found = []
        for m in _COUNTER_RX.finditer(full_text):
            idx, total, ticket_id = int(m.group(1)), int(m.group(2)), m.group(3)
            tickets_found.append({"index": idx, "total": total, "ticket_id": ticket_id})

        if not tickets_found:
            print(f"  [warn] could not find ticket counter on {url}")
            if debug and debug_dir:
                Path(debug_dir).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(debug_dir) / "debug_full.png"), full_page=True)
                (Path(debug_dir) / "debug_full.html").write_text(
                    page.content(), encoding="utf-8"
                )
            ctx.close()
            return []

        total = tickets_found[0]["total"]

        # Extract event name + date from the page heading.
        event_name = ""
        date_iso = ""
        for sel in ("h1", "h2", "[class*='title']", "[class*='event']"):
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t:
                    event_name = t
                    # Strip trailing " - DD.M" date suffix tickchak adds to the title.
                    event_name = re.sub(r"\s*-\s*\d{1,2}\.\d{1,2}$", "", event_name).strip()
                    break
        # Date from a generic date cell: "13.08.2026" or "13.08.26"
        m = re.search(r"\b(\d{1,2})\.(\d{2})\.(\d{2,4})\b", full_text)
        if m:
            d, mo, y = m.groups()
            yr = int(y)
            if yr < 100:
                yr += 2000
            date_iso = f"{yr:04d}-{int(mo):02d}-{int(d):02d}"

        if debug and debug_dir:
            Path(debug_dir).mkdir(parents=True, exist_ok=True)
            (Path(debug_dir) / "page.html").write_text(page.content(), encoding="utf-8")

        # --- navigate to each card and screenshot ---
        for t in tickets_found:
            i = t["index"]
            ticket_id = t["ticket_id"]

            # Click "Card N" button to bring this card into view.
            card_btn = page.query_selector(f'button[aria-label="Card {i}"]')
            if card_btn:
                card_btn.click()
                page.wait_for_timeout(400)
            else:
                # Fall back: click Next until we reach the right card.
                for _ in range(i - 1):
                    nxt = page.query_selector('button[aria-label="Next"]')
                    if nxt:
                        nxt.click()
                        page.wait_for_timeout(300)

            # Screenshot the currently-visible ticket area.
            # The ticket card is the first visible carousel slide.
            card_el = None
            for sel in (
                "[class*='slide'][class*='active']",
                "[class*='card'][class*='active']",
                "[class*='ticket'][class*='active']",
                "[class*='swiper-slide-active']",
                "[class*='carousel-item'][class*='active']",
            ):
                card_el = page.query_selector(sel)
                if card_el:
                    break

            if card_el:
                png = card_el.screenshot()
            else:
                # Fallback: full page screenshot clipped to viewport.
                png = page.screenshot(clip={"x": 0, "y": 0, "width": 390, "height": 844})

            if debug and debug_dir:
                (Path(debug_dir) / f"ticket_{i}.png").write_bytes(png)

            pdf_bytes = _image_to_pdf(png)
            results.append({
                "pdf_bytes": pdf_bytes,
                "index": i,
                "total": total,
                "ticket_id": ticket_id,
                "event_name": event_name,
                "date_iso": date_iso,
            })

        ctx.close()
    finally:
        browser.close()
        pw.stop()

    return results


# ── file naming ──────────────────────────────────────────────────────────────

def _ticket_filename(index, total, event_name, date_iso, ticket_id):
    label = _sanitize(event_name or "Unknown Event")
    if date_iso:
        label += f" {date_iso}"
    name = f"tickchak ticket {index} of {total} - {label} - {ticket_id}"
    return _sanitize(name) + ".pdf"


# ── orchestration ─────────────────────────────────────────────────────────────

def collect(days=DEFAULT_LOOKBACK_DAYS, folder=DEFAULT_FOLDER, extra_urls=None, debug=False):
    """Gather all ticket PDFs without writing to disk.

    Returns (groups, skipped).
    """
    debug_dir = str(Path(__file__).parent / "debug" / "tickchak_tickets") if debug else None
    token_meta = {}
    skipped = []

    if extra_urls:
        for url in extra_urls:
            m = _TICKET_URL_RX.search(url)
            if m:
                token_meta[m.group(1)] = {
                    "event_name": "", "date_iso": "", "url": url,
                }
            else:
                skipped.append(f"not a tickchak /n/ URL: {url}")
    else:
        for uid, msg, received in fetch_tickchak_messages(days, folder):
            meta = parse_email_meta(msg)
            for token in meta["tokens"]:
                if token not in token_meta:
                    token_meta[token] = {
                        "event_name": meta["event_name"],
                        "date_iso": meta["date_iso"],
                        "url": f"https://app.tickchak.co.il/n/{token}",
                    }

    by_key = {}
    for token, meta in token_meta.items():
        url = meta["url"]
        print(f"  scraping {url} …")
        try:
            tickets = scrape_ticket_url(url, debug=debug, debug_dir=debug_dir)
        except Exception as e:
            skipped.append(f"{token}: browser error — {type(e).__name__}: {e}")
            continue
        if not tickets:
            skipped.append(f"{token}: no tickets found on page")
            continue

        # Prefer event name/date from the page itself over the email.
        page_event = tickets[0]["event_name"] if tickets else meta["event_name"]
        page_date = tickets[0]["date_iso"] if tickets else meta["date_iso"]
        event_name = page_event or meta["event_name"]
        date_iso = page_date or meta["date_iso"]

        key = (event_name, date_iso, token)
        for t in tickets:
            name = _ticket_filename(t["index"], t["total"], event_name, date_iso, t["ticket_id"])
            by_key.setdefault(key, []).append({
                "name": name,
                "pdf_bytes": t["pdf_bytes"],
                "index": t["index"],
                "total": t["total"],
            })

    groups = []
    for (event_name, date_iso, _token), items in by_key.items():
        items.sort(key=lambda x: x["index"])
        folder_name = _sanitize(f"{event_name} {date_iso}".strip() or "Tickchak Tickets")
        groups.append({
            "event_name": event_name,
            "date_iso": date_iso,
            "folder_name": folder_name,
            "count": len(items),
            "items": items,
        })
    return groups, skipped


def run(days=DEFAULT_LOOKBACK_DAYS, dest=None, dry_run=False,
        folder=DEFAULT_FOLDER, extra_urls=None, debug=False):
    dest = Path(dest) if dest else _default_dest()
    groups, skipped = collect(days, folder, extra_urls=extra_urls, debug=debug)

    saved = 0
    for g in groups:
        folder_path = dest / g["folder_name"]
        if dry_run:
            print(f"[dry-run] {folder_path}/")
            for it in g["items"]:
                print(f"            {it['name']}")
            continue
        folder_path.mkdir(parents=True, exist_ok=True)
        for old in folder_path.iterdir():
            if old.is_file() and _OWNED_FILE_RX.match(old.name):
                try:
                    old.unlink()
                except OSError:
                    pass
        for it in g["items"]:
            (folder_path / it["name"]).write_bytes(it["pdf_bytes"])
            saved += 1

    return {
        "events": len(groups),
        "tickets_saved": 0 if dry_run else saved,
        "dry_run": dry_run,
        "dest": str(dest),
        "skipped": skipped,
        "groups": [
            {"event": g["event_name"], "date": g["date_iso"],
             "count": g["count"], "files": [it["name"] for it in g["items"]]}
            for g in groups
        ],
    }


def fetch_zip(days=DEFAULT_LOOKBACK_DAYS, folder=DEFAULT_FOLDER):
    """Web path: collect and bundle into a zip in memory."""
    groups, skipped = collect(days, folder)
    total = sum(g["count"] for g in groups)
    summary = {
        "events": len(groups), "tickets": total, "skipped": skipped,
        "groups": [
            {"event": g["event_name"], "date": g["date_iso"],
             "count": g["count"], "files": [it["name"] for it in g["items"]]}
            for g in groups
        ],
    }
    if total == 0:
        return None, summary
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for g in groups:
            for it in g["items"]:
                z.writestr(f"{g['folder_name']}/{it['name']}", it["pdf_bytes"])
    return buf.getvalue(), summary


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Save tickchak /n/ ticket links as numbered per-ticket PDFs."
    )
    ap.add_argument("--url", action="append", dest="urls", metavar="URL",
                    help="Ticket URL to process directly (may repeat). Skips Gmail scan.")
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"Gmail lookback window in days (default {DEFAULT_LOOKBACK_DAYS}).")
    ap.add_argument("--dest", default=None,
                    help="Destination folder (default: Desktop\\Tickchak Tickets).")
    ap.add_argument("--folder", default=DEFAULT_FOLDER,
                    help=f'Gmail folder/label (default "{DEFAULT_FOLDER}").')
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be saved without writing files.")
    ap.add_argument("--debug", action="store_true",
                    help="Dump HTML + per-card screenshots to debug/tickchak_tickets/.")
    args = ap.parse_args()

    summary = run(
        days=args.days,
        dest=args.dest,
        dry_run=args.dry_run,
        folder=args.folder,
        extra_urls=args.urls,
        debug=args.debug,
    )

    print()
    print(f"dest:    {summary['dest']}")
    print(f"events:  {summary['events']}")
    print(f"saved:   {summary['tickets_saved']}{' (dry-run)' if summary['dry_run'] else ''}")
    for g in summary["groups"]:
        print(f"  - {g['event'] or '(unknown event)'} ({g['date'] or 'no date'}): {g['count']} ticket(s)")
        for f in g["files"]:
            print(f"      {f}")
    if summary["skipped"]:
        print(f"skipped: {len(summary['skipped'])}")
        for s in summary["skipped"]:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
