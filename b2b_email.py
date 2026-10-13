"""Parse resale-purchase ("B2B") order emails for the /b2b page.

Built against real samples from the intake inbox (2026-10). Per platform:

  automatiq  no-reply@automatiq.com
      "Purchase Placed - PU_xxxx - <Event>"     -> placed
      "Purchase Confirmed - PU_xxxx - <Event>"  -> confirmed
      "Purchase Fulfilled - PU_xxxx - <Event>"  -> delivered
      Placed and Confirmed land 1-2 s apart and NOT always in that order;
      both carry the full order, so either one can create the row. Body is
      one label per line with the value on the next line; the header block
      is three labels (PURCHASE ID / ORDER TOTAL / ORDER DATE) followed by
      the three values. Seats print "-" when unassigned.
  tickpick   orders-1@ / orders-2@ / broker.support@tickpick.com
      "You Purchased Tickets on TickPick (Order Number N)"     -> placed
      "Your TickPick Order Has Been Confirmed (Order Number N)" -> confirmed
      "Your Tickets Are Ready For Delivery (Order Number N)"   -> delivered
      "Your TickPick Order N Has Been Cancelled"                -> cancelled
      "TickPick Order #N Refund Receipt"                        -> cancelled
      Body "Label:  value" (Price / Quantity / Order Total put the value on
      the next line; Quantity reads "x 4"). No "Seats:" line when the
      listing has none — a "Notes:" line sits there instead.
  crowdvolt  alerts@tx.crowdvolt.com
      "Order Confirmed: <Event> ticket(s)"  -> placed
      "<Event> tickets delivered"           -> delivered (the green check)
      "We have cancelled your <Event> order" -> cancelled
      Trap: an "Order Confirmed" subject can carry the DELIVERED body
      ("Tickets Arrived!"), so the body decides. Both bodies carry
      "ORDER NO: P98150", "$122/ticket", "6 × General Admission",
      "Total after fees: $756"; no row/seat. A delivered body may name the
      wallet: "Tickets were sent to your DICE wallet: Alex G. (+1...)".

Plus DICE "<Name> sent you tickets" / "sent you a ticket" (noreply@dice.fm):
an incoming transfer. The To: address is the DICE account the tickets
landed in; it arrives seconds to a minute BEFORE CrowdVolt's delivered mail.
DICE prints the event date without a year and its own event name
("unKommon Events & Framework present: ANOTR" vs CrowdVolt's "ANOTR").

Pure text work — no DB, no IMAP. Parsers never raise; a missing field is
just absent.
"""
import html as html_mod
import re
from datetime import date

PLATFORMS = ("automatiq", "tickpick", "crowdvolt")

# IMAP FROM-search needles (one search each per sweep). Automatiq's domain
# also carries support/Lysted threads, so the no-reply address is searched.
SWEEP_SENDERS = (
    ("automatiq", "no-reply@automatiq.com"),
    ("tickpick", "tickpick.com"),
    ("crowdvolt", "tx.crowdvolt.com"),
)

_AQ_SUBJ = re.compile(r"^\s*Purchase\s+(Placed|Confirmed|Fulfilled)\s*-\s*(PU_[A-Za-z0-9]+)\s*-\s*(.+?)\s*$", re.I)
_TP_SUBJ = (
    ("placed", re.compile(r"You\s+Purchased\s+Tickets\s+on\s+TickPick\s*\(Order\s+Number\s+(\d+)\)", re.I)),
    ("confirmed", re.compile(r"Order\s+Has\s+Been\s+Confirmed\s*\(Order\s+Number\s+(\d+)\)", re.I)),
    ("delivered", re.compile(r"Tickets\s+Are\s+Ready\s+For\s+Delivery\s*\(Order\s+Number\s+(\d+)\)", re.I)),
    ("cancelled", re.compile(r"TickPick\s+Order\s+#?(\d+)\s+Has\s+Been\s+Cancel+ed", re.I)),
    # A refund after confirmation (seller couldn't deliver) ends the order too.
    ("cancelled", re.compile(r"TickPick\s+Order\s+#?(\d+)\s+Refund\s+Receipt", re.I)),
)
_CV_PLACED = re.compile(r"^\s*Order\s+Confirmed\s*:\s*(.+?)\s+tickets?\s*$", re.I)
_CV_DELIVERED = re.compile(r"^\s*(.+?)\s+tickets?\s+delivered\s*$", re.I)
_CV_CANCELLED = re.compile(r"^\s*We\s+have\s+cancel+ed\s+your\s+(.+?)\s+order\s*$", re.I)
_DICE_INCOMING_RX = re.compile(r"^\s*(.+?)\s+sent\s+you\s+(a\s+ticket|tickets)\s*$", re.I)

