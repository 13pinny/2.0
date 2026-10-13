"""Parse resale-purchase ("B2B") order emails for the /b2b page.

Three platforms, each a pair of emails per order:

  automatiq  no-reply@automatiq.com
      "Purchase Placed ..."     -> placed   (event, row, seats, price, total)
      "Purchase Confirmed ..."  -> confirmed
  tickpick   orders-1@tickpick.com
      "You Purchased Tickets on TickPick <order #>"  -> placed
      "Your TickPick Order Has Been Confirmed ..."   -> confirmed
  crowdvolt  alerts@tx.crowdvolt.com
      "Order Confirmed: <EVENT> ticket(s)"  -> placed
      "<EVENT> tickets delivered"           -> confirmed (delivered)

Plus DICE "<Name> sent you tickets" (noreply@dice.fm): an incoming transfer.
The account it was mailed to is the DICE account the b2b tickets landed in.

Everything here is pure text work — no DB, no IMAP. `classify` decides the
kind from sender + subject; `parse_order` pulls whatever fields it can find
and never raises. A field it can't find is simply absent, so a template
change degrades to a row with blanks rather than a lost order.
"""
import html as html_mod
import re
from datetime import date

PLATFORMS = ("automatiq", "tickpick", "crowdvolt")

# IMAP FROM-search needles (one search each per sweep).
SWEEP_SENDERS = (
    ("automatiq", "automatiq.com"),
    ("tickpick", "tickpick.com"),
    ("crowdvolt", "tx.crowdvolt.com"),
)

_SUBJECT_RULES = (
    # (platform, kind, sender needle, subject regex)
    ("automatiq", "placed", "automatiq", re.compile(r"purchase\s+placed", re.I)),
    ("automatiq", "confirmed", "automatiq", re.compile(r"purchase\s+confirmed", re.I)),
    ("tickpick", "placed", "tickpick", re.compile(r"you\s+purchased\s+tickets\s+on\s+tickpick", re.I)),
    ("tickpick", "confirmed", "tickpick", re.compile(r"order\s+has\s+been\s+confirmed", re.I)),
    ("crowdvolt", "placed", "crowdvolt", re.compile(r"^\s*order\s+confirmed\s*:", re.I)),
    ("crowdvolt", "confirmed", "crowdvolt", re.compile(r"tickets?\s+delivered", re.I)),
)

_DICE_INCOMING_RX = re.compile(r"^\s*(?:\((.+?)\)\s*)?(.+?)\s+sent\s+you\s+(?:\d+\s+)?tickets?\b", re.I)


def classify(sender, subject):
    """(platform, kind) for a b2b email, else (None, None). kind is
    'placed' | 'confirmed' | 'dice_incoming'."""
    s = (sender or "").lower()
    subj = subject or ""
    if "dice.fm" in s and _DICE_INCOMING_RX.search(subj):
        return "dice", "dice_incoming"
    for platform, kind, needle, rx in _SUBJECT_RULES:
        if needle in s and rx.search(subj):
            return platform, kind
    return None, None


# ---- text helpers ---------------------------------------------------------

_BLOCK_TAGS = re.compile(r"<\s*(?:br|/p|/div|/tr|/h\d|/li|/table|p|div|tr|li|h\d)\b[^>]*>", re.I)
_CELL_TAGS = re.compile(r"<\s*/t[dh]\s*>", re.I)


def html_to_text(html):
    """HTML -> text keeping the layout: block tags become newlines and table
    cells become a double space, so 'Row</td><td>12' reads 'Row  12'."""
    if not html:
        return ""
    h = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
    h = _CELL_TAGS.sub("  ", h)
    h = _BLOCK_TAGS.sub("\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html_mod.unescape(h).replace("\xa0", " ").replace("‌", "")
    lines = [re.sub(r"[ \t]{3,}", "  ", ln).strip() for ln in h.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _labelled(lines, labels, value_rx=r".+"):
    """Value for the first line starting with one of `labels` — on the same
    line ('Row: 12' / 'Row  12') or, when the label stands alone, the next
    line. Returns '' when not found."""
    lab = "|".join(labels)
    same = re.compile(rf"^(?:{lab})\s*(?::|#|\s{{2,}}|\s(?=\S))\s*({value_rx})$", re.I)
    alone = re.compile(rf"^(?:{lab})\s*:?\s*$", re.I)
    for i, ln in enumerate(lines):
        m = same.match(ln)
        if m:
            return m.group(1).strip()
        if alone.match(ln) and i + 1 < len(lines):
            nxt = lines[i + 1]
            if re.fullmatch(value_rx, nxt, re.I):
                return nxt.strip()
    return ""


_MONEY_RX = re.compile(r"([\$€£])\s*([\d,]+(?:\.\d{1,2})?)")
_CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP"}


def _money(s):
    m = _MONEY_RX.search(s or "")
    if not m:
        return None, ""
    try:
        return float(m.group(2).replace(",", "")), _CURRENCIES.get(m.group(1), "")
    except ValueError:
        return None, ""


_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DATE_RXES = (
    # Thu, Oct 30, 2026 / October 30 2026 / Oct 30th, 2026
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I),
    # 30 Oct 2026
    re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(\d{4})\b", re.I),
    # 10/30/2026 (US order)
    re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
    # Oct 30 / Thu Oct 30 (no year)
    re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I),
)


