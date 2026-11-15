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

Flaps are not pinged: DICE cart holds make a release flip back and forth
(GA $99.99 -> $124.99 -> $99.99 ...), so a change INTO a price/on-off state
the same type had within KARTIS_DICE_FLAP_MINUTES (60) is stored silently
(see drop_flaps). Event-level sold out / back on sale always pings. Shows a
dice_follow watches get no 'onsale' ping here -- the follow sends that one.

Events muted with the bell on /dice (db.dice_event_mutes) are fetched and
diffed exactly the same -- state and tier log still move -- only the send is
skipped, so unmuting picks up from the current state with no backlog.

Probe: python dice_alerts.py   (one dry tick: prints, stores nothing, no pings)
"""
import json
import os
from datetime import datetime, timedelta, timezone

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
    """[(kind, line, old_price, new_price, type_name)] for everything that
    moved between two summaries; the prices are what the ping's headline
    shows (None when the change has no before/after price), type_name the
    ticket type it is about (None = the whole event / the cheapest price)."""
    out = []
    cur = new.get("currency")
    os_, ns = old.get("status"), new.get("status")
    if os_ == "soldout" and ns == "selling":
        out.append(("restock", f"Back on sale from {_money(new['min_price'], cur)} — it was sold out / waitlist only",
                    old.get("min_price"), new.get("min_price"), None))
    elif os_ == "selling" and ns == "soldout":
        out.append(("soldout", "Sold out — waitlist only now"
                    + (f" (last price {_money(old['min_price'], cur)})" if old.get("min_price") is not None else ""),
                    old.get("min_price"), None, None))
    elif os_ in ("upcoming", "unknown", None) and ns == "selling":
        out.append(("onsale", f"On sale now from {_money(new['min_price'], cur)}", None, new.get("min_price"), None))
    ot, nt = old.get("types") or {}, new.get("types") or {}
    for name, t in nt.items():
        o = ot.get(name)
        if not (o and o["on"] and t["on"]) or o["price"] is None or t["price"] is None:
            continue
        if t["price"] > o["price"]:
            out.append(("price_up", f"{name}: {_money(o['price'], cur)} → **{_money(t['price'], cur)}**",
                        o["price"], t["price"], name))
        elif t["price"] < o["price"]:
            out.append(("price_down", f"{name}: {_money(o['price'], cur)} → **{_money(t['price'], cur)}**",
                        o["price"], t["price"], name))
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
                out.append((kind, f"{name} {'back ' if kind == 'restock' else ''}on sale at {_money(t['price'], cur)}",
                            o["price"] if o else None, t["price"], name))
        for name, o in ot.items():
            t = nt.get(name)
            if o["on"] and not (t and t["on"]):
                out.append(("type_soldout", f"{name} sold out / waitlist"
                            + (f" (was {_money(o['price'], cur)})" if o.get("price") is not None else ""),
                            o.get("price"), None, name))
        # A release selling through usually shows up as one type going off
        # sale and a pricier one coming on, not as a price edit -- so the
        # event's cheapest buyable price is compared too.
        op, np_ = old.get("min_price"), new.get("min_price")
        kinds = {c[0] for c in out}
        if op is not None and np_ is not None and op != np_ and not kinds & {"price_up", "price_down"}:
            out.append(("price_up" if np_ > op else "price_down",
                        f"Cheapest ticket {_money(op, cur)} → **{_money(np_, cur)}**", op, np_, None))
    return out


# --- flap suppression --------------------------------------------------------
# DICE holds tickets in carts for a few minutes, so when a release "sells
# out" into carts the next, pricier release opens -- and when the carts
# expire the cheaper one comes back. Measured on prod 2026-11-12: GA flipping
# $99.99 <-> $124.99 four times in an hour, $149.99 <-> $174.99, $70 <-> $80,
# every flip a ping. So each type's (and the cheapest price's) recent states
# are remembered with when they were last seen, and a change INTO a state
# seen within FLAP_MINUTES is a flap: stored, not pinged. A genuinely new
# price still pings at once. Event-level sold out / back on sale is never
# suppressed -- a sold-out show coming back is the ping that matters most.
FLAP_MINUTES = float(os.environ.get("KARTIS_DICE_FLAP_MINUTES", "60"))


def _parse(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def _states(summary):
    """{scope: state} for the flap memory: per type "on:<price>" / "off",
    plus the cheapest buyable price under scope ""."""
    out = {"": f"on:{summary.get('min_price')}"}
    for name, t in (summary.get("types") or {}).items():
        out[name] = f"on:{t['price']}" if t["on"] else "off"
    return out


def _change_state(c):
    """(scope, state) a change moves INTO, or None when it is never a flap."""
    kind, _line, _op, np_, typ = c
    if kind in ("price_up", "price_down"):
        return (typ or "", f"on:{np_}")
    if kind in ("restock", "info") and typ:
        return (typ, f"on:{np_}")
    if kind == "type_soldout":
        return (typ, "off")
    return None


def seen_update(seen, summary, now):
    """Stamp every current state as seen now; forget ones past the window."""
    seen = {k: dict(v) for k, v in (seen or {}).items()}
    for scope, st in _states(summary).items():
        seen.setdefault(scope, {})[st] = now.isoformat()
    cutoff = now - timedelta(minutes=max(FLAP_MINUTES, 0))
    for scope in list(seen):
        seen[scope] = {st: t for st, t in seen[scope].items()
                       if _parse(t) and _parse(t) >= cutoff}
        if not seen[scope]:
            del seen[scope]
    return seen


def drop_flaps(changes, seen, now):
    """(kept, dropped): a change is dropped when the state it moves into was
    already seen within FLAP_MINUTES (seen = the memory BEFORE this tick)."""
    if FLAP_MINUTES <= 0:
        return list(changes), []
    cutoff = now - timedelta(minutes=FLAP_MINUTES)
    kept, dropped = [], []
    for c in changes:
        sc = _change_state(c)
        t = _parse(((seen or {}).get(sc[0]) or {}).get(sc[1])) if sc else None
        (dropped if t and t >= cutoff else kept).append(c)
    return kept, dropped


def followed_codes():
    """DICE ids of shows an artist/venue follow (dice_follow) watches. The
    follow sends their on-sale ping itself (at the sale minute, into the same
    show-night channel), so this tick doesn't send a second one."""
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT DISTINCT dice_id FROM dice_follow_events "
                                "WHERE dice_id IS NOT NULL AND foreign_ = 0").fetchall()
        return {str(r["dice_id"]) for r in rows}
    except Exception:
        return set()


