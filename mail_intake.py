"""Pull purchase-confirmation emails from a Gmail inbox via IMAP and stage
them as pending_intake rows for the user to confirm on /pending.

Flow:
  Gmail inbox  →  imap_fetch_new()  →  parse_email()  →  extract_fields()
  →  PDFs saved to attachments/intake-<id>/  →  pending_intake row inserted

The user reviews each row in the Inbox panel on /pending; clicking Confirm
promotes it to a manual_inventory row and re-points the attachments at the
new owner_id (files stay on disk, just the DB pointer moves).

Per-provider parsers are intentionally rough — they extract what's
recognisable from the subject + body. Forward sample emails so the regexes
can be tightened.
"""
import imaplib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

import attachments as attachments_mod
import cashback_email
import db
import series
import dice_email
import fx
import notify
import viagogo_listing

GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993
INTAKE_FOLDER = os.getenv("KARTIS_INTAKE_FOLDER") or "INBOX"
# Limit per poll so a forgotten huge inbox doesn't lock us up.
MAX_PER_POLL = int(os.getenv("KARTIS_INTAKE_MAX_PER_POLL") or 25)
# Look back this many days when scanning for purchase emails. We use SINCE
# rather than UNSEEN so re-polls can pick up messages we've already touched
# (Message-ID dedup keeps it idempotent at the DB layer).
LOOKBACK_DAYS = int(os.getenv("KARTIS_INTAKE_LOOKBACK_DAYS") or 3)
# Capital One cash-back emails get a dedicated, targeted IMAP search (by
# sender) so a busy inbox can't push them out of the capped general poll.
# Wider lookback than the general poll since these are rare and must-not-miss;
# the source_ref dedup makes a long window cheap and idempotent.
CASHBACK_LOOKBACK_DAYS = int(os.getenv("KARTIS_CASHBACK_LOOKBACK_DAYS") or 14)
# DICE purchase/transfer emails get the same targeted-sweep treatment as
# cash-back: a dedicated FROM search with a wide window, idempotent via the
# dice tables' message_id UNIQUE.
DICE_LOOKBACK_DAYS = int(os.getenv("KARTIS_DICE_LOOKBACK_DAYS") or 14)
# Purchase/transfer pings are off by default in v1; unmatched/overflow
# transfer warnings always fire regardless.
DICE_PINGS_ENABLED = os.getenv("KARTIS_DICE_PINGS_ENABLED", "0") == "1"


# Map of sender substring → provider tag. Add more as we see real emails.
PROVIDER_HINTS = (
    ("ticketmaster.co.il", "ticketmaster_il"),
    ("ticketmaster.com", "ticketmaster_us"),
    ("ticketmaster", "ticketmaster"),
    ("kupat.co.il", "kupat"),
    ("2207.co.il", "kupat"),   # real sender domain (donotreply1@2207.co.il)
    ("kupat", "kupat"),
    ("tickchak", "tickchak"),
)

# Senders inside known providers that we still want to skip — newsletters
# and pure marketing lists. Match is a sender substring (case-insensitive).
# We deliberately do NOT block "support@" because Ticketmaster sends
# purchase confirmations from customer_support@email.ticketmaster.com.
PROVIDER_BLOCKLIST = (
    "newsletter@",
    "no-reply@email",  # marketing lists
)


def _decode(s):
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


# Markers that indicate a forwarded email body. Order doesn't matter — we
# split on the first one we see.
_FWD_MARKERS = (
    "----- Forwarded message -----",
    "---------- Forwarded message ---------",
    "---------- Forwarded message ----------",
    "Begin forwarded message:",
    "-------- Forwarded Message --------",
)
_FWD_SUBJECT_RX = re.compile(r"^\s*(fwd?|fw|forward)\s*:", re.I)
# Inside a forwarded block, headers look like:
#   From: "Name" <addr@host>
#   Subject: original subject
# We only need From and Subject for provider detection + display.
_FWD_FROM_RX = re.compile(r"^\s*From:\s*(.+?)\s*$", re.I | re.M)
_FWD_TO_RX = re.compile(r"^\s*To:\s*(.+?)\s*$", re.I | re.M)
_EMAIL_ADDR_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _buyer_email(msg, body):
    """Which of the user's addresses the ticket provider originally mailed —
    i.e. the account the purchase was made under (needed to know where the
    seller must deliver from). Manual forwards keep the original 'To:' in
    the forwarded header block; direct/auto-forwarded mail keeps it in the
    real To:/Delivered-To headers. The forwarding inbox's own address is
    never the answer."""
    fwd_addr = (os.getenv("GMAIL_USER") or "").lower()
    candidates = []
    for m in _FWD_TO_RX.finditer(body or ""):
        candidates.extend(_EMAIL_ADDR_RX.findall(m.group(1)))
    for hdr in ("To", "Delivered-To", "X-Forwarded-To"):
        candidates.extend(_EMAIL_ADDR_RX.findall(_decode(msg.get(hdr) or "")))
    for addr in candidates:
        if addr.lower() != fwd_addr:
            return addr.lower()
    return candidates[0].lower() if candidates else ""
_FWD_SUBJECT_INNER_RX = re.compile(r"^\s*Subject:\s*(.+?)\s*$", re.I | re.M)


def _unwrap_forwarded(parsed):
    """If the email looks forwarded (subject prefix or body marker), pull
    the original sender + subject out of the inner forwarded headers.
    Returns (effective_from, effective_subject, effective_body).

    Gmail's "Forward" button rewrites From: to the user, so the only place
    the real provider address survives is the body header block. Without
    this unwrap, every manual forward gets filtered as 'unknown'."""
    outer_from = parsed.get("from") or ""
    outer_subject = parsed.get("subject") or ""
    body = parsed.get("body") or ""

    looks_forwarded = (
        _FWD_SUBJECT_RX.match(outer_subject)
        or any(m in body for m in _FWD_MARKERS)
    )
    if not looks_forwarded:
        return outer_from, outer_subject, body

    # Slice the body at the first forwarded marker so we only look at the
    # inner headers, not whatever the user typed above the forward.
    inner_body = body
    for m in _FWD_MARKERS:
        idx = body.find(m)
        if idx >= 0:
            inner_body = body[idx + len(m):]
            break

    # Search ALL From: headers in the unwrapped body and pick the first one
    # that resolves to a known provider (handles double-forward where the
    # first From is the re-forwarder, not the original ticket sender).
    inner_from_match = None
    for _m in _FWD_FROM_RX.finditer(inner_body):
        if _detect_provider(_m.group(1)) != "unknown":
            inner_from_match = _m
            break
    if inner_from_match is None:
        inner_from_match = _FWD_FROM_RX.search(inner_body)
    inner_subject_match = _FWD_SUBJECT_INNER_RX.search(inner_body)
    eff_from = inner_from_match.group(1).strip() if inner_from_match else outer_from
    eff_subject = inner_subject_match.group(1).strip() if inner_subject_match else outer_subject
    # Drop any remaining "Fwd:" / "Re:" prefixes from the unwrapped subject.
    eff_subject = _FWD_SUBJECT_RX.sub("", eff_subject).strip(" :-")
    return eff_from, eff_subject, inner_body


def _detect_provider(sender):
    s = (sender or "").lower()
    for needle, tag in PROVIDER_HINTS:
        if needle in s:
            return tag
    return "unknown"


def _is_blocked_sender(sender):
    s = (sender or "").lower()
    return any(b in s for b in PROVIDER_BLOCKLIST)


def _email_body_text(msg):
    """Best-effort plain-text body. Walks the parts and prefers text/plain;
    falls back to text/html stripped of tags. We intentionally tolerate
    decode errors — the body is parsing fodder, not user-facing."""
    chunks = []
    for part in msg.walk():
        ctype = part.get_content_type()
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            continue
        if ctype == "text/plain":
            try:
                chunks.append(part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:
                pass
        elif ctype == "text/html" and not chunks:
            try:
                html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                chunks.append(re.sub(r"<[^>]+>", " ", html))
            except Exception:
                pass
    return "\n".join(chunks).strip()


def _email_html(msg):
    """Return the email's raw HTML part (or "" if none)."""
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        if "attachment" in (part.get("Content-Disposition") or "").lower():
            continue
        try:
            return part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        except Exception:
            continue
    return ""


def _email_html_links(msg):
    """Return all href URLs found in the email's HTML part."""
    html = _email_html(msg)
    return re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE) if html else []


