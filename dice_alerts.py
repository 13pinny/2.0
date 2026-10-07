"""Price / availability pings for every DICE event on the /dice tracker.

The drop watchers only ping for events that have a watcher, and only on
seat-style adds -- events that are merely tracked (auto-tracked purchases,
followed artists' shows, pasted URLs) never pinged at all, so a GA type
stepping from $140 to $180, or a show flipping to waitlist-only, went
unnoticed. This tick reads each tracked upcoming event's ticket types
(dice.get_labels(force=True): the API off-VPS, the desktop relay's copy on
it), diffs a small per-event summary against the last one stored in
dice_alert_state and pings what moved:

  'restock'     the event, or one of its types, was sold out / waitlist
                only and is buyable again -> shocks
  'price_down'  a type (or the cheapest buyable price) got cheaper -> shocks
  'soldout'     buyable -> sold out, waitlist only -> status
  'price_up'    a type's price climbed, or the cheapest buyable price did
                because a release sold through -> jumps
  'type_soldout' one ticket type sold out / went to waitlist while others
                still sell -> status
  'onsale'      announced -> on sale -> new_events

One ping per event per tick, headed by its most urgent change, with every
other change listed under it. The first sighting of an event is a silent
baseline, and a failed fetch leaves the stored state alone -- an unreadable
event must never read as "sold out". Also feeds dice_tier_log, so the /dice
price ladder fills in without anyone pressing Refresh.

Probe: python dice_alerts.py   (one dry tick: prints, stores nothing, no pings)
"""
import json
from datetime import datetime, timezone

import db
import dice

# Most urgent first: the headline (and the channel) is the first kind present.
KIND_ORDER = ("restock", "price_down", "soldout", "price_up", "type_soldout", "onsale")


def summarize(labels):
    """{"status", "min_price", "currency", "types": {name: {price, on}}} from
    one event's labels. Per type name, the buyable entry wins, else the
    cheapest -- DICE can list a type once per price tier."""
    meta = labels.get("meta") or {}
    types = {}
    for b in (labels.get("blocks") or {}).values():
        name = (b.get("name") or "").strip()
        if not name:
            continue
        on = b.get("status") == "on-sale"
        cur = types.get(name)
        cand = {"price": b.get("price"), "on": on}
        if cur is None or (on and not cur["on"]) or (
                on == cur["on"] and (cand["price"] or 0) < (cur["price"] or 0)):
            types[name] = cand
    on_prices = [t["price"] for t in types.values() if t["on"] and t["price"] is not None]
    currency = next((b.get("currency") for b in (labels.get("blocks") or {}).values()
                     if b.get("currency")), "USD")
    return {"status": meta.get("status"), "min_price": min(on_prices) if on_prices else None,
            "currency": currency, "types": types}


def _money(v, cur):
    sym = {"USD": "$", "GBP": "£", "EUR": "€"}.get(cur or "USD", (cur or "") + " ")
    return f"{sym}{v:,.2f}" if v is not None else "—"


def diff(old, new):
    """[(kind, line)] for everything that moved between two summaries."""
    out = []
    cur = new.get("currency")
    os_, ns = old.get("status"), new.get("status")
    if os_ == "soldout" and ns == "selling":
        out.append(("restock", f"Back on sale from {_money(new['min_price'], cur)} — it was sold out / waitlist only"))
    elif os_ == "selling" and ns == "soldout":
        out.append(("soldout", "Sold out — waitlist only now"
                    + (f" (last price {_money(old['min_price'], cur)})" if old.get("min_price") is not None else "")))
    elif os_ in ("upcoming", "unknown", None) and ns == "selling":
        out.append(("onsale", f"On sale now from {_money(new['min_price'], cur)}"))
    ot, nt = old.get("types") or {}, new.get("types") or {}
    for name, t in nt.items():
        o = ot.get(name)
        if not (o and o["on"] and t["on"]) or o["price"] is None or t["price"] is None:
            continue
        if t["price"] > o["price"]:
            out.append(("price_up", f"{name}: {_money(o['price'], cur)} → **{_money(t['price'], cur)}**"))
        elif t["price"] < o["price"]:
            out.append(("price_down", f"{name}: {_money(o['price'], cur)} → **{_money(t['price'], cur)}**"))
    if ns == "selling" and os_ == "selling":
        for name, t in nt.items():
            o = ot.get(name)
            if t["on"] and not (o and o["on"]):
                if o and o["price"] is not None and t["price"] is not None and t["price"] > o["price"]:
                    kind = "price_up"
                elif o:
                    kind = "restock"   # this type was sold out and is back
                else:
                    kind = "info"      # a brand-new type; the price check below covers it
                out.append((kind, f"{name} {'back ' if kind == 'restock' else ''}on sale at {_money(t['price'], cur)}"))
        for name, o in ot.items():
            t = nt.get(name)
            if o["on"] and not (t and t["on"]):
                out.append(("type_soldout", f"{name} sold out / waitlist"
                            + (f" (was {_money(o['price'], cur)})" if o.get("price") is not None else "")))
        # A release selling through usually shows up as one type going off
        # sale and a pricier one coming on, not as a price edit -- so the
        # event's cheapest buyable price is compared too.
        op, np_ = old.get("min_price"), new.get("min_price")
        kinds = {k for k, _ in out}
        if op is not None and np_ is not None and op != np_ and not kinds & {"price_up", "price_down"}:
            out.append(("price_up" if np_ > op else "price_down",
                        f"Cheapest ticket {_money(op, cur)} → **{_money(np_, cur)}**"))
    return out


def headline_kind(changes):
    kinds = {k for k, _ in changes}
    return next((k for k in KIND_ORDER if k in kinds), None)


def run_tick(codes, notify_fn=None, dry=False, now_iso=None):
    """Check each event code once. notify_fn(kind, info) sends a ping (None
    = muted: state is still stored). Returns a summary dict."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    state = {} if dry else db.dice_alert_state_all()
    checked = pinged = errors = 0
    for code in codes:
        labels = dice.get_labels(code, "0", force=True)
        if labels.get("_error"):
            errors += 1
            continue
        meta = labels.get("meta") or {}
        first_ms = meta.get("firstPerfMs")
        if first_ms and first_ms + 12 * 3600e3 < now_ms:
            continue
        checked += 1
        new = summarize(labels)
        if dry:
            print(f"{code} {meta.get('eventName')!r}: {json.dumps(new)}")
            continue
        try:
            db.dice_tier_log_update(code, labels.get("blocks") or {}, now_iso)
        except Exception:
            pass
        old = state.get(code)
        db.dice_alert_state_put(code, new, now_iso)
        if old is None:
            continue  # silent baseline
        changes = [c for c in diff(old, new)]
        kind = headline_kind(changes)
        if not kind or notify_fn is None:
            continue
        notify_fn(kind, {
            "event_code": code,
            "name": meta.get("eventName") or code,
            "venue": " · ".join(v for v in (meta.get("venueName"), meta.get("venueCity")) if v),
            "date_text": meta.get("firstPerfText"),
            "url": dice.perf_url(code),
            "min_price": new.get("min_price"),
            "old_min_price": old.get("min_price"),
            "currency": new.get("currency"),
            "lines": [line for _, line in changes],
        })
        pinged += 1
    return {"checked": checked, "pinged": pinged, "errors": errors}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    codes = sys.argv[1:] or [str(r["code"]) for r in db.market_manual_all() if r["source"] == "dice"]
    print(run_tick(codes, dry=True))
