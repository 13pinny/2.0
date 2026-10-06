"""Attach resale-platform sales (viagogo / CrowdVolt / Lysted) to DICE
purchases without the /dice match-a-sale picker.

A sale is linked only when the answer is unambiguous:
  - the sale's event date equals the purchase's event date (both known);
  - the names match tightly (normalized equality/containment, or a difflib
    ratio >= AUTO_RATIO -- stricter than the transfer matcher's 0.75);
  - every matching purchase is the SAME DICE event (one slug), so two shows
    on one night never get guessed between;
  - no non-DICE purchase (Lysted / manual / JeruJam) exists for that event,
    because then the sale could be those tickets;
  - the ticket type matches (dice_types: Early Entry, VIP etc. must agree;
    price tiers don't count, so GA at $60 and GA at $75 are one type);
  - the DICE purchases of that type still have enough unsold tickets for the
    whole sale.
Anything else is left for the picker. Within one event the sale is spread
over the purchases oldest-email-first, the same FIFO the transfer matcher
uses. Links are stored with auto=1; unlinking one on /dice records the sale
in dice_autolink_skip so it is never re-linked.

    python dice_autolink.py           # dry run: print what it would link
    python dice_autolink.py --apply
"""
import sys
from datetime import datetime, timezone
from difflib import SequenceMatcher

import db
import dice_email
import dice_types

AUTO_RATIO = 0.85
SALE_LOOKBACK_DAYS = 180


def _names_match(a, b):
    na, nb = dice_email._norm_name(a), dice_email._norm_name(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= AUTO_RATIO


def _day(v):
    return (v or "")[:10] or None


def _other_inventory():
    """[(event_name, day)] of every non-DICE purchase record."""
    out = []
    for r in db.all_lysted_purchases():
        out.append((r.get("event_name"), _day(r.get("event_date_iso"))))
    for r in db.all_manual_inventory():
        out.append((r.get("event_name"), _day(r.get("event_date_iso"))))
    for r in db.all_jerujam_tickets():
        out.append((r.get("event_name"), _day(r.get("event_date_iso"))))
    return [(n, d) for n, d in out if n and d]


def _pick_type(cands, sale):
    """Narrow the purchases to the sale's ticket type. Returns
    (purchases, None) or ([], reason). The sale's label must name the same
    distinctive words as the DICE type (Early Entry ≠ GA); a sale that says
    nothing extra is plain GA. When the event was bought as a single type,
    a sale that names fewer words than it still matches (the listing just
    left the extra word out); a word the DICE type lacks never does."""
    want = dice_types.type_key(sale.get("section"), sale.get("ticket_type"))
    by_type = {}
    for p in cands:
        by_type.setdefault(dice_types.purchase_type_key(p.get("ticket_type")), []).append(p)
    if want in by_type:
        return by_type[want], None
    typed = {k: v for k, v in by_type.items() if k is not None}
    if len(typed) == 1:
        (k, ps), = typed.items()
        if want <= k:
            return ps, None
        return [], f"sale is {dice_types.describe(want)}, DICE tickets are {dice_types.describe(k)}"
    if not typed:  # only mixed-type purchases
        return cands, None
    return [], (f"sale is {dice_types.describe(want)}, DICE has "
                + " / ".join(sorted(dice_types.describe(k) for k in typed)))


def run(apply=True, now_iso=None):
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    purchases = db.dice_purchases_all()
    linked = db.dice_linked_qty_by_purchase()
    avail = {p["id"]: (p.get("qty") or 0) - linked.get(p["id"], 0) for p in purchases}
    # Hidden/canceled sales, ones the user unlinked from an auto match, and
    # ones already matched to non-DICE inventory on /sales.
    skip = db.dice_autolink_skipped() | db.sales_excluded_keys() | {
        (m.get("sale_source"), str(m.get("sale_id"))) for m in db.all_matches()}
    others = _other_inventory()

    sales = [s for s in db.dice_sale_candidates(days=SALE_LOOKBACK_DAYS)
             if (s["source"], str(s["id"])) not in skip]
    sales.sort(key=lambda s: s.get("sale_date_iso") or "")  # oldest first

    result = {"linked": [], "skipped": []}
    for s in sales:
        need = (s.get("qty") or 0) - (s.get("qty_linked") or 0)
        if need <= 0 or (s.get("qty_linked") or 0) > 0:
            continue  # done, or already (partly) handled by hand
        day = _day(s.get("event_date_iso"))
        if not day:
            continue
        cands = [p for p in purchases
                 if _day(p.get("event_date_iso")) == day
                 and _names_match(p.get("event_name"), s.get("event_name"))]
        if not cands:
            continue  # not a DICE event at all
        why = None
        # One DICE event? Slugs decide when every row has one; a row missing
        # its slug (link absent from the email) falls back to the name.
        if all(p.get("event_slug") for p in cands):
            events = {p["event_slug"] for p in cands}
        else:
            events = {dice_email._norm_name(p.get("event_name")) for p in cands}
        if len(events) > 1:
            why = "several DICE events match"
        elif any(d == day and _names_match(n, s.get("event_name")) for n, d in others):
            why = "also bought outside DICE"
        else:
            cands, why = _pick_type(cands, s)
        if not why:
            open_ = sorted((p for p in cands if avail[p["id"]] > 0),
                           key=lambda p: (p.get("email_date") or "", p["id"]))
            if sum(avail[p["id"]] for p in open_) < need:
                why = "not enough unsold DICE tickets"
        if why:
            result["skipped"].append({"source": s["source"], "sale_id": s["id"],
                                      "event_name": s.get("event_name"), "reason": why})
            continue
        for p in open_:
            if need <= 0:
                break
            take = min(need, avail[p["id"]])
            if apply:
                db.dice_sale_link_add(p["id"], s["source"], s["id"], take, now_iso, auto=True)
            avail[p["id"]] -= take
            need -= take
            result["linked"].append({"source": s["source"], "sale_id": s["id"],
                                     "order_id": s.get("order_id"),
                                     "event_name": s.get("event_name"),
                                     "purchase_id": p["id"],
                                     "account": p.get("account_email"), "qty": take})
    return result


if __name__ == "__main__":
    db.init()
    res = run(apply="--apply" in sys.argv)
    for l in res["linked"]:
        print(f"LINK  {l['source']:9} #{l['order_id'] or l['sale_id']}  {l['event_name']}  "
              f"x{l['qty']} -> {l['account']}")
    for k in res["skipped"]:
        print(f"SKIP  {k['source']:9} {k['event_name']}  ({k['reason']})")
    if "--apply" not in sys.argv:
        print("(dry run; add --apply to write the links)")
