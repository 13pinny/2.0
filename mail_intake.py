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


def _rank_viagogo_candidates(candidates, event_date_iso):
    """Prefer the candidate whose date PARSES to exactly the Kupat email's
    date; fall back to the old day-of-month-substring heuristic, then to the
    first live candidate. The user still confirms on the card, and a chosen
    candidate whose date disagrees with the email gets a DATE MISMATCH
    warning stamped into the row (see _candidate_date_mismatch).

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
    want = None
    try:
        want = datetime.strptime((event_date_iso or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    if want:
        for c in pool:
            if _parse_candidate_date(c.get("date")) == want:
                return c
    day = (event_date_iso or "")[8:10]
    if day:
        for c in pool:
            if day in (c.get("date") or ""):
                return c
    return pool[0]


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


def _search_viagogo(search_term):
    """search_event, but with a guard: viagogo's picker can't actually match
    Hebrew — when fed a Hebrew query it returns its default/popular event
    list, which once "matched" פאר טסי to a World Cup game. If the resolved
    term is still Hebrew (no name mapping taught yet), report no candidates
    so the push lands as no_match and the user gets the teach flow instead
    of a bogus awaiting_approval."""
    if _HEBREW_RX.search(search_term or ""):
        return []
    return viagogo_listing.search_event(search_term, limit=8)


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
        candidates = _search_viagogo(search_term)
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
        }
        if search_error:
            row["status"] = "error"
            row["error"] = search_error
        elif not candidates:
            row["status"] = "no_match"
            row["candidates_json"] = "[]"
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


def _push_kupat_to_viagogo_update(push_id, fields, now_iso=None):
    """Re-run the viagogo event search for an existing push row (e.g. after
    the user teaches a Hebrew→English name mapping) and update it in place."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    event_name = fields.get("event_name") or ""
    venue = fields.get("venue") or ""
    cost_per_unit = fields.get("cost_per_unit")
    search_term = _resolve_search_term(event_name, venue)
    try:
        candidates = _search_viagogo(search_term)
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

    chosen = _rank_viagogo_candidates(candidates, fields.get("event_date_iso"))
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
                "status": "new",
            }
            db.insert_pending_intake(row, datetime.now(timezone.utc).isoformat())
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
    return {
        "fetched": seen,
        "saved": saved,
        "cashback_saved": cashback_saved,
        "skipped_dupe": skipped_dupe,
        "skipped_provider": skipped_provider,
        "errors": errors,
    }
