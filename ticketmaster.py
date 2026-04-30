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
import re
import urllib.request
import urllib.error
import json
import gzip
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


def parse_url(url):
    """Extract (event_code, perf_code) from a Ticketmaster.co.il URL.

    Accepts:
      - https://www.ticketmaster.co.il/performance/MBP19/001/ALL/iw
      - bare "MBP19/001" shorthand
    Returns (event_code, perf_code) on success.
    Raises TicketmasterError with a useful message on any failure, including
    an /event/ URL (which is event-level and has no perf code yet).
    """
    if not url:
        raise TicketmasterError("URL is empty")
    s = url.strip()
    # Shorthand "EVENT/PERF"
    m = re.fullmatch(r"\s*([A-Z0-9]+)\s*/\s*(\d+)\s*", s, re.IGNORECASE)
    if m:
        return m.group(1).upper(), m.group(2)
    m = _PERF_RE.search(s)
    if m:
        return m.group(1).upper(), m.group(2)
    m = _EVENT_RE.search(s)
    if m:
        raise TicketmasterError(
            f"That's an event URL ({m.group(1)}); paste a /performance/<EVENT>/<PERF>/... URL "
            "or use the shorthand 'EVENT/PERF' for the specific date you want to watch."
        )
    raise TicketmasterError(
        "Couldn't parse that URL. Expected a ticketmaster.co.il /performance/ URL "
        "or shorthand 'EVENT/PERF'."
    )


def perf_url(event_code, perf_code):
    return f"{API_HOST}/performance/{event_code}/{perf_code}/ALL/iw"


def event_url(event_code):
    return f"{API_HOST}/event/{event_code}/ALL/iw"


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
    return out


def seat_key(seat):
    """Stable dedup key for a seat across calls. Uses the normalized
    block/row/seat keys; ignores the source's internal id (which can differ
    across calls for the same physical seat under different price profiles).
    """
    return f"{seat.get('block') or seat.get('b','')}|{seat.get('row') or seat.get('r','')}|{seat.get('seat') or seat.get('l','')}"


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
