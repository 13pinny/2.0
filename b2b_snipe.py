"""B2B snipes: Discord pings when broker listings hit a price.

Three platforms, two transports:

  * Automatiq (Lysted's broker-to-broker site) and the TickPick broker portal
    need a logged-in browser, so the home relay laptop reads them and POSTs
    normalized listings to /api/snipe/relay (see `ingest`). The server never
    logs into either.
  * CrowdVolt's order book (asks AND buy offers) is read from the server over
    plain HTTP with the separate "watch" CrowdVolt account
    (cv_auth profile "watch") - never the selling account, which is the
    user's explicit rule. cv_auth refuses a watch cookie that turns out to
    belong to the main account.

Model:
  snipe_events  - one row per show; cv_event_uqid / automatiq_ref /
                  tickpick_ref say where to look. Per-event check interval
                  and active hours override the global settings.
  snipe_rules   - hang off an event.
                    scope 'b2b' = Automatiq AND TickPick with ONE rule (the
                    point: set it once from either site).
                    scope 'cv'  = CrowdVolt.
                  kinds:
                    under      - an ask at or below `price` (all-in, per
                                 ticket) with qty >= min_qty
                    offer_over - (cv) a buy offer at or above `price`
                    arb        - (cv) a CrowdVolt offer minus the cheapest
                                 matching Automatiq/TickPick ask >= `price`
                                 (buy the B2B ticket, fill the CV order)
                  sections: [] = any; "zone:ga" / "zone:ga+" / "zone:vip"
                  match by name family across sites; anything else is an
                  exact (case-insensitive) section name.
                  include_unmapped: TickPick "unmapped listings" match the
                  rule whatever their section text says.
  snipe_snapshots - latest read per (event, platform); a failed read keeps
                  the last good listings and records the error.
  snipe_seen    - per rule, what was already pinged and at what value, so a
                  listing pings once and again only when it gets better.

All prices are USD, all-in per ticket (that's what the broker sites show).
"""
import json
import os
import re
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import db

NY = ZoneInfo("America/New_York")

PLATFORMS = ("automatiq", "tickpick", "cv")
B2B_PLATFORMS = ("automatiq", "tickpick")
SCOPES = {"b2b": B2B_PLATFORMS, "cv": ("cv",)}
KINDS = ("under", "offer_over", "arb")
PLATFORM_LABEL = {"automatiq": "Automatiq", "tickpick": "TickPick", "cv": "CrowdVolt"}

SET_ENABLED = "snipe_enabled"
SET_INTERVAL = "snipe_interval_seconds"
SET_HOURS = "snipe_active_hours"
DEFAULT_INTERVAL = int(os.environ.get("KARTIS_SNIPE_INTERVAL_SECONDS", "60"))
MIN_INTERVAL = 15
MAX_PINGS_PER_EVENT = 6
SEEN_FORGET_HOURS = 6        # a listing gone this long pings again if it returns
DISCORD_CATEGORY = "B2B Snipes"

CV_BOOK_URL = "https://what.crowdvolt.com/api/book/get?event_uqid={}"


class SnipeError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat()


def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _rows(sql, args=()):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --- section zones ------------------------------------------------------

# Every site spells GA differently ("GA", "General Admission", "Floor GA",
# "GA Floor", "Gen Adm"); a zone rule matches the whole family. Checked in
# this order, so "VIP GA" is vip and "GA Early Entry" is ga+.
ZONES = ("ga+", "vip", "ga")
ZONE_LABEL = {"ga": "GA", "ga+": "GA+ / Early Entry", "vip": "VIP"}
_GA_PLUS_RE = re.compile(r"\bga\s*\+|\bga plus\b|\bearly entry\b|\bpriority entry\b|\bga priority\b")
_VIP_RE = re.compile(r"\bvip\b")
_GA_RE = re.compile(r"^(ga|general|general admission|gen adm|gen admission|"
                    r"standing|floor)$|\bga\b|\bgeneral admission\b|\bgen\.? adm")


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w+ ]", " ", (text or "").lower())).strip()


def zone_of(section):
    t = _norm(section)
    if not t:
        return None
    if _GA_PLUS_RE.search(t):
        return "ga+"
    if _VIP_RE.search(t):
        return "vip"
    if _GA_RE.search(t):
        return "ga"
    return None


