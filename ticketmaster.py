"""Ticketmaster.co.il drop checker.

Polls the public ISM API used by the seat-map SPA to detect when new seats
become selectable for a given performance. No login, no captcha — the API
serves anonymous JSON given two custom headers (CHANNEL, CPU).

Reverse-engineered from the SPA bundle at /webclient/main.*.js. The endpoint
returns one row per currently-buyable seat:

    { "b": "<block>", "r": "<row>", "l": <level>, "s": "AVAILABLE",
      "id": <seat_id>, "f": <price_profile> }

We diff the set of (block, row, id) tuples between consecutive checks and
treat anything in `new - old` as a fresh drop.

`l` was long read here as the seat number. It is NOT — it is the venue
LEVEL, and it is constant across a whole block/row: on NEXT 2026 at the
National Stadium (2026-08) it took exactly four values across the entire
feed, {0, 1, 2, 8}, while 22 distinct seats in OR07 row 16 all carried
`l: 8` with sequential ids 702-721. The same field appears on the GA rows
of getAllGaBlock, where it plainly isn't a seat number either.

That mattered enormously, because `l` used to be the last component of the
dedup key: (block, row, level) collapsed 1,195 real seats on MRG17/001 into
122 keys and 176 seats on MRG15/001 into 22. Roughly 90% of TM inventory
was invisible to the diff — a row could go from one seat to twenty-two and
produce no `added` key and therefore no ping, which is the exact failure
this module exists to prevent.

`id` is the real per-seat identity: (block, row, id) is exactly unique on
every perf measured (176/176, 232/232, 1195/1195, 112/112) and stable for
the same physical seat across calls. It is also positional within a row —
six free seats in OR01 row 18 came back as ids 119-124 — so the adjacency
filter works on it, and works better than it did on a per-row constant.

TM publishes no printed seat LABEL in this payload, so `id` is what the
notification shows; it is a stable reference, not the number on the ticket.
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

# Seated sources otherwise fall back to {"min_group_size": 2} (see
# app.py._add_one_watcher), which is wrong for TM specifically: the drop this
# watcher exists to catch is a RETURN on a sold-out show, and returns arrive
# one seat at a time. Worse, the unconfirmed-radar seats are physical (no
# `festival`/`ga` flag), so they go through the adjacency filter too — a lone
# seat surfacing in a sold-out perf's feed, the single earliest and most
# valuable signal this module produces, was being dropped before notify.
# kupat keeps the pair default: its returns genuinely come back in pairs.
DEFAULT_FILTERS = {"min_group_size": 1}


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
            # `id`, not `l` — see the module docstring. Falls back to `l` only
            # for a shape that somehow carries no id, which would at worst
            # restore the old over-collapsed behaviour rather than crash.
            "seat": str(s.get("id") if s.get("id") is not None
                        else (s.get("l") if s.get("l") is not None else "")),
            "level": s.get("l"),
            # Tells notify.py to render this as "seat #N": it's TM's stable
            # seat id, not the number printed on the ticket (TM's feed has no
            # seat label at all), and sending someone to hunt for "seat 717"
            # in row 14 is worse than showing it plainly as a reference.
            "seat_ref": True,
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

    NOTE the path has no `/bxcached/` segment. The SPA calls the cached
    variant, which answers `Age: <n>` / `cache-control: max-age=30,public`
    behind the CDN; the uncached route below returns the byte-identical
    payload with `no-cache, no-store, must-revalidate` (verified 2026-08).
    That matters more here than anywhere else in this repo: the perf-list
    `status` is the ONLY buyability signal the drop checker has (see
    `_ON_SALE_STATUSES`), so up to 30s of CDN staleness is 30s of blindness
    on every gray→green flip. Metadata/price calls in labels.py keep the
    cached route on purpose — they are not time-critical.
    """
    url = f"{API_HOST}/wbtxapi/api/v1/event/getPerformanceList/{event_code}/{CHANNEL}/iw"
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


