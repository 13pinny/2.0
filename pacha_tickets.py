"""Pacha New York ticket extractor.

When you buy N tickets to a Pacha New York event, FourVenues (the ticketing
platform) emails each ticket *separately* — N tickets means N emails, each with
one 4-page PDF attachment. You only want page 1 of each PDF (the scannable
ticket), saved as its own single-page PDF, numbered and grouped per event so the
batch is easy to print.

Pacha does not attach the PDF to the email — there's a "Download tickets" button
linking to a token URL on FourVenues' connector service. This script downloads
that PDF (falling back to a real attachment if one is ever present).

This is an on-demand script (not wired into the Flask scheduler). It polls Gmail
over IMAP for mail from FourVenues, extracts the first page of each ticket PDF,
and writes:

    <dest>/<event> <date>/Pacha ticket 1 of 10 - <event>.pdf
    <dest>/<event> <date>/Pacha ticket 2 of 10 - <event>.pdf
    ...

Run:
    .venv\\Scripts\\python pacha_tickets.py            # last 60 days -> Desktop\\Pacha Tickets
    .venv\\Scripts\\python pacha_tickets.py --dry-run  # preview, write nothing
    .venv\\Scripts\\python pacha_tickets.py --days 14

Auth reuses the same GMAIL_USER / GMAIL_APP_PASSWORD env vars as the dashboard's
mail_intake job. The IMAP fetch uses BODY.PEEK, so emails are never marked read.

The event name comes from the email text, not the PDF — the ticket PDF is
image-only with no text layer. Each ticket's order code (e.g. E0HS2YCRT, also
embedded in the attachment name 'entradas-<CODE>.pdf') is unique, so it's used as
the per-ticket dedupe key.
"""
import argparse
import html as _html
import imaplib
import io
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date as _date, datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path

import fitz  # PyMuPDF
from dotenv import load_dotenv

# Load .env from the repo root next to this script, not the current working
# directory, so the script works no matter where it's launched from.
load_dotenv(Path(__file__).parent / ".env")

# Reuse the dashboard's MIME plumbing so there's one source of truth for
# attachment decoding and body extraction. We do our own IMAP connect because
# Pacha mail lands in a different Gmail account than the dashboard's intake.
from mail_intake import (
    GMAIL_HOST,
    GMAIL_PORT,
    _decode,
    _email_body_text,
    _extract_attachments,
)

# Pacha New York sells through FourVenues; mail arrives from no-reply@fourvenues.com
# and tickets.pachanewyork@fourvenues.com. Match the whole domain.
PACHA_FROM_DOMAIN = "fourvenues.com"

DEFAULT_LOOKBACK_DAYS = int(os.getenv("KARTIS_PACHA_DAYS") or 60)
# Pacha mail arrives in a personal inbox, not the dashboard's forwarding
# account. Use dedicated creds if provided, otherwise fall back to the shared
# GMAIL_* vars. The folder defaults to INBOX (override for a label / All Mail).
DEFAULT_FOLDER = os.getenv("KARTIS_PACHA_FOLDER") or "INBOX"

# "Your ticket for Alok: E0HS2YCRT" — appears in both the subject and the body.
_EVENT_RX = re.compile(r"Your ticket for\s+(.+?)\s*:\s*([A-Za-z0-9]{5,})", re.I)
# "August 1, 2026"
_DATE_RX = re.compile(r"\b([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})\b")
# Filename fallback: entradas-E0HS2YCRT.pdf (attachment name or download URL).
_ATTACH_CODE_RX = re.compile(r"entradas-([A-Za-z0-9]+)\.pdf", re.I)
# Pacha doesn't attach the PDF — the "Download tickets" button links to it. The
# link is a token URL on FourVenues' connector service ending in .pdf.
_DOWNLOAD_URL_RX = re.compile(
    r"https://[^\s\"'<>]*fourvenues\.com/[^\s\"'<>]*?entradas-[A-Za-z0-9]+\.pdf", re.I
)
_UA = "Mozilla/5.0 (Kartis pacha_tickets)"
# Pause between successful PDF downloads so a 15-ticket order doesn't trip
# FourVenues' rate limit (HTTP 429) in the first place.
_DOWNLOAD_SPACING_SECONDS = float(os.getenv("KARTIS_PACHA_DOWNLOAD_SPACING") or 3)
# Files this tool owns, so a re-run can clear stale numbering before rewriting.
_OWNED_FILE_RX = re.compile(r"^Pacha ticket \d+ of \d+ - .*\.pdf$", re.I)