def section_matches(wanted, listing, include_unmapped=False):
    if include_unmapped and listing.get("unmapped"):
        return True
    if not wanted:
        return True
    sec = listing.get("section") or ""
    for w in wanted:
        if w.startswith("zone:"):
            if zone_of(sec) == w[5:]:
                return True
        elif _norm(w) == _norm(sec):
            return True
    return False


def same_bucket(a_section, b_section):
    """Arbitrage pairing: same zone family, or the same section name."""
    za, zb = zone_of(a_section), zone_of(b_section)
    if za and za == zb:
        return True
    return bool(_norm(a_section)) and _norm(a_section) == _norm(b_section)


# --- settings -----------------------------------------------------------

def settings():
    try:
        interval = int(db.setting_get(SET_INTERVAL) or DEFAULT_INTERVAL)
    except ValueError:
        interval = DEFAULT_INTERVAL
    return {
        "enabled": db.setting_get_bool(SET_ENABLED, True),
        "interval_seconds": max(MIN_INTERVAL, interval),
        "active_hours": db.setting_get(SET_HOURS) or "",
    }


def save_settings(body):
    now = _iso()
    if "enabled" in body:
        db.setting_set(SET_ENABLED, "1" if body["enabled"] else "0", now)
    if "interval_seconds" in body:
        db.setting_set(SET_INTERVAL, max(MIN_INTERVAL, int(body["interval_seconds"])), now)
    if "active_hours" in body:
        db.setting_set(SET_HOURS, parse_hours(body["active_hours"]) or "", now)
    return settings()


def parse_hours(text):
    """'09:00-23:30' (NY) -> normalized string, '' = all day. Raises on junk."""
    t = (text or "").strip()
    if not t:
        return ""
    m = re.fullmatch(r"(\d{1,2})(?::(\d\d))?\s*-\s*(\d{1,2})(?::(\d\d))?", t)
    if not m:
        raise SnipeError(f"active hours must look like 09:00-23:00, got {text!r}")
    h1, m1, h2, m2 = int(m[1]), int(m[2] or 0), int(m[3]), int(m[4] or 0)
    if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and m1 < 60 and m2 < 60):
        raise SnipeError(f"bad time in {text!r}")
    return f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"


def in_hours(spec, now=None):
    if not spec:
        return True
    a, b = spec.split("-")
    now_ny = (now or _now()).astimezone(NY)
    cur = now_ny.hour * 60 + now_ny.minute
    start = int(a[:2]) * 60 + int(a[3:])
    end = int(b[:2]) * 60 + int(b[3:])
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end            # wraps midnight


def effective(event, glob=None):
    glob = glob or settings()
    return {
        "interval_seconds": max(MIN_INTERVAL, event.get("interval_seconds") or glob["interval_seconds"]),
        "active_hours": event.get("active_hours") if event.get("active_hours") is not None
        else glob["active_hours"],
    }


# --- events + rules CRUD ------------------------------------------------

CV_URL_RE = re.compile(r"crowdvolt\.com/(?:event|events)/([\w-]+)", re.I)
UQID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def get_event(event_id):
    rows = _rows("SELECT * FROM snipe_events WHERE id = ?", (event_id,))
    return rows[0] if rows else None


def list_events():
    return _rows("SELECT * FROM snipe_events ORDER BY COALESCE(event_date, '9999'), name")


def rules_for(event_id=None):
    if event_id:
        rows = _rows("SELECT * FROM snipe_rules WHERE event_id = ? ORDER BY created_at", (event_id,))
    else:
        rows = _rows("SELECT * FROM snipe_rules ORDER BY created_at")
    for r in rows:
        try:
            r["sections"] = json.loads(r.get("sections_json") or "[]")
        except ValueError:
            r["sections"] = []
    return rows


def _clean(v):
    v = (v or "").strip() if isinstance(v, str) else v
    return v or None