# Sold-out perf "unconfirmed drop" radar (see fetch_event_seats docstring).
UNCONFIRMED_ENABLED = (os.getenv("KARTIS_TM_UNCONFIRMED") or "1").strip().lower() not in (
    "0", "false", "no", "off")

# Above this many raw seats, a non-buyable perf's feed is the UNTOUCHED full
# venue map, not residual inventory, and diffing it is pointless *and* costly.
# The distinction is sharp in practice — NEXT 2026 at the National Stadium,
# 2026-08, every perf gated non-available:
#     MRG16/001  s02_soldout    232 seats   99% sold — residual, worth diffing
#     MRG19/001  s00_closed   3,802 seats   81% sold — residual, worth diffing
#     MRG15/005  s00_closed  15,010 seats           — never opened
#     MRG18/001  s00_closed  20,157 seats           — never opened
#     MRG20/001  s00_closed  20,289 seats           — never opened
# Seats can only DISAPPEAR from a full map (they sell), so the radar — which
# pings on seats APPEARING — has nothing to say about one, while storing
# 20k rows per perf per tick in tm_seat_state would dominate the whole DB.
# A perf crossing the threshold in either direction just re-baselines through
# the existing `_tracking` sentinel path, same as an error would.
UNCONFIRMED_MAX_SEATS = int(os.getenv("KARTIS_TM_UNCONFIRMED_MAX_SEATS") or 5000)

# Measuring "is this a full map?" costs a full-map download (~2s / 20k seats
# for a stadium), which is exactly the traffic the guard exists to avoid — and
# the answer barely changes: a perf that never opened stays that way for days.
# So remember the verdict per (event, perf) and re-measure only every
# UNCONFIRMED_SKIP_TTL_SECONDS, which is still far quicker than any perf can
# sell 15k seats down past the threshold. In-process only, on purpose: a
# restart re-measures, and the tick loops re-baseline such a perf safely
# through the `_tracking` sentinel.
UNCONFIRMED_SKIP_TTL_SECONDS = int(os.getenv("KARTIS_TM_UNCONFIRMED_SKIP_TTL") or 900)
_unconfirmed_skip = {}  # (event_code, perf_code) -> (measured_at, seat_count)

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


