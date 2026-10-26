"""Box-office purchase receipts from the intake inbox -> one row per order.

Feeds the `email_purchases` table behind /purchases. Unlike /pending (a
staging queue you confirm by hand, kept for the viagogo push), a receipt
recognised here is a purchase as soon as it lands. Every parser was written
against real receipts forwarded to the intake inbox in Oct 2026; a sender
whose mail doesn't match its parser's shape returns None, never a guess.

parse(sender, subject, text, email_date) -> dict | None
  kind        'purchase' | 'cancel' | 'refund'
  provider    leap | frontgate | fourvenues | posh | eventbrite | seetickets_us |
              shotgun | ticketmaster_us | kupat | ticketmaster_il | universe | dice
  order_ref   the platform's order number ('' when the receipt has none)
  group_ref   rows sharing one are a single order on the page (FourVenues
              mails one email per ticket and no order number)
  event_name, event_date_iso, venue, ticket_type, section, row_label, seats,
  qty, total, price_each, currency, card_last4

`text` is b2b_email.html_to_text of the HTML part (layout-preserving), the
same text the /b2b parsers read.
"""
import re
from datetime import date

from b2b_email import find_date

# Senders the targeted IMAP sweep searches for, as (IMAP field, needle).
# Ticketmaster US receipts arrive FORWARDED from the buying accounts (From: is
# the hotmail account), so that one is found by subject instead.
SWEEP_CRITERIA = (
    ("FROM", "leapevents.com"),
    ("FROM", "frontgatetickets.com"),
    ("FROM", "fourvenues.com"),
    ("FROM", "receipts.posh.vip"),
    ("FROM", "order.eventbrite.com"),
    ("FROM", "seetickets.us"),
    ("FROM", "tickets-shotgun.live"),
    ("FROM", "universe.com"),
    ("FROM", "2207.co.il"),
    ("FROM", "confirm@ticketmaster.co.il"),
    ("FROM", "noreply@dice.fm"),
    ("SUBJECT", "You got ticket"),
)

# Bump when a parser changes so messages recorded as 'other' are re-read.
CLASSIFY_VERSION = 1


def _flat(text):
    return re.sub(r"\s+", " ", (text or "").replace("​", " ")).strip()


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _usd(s):
    m = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", s or "")
    return _num(m.group(1)) if m else None


def _finish(out):
    if out.get("price_each") is None and out.get("total") is not None and out.get("qty"):
        out["price_each"] = round(out["total"] / out["qty"], 2)
    if out.get("total") is None and out.get("price_each") is not None and out.get("qty"):
        out["total"] = round(out["price_each"] * out["qty"], 2)
    out.setdefault("kind", "purchase")
    out.setdefault("currency", "USD")
    return out


def _card(flat):
    for rx in (r"card ending in\s*(\d{4})", r"Card Ending In\s*(\d{4})",
               r"Last 4 digits:\s*(\d{4})", r"Payment Method\s*(\d{4})\b",
               r"\*{4,}(\d{4})\b", r"x{4}-x{4}-x{4}-(\d{4})", r"Payment\s+(\d{4})\b"):
        m = re.search(rx, flat, re.I)
        if m:
            return m.group(1)
    return ""


# ---- per-provider parsers ---------------------------------------------------

def _leap(subject, flat, email_date, text=""):
    # "... 5 × General Sale - Tier 4 $308.25 + $61.75 Service Fee $370.00 ...
    #  Order Total $400.00"
    m = re.search(r"Your confirmation ID is:\s*([A-Z0-9]+)", flat)
    tot = re.search(r"Order Total\s*\$\s*([\d,]+\.\d{2})", flat)
    if not m or not tot:
        return None
    out = {"provider": "leap", "order_ref": m.group(1), "total": _num(tot.group(1))}
    out["event_name"] = re.sub(r"^Your order confirmation for\s*", "", subject).strip()
    lines = re.findall(r"(\d+)\s*×\s*(.+?)\s*\$", flat)
    if lines:
        out["qty"] = sum(int(q) for q, _ in lines)
        out["ticket_type"] = " + ".join(dict.fromkeys(t.strip() for _, t in lines))
    out["event_date_iso"] = find_date(flat.split("Your confirmation ID")[0])
    # "... 6:00pm - 11:00pm Petco Park in San Diego, CA 5 × General Sale"
    v = re.search(r"[ap]m - [\d:]+[ap]m (\S.+?) in ([^,]+, [A-Z]{2}) \d+ ×",
                  flat.split("Date of Purchase")[-1], re.I)
    if v:
        out["venue"] = v.group(1).strip()
    out["card_last4"] = _card(flat)
    return out