AQ_KIND = {"placed": "placed", "confirmed": "confirmed", "fulfilled": "delivered"}


def classify(sender, subject):
    """(platform, kind) for a b2b email, else (None, None). kind is
    'placed' | 'confirmed' | 'delivered' | 'cancelled' | 'dice_incoming'.
    CrowdVolt's placed-vs-delivered is refined from the body by parse_order."""
    s = (sender or "").lower()
    subj = subject or ""
    if "dice.fm" in s:
        return ("dice", "dice_incoming") if _DICE_INCOMING_RX.match(subj) else (None, None)
    if "automatiq" in s:
        m = _AQ_SUBJ.match(subj)
        return ("automatiq", AQ_KIND[m.group(1).lower()]) if m else (None, None)
    if "tickpick" in s:
        for kind, rx in _TP_SUBJ:
            if rx.search(subj):
                return "tickpick", kind
        return None, None
    if "crowdvolt" in s:
        if _CV_PLACED.match(subj):
            return "crowdvolt", "placed"
        if _CV_DELIVERED.match(subj):
            return "crowdvolt", "delivered"
        if _CV_CANCELLED.match(subj):
            return "crowdvolt", "cancelled"
    return None, None


# ---- text helpers ---------------------------------------------------------

_BLOCK_TAGS = re.compile(r"<\s*(?:br|/p|/div|/tr|/h\d|/li|/table|p|div|tr|li|h\d)\b[^>]*>", re.I)
_CELL_TAGS = re.compile(r"<\s*/t[dh]\s*>", re.I)