def _skip_unconfirmed(event_code, perf_code):
    """True while (event, perf) is a known full-map perf inside the re-measure
    window — see UNCONFIRMED_SKIP_TTL_SECONDS."""
    hit = _unconfirmed_skip.get((event_code, perf_code))
    if not hit:
        return False
    if time.time() - hit[0] >= UNCONFIRMED_SKIP_TTL_SECONDS:
        del _unconfirmed_skip[(event_code, perf_code)]
        return False
    return True


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

    EVERY non-available perf gets the "unconfirmed drop" radar
    (KARTIS_TM_UNCONFIRMED, default on): the raw seat feed is fetched anyway
    and its seats are included flagged `unconfirmed=True` with a "U|"-prefixed
    dedup key. A NEW seat appearing in a non-buyable perf's raw feed is the
    earliest observable signal of a release — it shows up there before the
    perf-list `status` flips to on-sale for us.

    The radar used to cover `s02_soldout` only, which left a real hole: TM
    parks plenty of live shows in `s00_closed` with genuine residual
    inventory (NEXT 2026, 2026-08: MRG19/001 was s00_closed holding 3,802
    unsold seats — 81% sold and completely unwatched, while the whole check
    for that date was one status pseudo-seat). `_ON_SALE_STATUSES` is a
    small allowlist and every other status — closed, soon, checkbacklater,
    other-channels — got the same blind treatment.

    `UNCONFIRMED_MAX_SEATS` keeps that affordable: a perf whose feed is the
    untouched full venue map is skipped (a note lands in `per_perf_errors`),
    since seats can only vanish from a full map and an appearance-diff has
    nothing to say about it.

    The tick loops suppress the first batch per perf (the standing unsold-seat
    set is not a drop) via the per-perf `_tracking` sentinel; only additions
    after that ping. If the feed fetch fails or the perf is over the size
    guard, the perf's sentinel AND seats are omitted, so the tracking baseline
    self-resets on recovery instead of re-pinging the whole standing set.

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
    failed = 0
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
            if UNCONFIRMED_ENABLED and not _skip_unconfirmed(event_code, perf_code):
                try:
                    ps = fetch_selectable_seats(event_code, perf_code)
                except Exception as e:
                    per_perf_errors[perf_code] = f"unconfirmed seats: {type(e).__name__}: {e}"
                else:
                    # Untouched full map → nothing for an appearance-diff to
                    # find; skip per-seat tracking (see UNCONFIRMED_MAX_SEATS).
                    # Omitting the sentinel too means it re-baselines cleanly
                    # if the perf later sells down under the threshold.
                    if len(ps) > UNCONFIRMED_MAX_SEATS:
                        _unconfirmed_skip[(event_code, perf_code)] = (time.time(), len(ps))
                        per_perf_errors[perf_code] = (
                            f"unconfirmed skipped: {len(ps)} seats > "
                            f"{UNCONFIRMED_MAX_SEATS} (full map, not residual); "
                            f"re-measuring in {UNCONFIRMED_SKIP_TTL_SECONDS}s"
                        )
                    else:
                        _unconfirmed_skip.pop((event_code, perf_code), None)
                        seats.append({
                            "_perf": perf_code, "unconfirmed": True, "_tracking": True,
                            "block": "#tracking", "row": "", "seat": "",
                        })
                        for s in ps:
                            s["_perf"] = perf_code
                            s["unconfirmed"] = True
                        seats.extend(ps)
            continue
        attempted += 1
        try:
            ps = fetch_selectable_seats(event_code, perf_code)
            for s in ps:
                s["_perf"] = perf_code
            seats.extend(ps)
        except Exception as e:
            failed += 1
            per_perf_errors[perf_code] = f"seats: {type(e).__name__}: {e}"
    if attempted and failed == attempted:
        raise TicketmasterError(
            f"all {attempted} on-sale perf(s) failed for {event_code}: {per_perf_errors}"
        )
    return seats, per_perf_errors


def seat_key(seat):
    """Stable dedup key for a seat across calls: block | row | seat.

    `seat` is normalized to the API's `id` for real seats (see the module
    docstring — `l` is the venue level and collapses ~90% of the feed), and
    to a synthetic label for the GA / per-perf status pseudo-seats, which
    carry no id and are keyed by their status text on purpose.

    Must stay in sync with db.tm_replace_seat_state, which rebuilds this key
    from the stored columns.
    """
    return f"{seat.get('block') or seat.get('b','')}|{seat.get('row') or seat.get('r','')}|{seat.get('seat') or seat.get('l','')}"


def event_seat_key(seat):
    """Event-level dedup key — prefixes with `_perf` so identical seat ids
    from different performances don't collide in the watcher's seat-state set.
    Unconfirmed (sold-out-perf) seats get an extra "U|" prefix so the same
    physical seat re-pings as a CONFIRMED drop when the perf status finally
    flips to on-sale. Must stay in sync with db.tm_replace_seat_state."""
    key = f"{seat.get('_perf','')}|{seat_key(seat)}"
    if seat.get("unconfirmed"):
        key = "U|" + key
    return key


def format_seat(seat):
    block = seat.get("block") or seat.get("b") or "?"
    row = seat.get("row") or seat.get("r") or "?"
    # A seat REFERENCE (the API's id), not the number printed on the ticket —
    # TM's seat feed doesn't publish a seat label. Hence the "#".
    num = seat.get("seat") or seat.get("l") or "?"
    return f"{block} row {row} seat #{num}"


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
