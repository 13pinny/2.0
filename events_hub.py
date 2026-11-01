"""/events: one list of every tracked show, whatever tracks it.

The trackers grew up as separate pages (/edm, /marquee, /frontgate,
/universe, /liv, the /dice tracker, /watchers, /pacha, /checkin), each with
its own row shape, add box and idea of "status". This module turns their
existing API payloads into ONE row shape -- it fetches nothing itself and
keeps no state, so every source's own logic stays where it is and /events is
just a view. app.py gathers the payloads (`api_events`) and hands them here.

Row shape (every source):
    id        "<kind>:<key>" -- stable, what /api/events/action takes
    kind      edm | marquee | frontgate | universe | liv | dice | watcher
              | pacha | checkin  (which detail view the page draws)
    source    the platform (posh, tao, dice, ticketmaster, kupat, ...)
    name, venue, when (display text), night (venue-local YYYY-MM-DD or None)
    status    on_sale | sold_out | not_yet | closed | unknown
    price, currency, trend (up | down | None), left / sold (None = unknown)
    url       buy / event link
    paused, muted   bool, or None where the source has no such control
    controls  the actions this row supports (pause, mute, remove, alert)
    badges    extra facts ("also a drop watcher", "LIV pair", "face price")
    health    {state, reason, last_good, stale_after_min} -- see `_health`
    archived  past show, or removed by the site
    detail    the source's own row, untouched, for the expanded view

`None` means UNKNOWN, never zero -- the same rule as the EDM stack.
"""
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
IL = ZoneInfo("Asia/Jerusalem")
US_CENTRAL = ZoneInfo("America/Chicago")

# How long a row may go without a good read before it is "stale": about
# three missed checks of its source, plus the relay's own allowance where a
# desktop relay delivers the page (eventim/shotgun 15 min, tao 30 min).
STALE_MINUTES = {
    "edm": 10, "tixr": 10, "posh": 10, "leap": 10, "frontgate": 10,
    "universe": 10, "urvenue": 15,
    "eventim": 25, "shotgun": 25, "tao": 40,
    "dice": 20, "watcher": 10, "kupat": 15, "pacha": 10,
}

# Fetch errors that mean the event is gone from the site, not that we
# couldn't read it: archived, never alarmed about.
# Only an explicit "gone": a 404 is too often something else (posh answers
# 404 to a wrong presale code), and that needs attention, not the archive.
_REMOVED_RE = re.compile(r"\b410\b|content removed|no longer available", re.I)
_BLOCKED_RE = re.compile(r"challenge|cloudflare|captcha|datadome|checkpoint|403|blocked", re.I)
_RELAY_RE = re.compile(r"relay last sent", re.I)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_TEXT_DATE_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_ISO_DAY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# --------------------------------------------------------------- helpers ---
def _parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _strip_err(err):
    """edm_seen_events.last_error is "<message> @ <iso>"."""
    if not err:
        return None, None
    msg, _, at = str(err).rpartition(" @ ")
    if not msg:
        return str(err), None
    return msg, _parse_iso(at)


def night_of(date_text=None, start=None, tz=US_CENTRAL):
    """The show's VENUE-LOCAL calendar date as YYYY-MM-DD.

    Sources store start times inconsistently (CLAUDE.md / the /events map):
    some a venue-local display string, some a bare date, some a UTC instant
    -- and a 10pm show stored in UTC lands on the next day. So a date
    written out in `date_text` wins, then a bare or zone-less `start`, and a
    zoned instant is converted to `tz` (Central for US shows: right for
    East/Central rooms, and it never pushes a West-coast night into the next
    day the way Eastern would)."""
    txt = (date_text or "").strip()
    m = _ISO_DAY_RE.match(txt)
    if m:
        return m.group(1)
    m = _TEXT_DATE_RE.search(txt)
    if m and m.group(1)[:3].lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))).isoformat()
        except ValueError:
            pass
    s = str(start or "").strip()
    if not s:
        return None
    if len(s) == 10 and _ISO_DAY_RE.match(s):
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        m = _ISO_DAY_RE.match(s)
        return m.group(1) if m else None
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone(tz).date().isoformat()