def find_date(text, email_date=None):
    """First event-looking date in text as YYYY-MM-DD, or ''. A date with no
    year takes the first year that puts it on/after the email's date."""
    for i, rx in enumerate(_DATE_RXES):
        m = rx.search(text or "")
        if not m:
            continue
        try:
            if i == 0:
                y, mo, d = int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))
            elif i == 1:
                y, mo, d = int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))
            elif i == 2:
                y, mo, d = int(m.group(3)), int(m.group(1)), int(m.group(2))
            else:
                mo, d = _MONTHS[m.group(1)[:3].lower()], int(m.group(2))
                base = email_date or date.today()
                y = base.year
                if date(y, mo, d) < base:
                    y += 1
            return date(y, mo, d).isoformat()
        except (ValueError, KeyError):
            continue
    return ""


_ORDER_RX = re.compile(
    r"\border\s*(?:#|no\.?|number|id)?\s*[:#]?\s*#?\s*([A-Z0-9][A-Z0-9-]{3,})\b", re.I)


def find_order_ref(subject, text):
    """Platform order number from the subject first, then the body."""
    for src in (subject or "", text or ""):
        for m in _ORDER_RX.finditer(src):
            ref = m.group(1)
            # "Order Confirmed" / "Order Has Been" etc. are words, not refs.
            if any(c.isdigit() for c in ref):
                return ref
    return ""


# ---- per-order field extraction ------------------------------------------

def _event_from_subject(platform, kind, subject):
    s = subject or ""
    if platform == "crowdvolt":
        m = re.match(r"^\s*order\s+confirmed\s*:\s*(.+?)\s+tickets?\s*$", s, re.I) \
            or re.match(r"^\s*order\s+confirmed\s*:\s*(.+?)\s*$", s, re.I) \
            or re.match(r"^\s*(.+?)\s+tickets?\s+delivered\b", s, re.I)
        if m:
            return m.group(1).strip(" -:")
    if platform == "automatiq":
        m = re.search(r"purchase\s+(?:placed|confirmed)\s*[:\-–]\s*(.+?)\s*$", s, re.I)
        if m:
            return m.group(1).strip()
    return ""


def parse_order(platform, kind, subject, text, email_date=None):
    """Best-effort fields for a b2b order email. Keys (all optional):
    order_ref, event_name, event_date_iso, event_date_text, venue, section,
    row_label, seats, qty, price_each, total, currency."""
    lines = _lines(text)
    out = {}
    ref = find_order_ref(subject, text)
    if ref:
        out["order_ref"] = ref

    ev = _labelled(lines, ("Event", "Event Name", "Performer")) or _event_from_subject(platform, kind, subject)
    if ev:
        out["event_name"] = ev[:300]
    venue = _labelled(lines, ("Venue", "Location"))
    if venue:
        out["venue"] = venue[:300]
    date_txt = _labelled(lines, ("Event Date", "Date & Time", "Date and Time", "Date", "When"))
    iso = find_date(date_txt, email_date) or find_date(text, email_date)
    if iso:
        out["event_date_iso"] = iso
    if date_txt:
        out["event_date_text"] = date_txt[:120]

    sec = _labelled(lines, ("Section", "Sec"))
    row = _labelled(lines, ("Row",), r"[A-Za-z0-9 -]{1,12}")
    seats = _labelled(lines, ("Seats", "Seat\\(s\\)", "Seat"), r"[A-Za-z0-9 ,&-]{1,40}")
    # One-line "Section 101, Row 12, Seats 5-6" layouts.
    one = re.search(r"Sec(?:tion)?\.?\s*([A-Za-z0-9 ]{1,20}?)\s*[,|•]\s*Row\s*([A-Za-z0-9]{1,6})"
                    r"(?:\s*[,|•]\s*Seats?\s*([A-Za-z0-9 ,&-]{1,30}))?", text or "", re.I)
    if one:
        sec = sec or one.group(1).strip()
        row = row or one.group(2).strip()
        seats = seats or (one.group(3) or "").strip()
    # "Section: 104, Row 12, Seats 5-6" caught by the Section label alone.
    sm = re.match(r"(.+?)\s*[,|•]\s*Row\b", sec or "", re.I)
    if sm:
        sec = sm.group(1).strip()
    if sec:
        out["section"] = sec[:80]
    if row:
        out["row_label"] = row[:20]
    if seats:
        out["seats"] = seats[:60]

    qty = _labelled(lines, ("Quantity", "Qty", "Tickets", "Number of Tickets"), r"\d{1,3}\b.*")
    m = re.match(r"(\d{1,3})", qty or "")
    if not m:
        m = re.search(r"\b(\d{1,2})\s*[x×]\s", text or "") \
            or re.search(r"\b(\d{1,2})\s+tickets?\b", (subject or "") + "\n" + (text or ""), re.I)
    if m:
        out["qty"] = int(m.group(1))
    elif seats:
        rng = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", seats)
        if rng and 0 < int(rng.group(2)) - int(rng.group(1)) < 20:
            out["qty"] = int(rng.group(2)) - int(rng.group(1)) + 1
        elif re.fullmatch(r"[\w ]+(?:\s*[,&]\s*[\w ]+)+", seats):
            out["qty"] = len(re.split(r"\s*[,&]\s*", seats.strip()))

    total, cur = _money(_labelled(lines, ("Order Total", "Grand Total", "Total Charged", "Total Paid", "Total")))
    each, cur2 = _money(_labelled(lines, ("Price per Ticket", "Price Per Ticket", "Ticket Price", "Price Each", "Price", "Each")))
    if total is not None:
        out["total"] = total
    if each is not None:
        out["price_each"] = each
    if out.get("total") is not None and out.get("price_each") is None and out.get("qty"):
        out["price_each"] = round(out["total"] / out["qty"], 2)
    cur = cur or cur2 or _money(text)[1]
    if cur:
        out["currency"] = cur
    return out