def _extract_attachments(msg):
    """Returns list of (filename, mimetype, bytes). Skips inline images."""
    out = []
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in disp:
            continue
        fname = _decode(part.get_filename() or "")
        if not fname:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue
        out.append((fname, part.get_content_type() or "application/octet-stream", payload))
    return out


# ---- generic field extraction ------------------------------------------

_AMOUNT_PATTERNS = [
    re.compile(r"(?:total|sum|amount|charge|paid|grand\s*total)\D{0,20}([\$₪€]?\s*\d[\d,]*\.\d{2})", re.I),
    re.compile(r"([\$₪€]\s*\d[\d,]*\.\d{2})"),
]
_QTY_PATTERNS = [
    re.compile(r"(\d+)\s*(?:tickets?|seats?|כרטיסים?)", re.I),
]
_DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b", re.I),
]
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def _to_iso(text):
    """Returns the FIRST plausible YYYY-MM-DD date found in text, or ""."""
    for pat in _DATE_PATTERNS:
        m = pat.search(text or "")
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups) == 3 and groups[0].isdigit() and len(groups[0]) == 4:
                y, mo, d = groups
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            if len(groups) == 3 and groups[0].isdigit() and groups[2].isdigit() and len(groups[2]) == 4 and groups[1].isalpha():
                d, mname, y = groups
                mo = _MONTHS.get(mname[:3].lower())
                if mo:
                    return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
            if len(groups) == 3 and groups[0].isdigit() and groups[2].isdigit() and len(groups[2]) == 4:
                # m/d/y assumption — TM US uses this; refine per-provider when needed.
                mo, d, y = groups
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except (ValueError, KeyError):
            continue
    return ""


def _extract_amount(text):
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text or "")
        if m:
            raw = m.group(1)
            num = re.sub(r"[^\d.]", "", raw)
            try:
                return float(num)
            except ValueError:
                continue
    return None


def _extract_qty(text):
    for pat in _QTY_PATTERNS:
        m = pat.search(text or "")
        if m:
            try:
                v = int(m.group(1))
                if 0 < v < 50:
                    return v
            except ValueError:
                continue
    return None


