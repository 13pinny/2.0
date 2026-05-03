"""tickchak.co.il drop checker — server-rendered HTML + JSON-LD.

Tickchak is the simplest source so far: every event page already embeds a
schema.org Event with an `offers` array as inline `<script
type="application/ld+json">`. Each Offer is a *ticket TYPE* (e.g. "Men's
Ticket", "Accessible Seat", "Donation tier") with `availability` set to
either "in_stock" or "out_of_stock". No XHR, no captcha, no API key — just
fetch the page and parse the JSON.

Granularity gotcha: tickchak does NOT expose seat numbers. A "drop" here
means a ticket type flipping back to in_stock. Notifications read like
"Type X (₪Y) is now available!" rather than "Section 3 row B seat 12."

URL forms accepted:
  https://tickchak.co.il/mada26          — slug
  https://tickchak.co.il/mada26?ref=...  — slug + tracking params (stripped)
  https://tickchak.co.il/103350          — numeric event id
  mada26                                  — bare slug shorthand

Note on slug vs numeric: both forms work and reach the same event, but we
DON'T auto-dedupe — a watcher added as "mada26" and one added as "103350"
will both poll. Document this explicitly so the user picks one form.
"""
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SOURCE_NAME = "tickchak"
SITE_BASE = "https://tickchak.co.il"
CACHE_DIR = Path(__file__).parent / "tm_cache"
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 20

# New watchers default to NO group filter — tickchak offers are GA-style
# ticket types, so "min 2 consecutive seats" doesn't apply.
DEFAULT_FILTERS = {"min_group_size": 1}

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

# Path segments that aren't event slugs — guards against stray paste
# (e.g. user copies tickchak.co.il/portal by accident).
_RESERVED_PATHS = {
    "portal", "search", "shows", "terms", "privacy", "about",
    "contact", "live", "form", "ajax", "static",
}

# Tracking-only query params we strip from URLs before storing.
_TRACKING_PARAMS = {
    "ref", "_ref", "hub", "live_ref", "ref_ccaid", "ref_id",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid",
}


class TickchakError(RuntimeError):
    pass


# --- URL handling ---------------------------------------------------------

def parse_url(url):
    """Returns (event_code, "0"). `event_code` is the slug or numeric id from
    the URL path, with tracking params stripped. perf_code is always "0" —
    tickchak doesn't have a multi-performance concept on a single page."""
    if not url:
        raise TickchakError("URL is empty")
    s = url.strip()

    # Bare shorthand — accept slug or digits without scheme/host.
    bare = re.fullmatch(r"\s*([A-Za-z0-9_\-]{2,})\s*", s)
    if bare and "/" not in s and "?" not in s and "." not in s:
        slug = bare.group(1)
        if slug.lower() in _RESERVED_PATHS:
            raise TickchakError(f"'{slug}' is not an event slug — paste a tickchak.co.il event URL")
        return slug, "0"

    # Full URL — extract first non-empty path segment.
    try:
        parts = urlparse(s)
    except ValueError as e:
        raise TickchakError(f"couldn't parse URL: {e}") from e
    if parts.netloc and "tickchak.co.il" not in parts.netloc.lower():
        raise TickchakError(f"not a tickchak URL: {parts.netloc}")
    path = (parts.path or "").strip("/")
    if not path:
        raise TickchakError("URL has no event slug — expected /<slug-or-id>")
    # Drop trailing /form or /tickets if present.
    head = path.split("/", 1)[0]
    if head.lower() in _RESERVED_PATHS:
        raise TickchakError(f"'{head}' is not an event slug")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", head):
        raise TickchakError(f"unexpected slug format: {head!r}")
    return head, "0"


def perf_url(event_code, perf_code="0"):
    return f"{SITE_BASE}/{event_code}"


# --- HTTP + JSON-LD parsing ----------------------------------------------

def _http_get_html(url):
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise TickchakError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise TickchakError(f"network error: {e.reason}") from e
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


_LD_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _extract_jsonld_event(html):
    """Walk every <script type="application/ld+json"> block, parse, flatten
    @graph arrays, prefer the node with @type == "Event". Returns None if
    no parseable JSON-LD is found."""
    candidates = []
    for m in _LD_RE.finditer(html):
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            candidates.extend(x for x in obj if isinstance(x, dict))
        elif isinstance(obj, dict):
            graph = obj.get("@graph")
            if isinstance(graph, list):
                candidates.extend(x for x in graph if isinstance(x, dict))
            else:
                candidates.append(obj)
    for c in candidates:
        t = c.get("@type")
        if t == "Event" or (isinstance(t, list) and "Event" in t):
            return c
    return candidates[0] if candidates else None


def _normalize_offers(offers):
    """schema.org `offers` can be a single dict, a list, or missing."""
    if not offers:
        return []
    if isinstance(offers, dict):
        return [offers]
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    return []


