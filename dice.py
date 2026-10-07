"""dice.fm drop checker — anonymous public JSON API, no login needed.

DICE's event pages are Next.js apps; the checkout data comes from
``https://api.dice.fm/events/<internal_id>/ticket_types`` which answers
plain anonymous HTTP (verified 2026-07-15 on the Kettama @ Knockdown
Center event). ``<internal_id>`` is a 24-char hex id, NOT the short slug
in the public URL — we resolve it once by fetching the event page and
reading the ``dice://open/events/<id>`` deep-link meta tags (also present
as ``product:retailer_item_id``), then cache the mapping on disk forever
(ids are immutable).

What the API exposes per ticket type: name, ``status`` ("on-sale" /
"off-sale" / "sold-out"), exact price in cents + currency, purchase
limits, and the CURRENT price tier ({index, name} — e.g. index 2,
"Second Release"). What it does NOT expose: remaining counts, total
allocation, or future tier prices. So the watcher diff is type-level:

  * a sold-out type coming back           → new seat key → RESTOCK ping
  * a new ticket type appearing           → new seat key → ping
  * a tier jump (index/price move)        → new seat key → ping
  * a type selling out / going off-sale   → key removed, silent

The seat key encodes (type_id, tier_index, price) so all of the above
fall out of the standard add/remove diff without touching the tick loop.
Like tickchak, this is GA-style — DEFAULT_FILTERS opts out of the
min-2-consecutive-seats default.

URL forms accepted:
    https://dice.fm/event/536dk8-rush-presents-kettama-...-tickets
    dice.fm/event/<slug>?dice_id=...          (tracking params ignored)
    6a0c75314469950001f6138e                  (bare internal 24-hex id)

All prices are whatever currency DICE serves for the event (USD for US
events); we surface the currency code alongside.

CLI probe (house convention):
    python dice.py <url-or-id>
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

SOURCE_NAME = "dice"
API_BASE = "https://api.dice.fm"
SITE_BASE = "https://dice.fm"
CACHE_DIR = Path(__file__).parent / "tm_cache"
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 20

# GA ticket-type buckets — seat adjacency doesn't apply.
DEFAULT_FILTERS = {"min_group_size": 1}

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

# Since 2026-10 api.dice.fm 403s (empty body) any call without the web
# client's identity headers; either of these alone restores 200. Values
# are what dice.fm's own bundle sends. Sent to the API host only. (Hetzner
# IPs are additionally blocked from api.dice.fm whatever the headers.)
API_HEADERS = {
    "X-Client-Platform": "web",
    "X-Api-Timestamp": "2024-04-15",
}

# The VPS's whole hosting network is refused by api.dice.fm, so the server
# can only reach it through another address. Either, API-host calls only:
#   KARTIS_DICE_PROXY     an HTTP(S) proxy, e.g. a residential one:
#                         http://user:pass@host:port
#   KARTIS_DICE_API_BASE  a forwarding endpoint (e.g. a Cloudflare Worker)
#                         that relays <base>/<path> to api.dice.fm/<path>
#                         (scripts/dice_worker.js), authenticated with
#                         KARTIS_DICE_API_KEY
DICE_PROXY = os.environ.get("KARTIS_DICE_PROXY", "").strip()
DICE_API_FORWARD = os.environ.get("KARTIS_DICE_API_BASE", "").strip().rstrip("/")
DICE_API_KEY = os.environ.get("KARTIS_DICE_API_KEY", "").strip()  # the forwarder's shared secret
_api_opener = None


def _api_urlopen(req):
    global _api_opener
    if not DICE_PROXY:
        return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    if _api_opener is None:
        _api_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": DICE_PROXY, "https": DICE_PROXY}))
    return _api_opener.open(req, timeout=REQUEST_TIMEOUT)


_ID_MAP_FILE = CACHE_DIR / "dice_ids.json"
_id_map = None  # {slug: internal_id} — immutable, cached forever

_HEX24_RE = re.compile(r"^[a-f0-9]{24}$")
_DEEPLINK_RE = re.compile(r"dice://open/events/([a-f0-9]{24})")
_RETAILER_RE = re.compile(
    r'property="product:retailer_item_id"\s+content="([a-f0-9]{24})"')


class DiceError(RuntimeError):
    pass


# --- HTTP -----------------------------------------------------------------

def _http_get(url, accept=None):
    headers = dict(REQUEST_HEADERS)
    is_api = url.startswith(API_BASE)
    if is_api:
        headers.update(API_HEADERS)
    if accept:
        headers["Accept"] = accept
    fetch_url = DICE_API_FORWARD + url[len(API_BASE):] if is_api and DICE_API_FORWARD else url
    if is_api and DICE_API_FORWARD and DICE_API_KEY:
        headers["X-Kartis-Key"] = DICE_API_KEY
    req = urllib.request.Request(fetch_url, headers=headers)
    try:
        resp = _api_urlopen(req) if is_api else urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise DiceError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise DiceError(f"network error: {e.reason}") from e
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


# --- slug → internal id resolution -----------------------------------------

def _load_id_map():
    global _id_map
    if _id_map is None:
        try:
            _id_map = json.loads(_ID_MAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            _id_map = {}
    return _id_map


def _remember_id(slug, internal_id):
    m = _load_id_map()
    if m.get(slug) != internal_id:
        m[slug] = internal_id
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            _ID_MAP_FILE.write_text(json.dumps(m), encoding="utf-8")
        except OSError:
            pass


def _resolve_internal_id(slug):
    """Fetch the public event page and pull the 24-hex internal id from
    the app deep-link / retailer meta tags. Cached on disk forever."""
    m = _load_id_map()
    if slug in m:
        return m[slug]
    html = _http_get(f"{SITE_BASE}/event/{slug}",
                     accept="text/html,application/xhtml+xml")
    match = _DEEPLINK_RE.search(html) or _RETAILER_RE.search(html)
    if not match:
        raise DiceError(f"couldn't find internal event id on dice.fm/event/{slug}")
    internal_id = match.group(1)
    _remember_id(slug, internal_id)
    return internal_id


# --- event page info (artwork + id) ----------------------------------------
# The ticket_types API carries no artwork, but every public event page has
# an og:image on dice-media.imgix.net. We keep the BASE attachment URL (the
# og:image's rect/w/h query is a 1300x630 social crop) so the page can ask
# imgix for whatever thumbnail size it wants. One page fetch also yields the
# 24-hex id, which is how /dice joins a purchase's slug to a tracked event.

_PAGE_INFO_FILE = CACHE_DIR / "dice_pages.json"
PAGE_INFO_TTL_SECONDS = 7 * 86400
PAGE_INFO_FAIL_TTL_SECONDS = 6 * 3600
_page_info = None
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"')


def _load_page_info():
    global _page_info
    if _page_info is None:
        try:
            _page_info = json.loads(_PAGE_INFO_FILE.read_text(encoding="utf-8"))
        except Exception:
            _page_info = {}
    return _page_info


def _fetch_page_info(key):
    """key = slug or 24-hex id. dice.fm redirects an id to the canonical
    slug URL, so both forms work."""
    try:
        html = _http_get(f"{SITE_BASE}/event/{key}",
                         accept="text/html,application/xhtml+xml")
    except Exception:
        return {"id": None, "image": None, "at": time.time(), "failed": True}
    m = _DEEPLINK_RE.search(html) or _RETAILER_RE.search(html)
    img = _OG_IMAGE_RE.search(html)
    image = None
    if img:
        image = img.group(1).replace("&amp;", "&").split("?", 1)[0]
        if "dice-media" not in image:
            image = None    # generic site logo, not event art
    return {"id": m.group(1) if m else None, "image": image, "at": time.time()}


def page_info_many(keys, max_fetch=16, workers=6):
    """{key: {"id", "image"}} for slugs/ids, fetching only stale or missing
    entries (at most max_fetch per call, in parallel) so a page load never
    stalls on a big backlog — the rest fill in on later loads. Failures are
    negative-cached for 6h."""
    info = _load_page_info()
    now = time.time()
    keys = [k for k in dict.fromkeys(str(k).strip().lower() for k in keys if k)]
    def stale(k):
        e = info.get(k)
        if not e:
            return True
        ttl = PAGE_INFO_FAIL_TTL_SECONDS if e.get("failed") else PAGE_INFO_TTL_SECONDS
        return now - (e.get("at") or 0) > ttl
    todo = [k for k in keys if stale(k)][:max_fetch]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
            results = list(ex.map(_fetch_page_info, todo))
        for k, r in zip(todo, results):
            if r.get("failed") and info.get(k, {}).get("image"):
                info[k]["at"] = now - PAGE_INFO_TTL_SECONDS + PAGE_INFO_FAIL_TTL_SECONDS
                continue   # keep the last good art through a blip
            info[k] = r
            if r.get("id") and not _HEX24_RE.fullmatch(k):
                _remember_id(k, r["id"])
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            _PAGE_INFO_FILE.write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass
    out = {}
    for k in keys:
        e = info.get(k) or {}
        ident = e.get("id") or (k if _HEX24_RE.fullmatch(k) else _load_id_map().get(k))
        out[k] = {"id": ident, "image": e.get("image")}
    return out


# --- Public source-plugin API ----------------------------------------------

def parse_url(url):
    """Returns (internal_24hex_id, "0"). Accepts a dice.fm event URL, a
    bare slug, or a bare internal id. perf_code is always "0" — a DICE
    event page is one performance."""
    if not url:
        raise DiceError("URL is empty")
    s = url.strip()

    if _HEX24_RE.fullmatch(s.lower()):
        return s.lower(), "0"

    if "/" not in s and "." not in s and "?" not in s:
        # bare slug shorthand
        return _resolve_internal_id(s), "0"

    try:
        parts = urlparse(s if "//" in s else "https://" + s)
    except ValueError as e:
        raise DiceError(f"couldn't parse URL: {e}") from e
    if parts.netloc and "dice.fm" not in parts.netloc.lower():
        raise DiceError(f"not a dice.fm URL: {parts.netloc}")
    path = (parts.path or "").strip("/")
    segs = path.split("/")
    if len(segs) >= 2 and segs[0] == "event":
        slug = segs[1]
    elif len(segs) == 1 and segs[0]:
        slug = segs[0]
    else:
        raise DiceError("URL has no event slug — expected dice.fm/event/<slug>")
    if _HEX24_RE.fullmatch(slug.lower()):
        return slug.lower(), "0"
    return _resolve_internal_id(slug), "0"


def perf_url(event_code, perf_code="0"):
    """Public event page. The API payload carries perm_name; prefer the
    cached one so links are human URLs, else fall back to the id form
    (dice.fm redirects ids to the canonical slug)."""
    cached = _read_cache(event_code)
    perm = ((cached or {}).get("meta") or {}).get("permName")
    return f"{SITE_BASE}/event/{perm or event_code}"


# --- desktop relay ----------------------------------------------------------
# api.dice.fm 403s the VPS's (Hetzner) IP whatever the headers, while a home
# connection gets through. dice_relay.py on the desktop fetches ticket_types
# for every tracked event and POSTs the raw JSON to /api/dice/relay; it lands
# here, and _fetch_ticket_types serves it when the API refuses this machine.
RELAY_DIR = CACHE_DIR / "dice_relay"
RELAY_MAX_AGE_SECONDS = int(os.getenv("KARTIS_DICE_RELAY_MAX_AGE_SECONDS") or 900)
# After a 403 from the API, go straight to the relay for this long instead of
# hammering an endpoint that has blocked us.
API_BLOCK_BACKOFF_SECONDS = 600
_api_blocked_until = 0.0


def _relay_path(event_code):
    code = str(event_code).strip().lower()
    if not _HEX24_RE.fullmatch(code):
        raise DiceError(f"relay: not a DICE event id: {event_code!r}")
    return RELAY_DIR / f"{code}.json"


def relay_store(event_code, data):
    """Keep a desktop-fetched ticket_types payload. Refuses anything that
    isn't one, so an error body can never overwrite a good copy."""
    if not isinstance(data, dict) or not isinstance(data.get("ticket_types"), list) \
            or not isinstance(data.get("dates"), dict):
        raise DiceError(f"relay: payload for {event_code} isn't a ticket_types response")
    path = _relay_path(event_code)
    RELAY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def relay_age(event_code):
    try:
        return time.time() - _relay_path(event_code).stat().st_mtime
    except (OSError, DiceError):
        return None