def save_event(body):
    """Create (no id) or update an event. Returns the stored row."""
    now = _iso()
    fields = {}
    for k in ("name", "event_date", "venue", "automatiq_ref", "tickpick_ref", "cv_url"):
        if k in body:
            fields[k] = _clean(body[k])
    if "cv_event_uqid" in body:
        u = _clean(body["cv_event_uqid"])
        if u and not UQID_RE.match(u):
            raise SnipeError(f"that doesn't look like a CrowdVolt event id: {u!r}")
        fields["cv_event_uqid"] = u
    if fields.get("event_date"):
        try:
            fields["event_date"] = datetime.fromisoformat(fields["event_date"][:10]).date().isoformat()
        except ValueError:
            raise SnipeError("event date must be YYYY-MM-DD")
    if "interval_seconds" in body:
        v = body["interval_seconds"]
        fields["interval_seconds"] = max(MIN_INTERVAL, int(v)) if v not in (None, "", 0, "0") else None
    if "active_hours" in body:
        v = body["active_hours"]
        fields["active_hours"] = None if v is None else parse_hours(v)
    for k in ("paused", "muted"):
        if k in body:
            fields[k] = 1 if body[k] else 0
    event_id = body.get("id")
    with db.connect() as conn:
        if event_id:
            if not fields:
                return get_event(event_id)
            fields["updated_at"] = now
            sets = ", ".join(f"{k} = ?" for k in fields)
            n = conn.execute(f"UPDATE snipe_events SET {sets} WHERE id = ?",
                             (*fields.values(), event_id)).rowcount
            if not n:
                raise SnipeError("no such event")
        else:
            if not fields.get("name"):
                raise SnipeError("an event needs a name")
            event_id = "sne-" + secrets.token_hex(5)
            fields.update(id=event_id, created_at=now)
            cols = ", ".join(fields)
            conn.execute(f"INSERT INTO snipe_events ({cols}) VALUES ({', '.join('?' * len(fields))})",
                         tuple(fields.values()))
    return get_event(event_id)


def delete_event(event_id):
    with db.connect() as conn:
        rule_ids = [r[0] for r in conn.execute("SELECT id FROM snipe_rules WHERE event_id = ?", (event_id,))]
        for rid in rule_ids:
            conn.execute("DELETE FROM snipe_seen WHERE rule_id = ?", (rid,))
        conn.execute("DELETE FROM snipe_rules WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM snipe_snapshots WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM snipe_events WHERE id = ?", (event_id,))


def save_rule(body):
    now = _iso()
    rule_id = body.get("id")
    fields = {}
    if not rule_id:
        if not get_event(body.get("event_id") or ""):
            raise SnipeError("rule needs an existing event_id")
        fields["event_id"] = body["event_id"]
    if "scope" in body or not rule_id:
        scope = body.get("scope") or "b2b"
        if scope not in SCOPES:
            raise SnipeError(f"scope must be one of {sorted(SCOPES)}")
        fields["scope"] = scope
    if "kind" in body or not rule_id:
        kind = body.get("kind") or "under"
        if kind not in KINDS:
            raise SnipeError(f"kind must be one of {KINDS}")
        fields["kind"] = kind
    if "price" in body or not rule_id:
        try:
            price = float(body.get("price"))
        except (TypeError, ValueError):
            raise SnipeError("rule needs a price")
        if price < 0:
            raise SnipeError("price can't be negative")
        fields["price"] = round(price, 2)
    if "min_qty" in body or not rule_id:
        fields["min_qty"] = max(1, int(body.get("min_qty") or 1))
    if "sections" in body or not rule_id:
        secs = body.get("sections") or []
        if isinstance(secs, str):
            secs = [s for s in re.split(r"[,\n]", secs)]
        secs = [s.strip() for s in secs if s and s.strip()]
        for s in secs:
            if s.startswith("zone:") and s[5:] not in ZONES:
                raise SnipeError(f"unknown zone {s!r} (zones: {', '.join(ZONES)})")
        fields["sections_json"] = json.dumps(list(dict.fromkeys(secs)))
    for k in ("include_unmapped", "enabled"):
        if k in body:
            fields[k] = 1 if body[k] else 0
    kind = fields.get("kind")
    scope = fields.get("scope")
    if rule_id and (kind is None or scope is None):
        cur = _rows("SELECT kind, scope FROM snipe_rules WHERE id = ?", (rule_id,))
        if not cur:
            raise SnipeError("no such rule")
        kind = kind or cur[0]["kind"]
        scope = scope or cur[0]["scope"]
    if kind in ("offer_over", "arb") and scope != "cv":
        # offers only exist on CrowdVolt; arb is anchored on a CV offer
        fields["scope"] = scope = "cv"
    with db.connect() as conn:
        if rule_id:
            sets = ", ".join(f"{k} = ?" for k in fields)
            if fields:
                n = conn.execute(f"UPDATE snipe_rules SET {sets} WHERE id = ?",
                                 (*fields.values(), rule_id)).rowcount
                if not n:
                    raise SnipeError("no such rule")
            # a changed threshold should re-evaluate from scratch
            conn.execute("DELETE FROM snipe_seen WHERE rule_id = ?", (rule_id,))
        else:
            rule_id = "snr-" + secrets.token_hex(5)
            fields.update(id=rule_id, created_at=now)
            fields.setdefault("enabled", 1)
            cols = ", ".join(fields)
            conn.execute(f"INSERT INTO snipe_rules ({cols}) VALUES ({', '.join('?' * len(fields))})",
                         tuple(fields.values()))
    return next(r for r in rules_for() if r["id"] == rule_id)