def _trend(prices):
    """up/down from the last two DIFFERENT known prices, oldest first."""
    seq = [p for p in prices if p is not None]
    for i in range(len(seq) - 1, 0, -1):
        if seq[i] != seq[i - 1]:
            return "up" if seq[i] > seq[i - 1] else "down"
    return None


def _status(on_sale, sold_out, closed=False):
    if sold_out:
        return "sold_out"
    if on_sale:
        return "on_sale"
    if closed:
        return "closed"
    return "not_yet" if on_sale is not None else "unknown"


def _health(now, *, source_key, last_good, error=None, error_at=None,
            paused=False, pending=False):
    """{state, reason, last_good, stale_after_min}.

    state: ok | stale | error | blocked | removed | pending | paused.
    An error newer than the last good read wins; an older one is history.
    Stale = no good read within STALE_MINUTES[source_key]."""
    stale_after = STALE_MINUTES.get(source_key, 15)
    lg = last_good if isinstance(last_good, datetime) else _parse_iso(last_good)
    out = {"state": "ok", "reason": None,
           "last_good": lg.isoformat() if lg else None,
           "stale_after_min": stale_after}
    if paused:
        out["state"] = "paused"
        return out
    if error and (lg is None or error_at is None or error_at >= lg):
        msg = str(error)
        out["reason"] = msg[:240]
        if _REMOVED_RE.search(msg):
            out["state"] = "removed"
        elif _RELAY_RE.search(msg) or _BLOCKED_RE.search(msg):
            out["state"] = "blocked"
        else:
            out["state"] = "error"
        return out
    if lg is None:
        out["state"] = "pending"
        out["reason"] = "not read yet"
        return out
    if pending:
        out["state"] = "pending"
    if now - lg > timedelta(minutes=stale_after):
        out["state"] = "stale"
        mins = int((now - lg).total_seconds() // 60)
        out["reason"] = f"no good read for {_human(mins)}"
    return out


def _human(mins):
    if mins < 90:
        return f"{mins} min"
    if mins < 48 * 60:
        return f"{mins // 60} h"
    return f"{mins // 1440} days"


def _archived(night, health, today):
    if health.get("state") == "removed":
        return True
    return bool(night) and night < today


# -------------------------------------------------------- normalizers ------
def _edm_like(e, kind, now, today, *, badges=None, controls=("pause", "remove")):
    src = e.get("source") or kind
    msg, err_at = _strip_err(e.get("last_error"))
    # /marquee rows carry `releases` as a COUNT (from _tier_ladder) and the
    # rungs in `ladder`; everyone else carries the release list itself.
    rels = e.get("releases") if isinstance(e.get("releases"), list) else (
        [r for r in e.get("ladder") or [] if r.get("is_ga", True)])
    rel_prices = [r.get("price") for r in sorted(rels, key=lambda r: r.get("first_seen_at") or "")]
    if e.get("lead_price") is not None:
        rel_prices.append(e.get("lead_price"))
    health = _health(now, source_key=src, last_good=e.get("last_seen_at"),
                     error=msg, error_at=err_at, paused=bool(e.get("paused")),
                     pending=bool(e.get("tracked_only")))
    night = night_of(e.get("date_text"), e.get("start_date"))
    left = e.get("left") if "left" in e else e.get("total_available")
    b = list(badges or [])
    if src in ("shotgun",):
        b.append("face price")
    return {
        "id": f"{kind}:{e['event_id']}",
        "kind": kind,
        "source": src,
        "name": e.get("label") or e.get("name") or e.get("event_key"),
        "venue": e.get("venue"),
        "when": e.get("date_text"),
        "night": night,
        "status": _status(e.get("on_sale"), e.get("sold_out")),
        "price": e.get("min_price"),
        "currency": e.get("currency") or "USD",
        "trend": _trend(rel_prices),
        "left": left,
        "sold": e.get("sold") if "sold" in e else e.get("total_sold"),
        "url": e.get("page_url") or e.get("url"),
        "paused": bool(e.get("paused")),
        "muted": None,
        "controls": list(controls),
        "badges": b,
        "health": health,
        "archived": _archived(night, health, today),
        "detail": e,
    }


def from_edm(payload, now, today):
    return [_edm_like(e, "edm", now, today) for e in (payload or {}).get("events") or []]


def from_marquee(payload, now, today):
    rows = []
    for v in (payload or {}).get("venues") or []:
        for e in v.get("events") or []:
            e = dict(e, source="tao", venue=e.get("venue") or v.get("name"))
            r = _edm_like(e, "marquee", now, today)
            if e.get("auto"):
                r["badges"].append("from venue calendar")
            rows.append(r)
    return rows


def from_frontgate(payload, now, today):
    return [_edm_like(dict(e, source="frontgate"), "frontgate", now, today,
                      controls=("pause", "remove", "alert"))
            for e in (payload or {}).get("events") or []]


def from_universe(payload, now, today, skip_ids=()):
    rows = []
    for e in (payload or {}).get("events") or []:
        if e.get("event_id") in skip_ids:
            continue      # shown inside its LIV night instead
        rows.append(_edm_like(dict(e, source="universe"), "universe", now, today,
                              controls=("pause", "remove", "alert")))
    return rows


def from_liv(payload, now, today):
    """One row per LIV night: the UrVenue tables half + the Universe GA half."""
    rows = []
    for n in (payload or {}).get("nights") or []:
        halves = [h for h in (n.get("ga"), n.get("tables")) if h]
        if not halves:
            continue
        lead = n.get("ga") or n.get("tables")
        r = _edm_like(dict(lead, name=n.get("name") or lead.get("name"),
                           venue=n.get("venue") or lead.get("venue"),
                           date_text=n.get("date_text") or lead.get("date_text"),
                           start_date=n.get("start_date") or lead.get("start_date"),
                           page_url=n.get("page_url") or lead.get("page_url")),
                      "liv", now, today, controls=("pause", "remove", "alert"))
        r["id"] = "liv:" + "+".join(h["event_id"] for h in halves)
        r["source"] = "liv"
        r["paused"] = all(h.get("paused") for h in halves)
        # The worse half decides health: a dead table feed is still a problem.
        hs = [_edm_like(h, "liv", now, today)["health"] for h in halves]
        order = ["removed", "error", "blocked", "stale", "pending", "paused", "ok"]
        r["health"] = min(hs, key=lambda h: order.index(h["state"]) if h["state"] in order else 9)
        r["archived"] = _archived(r["night"], r["health"], today)
        r["badges"] = [b for b in ("GA" if n.get("ga") else None,
                                    "tables" if n.get("tables") else None) if b]
        r["detail"] = n
        rows.append(r)
    return rows


def from_dice(payload, now, today, watchers_by_code=None):
    watchers_by_code = watchers_by_code or {}
    rows = []
    for e in (payload or {}).get("events") or []:
        code = str(e.get("event_code"))
        fetched = e.get("fetched_at")
        lg = datetime.fromtimestamp(float(fetched), timezone.utc) if fetched else None
        status = {"selling": "on_sale", "soldout": "sold_out",
                  "upcoming": "not_yet"}.get(e.get("status"), "unknown")
        health = _health(now, source_key="dice", last_good=lg, error=e.get("error"))
        if e.get("page_only") and health["state"] == "ok":
            health["reason"] = "prices from the public page only (relay copy too old)"
        night = night_of(e.get("date_text")) or (
            datetime.fromtimestamp(e["first_date_ms"] / 1000, US_CENTRAL).date().isoformat()
            if e.get("first_date_ms") else None)
        badges = []
        w = watchers_by_code.get(code)
        if w:
            badges.append("drop watcher")
        controls = ["mute", "remove"]
        rows.append({
            "id": f"dice:{code}",
            "kind": "dice",
            "source": "dice",
            "name": e.get("name") or code,
            "venue": e.get("venue"),
            "when": e.get("date_text"),
            "night": night,
            "status": status,
            "price": e.get("min_price"),
            "currency": e.get("currency") or "USD",
            "trend": _trend([p for _, p in (e.get("ladder") or [])]),
            "left": None,
            "sold": None,
            "url": e.get("url"),
            "paused": bool(w and w.get("paused")) if w else None,
            "muted": bool(e.get("muted")),
            "controls": controls,
            "badges": badges,
            "health": health,
            "archived": bool(e.get("past")) or _archived(night, health, today),
            "detail": dict(e, watcher=w),
        })
    return rows


def from_watchers(watchers, meta_by_id, now, today, skip_dice_codes=()):
    """tm_watchers rows. `meta_by_id` = {watcher id: {night, name, url}}
    built by app.py from each source's cached labels (no network here)."""
    rows = []
    for w in watchers or []:
        src = w.get("source") or "ticketmaster"
        if src == "dice" and str(w.get("event_code")) in skip_dice_codes:
            continue      # folded into its DICE tracker row
        meta = meta_by_id.get(w["id"]) or {}
        err = w.get("last_check_error")
        last_check = _parse_iso(w.get("last_check_at"))
        # last_check_at is stamped on failures too: good only when no error.
        health = _health(now, source_key="kupat" if src == "kupat" else "watcher",
                         last_good=None if err else last_check,
                         error=err, error_at=last_check, paused=bool(w.get("paused")))
        if err is None and last_check is None:
            health = {"state": "pending", "reason": "not checked yet",
                      "last_good": None, "stale_after_min": STALE_MINUTES["watcher"]}
        seats = w.get("last_seat_count")
        night = meta.get("night")
        rows.append({
            "id": f"watcher:{w['id']}",
            "kind": "watcher",
            "source": src,
            "name": w.get("label") or meta.get("name") or f"{w.get('event_code')}/{w.get('perf_code')}",
            "venue": meta.get("venue"),
            "when": meta.get("when"),
            "night": night,
            "status": ("on_sale" if seats else "sold_out") if seats is not None else "unknown",
            "price": None,
            "currency": "ILS" if src in ("ticketmaster", "kupat", "tickchak", "barby", "tmdiscover") else "USD",
            "trend": None,
            "left": seats,
            "sold": None,
            "url": meta.get("url"),
            "paused": bool(w.get("paused")),
            "muted": bool(w.get("muted")),
            "controls": ["pause", "mute", "remove"],
            "badges": ["drop watcher"],
            "health": health,
            "archived": _archived(night, health, today),
            "detail": w,
        })
    return rows


def from_pacha(payload, now, today, pacha_error=None):
    rows = []
    for e in (payload or {}).get("events") or []:
        health = _health(now, source_key="pacha", last_good=e.get("last_seen_at"),
                         error=pacha_error)
        night = night_of(e.get("date_text"), e.get("start_date"), tz=NY)
        rows.append({
            "id": f"pacha:{e['event_id']}",
            "kind": "pacha",
            "source": "pacha",
            "name": e.get("name"),
            "venue": "Pacha NYC",
            "when": e.get("date_text"),
            "night": night,
            "status": _status(e.get("on_sale"), e.get("ga_sold_out") and not e.get("on_sale")),
            "price": e.get("ga_price"),
            "currency": "USD",
            "trend": _trend([r.get("price") for r in sorted(e.get("releases") or [],
                                                            key=lambda r: r.get("first_seen_at") or "")]
                            + [e.get("ga_price")]),
            "left": e.get("total_available"),
            "sold": e.get("sold_cum"),
            "url": e.get("buy_url"),
            "paused": None,
            "muted": None,
            "controls": [],
            "badges": ["venue calendar"],
            "health": health,
            "archived": _archived(night, health, today),
            "detail": e,
        })
    return rows


def from_checkin(payload, now, today):
    rows = []
    for e in (payload or {}).get("events") or []:
        if e.get("archived"):
            continue
        last = None
        hist = [h for t in e.get("tiers") or [] for h in (t.get("history") or [])]
        left_today = None
        if hist:
            last_day = max(h["day"] for h in hist)
            left_today = sum(h.get("remaining") or 0 for h in hist if h["day"] == last_day) or None
        health = {"state": "due" if e.get("due") else "ok",
                  "reason": (f"last check-in {e.get('days_since')} days ago"
                             if e.get("due") else None),
                  "last_good": e.get("last_checkin"), "stale_after_min": None}
        night = e.get("event_date_iso")
        rows.append({
            "id": f"checkin:{e['id']}",
            "kind": "checkin",
            "source": "check-in",
            "name": e.get("name"),
            "venue": e.get("venue"),
            "when": night,
            "night": night,
            "status": "unknown",
            "price": None,
            "currency": e.get("currency") or "USD",
            "trend": None,
            "left": left_today,
            "sold": None,
            "url": e.get("link"),
            "paused": None,
            "muted": None,
            "controls": [],
            "badges": ["hand check-in"],
            "health": health,
            "archived": bool(e.get("past")) or (bool(night) and night < today),
            "detail": e,
        })
    return rows


# ---------------------------------------------------------------- track ----
# Where a pasted link goes. Order matters: discover.ticketmaster.co.il before
# ticketmaster.co.il is implied by both being watchers here.
TRACK_ROUTES = [
    # (route, test) -- route names match app.api_events_track's dispatch
    ("dice", lambda u, h: "dice.fm" in h and "/event/" in u),
    ("watcher", lambda u, h: any(x in h for x in (
        "ticketmaster.co.il", "kupat.co.il", "tickchak.co.il", "barby.co.il",
        "hakuapp.com", "haku.ly", "houstonmarathon.com"))),
    ("liv", lambda u, h: "livnightclub.com" in h or re.search(r"/event/EVE\d", u)),
    ("frontgate", lambda u, h: "frontgatetickets.com" in h),
    ("universe", lambda u, h: "universe.com" in h),
    ("edm", lambda u, h: any(x in h for x in (
        "posh.vip", "leapevents.com", "eventim.us", "shotgun.live",
        "tixr.com", "tickets.taogroup.com"))),
]


def classify_link(text):
    """(route, url) for a pasted link, or (None, reason). Only real links:
    the old bare-slug guessing (a slug became Posh on /edm but tickchak on
    /watchers) is exactly what this box is meant to end."""
    s = (text or "").strip()
    if not s:
        return None, "Paste a link to the event page."
    url = s.split()[0]
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
            s = "https://" + s
        else:
            return None, "That doesn't look like a link. Paste the full event URL."
    m = re.match(r"^https?://([^/?#]+)", url, re.I)
    host = (m.group(1) if m else "").lower()
    for route, test in TRACK_ROUTES:
        if test(url, host):
            return route, s
    return None, (f"Kartis doesn't track {host or 'that site'} yet. Supported: DICE, Tixr, Posh, "
                  "Leap, Shotgun, Eventim US, Marquee, Frontgate, Universe, LIV, "
                  "Ticketmaster IL, kupat, tickchak, barby.")


# --------------------------------------------------------------- summary ---
ATTENTION_STATES = ("error", "blocked", "stale")


def summarize(rows):
    live = [r for r in rows if not r["archived"]]
    return {
        "tracked": len(live),
        "on_sale": sum(1 for r in live if r["status"] == "on_sale"),
        "sold_out": sum(1 for r in live if r["status"] == "sold_out"),
        "attention": sum(1 for r in live if r["health"]["state"] in ATTENTION_STATES),
        "archived": len(rows) - len(live),
    }


def health_snapshot(rows):
    """{id: (state, name, reason)} for the non-archived rows -- what the
    #health job diffs between runs."""
    return {r["id"]: (r["health"]["state"], r["name"], r["health"].get("reason"))
            for r in rows if not r["archived"]}


def health_changes(old, new):
    """(broke, recovered) lists of (id, name, state, reason) between two
    snapshots. Only transitions into/out of ATTENTION_STATES count. With no
    previous snapshot at all (first run after deploy) nothing counts -- no
    backlog; after that, a newly tracked row that starts out bad counts."""
    broke, recovered = [], []
    if not old:
        return broke, recovered
    for rid, (state, name, reason) in new.items():
        prev = old.get(rid)
        if prev is None:
            if state in ATTENTION_STATES:
                broke.append((rid, name, state, reason))
            continue
        was_bad = prev[0] in ATTENTION_STATES
        is_bad = state in ATTENTION_STATES
        if is_bad and not was_bad:
            broke.append((rid, name, state, reason))
        elif was_bad and not is_bad:
            recovered.append((rid, name, state, reason))
    return broke, recovered
