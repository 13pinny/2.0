"""barby.co.il drop checker (Tel Aviv club — standing/seated shows).

Companion to barby_events.py, which watches the *catalog* for new shows.
This module is the per-show drop checker: the source plugin the watcher
tick uses to notice when a sold-out Barby show goes back on sale.

Barby is a React SPA fronted by Cloudflare. Everything here is anonymous
plain HTTP — no login, no browser, no captcha (~1s per check). CF blocks
curl's TLS fingerprint but plain urllib with browser-ish headers passes;
see barby_events.py for the same trick.

Two endpoints matter:

  GET /api/shows/show/{showId}
      → showName, showDate ("DD/MM/YYYY"), showTime, showSeatType
        ("מופע עמידה" = standing), and crucially `isSoldOut` (a real
        JSON bool). This is the site's own truth — it's what decides
        whether the page shows a buy button or the waitlist form.
  GET /api/shows/TS/{showId}
      → {"show2": {...}} carrying nochair_price (₪), nochair_max_buy,
        num2soldout and `sold`. Used only for the price on the label.

Deliberately NOT used as an availability signal:

  * `sold` / `nochair_max_buy` — `sold` can EXCEED `nochair_max_buy`
    (show 5003: sold=1474, max=1100), so "left = max - sold" goes
    negative and is not a ticket count. Barby publishes no reliable
    tickets-left number anywhere.
  * `num2soldout` — non-zero only on already-sold-out shows (526 for
    5086), zero on available ones. Semantics unclear; not a count of
    remaining tickets.

So `isSoldOut` is the only trustworthy signal, and a Barby show is
tracked as a single status-encoded "seat" (the same model tickchak
festival shows and kupat GA events use). The status rides in the seat's
`block`, so the core add/remove diff fires on every transition without
the tick loop needing to know anything about Barby:

  sold out → on sale   adds "GA available"  → notify routes to 'drops'
  on sale  → sold out  adds "GA sold out"   → notify routes to 'status'

Barby shows are single-date, so perf_code is always "0" (same convention
as tickchak).

CLI probe:  python barby.py <url-or-showId>
"""
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SOURCE_NAME = "barby"
SITE_BASE = "https://barby.co.il"
CACHE_DIR = Path(__file__).parent / "tm_cache"  # shared cache dir; files are namespaced
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 25

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip",
    "Referer": SITE_BASE + "/",
}


class BarbyError(RuntimeError):
    pass