def parse_dice_incoming(subject, text, email_date=None):
    """'<Name> sent you tickets' -> sender_name, event_name, event_slug,
    event_date_iso, qty."""
    out = {}
    m = _DICE_INCOMING_RX.search(subject or "")
    if m:
        out["sender_name"] = (m.group(2) or "").strip()
    slug = re.search(r"https?://dice\.fm/event/([A-Za-z0-9]+)-[^\s>\"']*", text or "", re.I)
    if slug:
        out["event_slug"] = slug.group(1)
    # "Francis sent you 2 tickets for ANOTR." / "... tickets to ANOTR"
    m = re.search(r"sent\s+you\s+(\d{1,2})\s+tickets?\s+(?:for|to)\s+(.+?)\.?\s*$", text or "", re.I | re.M)
    if m:
        out["qty"] = int(m.group(1))
        out["event_name"] = m.group(2).strip()[:300]
    else:
        q = re.search(r"Quantity\s+(\d{1,2})", text or "")
        if q:
            out["qty"] = int(q.group(1))
    dm = re.search(r"Date\s*&\s*time\s+\w{3}\s+(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
                   text or "", re.I)
    if dm:
        base = email_date or date.today()
        mo, d = _MONTHS[dm.group(2)[:3].lower()], int(dm.group(1))
        for y in (base.year, base.year + 1):
            try:
                if date(y, mo, d) >= base:
                    out["event_date_iso"] = date(y, mo, d).isoformat()
                    break
            except ValueError:
                break
    t = re.search(r"^\s*(\d{1,2})\s*[×x]\s*([^\n]+?)\s*$", text or "", re.M)
    if t:
        out.setdefault("qty", int(t.group(1)))
        out["ticket_type"] = t.group(2).strip()[:120]
    v = re.search(r"^Venue\s{2,}([^\n]+)", text or "", re.M)
    if v:
        out["venue"] = v.group(1).strip()[:300]
    if not out.get("event_name"):
        ev = re.search(r"https?://dice\.fm/event/[A-Za-z0-9]+-([a-z0-9-]+?)(?:-tickets)?[\s>\"'?]", text or "", re.I)
        if ev:
            out["event_name"] = ev.group(1).replace("-", " ")
    if not out.get("event_date_iso"):
        iso = find_date(text, email_date)
        if iso:
            out["event_date_iso"] = iso
    return out


# ---- matching helpers ------------------------------------------------------

_STOP = {"the", "a", "an", "and", "of", "at", "with", "presents", "ticket", "tickets",
         "live", "tour", "night", "in", "nyc", "ny", "new", "york", "feat", "ft"}


def name_tokens(s):
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _STOP and len(t) > 1}


def names_match(a, b):
    """Loose event-name match: one name's tokens are mostly inside the other's
    (platforms dress the same show up differently)."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & big) / len(small) >= 0.6