def _parse_kupat(subject, body, links=None):
    """Kupat (קופת תל אביב) order confirmation. The body has a fixed
    multi-line layout right under the order-id line."""
    out = {"warnings": []}
    # Event name + venue: the event name is the standalone line right
    # before "באולם - <venue>".
    m = re.search(r"\n\s*([^\n]+?)\s*\n\s*באולם\s*-\s*([^\n]+)", body or "")
    if m:
        out["event_name"] = m.group(1).strip()
        out["venue"] = m.group(2).strip()
    # Date: "ביום - חמישי 4/6/2026" → 2026-06-04 (Israeli DD/MM/YYYY).
    m = re.search(r"ביום\s*-\s*\S+\s+(\d{1,2})/(\d{1,2})/(\d{4})", body or "")
    if m:
        d, mo, y = m.groups()
        out["event_date_iso"] = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    # Qty + per-unit price: "2 x רגיל (₪399.00)"
    m = re.search(r"(\d+)\s*x\s*\S+\s*\(₪([\d,.]+)\)", body or "")
    if m:
        out["qty"] = int(m.group(1))
        try:
            out["cost_per_unit"] = float(m.group(2).replace(",", ""))
        except ValueError:
            pass
    # Total qty (overrides qty above when present): "סה"כ כרטיסים בהזמנה: 2"
    m = re.search(r'סה"כ\s*כרטיסים\s*בהזמנה:\s*(\d+)', body or "")
    if m:
        out["qty"] = int(m.group(1))
    # Total cost: "סה"כ תשלום בהזמנה: ₪798.00"
    m = re.search(r'סה"כ\s*תשלום\s*בהזמנה:\s*₪?\s*([\d,.]+)', body or "")
    if m:
        try:
            out["cost"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    # Section / row / seats. The layout varies per email: Eden's came on one
    # line ("אזור: GOLDEN ישיבה 1 , שורה: 6, כיסאות: 9 - 10"), Pe'er Tasi's
    # split each field onto its own line with literal &nbsp; entities between
    # them — a combined single-line regex silently dropped שורה/כיסאות there.
    # So parse each field independently on an entity-normalized body.
    seat_body = re.sub(r"&nbsp;|\xa0", " ", body or "")
    m = re.search(r"אזור:\s*([^\n,]+)", seat_body)
    if m:
        out["section"] = m.group(1).strip()
    elif re.search(r"(?:^|\s)עמידה(?:\s|$)", seat_body):
        # GA tickets have no אזור line; a standalone עמידה (standing) is how
        # GA appears on Kupat tickets (e.g. Hanan Ben Ari, Expo Tel Aviv).
        # Capturing it lets viagogo_section_map translate it (עמידה →
        # Standing) so the /listings dropdown pre-selects correctly.
        out["section"] = "עמידה"
    m = re.search(r"שורה:\s*([^\s,]+)", seat_body)
    if m:
        out["row_label"] = m.group(1).strip()
    m = re.search(r"כיסא(?:ות)?:\s*(\d+(?:\s*[-–]\s*\d+)?)", seat_body)
    if m:
        out["seats"] = re.sub(r"\s*([-–])\s*", r" \1 ", m.group(1).strip())
    # One Kupat order can hold SEVERAL distinct seat blocks (e.g. orchestra
    # row 2 AND row 10) — each must become its own viagogo listing. Scan
    # every אזור: block; when 2+ distinct groups exist, expose them as
    # seat_groups and _push_kupat_to_viagogo stages one push per group.
    starts = [m.start() for m in re.finditer(r"אזור:", seat_body)]
    groups, seen_groups = [], set()
    for i, s in enumerate(starts):
        chunk = seat_body[s:starts[i + 1] if i + 1 < len(starts) else s + 300]
        g = {}
        gm = re.match(r"אזור:\s*([^\n,]+)", chunk)
        if gm:
            g["section"] = gm.group(1).strip()
        gm = re.search(r"שורה:\s*([^\s,]+)", chunk)
        if gm:
            g["row_label"] = gm.group(1).strip()
        gm = re.search(r"כיסא(?:ות)?:\s*(\d+(?:\s*[-–]\s*\d+)?)", chunk)
        if gm:
            g["seats"] = re.sub(r"\s*([-–])\s*", r" \1 ", gm.group(1).strip())
            nums = [int(n) for n in re.findall(r"\d+", g["seats"])]
            g["qty"] = abs(nums[1] - nums[0]) + 1 if len(nums) == 2 else 1
        key = (g.get("section"), g.get("row_label"), g.get("seats"))
        if g.get("section") and key not in seen_groups:
            seen_groups.add(key)
            groups.append(g)
    if len(groups) > 1:
        out["seat_groups"] = groups
    if links:
        for _url in links:
            _low = _url.lower()
            if (("kupat" in _low or "2207.co.il" in _low)
                    and any(k in _low for k in ("ticket", "/my-", "order", "print"))):
                out["ticket_url"] = _url
                break
        else:
            # Kupat routes links through SendGrid click-tracking; the browser
            # follows the redirect automatically. Take the first HTTPS tracked
            # link — typically the "view tickets" CTA is the first in the email.
            for _url in links:
                if _url.startswith("https://") and "sendgrid.net" in _url.lower():
                    out["ticket_url"] = _url
                    break
    return out


def _parse_candidate_date(text):
    """viagogo picker date display -> datetime.date, or None. Seen formats:
    'Sep 06 2026', '4 Jun 2026', with or without a weekday prefix/commas."""
    if not text:
        return None
    parts = re.sub(r",", " ", str(text)).split()
    tail = " ".join(parts[-3:])
    for fmt in ("%b %d %Y", "%d %b %Y", "%B %d %Y", "%d %B %Y"):
        try:
            return datetime.strptime(tail, fmt).date()
        except ValueError:
            pass
    return None


def _candidate_date_mismatch(chosen, event_date_iso):
    """Human-readable warning when the auto-chosen viagogo event's date
    differs from the ticket email's date — multi-night runs (Eyal Golan
    Sep 06/07/10 2026) make a same-artist wrong-night match an expensive
    mistake. None when the dates agree or either side is unparseable."""
    if not chosen or not event_date_iso:
        return None
    try:
        want = datetime.strptime(event_date_iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    got = _parse_candidate_date(chosen.get("date"))
    if not got or got == want:
        return None
    return (f"DATE MISMATCH: ticket is {want.strftime('%a %b %d %Y')} but the "
            f"matched viagogo event is {got.strftime('%a %b %d %Y')} — pick the "
            "right show before approving")


# How far a candidate's date may sit from the ticket's and still be
# auto-chosen. An artist's own run of nights is the case this must get
# right, so it is deliberately tight.
CANDIDATE_DATE_WINDOW_DAYS = 3


def _rank_viagogo_candidates(candidates, event_date_iso):
    """The candidate closest to the ticket's date, or None if none is close.

    Returning None is the important half. Name matching alone finds shows
    that merely share a word — "Hysteria" surfaces forty Def Leppard tribute
    nights, and the old fallbacks (a day-of-month SUBSTRING, then simply the
    first candidate) auto-chose one of them for a Tel Aviv ticket, so seven
    cards sat at awaiting_approval offering a London pub gig (2026-09-08).
    A same-artist wrong-night match is expensive; a wrong-artist one is
    worse. So when the ticket's date is known and nothing lands within
    CANDIDATE_DATE_WINDOW_DAYS of it, pick nothing and let the caller say so.

    With no usable ticket date there is nothing to judge on, and the first
    live candidate is as good a starting point as any — the user confirms on
    the card either way.

    viagogo's picker also lists "requested event" placeholder rows alongside
    the real live event (same artist, same date). Never auto-pick a
    requested row while any live candidate exists — a listing created on the
    placeholder doesn't actually go on sale. If ONLY requested rows exist we
    still rank them (the user sees and can override on the card).
    """
    if not candidates:
        return None
    live = [c for c in candidates
            if "requested" not in (c.get("event_name") or "").lower()]
    pool = live or candidates
    try:
        want = datetime.strptime((event_date_iso or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return pool[0]
    near = []
    for c in pool:
        got = _parse_candidate_date(c.get("date"))
        if got is not None and abs((got - want).days) <= CANDIDATE_DATE_WINDOW_DAYS:
            near.append((abs((got - want).days), c))
    if not near:
        return None
    return min(near, key=lambda t: t[0])[1]


def _no_near_candidate_error(candidates, event_date_iso, term):
    """Message for the case _rank_viagogo_candidates refused to pick: name
    matches exist but none is on (or near) the ticket's night."""
    dates = [d for d in (_parse_candidate_date(c.get("date")) for c in candidates)
             if d is not None]
    nearest = ""
    if dates:
        try:
            want = datetime.strptime((event_date_iso or "")[:10], "%Y-%m-%d").date()
            best = min(dates, key=lambda d: abs((d - want).days))
            nearest = f" (nearest is {best.strftime('%a %b %d %Y')})"
        except ValueError:
            pass
    return (f"viagogo lists {len(candidates)} event(s) matching '{term}' but none "
            f"on your ticket's date{nearest} — it may be a different show with a "
            "similar name. Pick from the list if one is right, or paste the "
            "viagogo event link.")


def _resolve_search_term(event_name, venue):
    """Translate a Hebrew Kupat EVENT NAME into the English term to search
    viagogo with, using the learned kupat_name_map.

    Kupat event names often carry a suffix the map key won't include —
    "חנן בן ארי - הינדיקים" (opening act), "… - מופע נוסף", etc. So beyond an
    exact lookup we (a) try each fragment split on common separators, and
    (b) check whether any known map key is a substring of the event name.
    Falls back to the raw Hebrew (which usually returns nothing) so the push
    lands as no_match and the user can teach the mapping.

    The VENUE is deliberately never used to build the search term: venue map
    entries (היכל מנורה מבטחים → Menora Mivtachim Arena) exist for section
    translation, and searching the picker by venue returns every show at
    that hall — three Eyal Golan pushes auto-matched to a Miri Mesika event
    that way (2026-07-09). Better an honest no_match + teach box than a
    confident wrong artist.
    """
    if event_name:
        hit = db.kupat_name_map_get(event_name)
        if hit:
            return hit
        for frag in re.split(r"\s*[-–—|,/]\s*", event_name):
            frag = frag.strip()
            if frag:
                hit = db.kupat_name_map_get(frag)
                if hit:
                    return hit
        try:
            for m in (db.kupat_name_map_all() or []):
                heb = m.get("hebrew_name") or ""
                if heb and heb in event_name:
                    return m.get("english_name") or event_name
        except Exception:
            pass
    return event_name or venue


_HEBREW_RX = re.compile("[\u0590-\u05FF]")


# Tokens too common to prove a picker row is the artist we asked for —
# Hebrew name particles transliterate into a handful of shared words, so
# "Eden Ben Zaken" must not answer a search for "Pe'er Tasi and Hanan Ben Ari".
_WEAK_TOKENS = {
    "and", "the", "with", "feat", "featuring", "live", "tour", "show", "band",
    "ben", "bar", "bat", "abu", "les", "des", "van", "der", "night", "nights",
    "festival", "concert", "tickets", "only", "presents", "orchestra",
}
_TOKEN_RX = re.compile(r"[^a-z0-9]+")


def _name_tokens(text):
    return [t for t in _TOKEN_RX.split((text or "").lower())
            if len(t) >= 3 and t not in _WEAK_TOKENS]


def _relevant_candidates(search_term, rows):
    """Keep only picker rows that actually answer `search_term`.

    viagogo's New Listing picker does NOT return an empty list for a query
    it can't match — it falls back to its default upcoming-events list. So a
    search for a show viagogo doesn't carry comes back looking like 8 or 25
    perfectly good candidates, `_rank_viagogo_candidates` picks whichever one
    lands near the ticket's date, and the card confidently offers the wrong
    artist (2026-09-07: searching "Hysteria" returned Eyal Golan, Shlomo Artzi
    and Idan Amedi; the existing Hebrew guard only covered untranslated terms).

    A row qualifies when it shares enough distinctive tokens with the term:
    both of them when the term has two or more, otherwise its only one. That
    keeps genuinely related shows ("Hanan Ben Ari" for a "Pe'er Tasi and Hanan
    Ben Ari" ticket) while dropping the default list wholesale.
    """
    want = _name_tokens(search_term)
    if not want:
        return rows
    need = min(2, len(want))
    out = []
    for r in rows:
        have = set(_name_tokens(r.get("event_name")))
        if sum(1 for t in want if t in have) >= need:
            out.append(r)
    return out


# How many candidates a card offers in its dropdown. The picker can return
# fifty rows for a common word; the ones worth showing are the ones near the
# ticket's night, not the first N in viagogo's date order.
CANDIDATE_LIMIT = 12


def _nearest_candidates(rows, want_dates, limit=CANDIDATE_LIMIT):
    """The `limit` rows closest to any of `want_dates` (undated rows last).

    Slicing viagogo's own date order instead is how the right show got lost:
    a "Hysteria" search renders 49 rows and the three Israeli nights are the
    LAST of them, behind forty US tribute shows, so every earlier cap — 8,
    25, even 40 — cut off exactly the events the tickets were for.
    """
    if not want_dates:
        return rows[:limit]

    def distance(r):
        got = _parse_candidate_date(r.get("date"))
        if got is None:
            return 10 ** 6
        return min(abs((got - w).days) for w in want_dates)

    return sorted(rows, key=distance)[:limit]


def _search_viagogo(search_term, want_dates=None):
    """search_event, with three guards against a confidently wrong match.

    (1) viagogo's picker can't match Hebrew — fed a Hebrew query it returns
    its default/popular event list, which once "matched" פאר טסי to a World
    Cup game. If the resolved term is still Hebrew (no name mapping taught
    yet), report no candidates so the push lands as no_match and the user
    gets the teach flow instead of a bogus awaiting_approval.

    (2) The same default list comes back for an ENGLISH term viagogo simply
    doesn't carry, so the results are filtered for actual relevance too.

    (3) What survives is then narrowed to the shows nearest `want_dates` —
    the ticket dates this search is for — rather than the first few in
    viagogo's date order.

    An empty return means "no match", which is the honest answer.
    """
    if _HEBREW_RX.search(search_term or ""):
        return []
    rows = viagogo_listing.search_event(search_term)
    keep = _relevant_candidates(search_term, rows)
    if rows and not keep:
        print(f"[intake] viagogo picker had nothing for {search_term!r} "
              f"(it offered {[r.get('event_name') for r in rows[:5]]})")
    return _nearest_candidates(keep, want_dates)


def _ticket_dates(*iso_strings):
    """Parse ticket dates for _search_viagogo, skipping the unparseable."""
    out = []
    for iso in iso_strings:
        try:
            out.append(datetime.strptime((iso or "")[:10], "%Y-%m-%d").date())
        except ValueError:
            pass
    return out


def _push_kupat_to_viagogo(intake_id, fields):
    """Search viagogo for a matching event, price it at 5x the USD-converted
    per-ticket cost, and stage a viagogo_push row for the user to approve on
    /listings. Never creates a listing itself. Despite the name it serves all
    Israeli providers (kupat, tickchak, ticketmaster_il) — tickchak emails
    carry no pricing, so those stage with an empty price for the user to
    fill in on the card before approving.

    Best-effort by design — the caller wraps this in its own try/except so
    a viagogo/FX hiccup never affects normal intake recording.
    """
    event_name = fields.get("event_name") or ""
    venue = fields.get("venue") or ""
    cost_per_unit = fields.get("cost_per_unit")
    if not event_name:
        return None

    # Tickchak emails state neither quantity nor price, but the order's
    # official print PDF has both (plus seating) — resolve them so the card
    # comes pre-filled like Kupat's.
    if "tickchak" in (fields.get("ticket_url") or "").lower() and (
            fields.get("qty") is None or cost_per_unit is None):
        try:
            det = viagogo_listing.tickchak_order_details(fields["ticket_url"])
            for k in ("qty", "cost_per_unit", "cost", "section", "row_label", "seats"):
                if not fields.get(k) and det.get(k):
                    fields[k] = det[k]
            cost_per_unit = fields.get("cost_per_unit")
        except Exception:
            pass

    now_iso = datetime.now(timezone.utc).isoformat()

    # One push (→ one viagogo listing) per seat group. A single order can
    # hold seats in different sections/rows (e.g. orchestra row 2 AND row
    # 10) — those must never be merged into one listing.
    groups = fields.get("seat_groups") or [{
        "section": fields.get("section") or "",
        "row_label": fields.get("row_label") or "",
        "seats": fields.get("seats") or "",
        "qty": fields.get("qty"),
        "cost_per_unit": cost_per_unit,
        "cost": fields.get("cost"),
    }]

    # Kupat emails are in Hebrew; viagogo's event picker uses English names.
    # Resolve via the learned name map (tolerating suffixed event names),
    # falling back to the Hebrew string (usually no match) so the user can
    # teach it. The search + ranking are shared by all groups — same event.
    search_term = _resolve_search_term(event_name, venue)
    search_error = None
    candidates = []
    try:
        candidates = _search_viagogo(
            search_term, _ticket_dates(fields.get("event_date_iso")))
    except Exception as e:
        search_error = f"{type(e).__name__}: {e}"

    chosen = _rank_viagogo_candidates(candidates, fields.get("event_date_iso"))
    try:
        fx_rate = fx.ils_to_usd_rate()
    except Exception:
        fx_rate = None

    first_push = None
    for g in groups:
        g_cpu = g.get("cost_per_unit") if g.get("cost_per_unit") is not None else cost_per_unit
        g_qty = g.get("qty") if g.get("qty") is not None else fields.get("qty")
        g_cost = g.get("cost")
        if g_cost is None:
            g_cost = (round(g_cpu * g_qty, 2) if g_cpu is not None and g_qty
                      else (fields.get("cost") if len(groups) == 1 else None))
        try:
            cost_usd = fx.ils_to_usd(g_cpu) if g_cpu is not None else None
            website_price = round(cost_usd * 5, 2) if cost_usd is not None else None
        except Exception:
            cost_usd = website_price = None

        push_id = "vgp-" + uuid.uuid4().hex[:12]
        row = {
            "id": push_id,
            "intake_id": intake_id,
            "event_name": event_name,
            "venue": venue,
            "event_date_iso": fields.get("event_date_iso") or "",
            "section": g.get("section") or "",
            "row_label": g.get("row_label") or "",
            "seats": g.get("seats") or "",
            "qty": g_qty,
            "cost": g_cost,
            "cost_per_unit": g_cpu,
            "ticket_url": fields.get("ticket_url"),
            "buyer_email": fields.get("buyer_email"),
        }
        if search_error:
            row["status"] = "error"
            row["error"] = search_error
        elif not candidates:
            row["status"] = "no_match"
            row["candidates_json"] = "[]"
        elif chosen is None:
            # Name matches, but nothing on the ticket's night. Keep them
            # listed for reference and leave the card in the no_match state
            # that offers the teach box and the paste-a-link box.
            row["status"] = "no_match"
            row["candidates_json"] = json.dumps(candidates, ensure_ascii=False)
            row["error"] = _no_near_candidate_error(
                candidates, fields.get("event_date_iso"), search_term)
        else:
            row.update({
                "status": "awaiting_approval",
                "candidates_json": json.dumps(candidates, ensure_ascii=False),
                "chosen_event_id": chosen.get("event_id"),
                "chosen_event_name": chosen.get("event_name"),
                "chosen_venue": chosen.get("venue"),
                "chosen_event_date": chosen.get("date"),
                "fx_rate": fx_rate,
                "cost_usd_per_ticket": cost_usd,
                "website_price_usd": website_price,
                "error": _candidate_date_mismatch(
                    chosen, fields.get("event_date_iso")),
            })
        db.viagogo_push_insert(row, now_iso)
        push = db.viagogo_push_get(push_id)
        _notify_viagogo_push(push)
        if first_push is None:
            first_push = push
    return first_push


def _push_kupat_to_viagogo_update(push_id, fields, now_iso=None,
                                  force_term=None, force_event_id=None):
    """Re-run the viagogo event search for an existing push row (e.g. after
    the user teaches a Hebrew→English name mapping) and update it in place.

    force_term / force_event_id come from a pasted viagogo event URL: the
    picker is searched with the URL's slug-derived name and the exact event
    id from the URL must be among the results (the create flow replays the
    same search later, so the term must actually surface the event)."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    event_name = fields.get("event_name") or ""
    venue = fields.get("venue") or ""
    cost_per_unit = fields.get("cost_per_unit")
    search_term = force_term or _resolve_search_term(event_name, venue)
    try:
        candidates = _search_viagogo(
            search_term, _ticket_dates(fields.get("event_date_iso")))
    except Exception as e:
        db.viagogo_push_update(push_id, {
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
            "candidates_json": "[]",
        }, now_iso)
        return

    if not candidates:
        db.viagogo_push_update(push_id, {
            "status": "no_match",
            "candidates_json": "[]",
        }, now_iso)
        return

    if force_event_id:
        chosen = next((c for c in candidates
                       if str(c.get("event_id")) == str(force_event_id)), None)
        if chosen is None:
            db.viagogo_push_update(push_id, {
                "status": "no_match",
                "candidates_json": json.dumps(candidates, ensure_ascii=False),
                "error": (f"event {force_event_id} from the pasted link wasn't in "
                          f"the picker results for '{search_term}' — pick from the "
                          f"dropdown or try a different link"),
            }, now_iso)
            return
    else:
        chosen = _rank_viagogo_candidates(candidates, fields.get("event_date_iso"))
        if chosen is None:
            db.viagogo_push_update(push_id, {
                "status": "no_match",
                "candidates_json": json.dumps(candidates, ensure_ascii=False),
                "error": _no_near_candidate_error(
                    candidates, fields.get("event_date_iso"), search_term),
            }, now_iso)
            return
    try:
        fx_rate = fx.ils_to_usd_rate()
        cost_usd = fx.ils_to_usd(cost_per_unit) if cost_per_unit is not None else None
        website_price = round(cost_usd * 5, 2) if cost_usd is not None else None
    except Exception:
        fx_rate = cost_usd = website_price = None

    db.viagogo_push_update(push_id, {
        "status": "awaiting_approval",
        "candidates_json": json.dumps(candidates, ensure_ascii=False),
        "chosen_event_id": chosen.get("event_id"),
        "chosen_event_name": chosen.get("event_name"),
        "chosen_venue": chosen.get("venue"),
        "chosen_event_date": chosen.get("date"),
        "fx_rate": fx_rate,
        "cost_usd_per_ticket": cost_usd,
        "website_price_usd": website_price,
        "error": _candidate_date_mismatch(chosen, fields.get("event_date_iso")),
    }, now_iso)


# Stamped into viagogo_push.error while a taught-name re-search is in
# flight, so the card says something is happening instead of looking
# untouched for the ~30s the picker takes. Any outcome overwrites it.
SEARCHING_PREFIX = "re-searching viagogo"

# A viagogo_push row is retryable from the teach box in exactly these states.
_RETRYABLE_STATUSES = ("no_match", "error")


def pushes_for_search_term(term, extra_push_id=None):
    """Every stuck push whose event name now resolves to `term`.

    Teaching a name used to retry ONLY the card that was clicked, so the five
    other היסטריה tickets from the same week sat there as no_match with the
    mapping already learned (2026-09-08). They all want the same picker
    search, so collect them and run it once.
    """
    ids = []
    for p in db.viagogo_push_all():
        if p.get("status") not in _RETRYABLE_STATUSES:
            continue
        if (p.get("id") == extra_push_id
                or _resolve_search_term(p.get("event_name") or "",
                                        p.get("venue") or "") == term):
            ids.append(p["id"])
    return ids


def mark_pushes_searching(push_ids, term, now_iso=None):
    """Flag the rows as busy BEFORE the browser work starts, so the page's
    first reload already shows it (the search outlives several reloads)."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    for pid in push_ids:
        db.viagogo_push_update(
            pid, {"error": f"{SEARCHING_PREFIX} for '{term}' — up to a minute…"},
            now_iso)


def retry_pushes_for_name(term, push_ids, now_iso=None):
    """Re-search viagogo ONCE for `term` and apply the result to every push
    in `push_ids`, each ranked against its own ticket date.

    Every row is written whatever happens — a silent no-op is what made the
    teach box look broken. An empty picker result lands as no_match with a
    message saying so, rather than leaving the card exactly as it was.
    """
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    if not push_ids:
        return
    # The cards in a batch are usually different nights of the same run, so
    # the shared search has to keep candidates near EVERY one of them.
    pushes = [db.viagogo_push_get(pid) for pid in push_ids]
    want = _ticket_dates(*[(p or {}).get("event_date_iso") for p in pushes])
    try:
        candidates = _search_viagogo(term, want)
    except Exception as e:
        for pid in push_ids:
            db.viagogo_push_update(pid, {
                "status": "error",
                "error": f"viagogo search for '{term}' failed: {type(e).__name__}: {e}",
            }, now_iso)
        return

    if not candidates:
        for pid in push_ids:
            db.viagogo_push_update(pid, {
                "status": "no_match",
                "candidates_json": "[]",
                "error": (f"viagogo's seller picker has no event matching "
                          f"'{term}' — check the English spelling viagogo uses, "
                          f"or paste the event link below"),
            }, now_iso)
        return

    cands_json = json.dumps(candidates, ensure_ascii=False)
    for pid in push_ids:
        push = db.viagogo_push_get(pid)
        if not push:
            continue
        chosen = _rank_viagogo_candidates(candidates, push.get("event_date_iso"))
        if chosen is None:
            db.viagogo_push_update(pid, {
                "status": "no_match",
                "candidates_json": cands_json,
                "error": _no_near_candidate_error(
                    candidates, push.get("event_date_iso"), term),
            }, now_iso)
            continue
        try:
            cpu = push.get("cost_per_unit")
            fx_rate = fx.ils_to_usd_rate()
            cost_usd = fx.ils_to_usd(cpu) if cpu is not None else None
            website_price = round(cost_usd * 5, 2) if cost_usd is not None else None
        except Exception:
            fx_rate = cost_usd = website_price = None
        db.viagogo_push_update(pid, {
            "status": "awaiting_approval",
            "candidates_json": cands_json,
            "chosen_event_id": chosen.get("event_id"),
            "chosen_event_name": chosen.get("event_name"),
            "chosen_venue": chosen.get("venue"),
            "chosen_event_date": chosen.get("date"),
            "fx_rate": fx_rate,
            "cost_usd_per_ticket": cost_usd,
            "website_price_usd": website_price,
            "error": _candidate_date_mismatch(chosen, push.get("event_date_iso")),
        }, now_iso)


def set_push_event_from_url(push_id, url_or_id, now_iso=None):
    """Point an existing push at a viagogo event pasted as a public URL
    (.../E-<id>) or a bare event id — the escape hatch for shows the picker
    search didn't surface as candidates (e.g. a freshly added extra night).

    The seller flows (fetch_sections, create_draft_listing) locate events
    through the New Listing picker, so the pasted event must be findable
    there: we re-search the picker under the push's resolved English term
    and require a row with that id. Found -> merged into candidates, set as
    chosen, date-mismatch recomputed. Not found -> ValueError with a clear
    reason (the event may not be open for seller listings yet).
    """
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    m = re.search(r"/E-(\d+)", str(url_or_id)) or re.fullmatch(r"\s*(\d+)\s*", str(url_or_id))
    if not m:
        raise ValueError("couldn't find an event id in that link — expected .../E-123456 or a bare id")
    event_id = m.group(1)
    push = db.viagogo_push_get(push_id)
    if not push:
        raise ValueError(f"push {push_id} not found")

    term = _resolve_search_term(push.get("event_name") or "", push.get("venue") or "")
    queries = [term]
    if (push.get("chosen_event_name") or "") and push["chosen_event_name"] != term:
        queries.append(push["chosen_event_name"])
    # The picker's async filter is flaky — a search sometimes returns the
    # unfiltered event list without our row. Retry each query a few times
    # before concluding the event genuinely isn't listable.
    match = None
    searched = False          # did any attempt actually reach the picker?
    last_error = None
    seen = []
    for attempt in range(3):
        for q in queries:
            try:
                # No limit: a pasted link is often for a date deep in the list,
                # exactly where a cap used to hide it.
                rows = viagogo_listing.search_event(q)
            except Exception as e:
                last_error = e
                print(f"[intake] set-event picker search failed ({q!r}): {e}")
                continue
            searched = True
            seen = rows
            match = next((r for r in rows if str(r.get("event_id")) == event_id), None)
            if match:
                break
        if match:
            break
    if not match:
        # Don't blame the event for a browser/lock failure. Only claim the
        # picker can't see it when the picker actually answered — otherwise
        # surface the real reason (2026-09-07: every attempt was losing the
        # shared browser lock to the listings page's section fetches, and the
        # user was told the show "may not be open for seller listings yet").
        if not searched:
            raise ValueError(
                f"couldn't reach viagogo's seller picker to verify event "
                f"{event_id}: {last_error} — try again in a moment")
        listed = ", ".join(
            f"{r.get('date') or '?'} {r.get('venue') or ''}".strip()
            for r in seen[:6]) or "nothing"
        raise ValueError(
            f"viagogo's seller picker doesn't list event {event_id} under "
            f"'{term}' — the show may not be open for seller listings yet; "
            f"try again later (it offered: {listed})")

    candidate = {k: match.get(k) for k in
                 ("event_id", "event_name", "venue", "city", "weekday", "date", "time")}
    try:
        cands = json.loads(push.get("candidates_json") or "[]")
    except (TypeError, ValueError):
        cands = []
    if not any(str(c.get("event_id")) == event_id for c in cands):
        cands.append(candidate)
    db.viagogo_push_update(push_id, {
        "status": "awaiting_approval",
        "candidates_json": json.dumps(cands, ensure_ascii=False),
        "chosen_event_id": event_id,
        "chosen_event_name": candidate.get("event_name"),
        "chosen_venue": candidate.get("venue"),
        "chosen_event_date": candidate.get("date"),
        "error": _candidate_date_mismatch(candidate, push.get("event_date_iso")),
    }, now_iso)
    return candidate


def _notify_viagogo_push(push):
    if not push:
        return
    try:
        notify.notify_viagogo_match(push)
    except Exception:
        pass


def _parse_tickchak(subject, sender, body, links=None):
    """Tickchak registration confirmation (Hebrew). The body doesn't show
    qty or cost, so the user fills those manually. The event name lives in
    the sender display-name (e.g. 'בליבנו רק שיר אחד קיים <info@tickchak.co.il>'),
    and the date is in DD/MM/YYYY."""
    out = {"warnings": ["no_pricing_in_email"]}
    m = re.match(r'\s*"?([^<"]+?)"?\s*<', sender or "")
    if m:
        out["event_name"] = m.group(1).strip()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", body or "")
    if m:
        d, mo, y = m.groups()
        out["event_date_iso"] = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    # Venue often appears as the last line of the intro stanza right above
    # the greeting "שלום". Best-effort grab.
    m = re.search(r"\n([^\n]+)\n\s*\n\s*שלום\s", body or "")
    if m:
        out["venue"] = m.group(1).strip()
    # Ticket viewer link. Direct /n/ links are rare in the confirmation —
    # what it actually carries is a static.tickchak.co.il/all/emailLink URL
    # whose rendered page's 'הציגו כרטיס' button chains (via tic.li) into
    # the /n/ viewer; viagogo_listing._resolve_tickchak_viewer follows that
    # hop at download time. Prefer a direct /n/ link when present.
    for _url in (links or []):
        if re.search(r"app\.tickchak\.co\.il/n/", _url, re.I):
            out["ticket_url"] = _url
            break
    else:
        for _url in (links or []):
            if re.search(r"static\.tickchak\.co\.il/all/emailLink", _url, re.I):
                out["ticket_url"] = _url
                break
        else:
            m = re.search(r"https://app\.tickchak\.co\.il/n/[A-Za-z0-9_\-]+", body or "")
            if m:
                out["ticket_url"] = m.group(0)
    return out


_TMIL_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)


def _strip_cell_html(s):
    """HTML table cell -> plain text; <br> becomes a newline so multi-line
    cells (event name / datetime / venue) stay splittable."""
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;|\xa0", " ", s)
    return "\n".join(" ".join(line.split()) for line in s.splitlines()).strip()


def _parse_ticketmaster_il(subject, body, html=None, links=None):
    """Ticketmaster Israel 'Transaction Summary'.

    The reliable source is the HTML transaction table (header row אירוע /
    אזור / שורה / מושב(ים) / סוג / כמות / מחיר / סה״כ, then one data row
    whose first cell is name<br>datetime<br>venue). The old stripped-text
    regexes remain as fallback for pathological bodies.
    """
    out = {"warnings": []}

    # --- primary: HTML table ------------------------------------------------
    # One transaction can span multiple table rows (different sections/rows
    # in a single order) — parse every data row; the first fills the main
    # fields and 2+ rows become seat_groups (one viagogo listing each).
    hidx = (html or "").find(">אירוע<")
    if hidx >= 0:
        groups = []
        cursor = html.find("</tr>", hidx)
        while cursor > 0:
            tr_start = html.find("<tr", cursor)
            tr_end = html.find("</tr>", tr_start) if tr_start > 0 else -1
            if tr_end < 0:
                break
            cells = [_strip_cell_html(c) for c in _TMIL_TD_RE.findall(html[tr_start:tr_end])]
            if len(cells) < 8:
                break
            g = {}
            first = [l for l in cells[0].split("\n") if l.strip()]
            if first:
                g["event_name"] = first[0]
            for line in first[1:]:
                m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s", line)
                if m:
                    g["event_date_iso"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                else:
                    g.setdefault("venue", line)
            if cells[1]:
                g["section"] = cells[1]
            if cells[2]:
                g["row_label"] = cells[2]
            m = re.match(r"(\d+)\s*[-–]\s*(\d+)", cells[3] or "")
            if m:
                a, b = sorted((int(m.group(1)), int(m.group(2))))
                g["seats"] = f"{a} - {b}"
            elif cells[3]:
                g["seats"] = cells[3]
            try:
                g["qty"] = int(cells[5])
            except (ValueError, IndexError):
                pass
            try:
                g["cost_per_unit"] = float(cells[6].replace(",", ""))
            except (ValueError, IndexError):
                pass
            try:
                g["cost"] = float(cells[7].replace(",", ""))
            except (ValueError, IndexError):
                pass
            groups.append(g)
            cursor = tr_end
        if groups:
            for k, v in groups[0].items():
                out.setdefault(k, v)
            if len(groups) > 1:
                out["seat_groups"] = [
                    {k: g.get(k) for k in ("section", "row_label", "seats", "qty",
                                           "cost_per_unit", "cost")}
                    for g in groups
                ]

    # --- fallbacks: stripped-text regexes ------------------------------------
    if not out.get("event_name"):
        m = re.search(r"\*סיכום\s*העסקה\*[\s\S]*?\*([^\*\n]+)\*", body or "")
        if m:
            out["event_name"] = m.group(1).strip()
    if not out.get("event_date_iso"):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+\d{1,2}:\d{2}", body or "")
        if m:
            out["event_date_iso"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if out.get("cost") is None:
        m = re.search(r"\*סה[״\"]?כ\*\s*\*([\d,.]+)\*", body or "")
        if m:
            try:
                out["cost"] = float(m.group(1).replace(",", ""))
            except ValueError:
                pass

    # e-ticket viewer link: https://www.ticketmaster.co.il/t/<token>/<ref>/<lang>
    for _url in (links or []):
        if re.search(r"ticketmaster\.co\.il/t/", _url, re.I):
            out["ticket_url"] = _url
            break
    return out


def extract_fields(provider, subject, sender, body, attachments, links=None, html=None):
    """Dispatch to per-provider extractors, then fall back to generic
    regex search for any field still missing. Returned dict includes a
    `warnings` list with parser quibbles (e.g. cost_not_found)."""
    text = (subject or "") + "\n" + (body or "")

    if provider == "kupat":
        out = _parse_kupat(subject, body, links=links)
    elif provider == "tickchak":
        out = _parse_tickchak(subject, sender, body, links=links)
    elif provider in ("ticketmaster_il",):
        out = _parse_ticketmaster_il(subject, body, html=html, links=links)
    else:
        out = {"warnings": []}

    warnings = list(out.get("warnings") or [])

    # Generic fallbacks for whatever the provider parser didn't fill.
    if not out.get("event_name"):
        ev = (subject or "").strip()
        for prefix in ("Re: ", "Fwd: ", "FW: ", "Your Order Confirmation:",
                       "Order Confirmation:", "Your tickets for", "Tickets for"):
            if ev.lower().startswith(prefix.lower()):
                ev = ev[len(prefix):].strip(" -:")
        out["event_name"] = ev
    if not out.get("event_date_iso"):
        d = _to_iso(text)
        if d:
            out["event_date_iso"] = d
        else:
            warnings.append("date_not_found")
    if out.get("cost") is None:
        c = _extract_amount(text)
        if c is not None:
            out["cost"] = c
        else:
            warnings.append("cost_not_found")
    if out.get("qty") is None:
        q = _extract_qty(text)
        if q is not None:
            out["qty"] = q
    # Derive cost_per_unit when we have total + qty
    if out.get("cost_per_unit") is None and out.get("cost") and out.get("qty"):
        out["cost_per_unit"] = round(out["cost"] / out["qty"], 2)

    return {
        "event_name": out.get("event_name") or "",
        "event_date_iso": out.get("event_date_iso") or "",
        "venue": out.get("venue") or "",
        "section": out.get("section") or "",
        "row_label": out.get("row_label") or "",
        "seats": out.get("seats") or "",
        "qty": out.get("qty"),
        "cost": out.get("cost"),
        "cost_per_unit": out.get("cost_per_unit"),
        "ticket_url": out.get("ticket_url"),
        "warnings": warnings,
    }


# ---- IMAP plumbing -----------------------------------------------------

def _connect():
    user = os.getenv("GMAIL_USER")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    if not user or not pw:
        raise RuntimeError("GMAIL_USER and GMAIL_APP_PASSWORD must be set in .env")
    M = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
    M.login(user, pw.replace(" ", ""))
    return M


def imap_fetch_new(limit=MAX_PER_POLL, lookback_days=None):
    """Returns a list of (uid, raw_bytes, message_id) for messages received
    in the last `lookback_days` days. Idempotency is handled at the DB
    layer (Message-ID UNIQUE), so it's safe to re-fetch the same messages.

    We use BODY.PEEK so the \\Seen flag is *not* flipped — that way the
    user's own UI in Gmail still shows the forwarded mail as Unread until
    they read it themselves."""
    from datetime import date as _date, timedelta as _timedelta
    days = lookback_days if lookback_days is not None else LOOKBACK_DAYS
    since = (_date.today() - _timedelta(days=days)).strftime("%d-%b-%Y")
    M = _connect()
    try:
        M.select(INTAKE_FOLDER)
        typ, data = M.search(None, "SINCE", since)
        if typ != "OK":
            return []
        uids = data[0].split()
        # Take the most recent N (search returns oldest-first).
        uids = uids[-limit:] if len(uids) > limit else uids
        out = []
        for uid in uids:
            typ, msg_data = M.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = message_from_bytes(raw)
            mid = (msg.get("Message-ID") or "").strip()
            out.append((uid.decode() if isinstance(uid, bytes) else uid, raw, mid))
        return out
    finally:
        try:
            M.close()
        except Exception:
            pass
        M.logout()


def imap_fetch_from(sender_substr, lookback_days):
    """Targeted fetch: messages whose From matches `sender_substr` within
    `lookback_days`. For low-volume, must-not-miss senders (Capital One
    cash-back) that the newest-N general poll can drop in a busy inbox.
    Returns the same (uid, raw_bytes, message_id) tuples as imap_fetch_new."""
    from datetime import date as _date, timedelta as _timedelta
    since = (_date.today() - _timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    M = _connect()
    try:
        M.select(INTAKE_FOLDER)
        typ, data = M.search(None, "FROM", sender_substr, "SINCE", since)
        if typ != "OK":
            return []
        uids = data[0].split()
        out = []
        for uid in uids:
            typ, msg_data = M.fetch(uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = message_from_bytes(raw)
            mid = (msg.get("Message-ID") or "").strip()
            out.append((uid.decode() if isinstance(uid, bytes) else uid, raw, mid))
        return out
    finally:
        try:
            M.close()
        except Exception:
            pass
        M.logout()


def _record_cashback(parsed):
    """Parse an (already-unwrapped) email as a Capital One cash-back
    redemption and insert it into cashback_entries if new. Returns True when a
    row was added. Idempotent via the source_ref dedup."""
    cb = cashback_email.parse_cashback(
        parsed["from"], parsed["subject"], parsed["body"],
        parsed["received_at"], message_id=parsed["message_id"],
    )
    if cb and not db.has_cashback_source_ref(cb["source_ref"]):
        db.insert_cashback_entry({
            "id": "cb-" + uuid.uuid4().hex[:12],
            "date_iso": cb["date_iso"],
            "amount": cb["amount"],
            "card_name": cb["card_name"],
            "source_ref": cb["source_ref"],
        }, datetime.now(timezone.utc).isoformat())
        return True
    return False


def _received_date(parsed):
    """The email's own date as a datetime.date (or None) — used to infer the
    year DICE omits from event dates."""
    try:
        return datetime.fromisoformat(parsed.get("received_at") or "").date()
    except ValueError:
        return None


def _apply_dice_transfer(transfer, account_email):
    """FIFO-match a parsed transfer against the account's open purchases.
    Bumps qty_transferred across as many matching purchases as needed
    (oldest first). Returns (purchase_id, match_status, held_after):
    purchase_id is the first purchase touched; match_status is 'matched',
    'unmatched' (no candidate purchase), or 'overflow' (transfer qty
    exceeded what the account held — applied what fit)."""
    qty = transfer.get("qty") or 0
    candidates = [p for p in db.dice_purchases_open(account_email)
                  if dice_email.purchase_matches(transfer, p)]
    if not candidates:
        return None, "unmatched", None
    first_id = None
    remaining = qty
    for p in candidates:
        if remaining <= 0:
            break
        take = min(remaining, p["qty"] - p["qty_transferred"])
        if take <= 0:
            continue
        db.dice_purchase_add_transferred(p["id"], take)
        remaining -= take
        if first_id is None:
            first_id = p["id"]
    held_after = sum(p["qty"] - p["qty_transferred"] for p in candidates) - (qty - remaining)
    status = "overflow" if remaining > 0 else "matched"
    return first_id, status, max(held_after, 0)


def _record_dice(parsed, raw):
    """Route an (already-unwrapped) DICE email into dice_purchases /
    dice_transfers. Returns 'purchase', 'transfer', or None when the mail is
    neither kind (login codes, incoming 'sent you tickets', ...). Idempotent
    via has_dice_message + message_id UNIQUE."""
    sender, subject, body = parsed["from"], parsed["subject"], parsed["body"]
    is_purchase = dice_email.is_dice_purchase(sender, subject, body)
    is_transfer = not is_purchase and dice_email.is_dice_transfer(sender, subject, body)
    if not (is_purchase or is_transfer):
        return None
    mid = parsed.get("message_id") or ""
    if mid and db.has_dice_message(mid):
        return None
    msg = message_from_bytes(raw)
    account = _buyer_email(msg, _email_body_text(msg) if body is None else body)
    now = datetime.now(timezone.utc).isoformat()
    email_date = _received_date(parsed)

    if is_purchase:
        f = dice_email.parse_purchase(subject, body, email_date)
        row = {
            "id": "dcp-" + uuid.uuid4().hex[:12],
            "message_id": mid or None,
            "account_email": account,
            "event_name": f.get("event_name") or "",
            "event_slug": f.get("event_slug") or "",
            "event_date_iso": f.get("event_date_iso") or "",
            "venue": f.get("venue") or "",
            "ticket_type": f.get("ticket_type") or "",
            "qty": f.get("qty"),
            "price_total": f.get("price_total"),
            "price_per_unit": f.get("price_per_unit"),
            "currency": f.get("currency") or "",
            "email_date": parsed.get("received_at") or "",
            "subject": (subject or "")[:500],
            "raw_text": (body or "")[:8000],
            "warnings": ",".join(f.get("warnings") or []),
        }
        db.insert_dice_purchase(row, now)
        if DICE_PINGS_ENABLED:
            try:
                notify.notify_dice("purchase", {**row, "account_email": account})
            except Exception:
                pass
        return "purchase"

    f = dice_email.parse_transfer(subject, body, email_date)
    purchase_id, match_status, held_after = _apply_dice_transfer(f, account)
    row = {
        "id": "dct-" + uuid.uuid4().hex[:12],
        "message_id": mid or None,
        "account_email": account,
        "event_name": f.get("event_name") or "",
        "event_slug": f.get("event_slug") or "",
        "event_date_iso": f.get("event_date_iso") or "",
        "qty": f.get("qty"),
        "recipient": f.get("recipient") or "",
        "purchase_id": purchase_id,
        "match_status": match_status,
        "email_date": parsed.get("received_at") or "",
    }
    db.insert_dice_transfer(row, now)
    try:
        if match_status != "matched":
            notify.notify_dice("transfer_problem", row)
        elif DICE_PINGS_ENABLED:
            notify.notify_dice("transfer", {**row, "held_after": held_after})
    except Exception:
        pass
    return "transfer"


def sweep_dice(lookback_days=None):
    """Targeted DICE capture, independent of the capped general poll —
    auto-forwards keep From: dice.fm so a FROM search finds them even in a
    busy inbox. Returns dict(purchases=N, transfers=N)."""
    out = {"purchases": 0, "transfers": 0}
    try:
        candidates = imap_fetch_from("dice.fm", lookback_days or DICE_LOOKBACK_DAYS)
    except Exception:
        return out
    for _uid, raw, mid in candidates:
        try:
            if mid and db.has_dice_message(mid):
                continue
            parsed = parse_email(raw)
            eff_from, eff_subject, eff_body = _unwrap_forwarded(parsed)
            parsed["from"] = eff_from
            parsed["subject"] = eff_subject
            parsed["body"] = eff_body
            kind = _record_dice(parsed, raw)
            if kind == "purchase":
                out["purchases"] += 1
            elif kind == "transfer":
                out["transfers"] += 1
        except Exception:
            continue
    return out


def sweep_cashback():
    """Targeted Capital One cash-back capture, independent of the capped
    general poll. Returns the number of new entries added."""
    saved = 0
    try:
        candidates = imap_fetch_from("capitalone", CASHBACK_LOOKBACK_DAYS)
    except Exception:
        return 0
    for _uid, raw, _mid in candidates:
        try:
            parsed = parse_email(raw)
            eff_from, eff_subject, eff_body = _unwrap_forwarded(parsed)
            parsed["from"] = eff_from
            parsed["subject"] = eff_subject
            parsed["body"] = eff_body
            if not cashback_email.is_capitalone_cashback(parsed["from"], parsed["subject"], parsed["body"]):
                continue
            if _record_cashback(parsed):
                saved += 1
        except Exception:
            continue
    return saved


def parse_email(raw_bytes):
    msg = message_from_bytes(raw_bytes)
    sender = _decode(msg.get("From") or "")
    subject = _decode(msg.get("Subject") or "")
    received_at = ""
    try:
        d = parsedate_to_datetime(msg.get("Date"))
        if d:
            received_at = d.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    body = _email_body_text(msg)
    atts = _extract_attachments(msg)
    return {
        "from": sender,
        "subject": subject,
        "received_at": received_at,
        "body": body,
        "attachments": atts,
        "message_id": (msg.get("Message-ID") or "").strip(),
    }


def _save_intake_attachment(intake_id, filename, content_type, payload):
    """Persists one attachment using the same disk layout as the manual
    upload path. owner_type='manual_intake' keeps it visually separate from
    confirmed-row attachments until promotion."""
    safe_name = re.sub(r"[^\w.\-]+", "_", filename) or "file"
    att_id = "att-" + uuid.uuid4().hex[:12]
    sub = re.sub(r"[^A-Za-z0-9_\-]", "_", intake_id)
    dest_dir = attachments_mod.ATTACH_DIR / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{att_id}-{safe_name}"
    dest.write_bytes(payload)
    rel = dest.relative_to(attachments_mod.ATTACH_DIR).as_posix()
    db.insert_attachment({
        "id": att_id,
        "owner_type": "manual_intake",
        "owner_id": intake_id,
        "filename": filename,
        "stored_path": rel,
        "size_bytes": len(payload),
        "content_type": content_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    })


def run_intake():
    """Top-level: fetch + parse + save. Returns a summary dict the route
    can echo back to the UI.

    Sender filtering: by default we only ingest mail whose From: matches a
    known provider hint. The user's Gmail receives unrelated mail too
    (newsletters, support replies, SMS forwards) and we don't want them
    cluttering the Inbox panel. Set KARTIS_INTAKE_ALLOW_UNKNOWN=1 to override.
    """
    allow_unknown = os.getenv("KARTIS_INTAKE_ALLOW_UNKNOWN", "0") == "1"
    fetched = imap_fetch_new()
    seen = 0
    saved = 0
    cashback_saved = 0
    dice_purchases_saved = 0
    dice_transfers_saved = 0
    series_saved = 0
    skipped_dupe = 0
    skipped_provider = 0
    errors = 0
    for _uid, raw, mid in fetched:
        seen += 1
        try:
            if mid and db.has_intake_message(mid):
                skipped_dupe += 1
                continue
            parsed = parse_email(raw)
            # If this is a forwarded email, the outer From: is the user; the
            # real sender is buried in the inner forwarded block. Unwrap so
            # provider detection sees the real address.
            eff_from, eff_subject, eff_body = _unwrap_forwarded(parsed)
            parsed["from"] = eff_from
            parsed["subject"] = eff_subject
            parsed["body"] = eff_body
            # Capital One cash-back redemptions aren't ticket purchases — route
            # them straight to the /cashback page instead of pending_intake.
            # Checked before provider detection (capitalone isn't a provider, so
            # it would otherwise be dropped as "unknown"). Idempotent via the
            # source_ref dedup, so re-polls of the same mail are no-ops.
            if cashback_email.is_capitalone_cashback(parsed["from"], parsed["subject"], parsed["body"]):
                if _record_cashback(parsed):
                    cashback_saved += 1
                continue
            # DICE purchase/transfer emails are a tracker ledger, not
            # inventory intake — route them straight to dice_purchases /
            # dice_transfers and never stage a pending_intake row. Same
            # placement rationale as cashback: dice.fm isn't a PROVIDER_HINT
            # so it would otherwise be dropped as "unknown".
            if dice_email.is_dice_purchase(parsed["from"], parsed["subject"], parsed["body"]) \
                    or dice_email.is_dice_transfer(parsed["from"], parsed["subject"], parsed["body"]):
                kind = _record_dice(parsed, raw)
                if kind == "purchase":
                    dice_purchases_saved += 1
                elif kind == "transfer":
                    dice_transfers_saved += 1
                continue
            provider = _detect_provider(parsed["from"])
            if provider == "unknown" and not allow_unknown:
                skipped_provider += 1
                continue
            if _is_blocked_sender(parsed["from"]):
                skipped_provider += 1
                continue
            _msg = message_from_bytes(raw)
            html = _email_html(_msg)
            links = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE) if html else []
            fields = extract_fields(provider, parsed["subject"], parsed["from"], parsed["body"], parsed["attachments"], links=links, html=html)
            fields["buyer_email"] = _buyer_email(_msg, parsed["body"])
            intake_id = "intake-" + uuid.uuid4().hex[:12]
            row = {
                "id": intake_id,
                "message_id": parsed["message_id"] or None,
                "provider": provider,
                "email_from": parsed["from"][:500],
                "email_subject": parsed["subject"][:500],
                "email_received_at": parsed["received_at"],
                "event_name": fields["event_name"][:500] if fields.get("event_name") else "",
                "event_date_iso": fields.get("event_date_iso") or "",
                "venue": fields.get("venue") or "",
                "section": fields.get("section") or "",
                "row_label": fields.get("row_label") or "",
                "seats": fields.get("seats") or "",
                "qty": fields.get("qty"),
                "cost": fields.get("cost"),
                "cost_per_unit": fields.get("cost_per_unit"),
                "raw_text": (parsed["body"] or "")[:8000],
                "parse_warnings": ",".join(fields.get("warnings") or []),
                "ticket_url": (fields or {}).get("ticket_url"),
                "buyer_email": (fields or {}).get("buyer_email"),
                "status": "new",
            }
            db.insert_pending_intake(row, datetime.now(timezone.utc).isoformat())
            # If this purchase is for a date we already track on /series, append
            # it there too. Failure here must never lose the intake row, so it
            # is caught separately from the viagogo push below.
            try:
                if series.record_from_intake(fields, fields.get("buyer_email"), intake_id):
                    series_saved += 1
            except Exception:
                pass
            if provider in ("kupat", "tickchak", "ticketmaster_il"):
                try:
                    _push_kupat_to_viagogo(intake_id, fields)
                except Exception:
                    pass
            for fname, ctype, payload in parsed["attachments"]:
                try:
                    _save_intake_attachment(intake_id, fname, ctype, payload)
                except Exception:
                    errors += 1
            saved += 1
        except Exception:
            errors += 1
    # Targeted cash-back sweep — catches Capital One redemptions the capped
    # general poll above can miss in a busy inbox.
    try:
        cashback_saved += sweep_cashback()
    except Exception:
        errors += 1
    # Targeted DICE sweep — auto-forwards keep From: dice.fm, so this catches
    # anything the capped general poll dropped.
    try:
        _dice = sweep_dice()
        dice_purchases_saved += _dice["purchases"]
        dice_transfers_saved += _dice["transfers"]
    except Exception:
        errors += 1
    return {
        "fetched": seen,
        "saved": saved,
        "cashback_saved": cashback_saved,
        "dice_purchases_saved": dice_purchases_saved,
        "dice_transfers_saved": dice_transfers_saved,
        "series_saved": series_saved,
        "skipped_dupe": skipped_dupe,
        "skipped_provider": skipped_provider,
        "errors": errors,
    }
