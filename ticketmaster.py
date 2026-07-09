"""Ticketmaster.co.il drop checker.

Polls the public ISM API used by the seat-map SPA to detect when new seats
become selectable for a given performance. No login, no captcha — the API
serves anonymous JSON given two custom headers (CHANNEL, CPU).

Reverse-engineered from the SPA bundle at /webclient/main.*.js. The endpoint
returns one row per currently-buyable seat:

    { "b": "<block>", "r": "<row>", "l": <seat_num>, "s": "AVAILABLE",
      "id": <internal_seat_id>, "f": <flag> }

We diff the set of (block, row, seat_num) tuples between consecutive checks
and treat anything in `new - old` as a fresh drop.
"""
import os
import re
import time
import urllib.request
import urllib.error
import json
import gzip
from datetime import datetime
from urllib.parse import urlparse


API_HOST = "https://www.ticketmaster.co.il"
ISM_PATH = "/ismapi/api/v1/seatPlans"
CHANNEL = "INTERNET"
CPU = "32100"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "iw",
    "Accept-Encoding": "gzip, deflate",
    "CHANNEL": CHANNEL,
    "CPU": CPU,
    "CLIENT_APP_ENVIRONMENT": "prod",
    "Origin": API_HOST,
    "Referer": API_HOST + "/",
}

REQUEST_TIMEOUT = 20

# /performance/{event}/{perf}/{lang_or_all}/{lang}
_PERF_RE = re.compile(r"/performance/([A-Z0-9]+)/(\d+)/", re.IGNORECASE)
# /event/{event}/{lang_or_all}/{lang}  — event-level, no perf code
_EVENT_RE = re.compile(r"/event/([A-Z0-9]+)/", re.IGNORECASE)


class TicketmasterError(RuntimeError):
    pass


# Sentinel `perf_code` used for event-level watchers — one watcher row covers
# every performance under the event. Real TM perf codes are always numeric
# (_PERF_RE above), so "ALL" can't collide with a real perf and it round-trips
# the user-facing /event/<CODE>/ALL/iw URL form. Keeping the column NOT NULL
# also preserves the UNIQUE(event_code, perf_code) constraint, which would
# silently allow duplicates if we used SQL NULL (SQLite treats NULLs as
# distinct in UNIQUE indexes).
EVENT_LEVEL_PERF = "ALL"


def parse_url(url):
    """Extract (event_code, perf_code) from a Ticketmaster.co.il URL.

    Accepts:
      - /performance/MBP19/001/ALL/iw         → ("MBP19", "001")  perf-level
      - shorthand "MBP19/001"                 → ("MBP19", "001")  perf-level
      - /event/MSP03/ALL/iw                   → ("MSP03", "ALL")  event-level
      - shorthand "MSP03/ALL"                 → ("MSP03", "ALL")  event-level
      - bare "MSP03" (alpha-prefixed event)   → ("MSP03", "ALL")  event-level
    Returns (event_code, perf_code) on success; raises TicketmasterError
    with a useful message on any failure.
    """
    if not url:
        raise TicketmasterError("URL is empty")
    s = url.strip()
    # Shorthand "EVENT/ALL" — event-level
    m = re.fullmatch(r"\s*([A-Z][A-Z0-9]*)\s*/\s*ALL\s*", s, re.IGNORECASE)
    if m:
        return m.group(1).upper(), EVENT_LEVEL_PERF
    # Shorthand "EVENT/PERF" — perf-level (digits)
    m = re.fullmatch(r"\s*([A-Z0-9]+)\s*/\s*(\d+)\s*", s, re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2)
    # /performance/EVENT/PERF/... URL — perf-level
    m = _PERF_RE.search(s)
    if m:
        return m.group(1).upper(), m.group(2)
    # /event/EVENT/... URL — event-level (covers all perfs)
    m = _EVENT_RE.search(s)
    if m:
        return m.group(1).upper(), EVENT_LEVEL_PERF
    # Bare alpha-prefixed shorthand "MSP03" — event-level
    m = re.fullmatch(r"\s*([A-Z][A-Z0-9]+)\s*", s, re.IGNORECASE)
    if m:
        return m.group(1).upper(), EVENT_LEVEL_PERF
    raise TicketmasterError(
        "Couldn't parse that URL. Expected a ticketmaster.co.il /performance/ or "
        "/event/ URL, or shorthand 'EVENT/PERF', 'EVENT/ALL', or bare 'EVENT'."
    )


