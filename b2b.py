"""/b2b: resale buys (Automatiq, TickPick, CrowdVolt) and the DICE account
their tickets landed in.

Emails are captured by mail_intake.sweep_b2b into b2b_orders /
b2b_dice_incoming. This module does the read-side work:

  match_dice()  pairs each order with the DICE "<Name> sent you tickets"
                mail that came in around the same time (within
                B2B_DICE_WINDOW_HOURS of the order's placed/confirmed mail,
                for the same show). Each incoming transfer is used once,
                closest-in-time first. A pin on the order
                (dice_transfer_id: a message id, or '' for "none") beats the
                automatic pick.
  sync_dice()   mirrors every order matched to a DICE transfer into
                dice_purchases (id dcp-b2b-<order id>, b2b_order_id set), so
                CrowdVolt-bought DICE tickets sit under their event on /dice
                and take part in holdings, cost and sale matching like any
                other DICE purchase. Rewritten on every sync; a row whose
                match went away is removed.
"""
from datetime import datetime, timezone

import b2b_email
import db
import fx
import mail_intake

LABELS = {"automatiq": "Automatiq", "tickpick": "TickPick", "crowdvolt": "CrowdVolt"}


def _ts(s):
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _venue_match(a, b):
    ta, tb = b2b_email.name_tokens(a), b2b_email.name_tokens(b)
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(small & big) / len(small) >= 0.6


def _same_show(order, inc):
    """Does a DICE incoming transfer plausibly carry this order's tickets?
    Event date (when both have one) must agree, and the event names or the
    venues must overlap — DICE dresses names up ("unKommon Events &
    Framework present: ANOTR" for CrowdVolt's "ANOTR")."""
    od, idt = order.get("event_date_iso"), inc.get("event_date_iso")
    if od and idt and od != idt:
        return False
    if b2b_email.names_match(order.get("event_name"), inc.get("event_name")):
        return True
    return _venue_match(order.get("venue"), inc.get("venue"))


def match_dice(orders=None, incoming=None):
    """{order_id: incoming row} for every order with a DICE transfer."""
    orders = orders if orders is not None else db.b2b_orders_all()
    incoming = incoming if incoming is not None else db.b2b_dice_incoming_all()
    by_mid = {i["message_id"]: i for i in incoming}
    out, used = {}, set()
    # Pins first, so the automatic pass can't hand a pinned mail to another order.
    for o in orders:
        pin = o.get("dice_transfer_id")
        if pin and pin in by_mid:
            out[o["id"]] = by_mid[pin]
            used.add(pin)
    window = mail_intake.B2B_DICE_WINDOW_HOURS * 3600
    pairs = []
    for o in orders:
        if (o["id"] in out or o.get("dice_transfer_id") is not None or o.get("hidden")
                or o.get("status") == "cancelled"):
            continue
        anchors = [t for t in (_ts(o.get("placed_at")), _ts(o.get("confirmed_at")),
                               _ts(o.get("delivered_at"))) if t]
        if not anchors:
            continue
        for inc in incoming:
            if inc["message_id"] in used:
                continue
            t = _ts(inc.get("email_date"))
            if not t:
                continue
            gap = min(abs((t - a).total_seconds()) for a in anchors)
            if gap <= window and _same_show(o, inc):
                qty_off = bool(o.get("qty") and inc.get("qty") and o["qty"] != inc["qty"])
                pairs.append((qty_off, gap, o["id"], inc))
    # Same quantity first, then closest in time.
    pairs.sort(key=lambda p: (p[0], p[1]))
    for _q, _gap, oid, inc in pairs:
        if oid in out or inc["message_id"] in used:
            continue
        out[oid] = inc
        used.add(inc["message_id"])
    return out


def _usd(amount, currency):
    if amount is None:
        return None
    cur = (currency or "USD").upper()
    if cur == "USD":
        return amount
    try:
        rate = fx.to_usd_rate(cur)
    except Exception:
        rate = None
    return round(amount * rate, 2) if rate else None