def delete_rule(rule_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM snipe_seen WHERE rule_id = ?", (rule_id,))
        conn.execute("DELETE FROM snipe_rules WHERE id = ?", (rule_id,))


# --- snapshots ----------------------------------------------------------

def snapshots(event_id):
    out = {}
    for r in _rows("SELECT * FROM snipe_snapshots WHERE event_id = ?", (event_id,)):
        try:
            book = json.loads(r.get("listings_json") or "null")
        except ValueError:
            book = None
        r["book"] = book if isinstance(book, dict) else None
        out[r["platform"]] = r
    return out


def _store_snapshot(event_id, platform, book=None, error=None):
    now = _iso()
    with db.connect() as conn:
        if error is None:
            conn.execute(
                "INSERT INTO snipe_snapshots (event_id, platform, listings_json, fetched_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(event_id, platform) DO UPDATE SET "
                "listings_json = excluded.listings_json, fetched_at = excluded.fetched_at",
                (event_id, platform, json.dumps(book), now))
        else:
            conn.execute(
                "INSERT INTO snipe_snapshots (event_id, platform, error, error_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(event_id, platform) DO UPDATE SET "
                "error = excluded.error, error_at = excluded.error_at",
                (event_id, platform, str(error)[:500], now))


def last_attempt(snap):
    times = [t for t in (_parse_iso(snap.get("fetched_at")), _parse_iso(snap.get("error_at"))) if t]
    return max(times) if times else None


def is_fresh(snap, event, glob=None, now=None):
    """A snapshot good enough to pair against (arb): read successfully within
    3 check intervals (at least 5 minutes), and no newer error."""
    if not snap or not snap.get("book"):
        return False
    got = _parse_iso(snap.get("fetched_at"))
    if not got:
        return False
    err = _parse_iso(snap.get("error_at"))
    if err and err > got:
        return False
    window = max(300, 3 * effective(event, glob)["interval_seconds"])
    return ((now or _now()) - got).total_seconds() <= window


def _norm_listing(r):
    """Coerce one listing to {id, section, row, qty, price, unmapped, url}."""
    try:
        price = float(r.get("price"))
    except (TypeError, ValueError):
        return None
    lid = str(r.get("id") or "").strip()
    if not lid:
        return None
    try:
        qty = int(r.get("qty")) if r.get("qty") not in (None, "") else None
    except (TypeError, ValueError):
        qty = None
    return {
        "id": lid,
        "section": str(r.get("section") or "").strip(),
        "row": str(r.get("row") or "").strip() or None,
        "qty": qty,
        "price": round(price, 2),
        "unmapped": bool(r.get("unmapped")),
        "url": r.get("url") or None,
        "note": (str(r.get("note"))[:120] if r.get("note") else None),
    }


def _qty_ok(listing, min_qty):
    q = listing.get("qty")
    return q is None or q >= min_qty


# --- CrowdVolt ----------------------------------------------------------

_cv_lock = threading.Lock()
_cv_auth = None


def _cv():
    global _cv_auth
    import cv_auth
    with _cv_lock:
        if _cv_auth is None:
            _cv_auth = cv_auth.CvAuth(profile="watch")
        return _cv_auth


def _cv_row(r, side):
    """One CrowdVolt book row -> listing. Asks carry `price` (the seller's
    price) and `all_in_price` (what a buyer pays, an integer); for an ask we
    compare the ALL-IN price - that's what buying it costs. For a buy offer
    the row's `price` is the offer amount as the page shows it."""
    if side == "ask":
        price = r.get("all_in_price") if r.get("all_in_price") is not None else r.get("price")
    else:
        price = r.get("price") if r.get("price") is not None else r.get("all_in_price")
    return _norm_listing({
        "id": r.get("uqid") or r.get("bid_uqid") or r.get("ask_uqid"),
        "section": r.get("ticket_type"),
        "qty": r.get("qty") if r.get("qty") is not None else r.get("qty_left"),
        "price": price,
    })


def fetch_cv(event_uqid):
    """The CrowdVolt book for one event as {"asks": [...], "bids": [...]}."""
    import cv_auth
    try:
        status, raw = _cv().call(CV_BOOK_URL.format(urllib.parse.quote(event_uqid)))
    except cv_auth.CvAuthError as e:
        raise SnipeError(f"CrowdVolt watch account: {e}")
    if status != 200:
        raise SnipeError(f"CrowdVolt book -> HTTP {status}: {raw[:160]}")
    try:
        data = json.loads(raw)
    except ValueError:
        raise SnipeError(f"CrowdVolt book was not JSON: {raw[:120]}")
    if not isinstance(data, dict) or not ({"buy", "sell"} & set(data)):
        # never read an unexpected shape as "empty book"
        raise SnipeError(f"unexpected CrowdVolt book shape: {str(data)[:160]}")
    asks = [x for x in (_cv_row(r, "ask") for r in data.get("sell") or []) if x]
    bids = [x for x in (_cv_row(r, "bid") for r in data.get("buy") or []) if x]
    return {"asks": asks, "bids": bids}


# --- rule evaluation ----------------------------------------------------

def evaluate(event, rules, snaps, glob=None, now=None):
    """Every (rule, listing) currently meeting a rule. Pure: no DB writes.
    Returns hits: {rule, key, value, better ('lower'|'higher'), kind,
    platform, listing, pair?}."""
    hits = []
    for rule in rules:
        if not rule.get("enabled"):
            continue
        secs = rule.get("sections") or []
        unm = bool(rule.get("include_unmapped"))
        mq = rule.get("min_qty") or 1
        kind = rule["kind"]
        if kind == "under":
            for plat in SCOPES.get(rule["scope"], ()):
                book = (snaps.get(plat) or {}).get("book") or {}
                for a in book.get("asks") or []:
                    if a["price"] <= rule["price"] and _qty_ok(a, mq) \
                            and section_matches(secs, a, unm):
                        hits.append({"rule": rule, "kind": kind, "platform": plat,
                                     "key": f"{plat}:{a['id']}", "value": a["price"],
                                     "better": "lower", "listing": a})
        elif kind == "offer_over":
            book = (snaps.get("cv") or {}).get("book") or {}
            for b in book.get("bids") or []:
                if b["price"] >= rule["price"] and _qty_ok(b, mq) and section_matches(secs, b):
                    hits.append({"rule": rule, "kind": kind, "platform": "cv",
                                 "key": f"cvbid:{b['id']}", "value": b["price"],
                                 "better": "higher", "listing": b})
        elif kind == "arb":
            cv_snap = snaps.get("cv")
            if not is_fresh(cv_snap, event, glob, now):
                continue
            asks = []
            for plat in B2B_PLATFORMS:
                s = snaps.get(plat)
                if is_fresh(s, event, glob, now):
                    asks += [(plat, a) for a in s["book"].get("asks") or [] if _qty_ok(a, mq)]
            for b in cv_snap["book"].get("bids") or []:
                if not (_qty_ok(b, mq) and section_matches(secs, b)):
                    continue
                pair = [(p, a) for p, a in asks
                        if same_bucket(b["section"], a["section"])
                        or (unm and a.get("unmapped"))]
                if not pair:
                    continue
                plat, a = min(pair, key=lambda pa: pa[1]["price"])
                spread = round(b["price"] - a["price"], 2)
                if spread >= rule["price"]:
                    hits.append({"rule": rule, "kind": kind, "platform": plat,
                                 "key": f"arb:{b['id']}:{plat}:{a['id']}", "value": spread,
                                 "better": "higher", "listing": a, "bid": b})
    return hits


def _filter_new(hits, now_iso):
    """Drop hits already pinged at an equal-or-better value; record the rest
    (and refresh last_at on every current hit). Forget keys unseen for
    SEEN_FORGET_HOURS so a listing that leaves and comes back pings again."""
    fresh = []
    with db.connect() as conn:
        for h in hits:
            row = conn.execute("SELECT value FROM snipe_seen WHERE rule_id = ? AND hit_key = ?",
                               (h["rule"]["id"], h["key"])).fetchone()
            if row is None:
                fresh.append(h)
                conn.execute("INSERT INTO snipe_seen (rule_id, hit_key, value, first_at, last_at) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (h["rule"]["id"], h["key"], h["value"], now_iso, now_iso))
                continue
            old = row["value"]
            improved = old is None or (h["value"] < old - 0.009 if h["better"] == "lower"
                                       else h["value"] > old + 0.009)
            if improved:
                h["was"] = old
                fresh.append(h)
                conn.execute("UPDATE snipe_seen SET value = ?, last_at = ? WHERE rule_id = ? "
                             "AND hit_key = ?", (h["value"], now_iso, h["rule"]["id"], h["key"]))
            else:
                conn.execute("UPDATE snipe_seen SET last_at = ? WHERE rule_id = ? AND hit_key = ?",
                             (now_iso, h["rule"]["id"], h["key"]))
        cutoff = (_now() - timedelta(hours=SEEN_FORGET_HOURS)).isoformat()
        conn.execute("DELETE FROM snipe_seen WHERE last_at < ?", (cutoff,))
    return fresh


# --- pings --------------------------------------------------------------

def channel_name(event):
    import discord_bot
    slug = discord_bot.slugify_label(event.get("name") or "") or "event"
    d = event.get("event_date")
    if d:
        try:
            dd = datetime.fromisoformat(d).date()
            slug += f"-{dd.strftime('%b').lower()}-{dd.day}"
        except ValueError:
            pass
    return ("snipe-" + slug)[:95]


def _webhook(event):
    import notify
    url = None
    try:
        import discord_bot
        url = discord_bot.webhook_for(channel_name(event), category=DISCORD_CATEGORY)
    except Exception as e:
        print(f"[snipe] channel for {event.get('name')!r} failed: {e}", flush=True)
    return url or notify._discord_webhook("drops")


def _money(v):
    return f"${v:,.0f}" if abs(v - round(v)) < 0.005 else f"${v:,.2f}"


def _where(l):
    bits = [l.get("section") or "?"]
    if l.get("row"):
        bits.append(f"row {l['row']}")
    if l.get("unmapped"):
        bits.append("(unmapped)")
    return " ".join(bits)


def _qty(l):
    return f" ×{l['qty']}" if l.get("qty") else ""


def describe(hit, event):
    """(headline, detail line, link) for one hit."""
    l, plat, kind = hit["listing"], PLATFORM_LABEL[hit["platform"]], hit["kind"]
    was = f" (was {_money(hit['was'])})" if hit.get("was") is not None else ""
    if kind == "under":
        head = f"🎯 {plat}: {_where(l)} {_money(l['price'])}{_qty(l)}{was}"
        detail = f"under your {_money(hit['rule']['price'])}"
    elif kind == "offer_over":
        head = f"💰 CrowdVolt offer: {_where(l)} {_money(l['price'])}{_qty(l)}{was}"
        detail = f"over your {_money(hit['rule']['price'])}"
    else:
        b = hit["bid"]
        head = (f"⚡ ARB {_money(hit['value'])}/tkt: buy {plat} {_where(l)} {_money(l['price'])}{_qty(l)}"
                f" → CV offer {_money(b['price'])}{_qty(b)}{was}")
        detail = f"spread ≥ your {_money(hit['rule']['price'])}"
    link = l.get("url") or _platform_link(event, hit["platform"])
    return head, detail, link


def _platform_link(event, platform):
    if platform == "cv":
        return event.get("cv_url")
    ref = event.get(f"{platform}_ref") or ""
    return ref if ref.startswith("http") else None


def send_pings(event, hits):
    import notify
    if not hits:
        return []
    order = {"arb": 0, "under": 1, "offer_over": 2}
    hits = sorted(hits, key=lambda h: (order[h["kind"]], h["value"] if h["better"] == "lower" else -h["value"]))
    shown, extra = hits[:MAX_PINGS_PER_EVENT], hits[MAX_PINGS_PER_EVENT:]
    url = _webhook(event)
    results = []
    when = ""
    if event.get("event_date"):
        try:
            dd = datetime.fromisoformat(event["event_date"]).date()
            when = f" · {dd.strftime('%a %b')} {dd.day}"
        except ValueError:
            pass
    for i, h in enumerate(shown):
        head, detail, link = describe(h, event)
        lines = [detail]
        if h["kind"] == "arb":
            cv_link = event.get("cv_url")
            if cv_link:
                lines.append(f"[CrowdVolt]({cv_link})")
        if i == len(shown) - 1 and extra:
            lines.append(f"…and {len(extra)} more matching listings - see {notify.BASE_URL}/snipes")
        embed = {"title": head[:250], "description": "\n".join(lines)[:2000],
                 "footer": {"text": f"{event.get('name') or ''}{when}"[:200]},
                 "color": 0xF0B000 if h["kind"] == "arb" else 0x1A7F37}
        if link:
            embed["url"] = link
        res = notify._post_discord(url, {"content": f"{head} — {event.get('name') or ''}"[:1900],
                                         "embeds": [embed]}) if url else "skipped (no webhook)"
        results.append(res)
        with db.connect() as conn:
            conn.execute("INSERT INTO snipe_pings (event_id, rule_id, kind, title, detail, at, result) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (event["id"], h["rule"]["id"], h["kind"], head, detail, _iso(), res))
    return results


# --- the tick + relay ---------------------------------------------------

_tick_lock = threading.Lock()
last_run = {"at": None, "checked": 0, "pinged": 0, "errors": [], "running": False}


def _process(event, glob, now_iso):
    """Re-evaluate one event's rules against its latest snapshots and ping
    what's new. Muting still records what was seen (no backlog on unmute)."""
    rules = rules_for(event["id"])
    if not rules:
        return 0
    fresh = _filter_new(evaluate(event, rules, snapshots(event["id"]), glob), now_iso)
    if not fresh or event.get("muted") or db.setting_get_bool("master_muted", False):
        return 0
    send_pings(event, fresh)
    return len(fresh)


def ingest(event_id, platform, listings=None, bids=None, error=None):
    """Store one platform's read for an event (from the relay or the CV
    fetch) and ping anything new. `error` records a failed read and keeps the
    last good listings."""
    if platform not in PLATFORMS:
        raise SnipeError(f"platform must be one of {PLATFORMS}")
    event = get_event(event_id)
    if not event:
        raise SnipeError("no such event")
    if error:
        _store_snapshot(event_id, platform, error=error)
        return {"stored": False, "error": str(error)[:200]}
    if not isinstance(listings, list):
        raise SnipeError("listings must be a list")
    asks = [x for x in (_norm_listing(r) for r in listings if isinstance(r, dict)) if x]
    offers = [x for x in (_norm_listing(r) for r in (bids or []) if isinstance(r, dict)) if x]
    _store_snapshot(event_id, platform, {"asks": asks, "bids": offers})
    pinged = 0
    if not event.get("paused") and not db.setting_get_bool("master_paused", False):
        pinged = _process(event, settings(), _iso())
    return {"stored": True, "asks": len(asks), "bids": len(offers), "pinged": pinged}


def due(event, platform, glob=None, now=None, snaps=None):
    """Should `platform` be read for `event` now?"""
    glob = glob or settings()
    if not glob["enabled"] or event.get("paused"):
        return False
    ref = {"cv": "cv_event_uqid", "automatiq": "automatiq_ref",
           "tickpick": "tickpick_ref"}[platform]
    if not event.get(ref):
        return False
    if not rules_for(event["id"]):
        return False
    if event.get("event_date"):
        try:
            if datetime.fromisoformat(event["event_date"]).date() < (now or _now()).astimezone(NY).date():
                return False
        except ValueError:
            pass
    eff = effective(event, glob)
    if not in_hours(eff["active_hours"], now):
        return False
    snap = (snaps if snaps is not None else snapshots(event["id"])).get(platform) or {}
    last = last_attempt(snap)
    return last is None or ((now or _now()) - last).total_seconds() >= eff["interval_seconds"] - 2


def relay_targets(platforms=B2B_PLATFORMS):
    """What the laptop relay should read now: [{event_id, platform, ref, name}]."""
    glob = settings()
    out = []
    for ev in list_events():
        snaps = snapshots(ev["id"])
        for p in platforms:
            if p in B2B_PLATFORMS and due(ev, p, glob, snaps=snaps):
                out.append({"event_id": ev["id"], "platform": p, "ref": ev[f"{p}_ref"],
                            "name": ev["name"], "event_date": ev.get("event_date")})
    return out


def run_tick():
    """Scheduler job (every ~15s): read CrowdVolt for every due event. The
    B2B sites arrive through `ingest` from the relay."""
    if not _tick_lock.acquire(blocking=False):
        return last_run
    try:
        last_run.update(running=True)
        if db.setting_get_bool("master_paused", False):
            return last_run
        glob = settings()
        checked = pinged = 0
        errors = []
        for ev in list_events():
            if not due(ev, "cv", glob):
                continue
            checked += 1
            try:
                book = fetch_cv(ev["cv_event_uqid"])
            except SnipeError as e:
                errors.append(f"{ev['name']}: {e}")
                _store_snapshot(ev["id"], "cv", error=str(e))
                continue
            except Exception as e:
                errors.append(f"{ev['name']}: {type(e).__name__}: {e}")
                _store_snapshot(ev["id"], "cv", error=f"{type(e).__name__}: {e}")
                continue
            _store_snapshot(ev["id"], "cv", book)
            pinged += _process(ev, glob, _iso())
        last_run.update(at=_iso(), checked=checked, pinged=pinged, errors=errors[:20])
        if errors:
            print(f"[snipe] {len(errors)} read error(s): {errors[0]}", flush=True)
        return last_run
    finally:
        last_run["running"] = False
        _tick_lock.release()


def overview():
    """Everything /snipes renders."""
    import cv_auth
    glob = settings()
    events = []
    now = _now()
    for ev in list_events():
        snaps = snapshots(ev["id"])
        plats = {}
        for p in PLATFORMS:
            s = snaps.get(p)
            if not s:
                continue
            book = s.get("book") or {}
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            plats[p] = {
                "fetched_at": s.get("fetched_at"), "error": s.get("error"),
                "error_at": s.get("error_at"), "fresh": is_fresh(s, ev, glob, now),
                "asks": sorted(asks, key=lambda a: a["price"])[:40],
                "bids": sorted(bids, key=lambda b: -b["price"])[:20],
                "n_asks": len(asks), "n_bids": len(bids),
            }
        sections = sorted({a["section"] for p in plats.values() for a in p["asks"] + p["bids"]
                           if a.get("section")})
        events.append({**ev, "rules": rules_for(ev["id"]), "platforms": plats,
                       "sections": sections, "effective": effective(ev, glob)})
    pings = _rows("SELECT p.*, e.name AS event_name FROM snipe_pings p "
                  "LEFT JOIN snipe_events e ON e.id = p.event_id ORDER BY p.id DESC LIMIT 40")
    return {"settings": glob, "events": events, "pings": pings, "zones": ZONE_LABEL,
            "cv_watch": cv_auth.session_status("watch"), "last_run": dict(last_run)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="B2B snipes: probe / run once")
    ap.add_argument("--cv", metavar="EVENT_UQID", help="print one CrowdVolt book")
    ap.add_argument("--tick", action="store_true", help="run one tick (pings!)")
    ap.add_argument("--zone", metavar="TEXT", help="show which zone a section name falls in")
    a = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    if a.zone:
        print(zone_of(a.zone))
    elif a.cv:
        book = fetch_cv(a.cv)
        for side in ("asks", "bids"):
            print(f"{side}: {len(book[side])}")
            for r in sorted(book[side], key=lambda r: r["price"], reverse=side == "bids")[:10]:
                print(f"  {r['section']:<30} {_money(r['price']):>8}  x{r['qty']}")
    elif a.tick:
        db.init()
        print(json.dumps(run_tick(), indent=2, default=str))
    else:
        ap.print_help()