def _relay_load(event_code):
    age = relay_age(event_code)
    if age is None or age > RELAY_MAX_AGE_SECONDS:
        return None
    try:
        return json.loads(_relay_path(event_code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fetch_ticket_types(event_code):
    global _api_blocked_until
    err = None
    if time.time() >= _api_blocked_until:
        try:
            raw = _http_get(f"{API_BASE}/events/{event_code}/ticket_types")
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                raise DiceError(f"unparseable ticket_types JSON: {e}") from e
        except DiceError as e:
            if "HTTP 403" in str(e):
                _api_blocked_until = time.time() + API_BLOCK_BACKOFF_SECONDS
            err = e
    else:
        err = DiceError("api.dice.fm is refusing this machine (HTTP 403)")
    data = _relay_load(event_code)
    if data is not None:
        return data
    age = relay_age(event_code)
    when = f"{age / 60:.0f} min ago" if age is not None else "never"
    raise DiceError(f"{err} — desktop relay last sent this event {when}; "
                    "is dice_relay.py running on the PC?") from err


# --- event page summary (display fallback) ----------------------------------
# The public event page loads from the VPS even while the API doesn't. Its
# __NEXT_DATA__ (props.pageProps.initialState, a JSON string) carries the
# event-level status and cheapest price -- NOT per-type tiers -- so it only
# ever feeds the /dice page's headline when nothing better is available,
# never the watcher diff.
_NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
PAGE_SUMMARY_TTL_SECONDS = 600
_page_summary = {}


def _parse_page_summary(html):
    m = _NEXT_DATA_RE.search(html or "")
    if not m:
        return None
    try:
        nd = json.loads(m.group(1))
        st = ((nd.get("props") or {}).get("pageProps") or {}).get("initialState")
        if isinstance(st, str):
            st = json.loads(st)
        ev = ((st or {}).get("event") or {}).get("event") or {}
    except (ValueError, AttributeError):
        return None
    if not isinstance(ev, dict) or not ev:
        return None
    price = ev.get("price") if isinstance(ev.get("price"), dict) else {}
    cents = price.get("amount_from") if price.get("amount_from") is not None else price.get("amount")
    return {
        "status": str(ev.get("status") or "").lower() or None,
        "min_price": round(cents / 100, 2) if isinstance(cents, (int, float)) else None,
        "currency": price.get("currency"),
    }


def page_summary(event_code):
    """{status, min_price, currency} off the public event page, cached
    10 min in memory; None when the page can't be read."""
    code = str(event_code).strip().lower()
    hit = _page_summary.get(code)
    if hit and time.time() - hit[0] < PAGE_SUMMARY_TTL_SECONDS:
        return hit[1]
    try:
        out = _parse_page_summary(_http_get(f"{SITE_BASE}/event/{code}",
                                            accept="text/html,application/xhtml+xml"))
    except Exception:
        out = None
    _page_summary[code] = (time.time(), out)
    return out


_US_COUNTRIES = {"US", "USA"}
_NON_US_AMERICA_TZ = ("America/Toronto", "America/Vancouver", "America/Montreal",
                      "America/Edmonton", "America/Winnipeg", "America/Halifax",
                      "America/Mexico_City", "America/Cancun", "America/Tijuana",
                      "America/Sao_Paulo", "America/Buenos_Aires", "America/Bogota",
                      "America/Lima", "America/Santiago")


def region_of(country=None, currency=None, tz=None, start_iso=None):
    """'us' | 'intl' | None (unknown) for the /dice "US only" toggle.
    Strongest signal first: the venue's country, then the ticket currency
    (DICE prices US shows in USD, Canada in CAD, the UK in GBP...), then the
    venue time zone, then the start time's UTC offset (US = UTC-4..-10)."""
    c = (country or "").strip().upper()
    if c:
        return "us" if c in _US_COUNTRIES else "intl"
    cur = (currency or "").strip().upper()
    if cur:
        return "us" if cur == "USD" else "intl"
    z = (tz or "").strip()
    if z:
        if z in _NON_US_AMERICA_TZ:
            return "intl"
        return "us" if z.startswith(("America/", "US/", "Pacific/Honolulu")) else "intl"
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", (start_iso or "").strip())
    if m:
        off = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
        return "us" if -10 <= off <= -4 else "intl"
    return None


def _tier_text(tt):
    tier = tt.get("price_tier") or {}
    name = (tier.get("name") or "").strip()
    idx = tier.get("index")
    if name:
        return name
    if isinstance(idx, int) and idx > 0:
        return f"tier {idx + 1}"
    return ""


def _price_amount(tt):
    p = (tt.get("price") or {})
    amt = p.get("amount")
    return (amt / 100.0) if isinstance(amt, (int, float)) else None


def _currency(tt):
    return ((tt.get("price") or {}).get("currency") or "USD").upper()


def _block_label(tt):
    """Unique per (type, tier, price) — the seat key derives from this, so
    a tier jump or price change reads as a new seat and fires a ping."""
    name = (tt.get("name") or "ticket").strip()
    tier = _tier_text(tt)
    price = _price_amount(tt)
    parts = [name]
    if tier:
        parts.append(f"[{tier}]")
    if price is not None:
        parts.append(f"{_currency(tt)} {price:g}")
    return " ".join(parts)


def fetch_selectable_seats(event_code, perf_code="0"):
    """One virtual seat per ticket type that is buyable RIGHT NOW
    (status == "on-sale"). Sold-out / off-sale types are omitted, so a
    restock (or a new type, or a tier/price move) appears as an added
    seat and pings; a type selling out is a silent removal."""
    data = _fetch_ticket_types(event_code)
    try:
        _store_payload(event_code, data)
    except Exception:
        pass   # cache refresh is a bonus; never fail the tick over it
    out = []
    for tt in data.get("ticket_types") or []:
        if not isinstance(tt, dict):
            continue
        if str(tt.get("status") or "").strip().lower() != "on-sale":
            continue
        limits = tt.get("limits") or {}
        out.append({
            "block": _block_label(tt),
            "row": "GA",
            "seat": "1",
            "price": _price_amount(tt),
            "currency": _currency(tt),
            "raw": {
                "type_id": tt.get("id"),
                "tier_index": (tt.get("price_tier") or {}).get("index"),
                "increment": limits.get("increment"),
                "max_increments": limits.get("max_increments"),
            },
        })
    return out


def seat_key(seat):
    return f"{seat.get('block','')}|{seat.get('row','')}|{seat.get('seat','')}"


def format_seat(seat):
    return str(seat.get("block", "?"))


# --- Labels (cached event meta + ticket-type table) --------------------------

def _cache_path(event_code, lang="iw"):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"dice_{event_code}_{lang}.json"


def _read_cache(event_code, lang="iw"):
    path = _cache_path(event_code, lang)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _sale_not_started(dates):
    """True when the event's sale_start_date is in the future — an
    announced-but-not-yet-on-sale show (no ticket_types yet)."""
    start = (dates or {}).get("sale_start_date")
    if not start:
        return False
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(str(start))
        if dt.tzinfo is None:
            return False
        return dt > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def _parse_iso_perf(start_date):
    """'2026-10-07T22:00:00-04:00' → (epoch_ms, venue-local 'YYYY-MM-DD HH:MM')."""
    if not start_date:
        return None, ""
    text_display = str(start_date)[:16].replace("T", " ")
    try:
        dt = datetime.fromisoformat(str(start_date))
        return int(dt.timestamp() * 1000), text_display
    except (TypeError, ValueError):
        return None, text_display


def fetch_fresh(event_code, perf_code="0", lang="iw"):
    """Single API fetch → labels payload, same shape as the other sources
    so app.py / notify.py / the filter modal need no branching. lang is
    ignored (DICE is English) but kept for interface parity."""
    return _store_payload(event_code, _fetch_ticket_types(event_code), lang)


def _store_payload(event_code, data, lang="iw"):
    """ticket_types JSON → labels payload, written to the 1h cache. Shared
    by fetch_fresh and fetch_selectable_seats, so a watcher tick (every
    15-60s) keeps the /dice page's data fresh for free."""
    blocks = {}
    any_on_sale = False
    for tt in data.get("ticket_types") or []:
        if not isinstance(tt, dict):
            continue
        status = str(tt.get("status") or "").strip().lower()
        on_sale = status == "on-sale"
        any_on_sale = any_on_sale or on_sale
        key = _block_label(tt)
        blocks[key] = {
            "name": (tt.get("name") or "").strip(),
            "price": _price_amount(tt),
            "currency": _currency(tt),
            "tier_index": (tt.get("price_tier") or {}).get("index"),
            "tier_name": (tt.get("price_tier") or {}).get("name"),
            "status": status,
            "availability": "in_stock" if on_sale else "out_of_stock",
        }

    venues = data.get("venues") or []
    ven = venues[0] if venues and isinstance(venues[0], dict) else {}
    city = ven.get("city") if isinstance(ven.get("city"), dict) else {}
    dates = data.get("dates") or {}
    perf_ms, perf_text = _parse_iso_perf(dates.get("event_start_date"))

    # Status derivation. A buyable type wins outright. With NO ticket_types
    # in the payload the top-level `status` field lies (it stays "on-sale"
    # for a sold-out show — same lagging-flag trap as TM/tickchak), so read
    # the real signals: `is_fully_locked` = inventory gone (sold out, usually
    # waitlist-only), and a future sale_start = not on sale yet.
    if any_on_sale:
        status = "selling"
    elif blocks:
        status = "soldout"       # types exist but every one is off-sale
    elif data.get("is_fully_locked"):
        status = "soldout"       # locked, no types → sold out / waitlist
    elif _sale_not_started(dates):
        status = "upcoming"      # announced, sale hasn't opened yet
    else:
        status = "unknown"

    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(event_code),
        "perf_code": "0",
        "lang": lang,
        "meta": {
            "eventName": (data.get("name") or "").strip(),
            "venueName": (ven.get("name") or "").strip(),
            "venueCity": (city.get("name") or "").strip(),
            "firstPerfMs": perf_ms,
            "firstPerfText": perf_text,
            # DICE publishes no counts — tier jumps are the demand proxy.
            "totalSeats": None,
            "availSeats": None,
            "status": status,
            "permName": (data.get("perm_name") or "").strip() or None,
            "eventStatus": data.get("status"),
            "saleEnd": dates.get("sale_end_date"),
            # Inputs for region_of (the /dice "US only" toggle).
            "country": (city.get("country_alpha3") or city.get("country_code")
                        or ven.get("country_alpha3") or ven.get("country_code") or None),
            "timezone": dates.get("timezone") or ven.get("timezone") or None,
            "startIso": dates.get("event_start_date"),
        },
        "blocks": blocks,
    }
    _cache_path(event_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    """Read cached labels; refetch on staleness or when the caller probes
    for an unknown block. Soft-fail to cached on network errors."""
    cached = _read_cache(event_code, lang)
    fresh_needed = (
        force
        or cached is None
        or (time.time() - (cached.get("_fetched_at") or 0) > CACHE_TTL_SECONDS)
        or (missing_block and str(missing_block) not in (cached.get("blocks") or {}))
    )
    if not fresh_needed:
        return cached
    try:
        return fetch_fresh(event_code, perf_code, lang)
    except Exception as e:
        out = dict(cached) if cached else {
            "source": SOURCE_NAME, "event_code": str(event_code),
            "perf_code": "0", "lang": lang,
            "meta": {}, "blocks": {},
        }
        out["_error"] = f"{type(e).__name__}: {e}"   # not persisted
        return out


def cached_blocks(event_code):
    """Ticket-type blocks from the labels cache, no network. The tick calls
    this right after fetch_selectable_seats (which just rewrote the cache)
    to feed db.dice_tier_log_update."""
    return ((_read_cache(event_code) or {}).get("blocks")) or {}


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    parts = []
    if meta.get("eventName"):
        parts.append(meta["eventName"])
    venue = " ".join(v for v in (meta.get("venueName"), meta.get("venueCity")) if v)
    if venue:
        parts.append(venue)
    if meta.get("firstPerfText"):
        parts.append(meta["firstPerfText"])
    return " · ".join(parts)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("usage: python dice.py <dice.fm URL, slug, or 24-hex id>")
        sys.exit(2)
    ev, pf = parse_url(sys.argv[1])
    print(f"event={ev} perf={pf}")
    labels = get_labels(ev, pf, force=True)
    meta = labels.get("meta") or {}
    print(f"name:   {meta.get('eventName')!r}")
    print(f"venue:  {meta.get('venueName')!r} ({meta.get('venueCity')!r})")
    print(f"when:   {meta.get('firstPerfText')!r}")
    print(f"status: {meta.get('status')!r} (event: {meta.get('eventStatus')!r})")
    print(f"url:    {perf_url(ev)}")
    seats = fetch_selectable_seats(ev, pf)
    print(f"\n{len(seats)} ticket type(s) on sale now:")
    for s in seats:
        print(f"  {s.get('currency')} {s.get('price'):>8}  {s.get('block')}")
    print("\nall ticket types:")
    for code, info in (labels.get("blocks") or {}).items():
        price = info.get("price")
        price_s = f"{info.get('currency')} {price:g}" if isinstance(price, (int, float)) else "—"
        print(f"  [{info.get('status') or '?':<9}] {price_s:>12}  {code}")