def _frontgate(subject, flat, email_date, text=""):
    # "Factory 93 presents ANOTR Friday, October 30, 2026 at Ace*Mission ...
    #  Ticket Quantity Price VIP Admission 4 $748.72 ... Total: $748.72"
    m = re.search(r"Order #\s*(\d+)", subject) or re.search(r"Order #\s*(\d+)", flat)
    tot = re.search(r"\bTotal:\s*\$\s*([\d,]+\.\d{2})", flat)
    if not m or not tot or "Receipt" not in subject:
        return None
    out = {"provider": "frontgate", "order_ref": m.group(1), "total": _num(tot.group(1))}
    # The event name is the line right above the date line (the shipping
    # address above it has no fixed length, so the flat text can't anchor it).
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if i and re.match(r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, \w+ \d{1,2}, \d{4}$", ln):
            out["event_name"] = lines[i - 1]
            out["event_date_iso"] = find_date(ln)
            if i + 1 < len(lines) and lines[i + 1].startswith("at "):
                out["venue"] = lines[i + 1][3:].strip()
            break
    rows = re.findall(r"(?:Ticket Quantity Price|\$[\d,]+\.\d{2})\s+(?!Event Subtotal)([^$]+?)\s+(\d+)\s+\$([\d,]+\.\d{2})",
                      flat.split("Event Subtotal")[0])
    if rows:
        out["qty"] = sum(int(q) for _, q, _ in rows)
        out["ticket_type"] = " + ".join(dict.fromkeys(t.strip() for t, _, _ in rows))
    out["card_last4"] = _card(flat)
    return out


def _fourvenues(subject, flat, email_date, text=""):
    # One email PER TICKET: "Your ticket for Jimi Jules: 1TEE2EAVJ" ... "x1 Ticket
    # General Access ... Jimi Jules Thu, Dec 31, 2026 10:00 PM ..." — no price,
    # no order number. The ticket code is the row key; group_ref joins the
    # tickets bought together (same event, buyer and day) into one order.
    m = re.match(r"Your tickets? for (.+?):\s*([A-Z0-9]+)\s*$", subject)
    if not m:
        return None
    out = {"provider": "fourvenues", "event_name": m.group(1).strip(),
           "order_ref": m.group(2), "qty": 1}
    q = re.search(r"\bx(\d+)\s+Ticket\s+(.+?)\s+Download", flat)
    if q:
        out["qty"] = int(q.group(1))
        out["ticket_type"] = q.group(2).strip()
    out["event_date_iso"] = find_date(flat)
    v = re.search(r"Here's your ticket for (.+?)\s*!", flat)
    if v:
        out["venue"] = v.group(1).strip().title()
    out["seats"] = m.group(2)
    out["group_ref"] = "|".join(["fourvenues", out["event_name"].lower(),
                                 out.get("event_date_iso") or "", str(email_date or "")])
    return out


def _posh(subject, flat, email_date, text=""):
    # "16 x Tickets Order total: $ 3983.97 February 6th 2027, ... Order 38376570 -
    #  ... Pinny Brachfeld 1 x 2-DAY GA || CREW PACK $ 219.69 ..."
    o = re.search(r"Order (\d{5,}) -", flat)
    tot = re.search(r"Order total:\s*\$\s*([\d,]+(?:\.\d{1,2})?)", flat)
    if not o or not tot:
        return None
    out = {"provider": "posh", "order_ref": o.group(1), "total": _num(tot.group(1))}
    out["event_name"] = re.sub(r"^Your Tickets for\s*", "", subject).strip()
    q = re.search(r"(\d+) x Tickets", flat)
    if q:
        out["qty"] = int(q.group(1))
    types = re.findall(r"\b1 x (.+?) \$\s*[\d,]+(?:\.\d{1,2})?", flat.split("Order Summary")[-1])
    if types:
        out["ticket_type"] = " + ".join(dict.fromkeys(t.strip() for t in types))
    out["event_date_iso"] = find_date(flat.split("Order Summary")[0])
    v = re.search(r"(?:EST|EDT|CST|CDT|MST|MDT|PST|PDT)\s+(.+?)\s+\1", flat)
    if v:
        out["venue"] = v.group(1).strip()
    return out


def _eventbrite(subject, flat, email_date, text=""):
    # "Order #15968774193: - October 23, 2026 $152.00 paid by MasterCard Last 4
    #  digits: 3011 ... Pinny Brachfeld 1 x Last Chance GA $38.00 ..."
    if not subject.startswith("Order Confirmation for"):
        return None
    o = re.search(r"Order #(\d+)", flat)
    if not o:
        return None
    out = {"provider": "eventbrite", "order_ref": o.group(1),
           "event_name": subject.replace("Order Confirmation for", "").strip()}
    t = re.search(r"Order total:\s*([\d,]+\.\d{2})\s*USD", flat) or \
        re.search(r"\$([\d,]+\.\d{2}) paid by", flat)
    if t:
        out["total"] = _num(t.group(1))
    q = re.search(r"(\d+) x Tickets", flat)
    if q:
        out["qty"] = int(q.group(1))
    types = re.findall(r"\b1 x (.+?) \$[\d,]+\.\d{2}", flat)
    if types:
        out["ticket_type"] = " + ".join(dict.fromkeys(t.strip() for t in types))
    head = flat.split("Order Summary")[0]
    out["event_date_iso"] = find_date(head.split("Order total")[-1])
    v = re.search(r"Yahoo (.+?) View on map", head)
    if v:
        out["venue"] = v.group(1).strip()
    out["card_last4"] = _card(flat)
    return out


def _seetickets(subject, flat, email_date, text=""):
    # "... Order Number gnHG4A4JM36RIQ ... Billed to Card Ending In 3134
    #  10 x General Admission (Tier 2) (@ $37.22) $372.20 *Total* *$372.20*"
    o = re.search(r"Order Number\s+([A-Za-z0-9]+)", flat)
    if not o:
        return None
    out = {"provider": "seetickets_us", "order_ref": o.group(1),
           "event_name": re.sub(r"^Here Are Your Tickets for\s*", "", subject).strip()}
    t = re.search(r"\*?Total\*?\s*\*?\$([\d,]+\.\d{2})", flat)
    if t:
        out["total"] = _num(t.group(1))
    lines = re.findall(r"(\d+) x (.+?) \(@ \$([\d,]+\.\d{2})\)", flat)
    if lines:
        out["qty"] = sum(int(q) for q, _, _ in lines)
        out["ticket_type"] = " + ".join(dict.fromkeys(n.strip() for _, n, _ in lines))
    d = re.search(r"\b(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, \w+ \d{1,2}, \d{4}", flat)
    if d:
        out["event_date_iso"] = find_date(d.group(0))
        v = re.search(re.escape(d.group(0)) + r" (.+?) \d+ ", flat)
        if v:
            out["venue"] = v.group(1).strip()
    out["card_last4"] = _card(flat)
    return out


def _shotgun(subject, flat, email_date, text=""):
    # Only the "D-Day" ticket mail has been seen: count + type, no price and
    # no order number. Keyed on event + buyer so a later real receipt merges.
    m = re.search(r"Your tickets for (.+?)(?: are (?:ready|attached))", flat)
    q = re.search(r"Your tickets (\d+) tickets?:\s*(.+?)\s+(?:🕐|Date and time)", flat)
    if not m or not q:
        return None
    out = {"provider": "shotgun", "event_name": m.group(1).strip(),
           "qty": int(q.group(1)), "ticket_type": q.group(2).strip()}
    d = re.search(r"Date and time (.+?) at \d", flat)
    out["event_date_iso"] = find_date(d.group(1), email_date) if d else ""
    v = re.search(r"Address (.+?) \d+ ", flat)
    if v:
        out["venue"] = v.group(1).strip()
    return out


def _ticketmaster_us(subject, flat, email_date, text=""):
    # "You got tickets to MAU P ... ORDER # 31-20235/WES MAU P ... Sat · Jan 30,
    #  2027 · 6:30 PM BMO Stadium - Los Angeles, CA ... Your Tickets Artist
    #  Presale Section STAGE Row - Seat - Floor Section ... Total $650.60
    #  Payment Method 7334"
    m = re.match(r"You got tickets? to (.+)$", subject)
    o = re.search(r"ORDER #\s*([\w/-]+)", flat)
    if not m or not o:
        return None
    out = {"provider": "ticketmaster_us", "order_ref": o.group(1),
           "event_name": m.group(1).strip()}
    t = re.search(r"Payment Summary Total\s*\$([\d,]+\.\d{2})", flat)
    if t:
        out["total"] = _num(t.group(1))
    tix = re.split(r"Order Summary Your Tickets? ", flat, 1)[-1].split("Payment Summary")[0]
    seats = re.findall(r"(.+?) Section (\S+) Row (\S+) Seat (\S+) (?:Floor Section|\S+ Section)?\s*", tix)
    if seats:
        out["qty"] = len(seats)
        out["ticket_type"] = " + ".join(dict.fromkeys(s[0].strip() for s in seats))
        out["section"] = ", ".join(dict.fromkeys(s[1] for s in seats))
        rows = [s[2] for s in seats if s[2] != "-"]
        nums = [s[3] for s in seats if s[3] != "-"]
        out["row_label"] = ", ".join(dict.fromkeys(rows))
        out["seats"] = ", ".join(nums)
    d = re.search(r"\w{3} · (\w{3} \d{1,2}, \d{4}) · [\d:]+ [AP]M (.+?) Get Directions", flat)
    if d:
        out["event_date_iso"] = find_date(d.group(1))
        out["venue"] = d.group(2).strip()
    else:
        out["event_date_iso"] = find_date(flat.split("Order Summary")[0])
    out["card_last4"] = _card(flat)
    return out


def _kupat(subject, flat, email_date, text=""):
    # "אישור הזמנה - 06784-04022-08497 ... שלמה ארצי באולם - היכל מנורה מבטחים
    #  ביום - חמישי 31/12/2026 ... 2 x רגיל (₪399.00) אזור: ... סה"כ כרטיסים
    #  בהזמנה: 6 סה"כ תשלום בהזמנה: ₪2,394.00 אמצעי תשלום: מסטרקארד 518725******5382"
    o = re.search(r"אישור הזמנה\s*-\s*([\d-]{8,})", flat)
    tot = re.search(r'סה"כ\s*תשלום\s*בהזמנה:\s*₪?\s*([\d,.]+)', flat)
    if not o or not tot:
        return None
    out = {"provider": "kupat", "order_ref": o.group(1), "total": _num(tot.group(1)),
           "currency": "ILS"}
    ev = re.search(r"לחץ כאן\s+(.+?)\s+באולם\s*-\s*(.+?)\s+ביום", flat)
    if ev:
        out["event_name"], out["venue"] = ev.group(1).strip(), ev.group(2).strip()
    d = re.search(r"ביום\s*-\s*\S+\s+(\d{1,2})/(\d{1,2})/(\d{4})", flat)
    if d:
        out["event_date_iso"] = f"{int(d.group(3)):04d}-{int(d.group(2)):02d}-{int(d.group(1)):02d}"
    q = re.search(r'סה"כ\s*כרטיסים\s*בהזמנה:\s*(\d+)', flat)
    lines = re.findall(r"(\d+)\s*x\s*(\S+)\s*\(₪([\d,.]+)\)", flat)
    out["qty"] = int(q.group(1)) if q else sum(int(n) for n, _, _ in lines) or None
    if lines:
        out["ticket_type"] = " + ".join(dict.fromkeys(t for _, t, _ in lines))
    blocks = re.findall(r"אזור:\s*([^,]+?)\s*,\s*שורה:\s*(\S+?),\s*כיסא(?:ות)?:\s*(\d+(?:\s*-\s*\d+)?)", flat)
    if blocks:
        out["section"] = ", ".join(dict.fromkeys(b[0].strip() for b in blocks))
        if len(blocks) == 1:
            out["row_label"], out["seats"] = blocks[0][1], blocks[0][2].replace(" ", "")
        else:  # several rows in one order: each block carries its own row
            out["seats"] = "; ".join(f"R{b[1]} {b[2].replace(' ', '')}" for b in blocks)
    elif re.search(r"(?:^|\s)עמידה(?:\s|$)", flat):
        out["section"] = "עמידה"
    out["card_last4"] = _card(flat)
    return out


def _ticketmaster_il(subject, flat, email_date, text=""):
    # "מספר אסמכתא: 36L8P אירוע אזור שורה מושב(ים) סוג כמות מחיר סה״כ NEXT
    #  2026-10-24 20:50:00.0 האצטדיון הלאומי שער 4 עליון 36 562-561-563-564
    #  מחיר מלא 4 199.00 796.00 סה״כ 796.00"
    o = re.search(r"\(Ref:\s*(\w+)\)", subject) or re.search(r"מספר אסמכתא:\s*(\w+)", flat)
    if not o:
        return None
    out = {"provider": "ticketmaster_il", "order_ref": o.group(1), "currency": "ILS"}
    body = flat.split("סה״כ", 1)[-1]
    r = re.search(r"^\s*(.+?) (\d{4}-\d{2}-\d{2}) [\d:.]+ (.+?) (\S+) ([\d-]+) (\S+(?: \S+)?) (\d+) ([\d,.]+) ([\d,.]+)\b", body)
    if r:
        out["event_name"] = r.group(1).strip()
        out["event_date_iso"] = r.group(2)
        venue_section = r.group(3).strip()
        out["row_label"], out["seats"] = r.group(4), r.group(5)
        out["ticket_type"] = r.group(6)
        out["qty"], out["price_each"] = int(r.group(7)), _num(r.group(8))
        out["total"] = _num(r.group(9))
        # The venue and section run together ("האצטדיון הלאומי שער 4 עליון").
        vs = re.match(r"(.+?) ((?:שער|אולם|יציע|גלריה|אזור|מתחם)\b.*)$", venue_section)
        out["venue"], out["section"] = (vs.group(1), vs.group(2)) if vs else (venue_section, "")
    t = re.search(r"סה״כ ([\d,.]+) לצפייה", flat)
    if t:
        out["total"] = _num(t.group(1))
    return out


def _universe_cancel(subject, flat, email_date, text=""):
    # "Your order for Prospa has been cancelled ... Start Time: 2026-12-03 ...
    #  Ticket type General Admission Tier 2 - Male (×N) ... Refund Total : $92.28"
    m = re.match(r"Your order for (.+?) has been cancelled", subject)
    if not m:
        return None
    out = {"provider": "universe", "kind": "cancel", "event_name": m.group(1).strip()}
    d = re.search(r"Start Time:\s*(\d{4}-\d{2}-\d{2})", flat)
    out["event_date_iso"] = d.group(1) if d else ""
    v = re.search(r"Venue Location:\s*(.+?),", flat)
    if v:
        out["venue"] = v.group(1).strip()
    types = re.findall(r"Ticket type (.+?) (?=Name |Refund Information)", flat)
    out["qty"] = len(types) or None
    if types:
        out["ticket_type"] = " + ".join(dict.fromkeys(t.strip() for t in types))
    refunds = [_num(v) for v in re.findall(r"Refund Total\s*:?\s*\$([\d,]+\.\d{2})", flat)]
    if refunds:
        out["total"] = round(sum(refunds), 2)
    out["card_last4"] = _card(flat)
    return out


def _dice_refund(subject, flat, email_date, text=""):
    # "Refunded: 6 tickets to Locky" ... "Refund details Event Locky Payment 5382
    #  Total $114.36". Purchases themselves are dice_email's job (dice_purchases).
    m = re.search(r"Refunded:\s*(\d+) tickets? to (.+)$", subject.strip())
    if not m:
        return None
    return {"provider": "dice", "kind": "refund", "qty": int(m.group(1)),
            "event_name": m.group(2).strip(), "total": _usd(flat.split("Total", 1)[-1]),
            "card_last4": _card(flat)}


_SENDERS = (
    ("leapevents.com", _leap),
    ("frontgatetickets.com", _frontgate),
    ("fourvenues.com", _fourvenues),
    ("receipts.posh.vip", _posh),
    ("eventbrite.com", _eventbrite),
    ("seetickets.us", _seetickets),
    ("shotgun.live", _shotgun),
    ("universe.com", _universe_cancel),
    ("2207.co.il", _kupat),
    ("ticketmaster.co.il", _ticketmaster_il),
    ("dice.fm", _dice_refund),
    ("ticketmaster.com", _ticketmaster_us),
)


def wants(sender, subject):
    """Cheap header check: could this be a receipt we parse? Lets the sweep
    skip downloading the bodies of marketing mail from the same senders."""
    s, subj = (sender or "").lower(), subject or ""
    if subj.startswith("You got ticket"):
        return True
    if not any(n in s for n, _ in _SENDERS):
        return False
    if "dice.fm" in s:
        return subj.strip().startswith("Refunded")
    if "universe.com" in s:
        return "cancelled" in subj
    return not re.search(r"\(פרסומת\)|\( פרסומת \)|verification code|Login code|"
                         r"Billing Information Has Been Updated", subj, re.I)


def parse(sender, subject, text, email_date=None):
    """One receipt -> normalized dict (see module docstring), or None."""
    s, subj = (sender or "").lower(), (subject or "").strip()
    flat = _flat(text)
    if isinstance(email_date, str):
        try:
            email_date = date.fromisoformat(email_date[:10])
        except ValueError:
            email_date = None
    fn = None
    if subj.startswith("You got ticket"):
        fn = _ticketmaster_us
    else:
        for needle, f in _SENDERS:
            if needle in s:
                fn = f
                break
    if fn is None:
        return None
    out = fn(subj, flat, email_date, text)
    if not out or not out.get("event_name"):
        return None
    return _finish({k: v for k, v in out.items() if v not in (None, "")})