def _parse_price(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _availability_in_stock(value):
    """Match both schema.org canonical ("https://schema.org/InStock") and
    tickchak's snake-case ("in_stock"). Compare case-insensitively against
    the trailing token."""
    if not value:
        return False
    s = str(value).lower().strip()
    return s.endswith("in_stock") or s.endswith("instock")


def _parse_iso_perf(start_date):
    """Parse '2026-06-02T20:00:00+03:00' into (epoch_ms, 'YYYY-MM-DD HH:MM').

    We keep the venue-local string as-is so the dashboard never accidentally
    converts it through the watcher box's local timezone."""
    if not start_date:
        return None, ""
    text = str(start_date)[:16]  # YYYY-MM-DDTHH:MM → 'YYYY-MM-DDTHH:MM'
    text_display = text.replace("T", " ")
    try:
        dt = datetime.fromisoformat(str(start_date))
        return int(dt.timestamp() * 1000), text_display
    except (TypeError, ValueError):
        return None, text_display


# --- Public source-plugin API --------------------------------------------

def _block_label(name, price):
    """Make a unique label per (name, price) combo. Tickchak commonly has
    several Offers with the same Hebrew name at different prices (e.g. one
    "Donation to MDA" at ₪100, ₪250, ₪900) — those are distinct products
    and need to be tracked as distinct blocks so the user gets pinged when
    a specific price-point opens up."""
    if isinstance(price, (int, float)):
        return f"{name} (₪{price:g})"
    return name


def fetch_selectable_seats(event_code, perf_code="0"):
    """Return one normalized "seat" dict per in-stock Offer. Each entry is
    a single ticket-type bucket (no per-seat granularity) but the schema
    matches the other sources: block / row / seat keys."""
    html = _http_get_html(perf_url(event_code))
    ld = _extract_jsonld_event(html) or {}
    offers = _normalize_offers(ld.get("offers"))
    out = []
    for o in offers:
        if not _availability_in_stock(o.get("availability")):
            continue
        name = (o.get("name") or "").strip() or "כרטיס"
        price = _parse_price(o.get("price"))
        out.append({
            "block": _block_label(name, price),
            "row": "GA",
            "seat": "1",
            "price": price,
            "raw": o,
        })
    return out


def seat_key(seat):
    return f"{seat.get('block','')}|{seat.get('row','')}|{seat.get('seat','')}"


def format_seat(seat):
    block = seat.get("block", "?")
    price = seat.get("price")
    if isinstance(price, (int, float)):
        return f"{block} (₪{price:g})"
    return str(block)


# --- Labels (cached event meta + ticket-type table) ----------------------

def _cache_path(event_code, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"tickchak_{event_code}_{lang}.json"


def fetch_fresh(event_code, perf_code="0", lang="iw"):
    """Single page fetch → labels payload. Same shape as
    `kupat.fetch_fresh` and `labels.fetch_fresh` so app.py / notify.py /
    filter modal don't need per-source branching."""
    html = _http_get_html(perf_url(event_code))
    ld = _extract_jsonld_event(html) or {}

    offers = _normalize_offers(ld.get("offers"))
    blocks = {}
    any_in_stock = False
    for o in offers:
        name = (o.get("name") or "").strip() or "כרטיס"
        in_stock = _availability_in_stock(o.get("availability"))
        if in_stock:
            any_in_stock = True
        price = _parse_price(o.get("price"))
        # Composite key keeps (name, price) Offers distinct so the filter
        # modal can exclude a specific price-point, not the whole name.
        key = _block_label(name, price)
        existing = blocks.get(key)
        # Same composite key → dedupe; prefer in_stock so an availability
        # change in either direction doesn't get hidden by encounter order.
        if existing and existing.get("availability") == "in_stock" and not in_stock:
            continue
        blocks[key] = {
            "name": name,         # display name (Hebrew, no price suffix)
            "price": price,
            "currency": o.get("priceCurrency") or "ILS",
            "availability": "in_stock" if in_stock else "out_of_stock",
        }

    location = ld.get("location") if isinstance(ld.get("location"), dict) else {}
    address = location.get("address") if isinstance(location.get("address"), dict) else {}
    perf_ms, perf_text = _parse_iso_perf(ld.get("startDate"))
    venue_name = (address.get("streetAddress") or "").strip() or (location.get("name") or "").strip()
    venue_city = (address.get("addressLocality") or "").strip()

    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(event_code),
        "perf_code": "0",
        "lang": lang,
        "meta": {
            "eventName": (ld.get("name") or "").strip(),
            "venueName": venue_name,
            "venueCity": venue_city,
            "firstPerfMs": perf_ms,
            "firstPerfText": perf_text,
            "totalSeats": None,        # tickchak doesn't expose a count
            "status": ("selling" if any_in_stock else ("soldout" if blocks else "unknown")),
        },
        "blocks": blocks,
    }
    _cache_path(event_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    """Read cached labels; refetch on staleness or when caller probes for
    a block we don't have yet. Soft-fail to cached on network errors."""
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
    if meta.get("venueName"):
        parts.append(meta["venueName"])
    if meta.get("firstPerfText"):
        parts.append(meta["firstPerfText"])
    return " · ".join(parts)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("usage: python tickchak.py <slug-or-url>")
        sys.exit(2)
    ev, pf = parse_url(sys.argv[1])
    print(f"event={ev} perf={pf}")
    labels = get_labels(ev, pf, force=True)
    meta = labels.get("meta") or {}
    print(f"name:   {meta.get('eventName')!r}")
    print(f"venue:  {meta.get('venueName')!r} ({meta.get('venueCity')!r})")
    print(f"when:   {meta.get('firstPerfText')!r}")
    print(f"status: {meta.get('status')!r}")
    seats = fetch_selectable_seats(ev, pf)
    print(f"\n{len(seats)} ticket type(s) currently in_stock:")
    for s in seats:
        price = s.get("price")
        price_s = f"₪{price:g}" if isinstance(price, (int, float)) else "—"
        print(f"  {price_s:>8}  {s.get('block')}")
    print("\nall ticket types:")
    for code, info in (labels.get("blocks") or {}).items():
        avail = info.get("availability") or "?"
        price = info.get("price")
        price_s = f"₪{price:g}" if isinstance(price, (int, float)) else "—"
        print(f"  [{avail:<13}] {price_s:>8}  {code}")