def _get_json(path):
    req = urllib.request.Request(SITE_BASE + path, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise BarbyError(f"barby {path} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise BarbyError(f"barby {path} unreachable: {e.reason}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise BarbyError(
            f"non-JSON from {path} — Cloudflare challenge or API change?"
        ) from e


def parse_url(url):
    """Extract (show_id, "0") from a barby show URL or a bare show id.

    Accepts https://barby.co.il/show/5086 (with or without scheme/query)
    and the shorthand "5086" / "barby/5086". perf_code is always "0" —
    a Barby show is a single date.
    """
    if not url:
        raise BarbyError("URL is empty")
    s = url.strip()
    m = re.search(r"/show/(\d+)", s)
    if m:
        return m.group(1), "0"
    m = re.fullmatch(r"\s*(?:barby\s*/\s*)?(\d+)\s*(?:/\s*0\s*)?", s, re.I)
    if m:
        return m.group(1), "0"
    raise BarbyError(
        "URL must look like https://barby.co.il/show/<id> (or just the show id)"
    )


def perf_url(event_code, perf_code="0"):
    return f"{SITE_BASE}/show/{event_code}"


def _show_status(show):
    """available | soldout from the show detail's isSoldOut bool.

    Barby publishes no tickets-left count, so there is no 'lasttickets'
    middle state — see the module docstring.
    """
    return "soldout" if show.get("isSoldOut") else "available"


def fetch_selectable_seats(event_code, perf_code="0"):
    """One status-encoded virtual seat reflecting isSoldOut.

    Encoding the status into the seat key (via `block`) makes the standard
    add/remove diff fire a notification on every transition — sold-out and
    available-again — without touching the core tick loop. `ga=True` plus
    `status` is what notify.py reads to route the alert (a non-buyable
    'soldout' seat goes to the status channel; 'available' is actionable
    and goes to drops).
    """
    show = _get_json(f"/api/shows/show/{event_code}")
    if not isinstance(show, dict) or not show.get("showId"):
        raise BarbyError(f"unexpected /api/shows/show/{event_code} shape — API change?")
    status = _show_status(show)
    label = {"available": "GA available", "soldout": "GA sold out"}[status]
    return [{
        "block": label,
        "row": "GA",
        "seat": "1",
        "ga": True,
        "status": status,
        # Barby publishes no tickets-left number — see module docstring.
        "qty_available": None,
        "price": None,
    }]


def seat_key(seat):
    return f"{seat.get('block','')}|{seat.get('row','')}|{seat.get('seat','')}"


def format_seat(seat):
    return str(seat.get("block", "?"))


# --- Labels (event meta), cached on disk ---------------------------------

def _cache_path(event_code, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"barby_{event_code}_{lang}.json"


def _date_fields(date_str, time_str):
    """('DD.MM.YYYY HH:MM', epoch_ms) from the API's DD/MM/YYYY + HH:MM
    (venue-local). Mirrors barby_events._date_fields."""
    d = (date_str or "").strip()
    t = (time_str or "").strip()
    try:
        dt = datetime.strptime(f"{d} {t}", "%d/%m/%Y %H:%M")
    except ValueError:
        return (d + (" " + t if t else "")).strip(), None
    try:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Jerusalem"))
    except Exception:
        pass
    return dt.strftime("%d.%m.%Y %H:%M"), int(dt.timestamp() * 1000)


def fetch_fresh(event_code, perf_code="0", lang="iw"):
    show = _get_json(f"/api/shows/show/{event_code}")
    if not isinstance(show, dict) or not show.get("showId"):
        raise BarbyError(f"unexpected /api/shows/show/{event_code} shape — API change?")
    # Price lives on the TS endpoint; it's cosmetic, so a failure here must
    # not cost us the label.
    price = None
    try:
        ts = (_get_json(f"/api/shows/TS/{event_code}") or {}).get("show2") or {}
        raw_price = str(ts.get("nochair_price") or "").strip()
        price = float(raw_price) if raw_price else None
    except Exception:
        price = None
    perf_text, perf_ms = _date_fields(show.get("showDate"), show.get("showTime"))
    status = _show_status(show)
    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(event_code),
        "perf_code": "0",
        "lang": lang,
        "meta": {
            "eventName": (show.get("showName") or "").strip(),
            # Barby is the venue; the useful slot info is the show format
            # (standing/seated), same call barby_events.py makes.
            "venueName": "Barby",
            "venueCity": "תל אביב",
            "seatType": (show.get("showSeatType") or "").strip(),
            "firstPerfMs": perf_ms,
            "firstPerfText": perf_text,
            "status": "soldout" if status == "soldout" else "selling",
            # No tickets-left / capacity is published — see module docstring.
            "availSeats": None,
            "totalSeats": None,
            "ga": True,
            "gaStatus": status,
        },
        "blocks": {
            "GA available": {"name": "GA available", "price": price},
            "GA sold out": {"name": "GA sold out", "price": price},
        },
    }
    _cache_path(event_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    path = _cache_path(event_code, lang)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    fresh_needed = (
        force
        or cached is None
        or (time.time() - (cached.get("_fetched_at") or 0) > CACHE_TTL_SECONDS)
    )
    if not fresh_needed:
        return cached
    try:
        return fetch_fresh(event_code, perf_code, lang)
    except Exception:
        return cached or {
            "source": SOURCE_NAME, "event_code": str(event_code),
            "perf_code": "0", "lang": lang, "meta": {}, "blocks": {},
        }


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    parts = []
    if meta.get("eventName"):
        parts.append(meta["eventName"])
    if meta.get("venueName"):
        parts.append(meta["venueName"])
    if meta.get("firstPerfText"):
        parts.append(meta["firstPerfText"])
    return " · ".join(parts)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("usage: python barby.py <url-or-showId>")
        sys.exit(2)
    ev, perf = parse_url(sys.argv[1])
    print(f"show={ev} perf={perf}  {perf_url(ev, perf)}")
    labels = get_labels(ev, perf, force=True)
    print("meta:", labels.get("meta"))
    print("summary:", event_summary(labels))
    seats = fetch_selectable_seats(ev, perf)
    for s in seats:
        print(f"  seat: {format_seat(s)}  status={s.get('status')}  key={seat_key(s)}")