def headline_kind(changes):
    kinds = {c[0] for c in changes}
    return next((k for k in KIND_ORDER if k in kinds), None)


def _headline_prices(kind, changes, old, new):
    op, np_ = old.get("min_price"), new.get("min_price")
    if op is not None and np_ is not None and op != np_ and kind in ("price_up", "price_down", "restock"):
        # Name the type when exactly one type's move IS the cheapest move.
        same = {c[4] for c in changes if c[0] == kind and c[4] and c[2] == op and c[3] == np_}
        return op, np_, (same.pop() if len(same) == 1 else None)
    c = next((c for c in changes if c[0] == kind), None)
    if not c:
        return op, np_, None
    return c[2], c[3], c[4]


def run_tick(codes, notify_fn=None, dry=False, now_iso=None, muted=None, followed=None):
    """Check each event code once. notify_fn(kind, info) sends a ping (None
    = muted: state is still stored). `muted` = event codes whose pings are
    skipped (default: db.dice_muted_codes()); `followed` = codes whose
    on-sale ping comes from dice_follow (default: followed_codes()).
    Returns a summary dict."""
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    now = _parse(now_iso) or datetime.now(timezone.utc)
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    state = {} if dry else db.dice_alert_state_all()
    if muted is None:
        muted = db.dice_muted_codes()
    if followed is None:
        followed = followed_codes()
    checked = pinged = errors = silenced = flaps = 0
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
            print(f"{code} {meta.get('eventName')!r}{' [muted]' if code in muted else ''}: {json.dumps(new)}")
            continue
        try:
            db.dice_tier_log_update(code, labels.get("blocks") or {}, now_iso)
        except Exception:
            pass
        old = state.get(code)
        seen_before = (old or {}).get("_seen") or {}
        db.dice_alert_state_put(code, dict(new, _seen=seen_update(seen_before, new, now)), now_iso)
        if old is None:
            continue  # silent baseline
        changes = diff(old, new)
        if code in followed:
            changes = [c for c in changes if c[0] != "onsale"]
        changes, dropped = drop_flaps(changes, seen_before, now)
        if dropped:
            flaps += 1
        kind = headline_kind(changes)
        if not kind or notify_fn is None:
            continue
        if code in muted:
            silenced += 1
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
            "lines": [c[1] for c in changes],
            "sold_types": [c[4] for c in changes if c[0] == "type_soldout" and c[4]],
            # Old -> new for the headline. The cheapest buyable price when it
            # moved; else the headline change's own (e.g. one type's edit).
            **dict(zip(("headline_old", "headline_new", "headline_type"),
                       _headline_prices(kind, changes, old, new))),
        })
        pinged += 1
    return {"checked": checked, "pinged": pinged, "muted": silenced,
            "flaps_silenced": flaps, "errors": errors}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    db.init()
    codes = sys.argv[1:] or [str(r["code"]) for r in db.market_manual_all() if r["source"] == "dice"]
    print(run_tick(codes, dry=True))
