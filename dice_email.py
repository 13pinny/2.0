"""Parse DICE (dice.fm) purchase-confirmation and transfer emails.

Built against real samples from the intake inbox (see
scripts/probe_dice_mail.py). Two email kinds matter:

  Purchase — subject "Your tickets: <EVENT>", body has:
      "Purchase confirmation" header
      event link  <https://dice.fm/event/<slug>-...-tickets>
      "Venue      <venue name>"
      "Date & time Sat 15 Aug, 7:00 PM GMT-4"        (NO year!)
      "Tickets     6 × General Admission"            (may repeat per type)
      "Total  $568.56"

  Transfer OUT — subject "Ticket(s) sent to <Name>", body has:
      "Transfer sent" header
      "You've sent 4 tickets to Daniel for ODD MOB (OFFICIAL AFTERPARTY)."
      same event link + "Date & time Fri 03 Jul,10:00 PM" + "Quantity 4"

Other DICE mail (login codes, "<Name> sent you tickets" incoming transfers)
is deliberately ignored.

Event dates carry no year — we infer the first year that puts the event on
or after the email date (events are always in the future when the mail is
sent). The dice.fm/event/<slug> link is kept as `event_slug`: it is the
exact-match key between a transfer and its purchase.
"""
import re
from datetime import date, timedelta

_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}

_EVENT_LINK_RX = re.compile(r"https?://dice\.fm/event/([A-Za-z0-9]+)-[^\s>\"']*", re.I)
_VENUE_RX = re.compile(r"Venue\s{2,}([^\n<]+)")
# "Date & time Sat 15 Aug, 7:00 PM" / "Fri 03 Jul,10:00 PM" — day name,
# day-of-month, month; year absent.
_DATE_RX = re.compile(
    r"Date\s*&\s*time\s+\w{3}\s+(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",
    re.I,
)
# "6 × General Admission" — DICE uses the multiplication sign; tolerate "x".
_TICKET_LINE_RX = re.compile(r"(\d{1,2})\s*[×x]\s*([^\n]+?)(?:\s{2,}|\s*$)", re.M)
_TOTAL_RX = re.compile(r"Total\s+([\$€£])\s*([\d,]+\.\d{2})")
_SENT_RX = re.compile(
    r"You.{0,3}ve\s+sent\s+(\d{1,2})\s+tickets?\s+to\s+(.+?)\s+for\s+(.+?)\.?\s*$",
    re.I | re.M,
)
_QUANTITY_RX = re.compile(r"Quantity\s+(\d{1,2})")
_PURCHASE_SUBJECT_RX = re.compile(r"^\s*Your tickets:\s*(.+?)\s*$", re.I)
_TRANSFER_SUBJECT_RX = re.compile(r"^\s*Tickets?\s+sent\s+to\s+(.+?)\s*$", re.I)

_CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP"}


def _is_dice_sender(sender):
    return "dice.fm" in (sender or "").lower()


def is_dice_purchase(sender, subject, body):
    return bool(_is_dice_sender(sender) and _PURCHASE_SUBJECT_RX.match(subject or ""))


def is_dice_transfer(sender, subject, body):
    return bool(_is_dice_sender(sender) and _TRANSFER_SUBJECT_RX.match(subject or ""))


def _infer_event_date(body, email_date):
    """DICE prints the event date without a year. Pick the first year that
    puts the event on/after the email's own date (they never mail about a
    past event). email_date is a datetime.date or None."""
    m = _DATE_RX.search(body or "")
    if not m:
        return ""
    day = int(m.group(1))
    mo = _MONTHS[m.group(2)[:3].lower()]
    base = email_date or date.today()
    for year in (base.year, base.year + 1):
        try:
            d = date(year, mo, day)
        except ValueError:
            continue
        # Small grace window: a "your tickets" resend the day after doesn't
        # flip the event into next year.
        if d >= base - timedelta(days=2):
            return d.isoformat()
    return ""