def _connect():
    """IMAP login to the account that receives Pacha mail. Prefers
    PACHA_GMAIL_USER / PACHA_GMAIL_APP_PASSWORD, falling back to the shared
    GMAIL_USER / GMAIL_APP_PASSWORD used by the dashboard intake."""
    user = os.getenv("PACHA_GMAIL_USER") or os.getenv("GMAIL_USER")
    pw = os.getenv("PACHA_GMAIL_APP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pw:
        raise RuntimeError(
            "Set PACHA_GMAIL_USER + PACHA_GMAIL_APP_PASSWORD (or GMAIL_USER + "
            "GMAIL_APP_PASSWORD) in .env for the inbox that receives Pacha mail."
        )
    M = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
    M.login(user, pw.replace(" ", ""))
    return M


def _default_dest():
    env = os.getenv("KARTIS_PACHA_DEST")
    if env:
        return Path(env)
    return Path.home() / "OneDrive" / "Desktop" / "Pacha Tickets"


def _sanitize(name, fallback="Unknown"):
    """Make a string safe for a Windows file/folder name."""
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)   # illegal on Windows
    name = re.sub(r"\s+", " ", name).strip(" .")  # collapse + trim dots/spaces
    return name or fallback


def fetch_pacha_messages(days, folder=DEFAULT_FOLDER):
    """Return [(uid, parsed_msg, received_dt)] for FourVenues mail in the
    lookback window. Uses BODY.PEEK so the \\Seen flag is left untouched."""
    since = (_date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
    M = _connect()
    out = []
    try:
        # Quote the folder so labels with spaces (e.g. "[Gmail]/All Mail") work.
        M.select(f'"{folder}"' if " " in folder else folder)
        # Prefer Gmail's X-GM-RAW: matching "fourvenues.com" anywhere catches
        # both filter auto-forwards (original From: survives) and manual
        # forwards (where it only appears in the wrapped body). Fall back to a
        # plain FROM+SINCE search if the server isn't Gmail.
        raw = f"fourvenues.com newer_than:{days}d"
        typ, data = M.search(None, "X-GM-RAW", f'"{raw}"')
        if typ != "OK":
            typ, data = M.search(None, "FROM", PACHA_FROM_DOMAIN, "SINCE", since)
        if typ != "OK" or not data or not data[0]:
            return []
        for uid in data[0].split():
            typ, msg_data = M.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = message_from_bytes(msg_data[0][1])
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


def _normalize_date(text):
    """'August 1, 2026' -> '2026-08-01' (or '' if not found/parseable).

    The event date follows the "ticket for PACHA NEW YORK!" greeting. Anchoring
    there skips the "Date: Mon, Jun 1, 2026" line a forwarded-message header
    injects, which would otherwise be matched first."""
    text = text or ""
    # ".?" tolerates the apostrophe glyph; the greeting only appears in the
    # ticket body, not the forwarded-message header.
    anchor = re.search(r"Here.?s your ticket for", text, re.I)
    region = text[anchor.end():] if anchor else text
    m = _DATE_RX.search(region) or _DATE_RX.search(text)
    if not m:
        return ""
    raw = f"{m.group(1)} {m.group(2)}, {m.group(3)}"
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_meta(msg, attachment_name=""):
    """Pull {event, code, date_iso} from the email subject + body text.

    Event name and order code come from the 'Your ticket for <event>: <code>'
    line (present in both subject and body). Date comes from the body. If the
    body regex misses the code, fall back to the attachment filename."""
    subject = _decode(msg.get("Subject") or "")
    body = _email_body_text(msg)
    blob = f"{subject}\n{body}"

    event, code = "", ""
    m = _EVENT_RX.search(blob)
    if m:
        event = m.group(1).strip()
        code = m.group(2).strip()
    if not code and attachment_name:
        am = _ATTACH_CODE_RX.search(attachment_name)
        if am:
            code = am.group(1)
    date_iso = _normalize_date(body) or _normalize_date(subject)
    return {"event": event, "code": code, "date_iso": date_iso}


def _order_code_from_msg(msg):
    """The authoritative order code is the one in the PDF filename
    (entradas-<CODE>.pdf), from the attachment name or the download link.
    The 'Your ticket for <event>: <code>' subject line is ambiguous — events
    whose own name contains a colon (e.g. 'SOFI TUKKER Presents: ANIMAL')
    make the regex read the subtitle as the code, which is identical across
    every ticket in the order and collapses them all in the dedupe."""
    fname, _ = _first_pdf_attachment(msg)
    for candidate in (fname, find_download_url(msg)):
        if candidate:
            m = _ATTACH_CODE_RX.search(candidate)
            if m:
                return m.group(1)
    return None


def _first_pdf_attachment(msg):
    """Return (filename, bytes) of the first PDF attachment, or (None, None)."""
    for fname, ctype, payload in _extract_attachments(msg):
        if (ctype or "").lower() == "application/pdf" or fname.lower().endswith(".pdf"):
            return fname, payload
    return None, None


def _email_html(msg):
    """Return the first text/html part, decoded, or '' if none."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            try:
                return part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            except Exception:
                pass
    return ""


def find_download_url(msg):
    """Find the 'Download tickets' PDF link in the email HTML (Pacha doesn't
    attach the PDF). Returns the URL string or None."""
    html = _email_html(msg)
    if not html:
        return None
    m = _DOWNLOAD_URL_RX.search(_html.unescape(html))
    return m.group(0) if m else None


def _download_pdf(url, timeout=30):
    """Download one ticket PDF. FourVenues rate-limits the connector service
    (HTTP 429) when a big order pulls dozens of PDFs back-to-back, so space
    requests out and back off + retry on 429."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    delays = (15, 45, 90)
    for attempt in range(len(delays) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            time.sleep(_DOWNLOAD_SPACING_SECONDS)
            return data
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def get_ticket_pdf(msg):
    """Get the ticket PDF for one email. Tries a real attachment first, then
    falls back to downloading the 'Download tickets' link. Returns
    (pdf_bytes, source_name, error) where exactly one of pdf_bytes/error is set;
    source_name is a filename used as an order-code fallback."""
    fname, payload = _first_pdf_attachment(msg)
    if payload:
        return payload, fname, None

    url = find_download_url(msg)
    if not url:
        return None, None, "no PDF attachment or download link"
    name = url.rsplit("/", 1)[-1]
    try:
        data = _download_pdf(url)
    except Exception as e:
        return None, name, f"download failed: {type(e).__name__}: {e}"
    if not data[:5].startswith(b"%PDF"):
        return None, name, "download did not return a PDF (link may have expired)"
    return data, name, None


def extract_first_page(pdf_bytes):
    """Copy page 0 of a PDF into a fresh single-page PDF, returned as bytes.
    The copy is lossless (no rasterizing), so the image-only ticket survives.
    Returns None if the source has no pages."""
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if src.page_count == 0:
            return None
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=0, to_page=0)
            return out.tobytes(garbage=4, deflate=True)
        finally:
            out.close()
    finally:
        src.close()


def collect(days=DEFAULT_LOOKBACK_DAYS, folder=DEFAULT_FOLDER):
    """Fetch + extract + group + number, WITHOUT writing to disk. Shared by the
    disk-saving CLI path (run) and the browser-download path (fetch_zip).

    Returns (groups, skipped):
      groups: list of {event, date_iso, folder_name, count,
                       items: [{name, page_bytes, code}]}  (items numbered 1..M)
      skipped: list of human-readable skip reasons
    """
    # 1. Gather every matching ticket (one PDF per email), deduped by code.
    tickets = []          # [{event, code, date_iso, received, page_bytes}]
    seen_codes = set()
    skipped = []
    for uid, msg, received in fetch_pacha_messages(days, folder=folder):
        # Parse first (cheap) so we can dedupe before any download.
        meta = parse_meta(msg)
        # Prefer the per-ticket code from the entradas-<CODE>.pdf filename; the
        # subject-line "code" is really whatever follows the last colon, which
        # for events with a colon in their name is the event subtitle — shared
        # by the whole order. Fold that subtitle back into the event name.
        real_code = _order_code_from_msg(msg)
        if real_code and meta["code"] != real_code:
            if meta["code"] and meta["event"]:
                meta["event"] = f"{meta['event']}: {meta['code']}"
            meta["code"] = real_code
        label = meta["code"] or (uid.decode() if isinstance(uid, bytes) else str(uid))
        if not meta["event"]:
            skipped.append(f"{label}: could not read event name")
            continue
        if meta["code"] and meta["code"] in seen_codes:
            continue  # duplicate email for the same ticket
        pdf_bytes, src_name, err = get_ticket_pdf(msg)
        # Backfill the order code from the PDF filename if subject/body lacked it.
        if not meta["code"] and src_name:
            am = _ATTACH_CODE_RX.search(src_name)
            if am:
                meta["code"] = am.group(1)
                label = meta["code"]
        if err:
            skipped.append(f"{label}: {err}")
            continue
        if meta["code"]:
            seen_codes.add(meta["code"])
        page = extract_first_page(pdf_bytes)
        if page is None:
            skipped.append(f"{label}: PDF had no pages")
            continue
        tickets.append({**meta, "received": received, "page_bytes": page})

    # 2. Group by (event, date), order deterministically, number 1..M.
    by_key = {}
    for t in tickets:
        by_key.setdefault((t["event"], t["date_iso"]), []).append(t)

    groups = []
    for (event, date_iso), items in by_key.items():
        items.sort(key=lambda t: (t["received"] or datetime.max.replace(tzinfo=timezone.utc), t["code"]))
        total = len(items)
        groups.append({
            "event": event,
            "date_iso": date_iso,
            "folder_name": _sanitize(f"{event} {date_iso}".strip()),
            "count": total,
            "items": [
                {"name": f"Pacha ticket {i} of {total} - {_sanitize(event)}.pdf",
                 "page_bytes": t["page_bytes"], "code": t["code"]}
                for i, t in enumerate(items, start=1)
            ],
        })
    return groups, skipped


def _summarize(groups, skipped, extra):
    """Shared summary shape (without page_bytes) for run() and fetch_zip()."""
    return {
        "events": len(groups),
        "skipped": skipped,
        "groups": [
            {"event": g["event"], "date": g["date_iso"], "count": g["count"],
             "files": [it["name"] for it in g["items"]]}
            for g in groups
        ],
        **extra,
    }


def run(days=DEFAULT_LOOKBACK_DAYS, dest=None, dry_run=False, folder=DEFAULT_FOLDER):
    """Fetch Pacha emails and save numbered per-event PDFs to disk (CLI path).

    Returns a summary dict: {events, tickets_saved, skipped, groups, dest}."""
    dest = Path(dest) if dest else _default_dest()
    groups, skipped = collect(days, folder)

    saved = 0
    for g in groups:
        folder_path = dest / g["folder_name"]
        if dry_run:
            print(f"[dry-run] {folder_path}")
            for it in g["items"]:
                print(f"            {it['name']}")
            continue
        folder_path.mkdir(parents=True, exist_ok=True)
        # Clear our own previously-written files so a re-run with a new total
        # doesn't leave stale 'N of OLD' files behind.
        for old in folder_path.iterdir():
            if old.is_file() and _OWNED_FILE_RX.match(old.name):
                try:
                    old.unlink()
                except OSError:
                    pass
        for it in g["items"]:
            (folder_path / it["name"]).write_bytes(it["page_bytes"])
            saved += 1

    return _summarize(groups, skipped,
                      {"tickets_saved": 0 if dry_run else saved,
                       "dry_run": dry_run, "dest": str(dest)})


def fetch_zip(days=DEFAULT_LOOKBACK_DAYS, folder=DEFAULT_FOLDER):
    """Fetch Pacha tickets and bundle the first-page PDFs into a zip in memory
    (web/browser-download path — nothing is written to the server disk).

    Returns (zip_bytes_or_None, summary). zip_bytes is None when no tickets
    were found."""
    groups, skipped = collect(days, folder)
    total = sum(g["count"] for g in groups)
    summary = _summarize(groups, skipped, {"tickets": total})
    if total == 0:
        return None, summary
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for g in groups:
            for it in g["items"]:
                z.writestr(f"{g['folder_name']}/{it['name']}", it["page_bytes"])
    return buf.getvalue(), summary


def main():
    ap = argparse.ArgumentParser(description="Extract first pages of Pacha New York ticket PDFs from Gmail.")
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"Look back this many days (default {DEFAULT_LOOKBACK_DAYS}).")
    ap.add_argument("--dest", default=None,
                    help="Destination root folder (default: Desktop\\Pacha Tickets).")
    ap.add_argument("--folder", default=DEFAULT_FOLDER,
                    help=f'Gmail folder/label to search (default "{DEFAULT_FOLDER}"). '
                         'Use "[Gmail]/All Mail" to include archived mail.')
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be saved without writing files.")
    args = ap.parse_args()

    summary = run(days=args.days, dest=args.dest, dry_run=args.dry_run, folder=args.folder)
    print()
    print(f"dest:    {summary['dest']}")
    print(f"events:  {summary['events']}")
    print(f"saved:   {summary['tickets_saved']}{' (dry-run)' if summary['dry_run'] else ''}")
    for g in summary["groups"]:
        print(f"  - {g['event']} ({g['date'] or 'no date'}): {g['count']} ticket(s)")
    if summary["skipped"]:
        print(f"skipped: {len(summary['skipped'])}")
        for s in summary["skipped"]:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