def sync_dice():
    """Mirror DICE-delivered b2b orders into dice_purchases. Returns the
    number of mirrored rows. Never raises past a single bad order."""
    orders = db.b2b_orders_all()
    matches = match_dice(orders)
    now = datetime.now(timezone.utc).isoformat()
    keep = set()
    for o in orders:
        inc = matches.get(o["id"])
        if not inc or o.get("hidden") or o.get("status") == "cancelled":
            continue
        try:
            qty = o.get("qty") or inc.get("qty") or None
            total = _usd(o.get("total"), o.get("currency"))
            ppu = _usd(o.get("price_each"), o.get("currency"))
            if ppu is None and total is not None and qty:
                ppu = round(total / qty, 2)
            pid = "dcp-b2b-" + o["id"][len("b2b-"):]
            db.dice_purchase_upsert_b2b({
                "id": pid,
                "message_id": "b2b:" + o["id"],
                "account_email": inc.get("account_email") or "",
                "event_name": inc.get("event_name") or o.get("event_name") or "",
                "event_slug": inc.get("event_slug") or "",
                "event_date_iso": inc.get("event_date_iso") or o.get("event_date_iso") or "",
                "venue": inc.get("venue") or o.get("venue") or "",
                # DICE's transfer mail names no type; CrowdVolt's "6 × General
                # Admission" is stored as the section.
                "ticket_type": inc.get("ticket_type")
                               or (o.get("section") if o["platform"] == "crowdvolt" else "") or "",
                "qty": qty,
                "price_total": total,
                "price_per_unit": ppu,
                "currency": "USD",
                "email_date": inc.get("email_date") or o.get("placed_at") or "",
                "subject": (f"Bought on {LABELS.get(o['platform'], o['platform'])}"
                            + ("" if o["order_ref"].startswith("x-") else f" (order {o['order_ref']})"))[:500],
                "b2b_order_id": o["id"],
            }, now)
            keep.add(pid)
        except Exception:
            continue
    for pid in db.dice_purchases_b2b_ids():
        if pid not in keep:
            db.dice_purchase_delete(pid)
    return len(keep)


def build():
    """Rows for GET /api/b2b."""
    orders = db.b2b_orders_all()
    incoming = db.b2b_dice_incoming_all()
    matches = match_dice(orders, incoming)
    used = {i["message_id"] for i in matches.values()}
    rows = []
    for o in orders:
        inc = matches.get(o["id"])
        r = {k: o.get(k) for k in (
            "id", "platform", "order_ref", "event_name", "event_date_iso",
            "event_date_text", "venue", "section", "row_label", "seats", "qty",
            "price_each", "total", "currency", "buyer_email", "status",
            "placed_at", "confirmed_at", "delivered_at", "cancelled_at",
            "delivery", "source_notes", "dice_wallet", "hidden", "notes")}
        r["order_ref"] = "" if (o.get("order_ref") or "").startswith("x-") else o.get("order_ref")
        r["dice"] = None if not inc else {
            "message_id": inc["message_id"],
            "account": inc.get("account_email"),
            "sender": inc.get("sender_name"),
            "holder": inc.get("holder_name"),
            "qty": inc.get("qty"),
            "event_name": inc.get("event_name"),
            "at": inc.get("email_date"),
            "pinned": bool(o.get("dice_transfer_id")),
        }
        r["dice_pinned_none"] = o.get("dice_transfer_id") == ""
        rows.append(r)
    spare = [{"message_id": i["message_id"], "account": i.get("account_email"),
              "sender": i.get("sender_name"), "qty": i.get("qty"),
              "event_name": i.get("event_name"), "event_date_iso": i.get("event_date_iso"),
              "at": i.get("email_date")}
             for i in incoming if i["message_id"] not in used]
    return {"orders": rows, "dice_unmatched": spare,
            "dice_all": [{"message_id": i["message_id"], "account": i.get("account_email"),
                          "sender": i.get("sender_name"), "qty": i.get("qty"),
                          "event_name": i.get("event_name"), "at": i.get("email_date")}
                         for i in incoming]}