def _event_slug(body):
    m = _EVENT_LINK_RX.search(body or "")
    return m.group(1).lower() if m else ""


def _venue(body):
    m = _VENUE_RX.search(body or "")
    return m.group(1).strip() if m else ""


def parse_purchase(subject, body, email_date=None):
    """Returns dict with event_name, event_slug, event_date_iso, venue,
    ticket_type, qty, price_total, price_per_unit, currency, warnings."""
    out = {"warnings": []}
    m = _PURCHASE_SUBJECT_RX.match(subject or "")
    out["event_name"] = m.group(1).strip() if m else (subject or "").strip()
    out["event_slug"] = _event_slug(body)
    out["event_date_iso"] = _infer_event_date(body, email_date)
    out["venue"] = _venue(body)

    # Ticket lines live after the "Tickets" label; there can be several
    # "N × Type" entries. Restrict the scan to the ticket-details region so a
    # stray "2 x something" in marketing copy can't match.
    region = body or ""
    idx = region.find("Ticket details")
    if idx >= 0:
        region = region[idx:idx + 600]
    pairs = [(int(q), t.strip()) for q, t in _TICKET_LINE_RX.findall(region) if 0 < int(q) < 50]
    if pairs:
        out["qty"] = sum(q for q, _ in pairs)
        out["ticket_type"] = ", ".join(
            (f"{q} × {t}" if len(pairs) > 1 else t) for q, t in pairs)
    else:
        out["qty"] = None
        out["ticket_type"] = ""
        out["warnings"].append("no-ticket-line")

    m = _TOTAL_RX.search(body or "")
    if m:
        out["currency"] = _CURRENCIES.get(m.group(1), m.group(1))
        out["price_total"] = float(m.group(2).replace(",", ""))
        out["price_per_unit"] = (
            round(out["price_total"] / out["qty"], 2) if out.get("qty") else None)
    else:
        out["currency"] = ""
        out["price_total"] = None
        out["price_per_unit"] = None
        out["warnings"].append("no-total")

    if not out["event_slug"]:
        out["warnings"].append("no-event-link")
    if not out["event_date_iso"]:
        out["warnings"].append("no-date")
    return out


def parse_transfer(subject, body, email_date=None):
    """Returns dict with event_name, event_slug, event_date_iso, qty,
    recipient, warnings."""
    out = {"warnings": []}
    m = _SENT_RX.search(body or "")
    if m:
        out["qty"] = int(m.group(1))
        out["recipient"] = m.group(2).strip()
        out["event_name"] = m.group(3).strip()
    else:
        qm = _QUANTITY_RX.search(body or "")
        out["qty"] = int(qm.group(1)) if qm else None
        sm = _TRANSFER_SUBJECT_RX.match(subject or "")
        out["recipient"] = sm.group(1).strip() if sm else ""
        out["event_name"] = ""
        out["warnings"].append("no-sent-line")
    out["event_slug"] = _event_slug(body)
    out["event_date_iso"] = _infer_event_date(body, email_date)
    if not out.get("qty"):
        out["warnings"].append("no-qty")
    return out


# ---- transfer → purchase matching ---------------------------------------

def _norm_name(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def names_match(a, b):
    """Fuzzy event-name equality: normalized equality/containment or a
    difflib ratio ≥ 0.75."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, na, nb).ratio() >= 0.75


def purchase_matches(transfer, purchase):
    """Does this purchase row belong to the same event as the transfer?
    Exact event_slug wins; otherwise fuzzy name (+ date agreement when both
    sides have one)."""
    if transfer.get("event_slug") and purchase.get("event_slug"):
        return transfer["event_slug"] == purchase["event_slug"]
    if not names_match(transfer.get("event_name"), purchase.get("event_name")):
        return False
    td, pd = transfer.get("event_date_iso"), purchase.get("event_date_iso")
    if td and pd:
        try:
            delta = abs((date.fromisoformat(td) - date.fromisoformat(pd)).days)
        except ValueError:
            return True
        return delta <= 1
    return True