def perf_url(event_code, perf_code):
    if (perf_code or "").upper() == EVENT_LEVEL_PERF:
        return event_url(event_code)
    return f"{API_HOST}/performance/{event_code}/{perf_code}/ALL/iw"


def event_url(event_code):
    return f"{API_HOST}/event/{event_code}/ALL/iw"


def is_event_level(w):
    """True when the watcher row covers every performance of the event."""
    return (w.get("perf_code") or "").upper() == EVENT_LEVEL_PERF


def display_url(w):
    """The user-facing URL for a watcher — event page for event-level rows,
    performance page for perf-level rows."""
    if is_event_level(w):
        return event_url(w["event_code"])
    return perf_url(w["event_code"], w["perf_code"])


def _http_get(url):
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise TicketmasterError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise TicketmasterError(f"network error: {e.reason}") from e
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return resp.status, raw


def fetch_performance_detail(event_code, perf_code):
    """Returns the high-level performance object: enable flag, sales options, key.
    Useful as a sanity check that the show exists and is in a sellable state."""
    url = f"{API_HOST}{ISM_PATH}/getPerformanceDetail/{event_code}/{perf_code}"
    status, raw = _http_get(url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TicketmasterError(f"invalid JSON from getPerformanceDetail: {e}") from e
    if payload.get("status") != "SUCCESS":
        raise TicketmasterError(f"API said: {payload.get('status')} — {payload.get('errors')}")
    return payload.get("data") or {}


def fetch_selectable_seats(event_code, perf_code):
    """Returns the list of seats currently buyable through the INTERNET channel,
    normalized to the cross-source shape with `block`, `row`, `seat` keys.

    Raw API fields (b, r, l, s, id, f) are kept on the dict for audit.
    Returns [] when the show has no inventory or the endpoint returns null
    `data` (the SPA explicitly maps a null response to "no seats" — see the
    rxjs `catchError(() => of(null))` in the bundle). Hard errors are raised.
    """
    url = f"{API_HOST}{ISM_PATH}/getAllSelectableSeats/{CHANNEL}/{event_code}/{perf_code}"
    status, raw = _http_get(url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TicketmasterError(f"invalid JSON from getAllSelectableSeats: {e}") from e
    if payload.get("status") != "SUCCESS":
        raise TicketmasterError(f"API said: {payload.get('status')} — {payload.get('errors')}")
    data = payload.get("data") or []
    if not isinstance(data, list):
        return []
    out = []
    for s in data:
        if not isinstance(s, dict):
            continue
        out.append({
            **s,
            "block": s.get("b") or "",
            "row": str(s.get("r") or ""),
            "seat": str(s.get("l") if s.get("l") is not None else ""),
        })
    if not out:
        # No selectable seats can mean "seated show, sold out" OR "GA
        # (unnumbered) show, which never has a seat map". Check the GA
        # endpoint: if the perf has GA allocations, track it as one
        # status-encoded pseudo-seat (kupat GA model). Seated shows get []
        # from getAllGaBlock too, so this adds one cheap request only while
        # a show has zero seats.
        try:
            ga_seat = ga_status_seat(event_code, perf_code)
        except Exception:
            ga_seat = None
        if ga_seat is not None:
            return [ga_seat]
    return out


def fetch_ga_blocks(event_code, perf_code):
    """GA (unnumbered) allocations for a performance, or [] when the perf is
    fully seated. Each row is one (block, price-profile) allocation:
        {"b": "STND", "l": 2, "f": 13, "t": 5000, "a": 2600, "ga": true}
    `t` is the allocation's capacity and `a` its remaining count. The same
    block can appear once per profile (`f`) with the same `t` — the `a`
    values are disjoint and sum to at most `t`, so sum(a) is the real
    tickets-left number but block totals must be deduped per block.
    """
    url = f"{API_HOST}{ISM_PATH}/getAllGaBlock/{event_code}/{perf_code}"
    status, raw = _http_get(url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TicketmasterError(f"invalid JSON from getAllGaBlock: {e}") from e
    if payload.get("status") != "SUCCESS":
        raise TicketmasterError(f"API said: {payload.get('status')} — {payload.get('errors')}")
    data = payload.get("data") or []
    return [r for r in data if isinstance(r, dict) and r.get("ga")]


def ga_available(rows):
    """Total tickets-left across GA allocations (sum of every `a`)."""
    return sum(int(r.get("a") or 0) for r in rows)


def ga_total(rows):
    """Total GA capacity: `t` deduped per block (repeats across profiles)."""
    per_block = {}
    for r in rows:
        b = r.get("b") or ""
        t = int(r.get("t") or 0)
        per_block[b] = max(per_block.get(b, 0), t)
    return sum(per_block.values())


# GA events expose counts, not a "last tickets" flag — same threshold idea
# as kupat's GA tracker.
GA_LOW_THRESHOLD = int(os.getenv("KARTIS_TM_GA_LOW") or 25)


def ga_status(avail):
    """available | lasttickets | soldout from a tickets-left count."""
    if avail <= 0:
        return "soldout"
    if avail <= GA_LOW_THRESHOLD:
        return "lasttickets"
    return "available"


def ga_status_seat(event_code, perf_code):
    """Status-encoded pseudo-seat for a GA performance, or None when the perf
    has no GA allocations. Mirrors the kupat GA model: the status is part of
    the seat key, so soldout↔available transitions surface as an `added`
    seat through the normal diff, and `ga=True` opts it into the one-line
    status rendering in notify.py and the count-threshold path in filters.py.
    """
    rows = fetch_ga_blocks(event_code, perf_code)
    if not rows:
        return None
    avail = ga_available(rows)
    status = ga_status(avail)
    label = {"available": "GA available", "lasttickets": "GA last tickets",
             "soldout": "GA sold out"}[status]
    return {
        "block": label, "row": "GA", "seat": "1",
        "ga": True, "status": status,
        "qty_available": avail,
        "price": None,
    }


def list_performances(event_code):
    """Returns the list of performances under an event, with status.

    Each item shape (from the public SPA endpoint /wbtxapi/.../getPerformanceList):
        {"eventCode", "performanceCode", "performanceDate", "venueName",
         "status", "active", "perfType", "doorOpeningTime", ...}

    `active` is the buyable flag: false when sold out / not yet on sale.
    `status` is a string code like "s02_soldout", "s01_selling", etc.
    Raises TicketmasterError on network or shape failures.
    """
    url = f"{API_HOST}/wbtxapi/api/v1/bxcached/event/getPerformanceList/{event_code}/{CHANNEL}/iw"
    status, raw = _http_get(url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TicketmasterError(f"invalid JSON from getPerformanceList: {e}") from e
    if payload.get("status") != "SUCCESS":
        raise TicketmasterError(
            f"getPerformanceList API said: {payload.get('status')} — {payload.get('errors')}"
        )
    data = payload.get("data") or []
    if not isinstance(data, list):
        raise TicketmasterError(f"getPerformanceList unexpected shape for {event_code}")
    if not data:
        raise TicketmasterError(f"no performances found for event {event_code}")
    return data


# How long after showtime a perf is still worth polling. Late drops matter
# right up to (and a bit past) doors; after this the perf is over.
PAST_PERF_GRACE_MS = 6 * 3600 * 1000

# Perf-list `status` strings are the ONLY reliable buyability signal. The two
# obvious alternatives are both dead:
#   - `active` (same getPerformanceList payload) lags reality — a perf can be
#     buyable with active=false (MKS26/002 was s18_low_availability, active
#     false; tm_events.py ignores `active` for the same reason).
#   - getPerformanceDetail.salesOptions returns 0 for EVERY event as of
#     2026-07 (see CLAUDE.md / market.py), so it can't distinguish anything.
# Statuses where tickets can actually be bought online right now. Kept in sync
# with tm_events._SALE_OPENED_STATUSES, minus soldout/box-office/old-event
# (which are "sale opened" for the new-event monitor but NOT buyable here).
_ON_SALE_STATUSES = {"s01_onsale", "s18_low_availability"}
# Sold out: the sale is open but nothing's left — the drop we watch for is the
# transition OUT of this into an on-sale status.
_SOLDOUT_STATUSES = {"s02_soldout"}
# Everything else (s03_soon, s12_checkbacklater, s00_closed, s05_postponed,
# s04_canceled, s14_other_channels, s06_boxoffice, s13_old_over, s16*, s17*,
# unknown) = not on sale online → treated as "closed".


def _perf_sale_state(p):
    """Classify a performance from its perf-list `status` string:
    returns one of 'available' (buyable online now), 'soldout', 'closed'."""
    raw = str(p.get("status") or "")
    if raw in _ON_SALE_STATUSES:
        return "available"
    if raw in _SOLDOUT_STATUSES:
        return "soldout"
    return "closed"


def _perf_date_text(p):
    """Short venue-local date like '26.01 20:45' from performanceDate epoch-ms,
    or '' when unavailable. Stable per perf, so it's safe inside a seat key."""
    ms = p.get("performanceDate")
    if not ms:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(int(ms) / 1000, ZoneInfo("Asia/Jerusalem")).strftime("%d.%m %H:%M")
    except Exception:
        return ""


def _perf_status_seat(p, state):
    """Status-encoded pseudo-seat for one performance (festival/GA pattern:
    the status is part of the seat key, so any soldout↔selling transition
    surfaces as an `added` seat and pings through the normal diff — no
    changes to the tick loop needed).

    `state` is the classification from `_perf_sale_state` — one of
    'available', 'soldout', 'closed', derived from the perf-list `status`
    string (the only reliable signal; see the notes on `_ON_SALE_STATUSES`).

    `festival=True` opts the seat into the status-headline path in both
    tick implementations and the one-line rendering in notify.py.
    """
    perf_code = str(p.get("performanceCode") or "")
    raw = str(p.get("status") or "")
    status = state
    date_text = _perf_date_text(p)
    perf_name = f"Perf {perf_code}" + (f" · {date_text}" if date_text else "")
    label = {
        "available": f"{perf_name} — on sale",
        "soldout": f"{perf_name} — sold out",
        "closed": f"{perf_name} — sales closed",
    }[status]
    return {
        "block": label,
        "row": "STATUS",
        "seat": perf_code,
        "festival": True,
        "status": status,
        "raw_status": raw,
        "_perf": perf_code,
    }


def fetch_event_seats(event_code):
    """Aggregate selectable seats across every performance of an event.

    Returns (seats, per_perf_errors). Each seat dict is stamped with
    `_perf=<perf_code>` so the event-level dedup key (`event_seat_key`) can
    keep seats from different perfs distinct.

    Buyability gate — the subtle part. `getAllSelectableSeats` returns
    seats flagged AVAILABLE even for a performance whose sale is closed or
    sold out: they're just unsold physical seats, not purchasable inventory
    (MKS26/001 served 260 "AVAILABLE" seats while sold out). So seat *count*
    is NOT a reliable drop signal. The reliable one is the perf-list
    `status` string via `_perf_sale_state` — NOT `active` (lags) and NOT
    salesOptions (0 for everything since 2026-07). See `_ON_SALE_STATUSES`.

    For each non-past perf:
      - always emit a status pseudo-seat (`_perf_status_seat`) so a
        soldout/closed → on-sale flip pings even when no individual seat
        moves (the seat set can be unchanged across the flip);
      - only when the status says on-sale do we fetch and include the
        seat-level rows, so the watcher records buyable drops and not
        phantom closed-sale seats.

    Perfs past showtime (plus a grace window) are dropped entirely.

    Raises TicketmasterError if the perf list itself can't be fetched, or
    if every on-sale perf's seat fetch errored out (so a transient outage
    can't wipe the stored seat state and re-ping everything on recovery).
    Partial per-perf failures land in `per_perf_errors` so the caller can
    log them without failing the watcher.
    """
    perfs = list_performances(event_code)  # raises on failure
    seats = []
    per_perf_errors = {}
    attempted = 0
    now_ms = time.time() * 1000
    for p in perfs:
        perf_code = str(p.get("performanceCode") or "")
        if not perf_code:
            continue
        try:
            if p.get("performanceDate") and float(p["performanceDate"]) < now_ms - PAST_PERF_GRACE_MS:
                continue
        except (TypeError, ValueError):
            pass
        state = _perf_sale_state(p)
        seats.append(_perf_status_seat(p, state))
        if state != "available":
            continue
        attempted += 1
        try:
            ps = fetch_selectable_seats(event_code, perf_code)
            for s in ps:
                s["_perf"] = perf_code
            seats.extend(ps)
        except Exception as e:
            per_perf_errors[perf_code] = f"seats: {type(e).__name__}: {e}"
    if attempted and len(per_perf_errors) == attempted:
        raise TicketmasterError(
            f"all {attempted} perf(s) failed for {event_code}: {per_perf_errors}"
        )
    return seats, per_perf_errors


def seat_key(seat):
    """Stable dedup key for a seat across calls. Uses the normalized
    block/row/seat keys; ignores the source's internal id (which can differ
    across calls for the same physical seat under different price profiles).
    """
    return f"{seat.get('block') or seat.get('b','')}|{seat.get('row') or seat.get('r','')}|{seat.get('seat') or seat.get('l','')}"


def event_seat_key(seat):
    """Event-level dedup key — prefixes with `_perf` so identical seat ids
    from different performances don't collide in the watcher's seat-state set."""
    return f"{seat.get('_perf','')}|{seat_key(seat)}"


def format_seat(seat):
    block = seat.get("block") or seat.get("b") or "?"
    row = seat.get("row") or seat.get("r") or "?"
    num = seat.get("seat") or seat.get("l") or "?"
    return f"{block} row {row} seat {num}"


def diff_seats(prev_keys, curr_seats):
    """Return (added_seats, removed_keys, current_keys).

    `prev_keys` is an iterable of seat_key strings from the previous tick.
    `curr_seats` is the raw list from fetch_selectable_seats.
    `added_seats` are full seat dicts (so the caller has block/row/seat to
    show). `removed_keys` are bare keys (we don't keep the seat dict around
    once it disappears).
    """
    prev = set(prev_keys)
    curr_by_key = {}
    for s in curr_seats:
        k = seat_key(s)
        curr_by_key.setdefault(k, s)
    curr = set(curr_by_key)
    added = [curr_by_key[k] for k in curr - prev]
    removed = list(prev - curr)
    return added, removed, list(curr)


# Shims so app.py can call source modules uniformly.
SOURCE_NAME = "ticketmaster"


def get_labels(event_code, perf_code, lang="iw", force=False, missing_block=None):
    # Lazy import to avoid a circular dep at module load time.
    import labels as _labels
    return _labels.get_labels(event_code, perf_code, lang=lang, force=force, missing_block=missing_block)


def event_summary(labels):
    import labels as _labels
    return _labels.event_summary(labels)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python ticketmaster.py <url-or-EVENT/PERF>")
        sys.exit(2)
    ev, pf = parse_url(sys.argv[1])
    print(f"event={ev} perf={pf}")
    detail = fetch_performance_detail(ev, pf)
    print("detail:", detail)
    seats = fetch_selectable_seats(ev, pf)
    print(f"{len(seats)} selectable seats")
    by_block = {}
    for s in seats:
        by_block.setdefault(s.get("block"), 0)
        by_block[s["block"]] += 1
    for b, n in sorted(by_block.items(), key=lambda kv: -kv[1]):
        print(f"  {b}: {n}")