def html_to_text(html):
    """HTML -> text keeping the layout: block tags become newlines and table
    cells become a double space, so 'Row</td><td>12' reads 'Row  12'."""
    if not html:
        return ""
    h = re.sub(r"(?s)<!--.*?-->", " ", html)
    h = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", h)
    h = _CELL_TAGS.sub("  ", h)
    h = _BLOCK_TAGS.sub("\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html_mod.unescape(h).replace("\xa0", " ").replace("‌", "")
    lines = [re.sub(r"[ \t]{3,}", "  ", ln).strip() for ln in h.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


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


def _kv(lines, label):
    """'Label: value' on one line, or 'Label' / 'Label:' alone with the value
    on the next line. '' when absent."""
    rx_same = re.compile(rf"^{label}\s*:\s*(\S.*)$", re.I)
    rx_alone = re.compile(rf"^{label}\s*:?\s*$", re.I)
    for i, ln in enumerate(lines):
        m = rx_same.match(ln)
        if m:
            return m.group(1).strip()
        if rx_alone.match(ln) and i + 1 < len(lines):
            return lines[i + 1].strip()
    return ""


def _set_money(out, key, s):
    v, cur = _money(s)
    if v is not None:
        out[key] = v
        if cur:
            out.setdefault("currency", cur)


def _finish(out):
    if out.get("price_each") is None and out.get("total") is not None and out.get("qty"):
        out["price_each"] = round(out["total"] / out["qty"], 2)
    out.setdefault("currency", "USD")
    return {k: v for k, v in out.items() if v not in (None, "")}


# Automatiq labels in body order — a label line is never taken as the
# previous label's value (an empty Notes is followed directly by "Fees").
_AQ_LABELS = ("Event Info", "Event", "Occurs at", "Venue", "Location", "Ticket Details",
              "Quantity", "Section", "Row", "Seats", "Unit Price", "Stock Type",
              "Payment Method", "In Hand Date", "External Id", "Notes", "Fees",
              "Shipping Fee", "Taxes", "Payment Method Fee", "Buyer Fee", "View Purchase")


def _parse_automatiq(subject, text, email_date):
    lines = _lines(text)
    out = {}
    m = _AQ_SUBJ.match(subject or "")
    if m:
        out["order_ref"] = m.group(2)
        out["event_name"] = m.group(3)
    labels = {l.lower() for l in _AQ_LABELS}
    vals = {}
    for i, ln in enumerate(lines[:-1]):
        if ln in _AQ_LABELS and ln not in vals:
            nxt = lines[i + 1]
            vals[ln] = "" if nxt.lower() in labels else nxt
    try:
        i = lines.index("PURCHASE ID")
        if lines[i + 1] == "ORDER TOTAL" and lines[i + 2] == "ORDER DATE":
            out["order_ref"] = out.get("order_ref") or lines[i + 3]
            _set_money(out, "total", lines[i + 4])
    except (ValueError, IndexError):
        pass
    if vals.get("Event"):
        out["event_name"] = vals["Event"]
    occurs = vals.get("Occurs at", "")
    out["event_date_text"] = occurs[:120]
    out["event_date_iso"] = find_date(occurs, email_date)
    venue = vals.get("Venue", "")
    if vals.get("Location"):
        venue = f"{venue}, {vals['Location']}" if venue else vals["Location"]
    out["venue"] = venue[:300]
    q = re.match(r"(\d+)", vals.get("Quantity", ""))
    if q:
        out["qty"] = int(q.group(1))
    out["section"] = vals.get("Section", "")[:80]
    out["row_label"] = vals.get("Row", "")[:40]
    seats = vals.get("Seats", "")
    out["seats"] = "" if seats.strip() in ("-", "--") else seats[:300]
    _set_money(out, "price_each", vals.get("Unit Price", ""))
    out["delivery"] = vals.get("Stock Type", "")[:60]
    out["source_notes"] = vals.get("Notes", "")[:300]
    return _finish(out)


def _parse_tickpick(subject, text, email_date):
    lines = _lines(text)
    out = {}
    for _kind, rx in _TP_SUBJ:
        m = rx.search(subject or "")
        if m:
            out["order_ref"] = m.group(1)
            break
    ref = re.search(r"^Order:\s*#?(\d+)", text or "", re.M)
    if ref and not out.get("order_ref"):
        out["order_ref"] = ref.group(1)
    out["event_name"] = _kv(lines, "Event Name")[:300]
    dt = _kv(lines, "Event Date")
    out["event_date_text"] = dt[:120]
    out["event_date_iso"] = find_date(dt, email_date)
    out["venue"] = _kv(lines, "Venue")[:300]
    out["section"] = _kv(lines, "Section")[:80]
    out["row_label"] = _kv(lines, "Row")[:40]
    seats = _kv(lines, "Seats")
    if seats and not re.match(r"^(Notes|Price)\b", seats, re.I):
        out["seats"] = seats[:300].rstrip(" ,")
    out["source_notes"] = _kv(lines, "Notes")[:300]
    q = re.search(r"(\d+)", _kv(lines, "Quantity"))
    if q:
        out["qty"] = int(q.group(1))
    _set_money(out, "price_each", _kv(lines, "Price"))
    _set_money(out, "total", _kv(lines, "Order Total"))
    if not out.get("event_name"):
        # Ready-for-delivery body: "<Event>" then "<date> @ <venue>".
        for i, ln in enumerate(lines[1:], 1):
            mm = re.match(r"^(\w{3} \w{3} \d{1,2}, \d{4} [\d:]+[AP]M)\s*@\s*(.+)$", ln)
            if mm:
                out["event_name"] = lines[i - 1][:300]
                out["event_date_iso"] = find_date(mm.group(1), email_date)
                out["venue"] = mm.group(2)[:300]
                break
    return _finish(out)


def _parse_crowdvolt(subject, text, email_date):
    lines = _lines(text)
    out = {}
    for rx in (_CV_PLACED, _CV_DELIVERED, _CV_CANCELLED):
        m = rx.match(subject or "")
        if m:
            out["event_name"] = m.group(1)
            break
    if re.search(r"tickets\s+arrived|are\s+now\s+in\s+your\s+dice", text or "", re.I):
        out["_delivered"] = True
    ref = re.search(r"ORDER\s*NO\s*:?\s*([A-Z0-9-]{3,})", text or "", re.I)
    if ref:
        out["order_ref"] = ref.group(1)
    try:
        i = lines.index("Event Details")
        out["event_name"] = lines[i + 1][:300]
        out["event_date_text"] = lines[i + 2][:120]
        out["event_date_iso"] = find_date(lines[i + 2], email_date)
        if not lines[i + 3].upper().startswith("ORDER NO"):
            out["venue"] = lines[i + 3][:300]
    except (ValueError, IndexError):
        pass
    each = re.search(r"([\$€£]\s*[\d,.]+)\s*/\s*ticket", text or "", re.I)
    if each:
        _set_money(out, "price_each", each.group(1))
    tt = re.search(r"^(\d{1,3})\s*[×x]\s*(.+)$", text or "", re.M)
    if tt:
        out["qty"] = int(tt.group(1))
        out["section"] = tt.group(2).strip()[:80]
    tot = re.search(r"Total\s+after\s+fees\s*:?\s*([\$€£]\s*[\d,.]+)", text or "", re.I)
    if tot:
        _set_money(out, "total", tot.group(1))
    w = re.search(r"sent\s+to\s+your\s+DICE\s+wallet\s*:\s*(.+?)\s*(?:\(|$)", text or "", re.I | re.M)
    if w:
        out["dice_wallet"] = w.group(1).strip()[:80]
    return _finish(out)


def parse_order(platform, kind, subject, text, email_date=None):
    """Fields for a b2b order email (all optional): order_ref, event_name,
    event_date_iso, event_date_text, venue, section, row_label, seats, qty,
    price_each, total, currency, delivery, source_notes, dice_wallet. For
    CrowdVolt, `_delivered` is True when the BODY is the delivered template."""
    try:
        if platform == "automatiq":
            return _parse_automatiq(subject, text, email_date)
        if platform == "tickpick":
            return _parse_tickpick(subject, text, email_date)
        if platform == "crowdvolt":
            return _parse_crowdvolt(subject, text, email_date)
    except Exception:
        pass
    return {}


def parse_dice_incoming(subject, text, email_date=None):
    """'<Name> sent you tickets' -> sender_name, event_name, event_slug,
    event_date_iso, venue, qty, holder_name."""
    out = {}
    m = _DICE_INCOMING_RX.match(subject or "")
    if m:
        out["sender_name"] = m.group(1).strip()
        if m.group(2).lower().startswith("a "):
            out["qty"] = 1
    flat = " ".join((text or "").split())
    ev = re.search(r"sorted\s+for\s+(.+?)\.(?:\s|$)", flat, re.I)
    if ev:
        out["event_name"] = ev.group(1).strip()[:300]
    slug = re.search(r"https?://dice\.fm/event/([A-Za-z0-9]+)-([a-z0-9-]*)", text or "", re.I)
    if slug:
        out["event_slug"] = slug.group(1)
        if not out.get("event_name"):
            out["event_name"] = re.sub(r"-tickets$", "", slug.group(2)).replace("-", " ")
    v = re.search(r"Venue\s+([^\n<]+?)\s*(?:<|\n|$)", text or "")
    if v:
        out["venue"] = v.group(1).strip()[:300]
    q = re.search(r"Quantity\s+(\d{1,3})\b", text or "")
    if q:
        out["qty"] = int(q.group(1))
    n = re.search(r"\bName\s+([^\n]+?)\s*(?:\n|$|\s{2,})", text or "")
    if n:
        out["holder_name"] = n.group(1).strip()[:80]
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
