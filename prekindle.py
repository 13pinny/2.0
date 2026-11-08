"""prekindle.com drop checker (US promoter ticketing — e.g. Paxahau's
Detroit shows at the Russell Industrial Center).

Pure HTTP, anonymous, ~0.3s per check, and it answers the VPS: the event
page (`/event/<id>-<slug>`) is server-rendered Apache Wicket HTML with no
bot check. The checkout (`/checkout/id/<n>`) sits behind a Queue-it
waiting room (q.prekindle.com), so nothing past the event page is read.

The page publishes no ticket counts and no per-tier state, so this is
status-only, the barby model: ONE status-encoded pseudo-seat. The signal
is the action bar's own markup, the thing that decides what a buyer sees:

  sold out:  <span class="action-bar-button soldoutbutton"
                   wicketpath="body_soldOutContainer">Sold Out</span>
  on sale:   <div wicketpath="body_buyLinksListContainer"> … one
                   <a class="… buybutton" href="../checkout/id/…">Buy Tickets</a>
                   per buy link

Trap: the ld+json `offers.availability` reads "InStock" on a sold-out
show (it-s-murph-detroit, 2026-11-08), so it is read for name / date /
venue / price range ONLY. A page showing neither marker raises — a
layout change must read as an error, never as "sold out".

Status rides in the seat's `block`, so the plain add/remove diff fires
on each flip: sold out → on sale adds "GA available" (→ drops), the
reverse adds "GA sold out" (→ status). perf_code is always "0".

CLI probe:  python prekindle.py <prekindle.com event URL or id>
"""
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SOURCE_NAME = "prekindle"
SITE_BASE = "https://www.prekindle.com"
CACHE_DIR = Path(__file__).parent / "tm_cache"
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 25
# GA status seats skip the adjacency filter anyway; 1 keeps it explicit.
DEFAULT_FILTERS = {"min_group_size": 1}

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}


class PrekindleError(RuntimeError):
    pass


def parse_url(url):
    """(event_path, "0"). The key is the page path tail '52932-its-murph-detroit'
    (a bare '52932' also works — the site redirects the id alone)."""
    s = (url or "").strip()
    if not s:
        raise PrekindleError("URL is empty")
    m = re.search(r"/event/([0-9]+(?:-[A-Za-z0-9-]+)?)", s)
    if m:
        return m.group(1), "0"
    m = re.fullmatch(r"(?:prekindle\s*/\s*)?([0-9]+(?:-[A-Za-z0-9-]+)?)", s, re.I)
    if m:
        return m.group(1), "0"
    raise PrekindleError("URL must look like https://www.prekindle.com/event/<id>-<name>")


def perf_url(event_code, perf_code="0"):
    return f"{SITE_BASE}/event/{event_code}"


def _get_page(event_code):
    req = urllib.request.Request(perf_url(event_code), headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise PrekindleError(f"prekindle event {event_code} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise PrekindleError(f"prekindle unreachable: {e.reason}") from e
    return raw.decode("utf-8", "replace")


def _status(page):
    """available | soldout off the action bar; raises when neither shows."""
    buy = re.search(r'wicketpath="body_buyLinksListContainer"', page)
    if buy and re.search(r'class="[^"]*\bbuybutton\b', page):
        return "available"
    if re.search(r'wicketpath="body_soldOutContainer"', page):
        return "soldout"
    raise PrekindleError("no Buy Tickets or Sold Out marker on the page — layout change?")


def _jsonld(page):
    m = re.search(r'<script[^>]*wicketpath="googleJsonLd"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        m = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S)
    try:
        return json.loads(m.group(1)) if m else {}
    except ValueError:
        return {}


def fetch_selectable_seats(event_code, perf_code="0"):
    page = _get_page(event_code)
    status = _status(page)
    _write_labels(event_code, page, status)
    return [{
        "block": "GA available" if status == "available" else "GA sold out",
        "row": "GA",
        "seat": "1",
        "ga": True,
        "status": status,
        "qty_available": None,   # prekindle publishes no counts
        "price": None,
    }]


def seat_key(seat):
    return f"{seat.get('block','')}|{seat.get('row','')}|{seat.get('seat','')}"


def format_seat(seat):
    return str(seat.get("block", "?"))


# --- Labels (event meta), cached on disk ---------------------------------

def _cache_path(event_code, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"prekindle_{event_code}_{lang}.json"


def _write_labels(event_code, page, status, lang="iw"):
    ld = _jsonld(page)
    loc = ld.get("location") or {}
    addr = loc.get("address") or {}
    offers = ld.get("offers") or {}
    start = (ld.get("startDate") or "")[:10]
    perf_ms = None
    try:
        from zoneinfo import ZoneInfo
        # Date only on the page; anchor at 21:00 local so the night is right.
        perf_ms = int(datetime.strptime(start, "%Y-%m-%d").replace(
            hour=21, tzinfo=ZoneInfo("America/Detroit")).timestamp() * 1000)
    except Exception:
        pass
    price = None
    try:
        price = float(offers.get("lowPrice") or offers.get("price"))
    except (TypeError, ValueError):
        pass
    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(event_code),
        "perf_code": "0",
        "lang": lang,
        "meta": {
            "eventName": html.unescape(ld.get("name") or "").strip(),
            "venueName": html.unescape(loc.get("name") or "").strip(),
            "venueCity": ", ".join(x for x in (addr.get("addressLocality"), addr.get("addressRegion")) if x),
            "firstPerfMs": perf_ms,
            "firstPerfText": start,
            "status": "soldout" if status == "soldout" else "selling",
            "availSeats": None,
            "totalSeats": None,
            "ga": True,
            "gaStatus": status,
            "currency": offers.get("priceCurrency") or "USD",
            "priceRange": [offers.get("lowPrice"), offers.get("highPrice")],
        },
        # No price on the blocks: notify renders block prices as ₪.
        "blocks": {
            "GA available": {"name": "GA available", "price": None},
            "GA sold out": {"name": "GA sold out", "price": None},
        },
        "min_price": price,
    }
    _cache_path(event_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    path = _cache_path(event_code, lang)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    if not force and cached and time.time() - (cached.get("_fetched_at") or 0) <= CACHE_TTL_SECONDS:
        return cached
    try:
        page = _get_page(event_code)
        return _write_labels(event_code, page, _status(page), lang)
    except Exception:
        return cached or {"source": SOURCE_NAME, "event_code": str(event_code),
                          "perf_code": "0", "lang": lang, "meta": {}, "blocks": {}}


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    parts = [p for p in (meta.get("eventName"), meta.get("venueName"), meta.get("firstPerfText")) if p]
    return " · ".join(parts)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("usage: python prekindle.py <prekindle.com event URL or id>")
        sys.exit(2)
    ev, perf = parse_url(sys.argv[1])
    print(f"event={ev}  {perf_url(ev, perf)}")
    seats = fetch_selectable_seats(ev, perf)
    labels = get_labels(ev, perf)
    print("summary:", event_summary(labels), "| price range:", labels["meta"].get("priceRange"))
    for s in seats:
        print(f"  seat: {format_seat(s)}  status={s['status']}  key={seat_key(s)}")
