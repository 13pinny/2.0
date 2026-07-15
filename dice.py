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
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
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


def _fetch_ticket_types(event_code):
    raw = _http_get(f"{API_BASE}/events/{event_code}/ticket_types")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise DiceError(f"unparseable ticket_types JSON: {e}") from e


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
    data = _fetch_ticket_types(event_code)

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
            "status": ("selling" if any_on_sale else ("soldout" if blocks else "unknown")),
            "permName": (data.get("perm_name") or "").strip() or None,
            "eventStatus": data.get("status"),
            "saleEnd": dates.get("sale_end_date"),
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
    except Exception:
        return cached or {
            "source": SOURCE_NAME, "event_code": str(event_code),
            "perf_code": "0", "lang": lang,
            "meta": {}, "blocks": {},
        }


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
