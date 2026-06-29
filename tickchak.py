"""tickchak.co.il drop checker — page HTML + cached event JS.

Tickchak's public event page exposes two complementary sources we
combine for accurate state:

  1. Schema.org JSON-LD inside the page HTML (`<script
     type="application/ld+json">`). Always reachable, includes the
     event title, venue, start time, and an `offers` array — one Offer
     per ticket TYPE (Adult, Concession, Accessible…) with `price` +
     `availability` string. **But the availability flag here is STALE
     SEO markup** — it says `in_stock` even for fully sold-out events.
     We use it for names + prices only.

  2. The cached static event JS (`static.tickchak.co.il/js/ev_<hash>_t…`)
     — referenced by the page HTML. Inside there's a JS literal
     `tickchak_form_button={"enabled":"0|1","title":"SOLD OUT|…"}`
     which IS the live signal: enabled=1 means the buy button is
     live and at least one ticket type is purchasable. The endpoint
     requires a `Referer: tickchak.co.il/<event>` header but otherwise
     works over plain urllib — no browser needed.

We poll both per tick. If `enabled="1"`, every JSON-LD Offer flagged
in_stock is reported as a virtual seat (one per ticket TYPE — tickchak
doesn't expose per-seat granularity, just per-type buckets). If
`enabled="0"`, we return [] regardless of what JSON-LD claims, which
suppresses the false-positive "drop" the SEO markup would otherwise
trigger on every sold-out event.

URL forms accepted:
  https://tickchak.co.il/mada26          — slug
  https://tickchak.co.il/mada26?ref=...  — slug + tracking params (stripped)
  https://tickchak.co.il/103350          — numeric event id
  mada26                                  — bare slug shorthand

Note on slug vs numeric: both reach the same event, but we DON'T
auto-dedupe — a watcher added as "mada26" and one added as "103350"
will both poll. Pick one form per event.
"""
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SOURCE_NAME = "tickchak"
SITE_BASE = "https://tickchak.co.il"
CACHE_DIR = Path(__file__).parent / "tm_cache"
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 20

# Festival/hub support. Some tickchak events (e.g. the Chutzot Hayotzer
# festival) live under a hub: the plain event URL 302-redirects to
# home.tickchak.co.il/<slug>, so the normal event-form scrape can't see
# them. The hub's anonymous JSON feed exposes an eid->event_hash map plus
# per-event status flags; the hash unlocks the same anonymous
# /ajax/form/init counts as a standalone event. All pure HTTP — works
# headless, no browser.
API_LIVE_BASE = "https://api-live.tickchak.co.il"
HOME_HOST = "home.tickchak.co.il"
# tickchak is inconsistent about redirecting a festival event to its hub —
# the SAME event sometimes serves its own page directly. So festival
# membership, once discovered (a redirect to home.tickchak.co.il), is
# remembered STICKILY on disk and never downgraded; only events we've never
# seen under a hub get re-probed, on a short negative TTL so they're caught
# the next time they do redirect.
FESTIVAL_NEG_TTL = 300         # re-probe a not-yet-festival event every 5 min
FESTIVAL_FEED_TTL = 25         # dedupe the ~60 KB feed across a tick's watchers
FESTIVAL_SNAP_TTL = 20         # reuse a show's full snapshot within one tick

_FEST_MEMBER_FILE = CACHE_DIR / "festival_members.json"
_fest_members = None    # {event_code: slug} — sticky, loaded from disk
_fest_slug_cache = {}   # event_code -> (ts, None) — short negative cache only
_fest_feed_cache = {}   # slug -> (ts, {"hash_map": {...}, "events": {...}})
_fest_snap_cache = {}   # event_code -> (ts, labels_payload)

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

def _http_get_html(url, referer=None):
    """Plain GET → decoded text. The static.tickchak.co.il endpoint 403s
    without a tickchak.co.il Referer; pass it through here."""
    headers = dict(REQUEST_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
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


def _http_get_final(url, referer=None):
    """Like ``_http_get_html`` but also returns the post-redirect URL —
    used to detect when an event slug bounces to a festival hub."""
    headers = dict(REQUEST_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise TickchakError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise TickchakError(f"network error: {e.reason}") from e
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return resp.geturl(), raw.decode("utf-8", errors="replace")


_EV_JS_RE = re.compile(
    # The static URL includes dots in the middle ("…_he.js_<uuid>.js"),
    # so the character class must include `.` — without it the regex
    # stops at the first `.js` infix and we never find the script tag.
    r'src=["\'](https://static\.tickchak\.co\.il/js/ev_[A-Za-z0-9_.\-]+\.js)["\']',
    re.IGNORECASE,
)
_FORM_BUTTON_RE = re.compile(r"tickchak_form_button\s*=\s*(\{[^}]*\})")


def _fetch_event_meta_from_static_js(event_code, page_html):
    """Returns ``(form_button_dict, event_hash)`` from the live cached
    event JS. Both pieces live in the same file so we fetch it once.

    The JS URL is dynamic (cache-busted with a UUID per response) so we
    extract it from the page HTML each time, then GET with the right
    Referer header."""
    m = _EV_JS_RE.search(page_html)
    if not m:
        return None, None
    try:
        js = _http_get_html(m.group(1), referer=f"{SITE_BASE}/{event_code}")
    except TickchakError:
        return None, None
    fb_match = _FORM_BUTTON_RE.search(js)
    fb = None
    if fb_match:
        try:
            fb = json.loads(fb_match.group(1))
        except json.JSONDecodeError:
            fb = None
    eh_match = re.search(r'tickchak_event_hash\s*=\s*["\']([^"\']+)', js)
    return fb, (eh_match.group(1) if eh_match else None)


def _fetch_form_button_state(event_code, page_html):
    """Back-compat wrapper — only the form_button half. Used by callers
    that don't need the event_hash."""
    fb, _ = _fetch_event_meta_from_static_js(event_code, page_html)
    return fb


def _fetch_form_init(event_code, event_hash):
    """Call ``POST /ajax/form/init`` (the same endpoint the iframe form
    uses to populate ticket pickers). Returns the parsed JSON or None.

    The response includes a ``tickets`` list with per-type counts:
      ``amount`` — capacity for that type at this venue
      ``sold`` — how many have been bought
      ``amount_avaliable`` — current count (sometimes inflated for
          unlimited donation tiers; cap at amount when summing)
      ``active`` — "0" means the type is paused/hidden; skip these
      ``title``, ``price``, ``tid`` — display + identity fields
    """
    if not event_hash:
        return None
    body = urllib.parse.urlencode({
        "event": event_hash,
        "lang": "he",
        "source": "landing",
    }).encode()
    req = urllib.request.Request(
        f"{SITE_BASE}/ajax/form/init",
        data=body,
        method="POST",
        headers={
            "User-Agent": REQUEST_HEADERS["User-Agent"],
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "gzip, deflate",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{SITE_BASE}/{event_code}",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _normalize_form_init_tickets(init):
    """Flatten /ajax/form/init's tickets list into a {block_label: dict}
    map with normalized fields.

    Two kinds of types come back from tickchak:
      * Capped types (``amount > 0``) — real seats. Count toward
        ``total_capacity`` and ``total_available``.
      * Uncapped types (``amount == 0``, e.g. donation tiers, "guest
        of friend" entries) — no inventory limit. tickchak reports
        ``amount_avaliable`` as a sentinel (typically 500_000) for
        these. We keep them in the blocks map (so the filter modal
        can list them) but don't include them in the seat totals — a
        donation isn't a seat.

    Returns ``(blocks_map, total_capacity, total_available)``."""
    out = {}
    cap = 0
    avail = 0
    for t in (init.get("tickets") if isinstance(init, dict) else []) or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("active") or "1") != "1":
            continue
        try:
            amount = int(t.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        try:
            raw_avail = int(t.get("amount_avaliable") or 0)
        except (TypeError, ValueError):
            raw_avail = 0
        try:
            sold = int(t.get("sold") or 0)
        except (TypeError, ValueError):
            sold = 0

        if amount > 0:
            # Real seats — cap availability at the type's capacity (some
            # rows over-report when the venue moves seats around).
            avail_capped = max(0, min(raw_avail, amount))
            cap += amount
            avail += avail_capped
            unlimited = False
        else:
            # Donation / unlimited entry — present but not a seat.
            avail_capped = 1 if raw_avail > 0 else 0
            unlimited = True

        title = (t.get("title") or "").strip() or "כרטיס"
        price = _parse_price(t.get("price"))
        key = _block_label(title, price)
        out[key] = {
            "name": title,
            "price": price,
            "currency": "ILS",
            "amount": amount,
            "sold": sold,
            "available": avail_capped,
            "active": True,
            "unlimited": unlimited,
            "tid": t.get("tid"),
        }
    return out, cap, avail


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


# --- Festival / hub support ----------------------------------------------

def _israel_time_text(epoch_seconds):
    """Format an epoch as venue-local (Asia/Jerusalem) 'YYYY-MM-DD HH:MM'.
    The hub feed gives UTC epochs; without a fixed zone the dashboard's own
    machine timezone would shift the time. Falls back to IDT (+3) when the
    tz database isn't installed."""
    from datetime import timezone, timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Asia/Jerusalem")
    except Exception:
        tz = timezone(timedelta(hours=3))  # IDT; festival season is summer
    return datetime.fromtimestamp(epoch_seconds, tz).strftime("%Y-%m-%d %H:%M")


def _load_fest_members():
    global _fest_members
    if _fest_members is None:
        try:
            _fest_members = json.loads(_FEST_MEMBER_FILE.read_text(encoding="utf-8"))
        except Exception:
            _fest_members = {}
    return _fest_members


def _remember_fest_member(event_code, slug):
    members = _load_fest_members()
    if members.get(str(event_code)) != slug:
        members[str(event_code)] = slug
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            _FEST_MEMBER_FILE.write_text(json.dumps(members, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def _resolve_festival(event_code):
    """Return the festival hub slug for this event, else None.

    Sticky: once an event has redirected to a home.tickchak.co.il hub we
    record (event_code -> slug) on disk and always treat it as that
    festival, because tickchak flip-flops between redirecting the event to
    its hub and serving the event page directly — without stickiness a
    festival watcher would oscillate between representations and fire
    spurious drops. Events we've never seen under a hub are re-probed on a
    short negative TTL so the next redirect catches them."""
    ev = str(event_code)
    members = _load_fest_members()
    if ev in members:
        return members[ev]
    hit = _fest_slug_cache.get(ev)
    if hit and (time.time() - hit[0] < FESTIVAL_NEG_TTL):
        return None
    try:
        final_url, _ = _http_get_final(perf_url(ev))
        parts = urlparse(final_url)
        if parts.netloc.lower() == HOME_HOST:
            slug = (parts.path or "").strip("/").split("/", 1)[0] or None
            if slug:
                _remember_fest_member(ev, slug)
                return slug
    except TickchakError:
        # Network hiccup — don't start the negative timer; retry next call.
        return None
    _fest_slug_cache[ev] = (time.time(), None)
    return None


def _iter_strings(o):
    if isinstance(o, dict):
        for v in o.values():
            yield from _iter_strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _iter_strings(v)
    elif isinstance(o, str):
        yield o


def _fetch_festival_feed(slug):
    """GET the anonymous hub feed and parse it into
    ``{"hash_map": {eid: event_hash}, "events": {eid: {...status...}}}``.

    The hash map lives in an inline ``var HASH = {"<eid>":"<hash>", ...}``
    script blob (commented ``eid -> tick_encrypt('event'+eid)``); the hash
    is what /ajax/form/init wants. Cached briefly so the festival's
    watchers don't each refetch the feed every tick."""
    hit = _fest_feed_cache.get(slug)
    if hit and (time.time() - hit[0] < FESTIVAL_FEED_TTL):
        return hit[1]
    headers = dict(REQUEST_HEADERS)
    headers["Accept"] = "application/json"
    headers["Referer"] = f"https://{HOME_HOST}/"
    req = urllib.request.Request(f"{API_LIVE_BASE}/page/{slug}", headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        raise TickchakError(f"festival feed fetch failed: {e}") from e
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8", errors="replace"))

    hash_map = {}
    blob = next((s for s in _iter_strings(data) if "var HASH" in s), None)
    if blob:
        i = blob.find("var HASH")
        start = blob.find("{", i)
        end = blob.find("}", start)
        if start != -1 and end != -1:
            for m in re.finditer(r'"(\d{4,8})"\s*:\s*"([^"]+)"', blob[start:end + 1]):
                hash_map[m.group(1)] = m.group(2)

    events = {}

    def _walk(o):
        if isinstance(o, dict):
            if "eid" in o and ("soldOut" in o or "title" in o):
                eid = str(o.get("eid"))
                if eid not in events:
                    ven = o.get("venue")
                    events[eid] = {
                        "title": o.get("title") or o.get("name"),
                        "venue": (ven.get("title") if isinstance(ven, dict) else None) or o.get("location"),
                        "timeStart": o.get("timeStart") or o.get("date"),
                        "soldOut": int(o.get("soldOut") or 0),
                        "lastTickets": int(o.get("lastTickets") or 0),
                        "preSale": int(o.get("preSale") or 0),
                        "entityId": o.get("entityId"),
                    }
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(data)
    feed = {"hash_map": hash_map, "events": events}
    _fest_feed_cache[slug] = (time.time(), feed)
    return feed


def _festival_status(ev_info, total_available, have_counts):
    """Derive a single status flag. The hub's own soldOut/lastTickets
    flags are authoritative for the headline; live counts corroborate
    sold-out (sometimes the flag lags)."""
    sold_out = bool(ev_info.get("soldOut")) or (have_counts and total_available <= 0)
    if sold_out:
        return "soldout"
    if ev_info.get("lastTickets"):
        return "lasttickets"
    return "available"


def _festival_stub(event_code, slug, lang):
    """Minimal festival payload for when the feed is unreachable and we
    have no cached snapshot yet. Status defaults to 'available' so the
    first (baseline) tick stores it without a spurious ping; real data
    corrects it on the next successful fetch."""
    return {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME, "event_code": str(event_code), "perf_code": "0",
        "lang": lang, "festival": True, "festival_slug": slug,
        "meta": {
            "eventName": "", "venueName": "", "venueCity": "",
            "firstPerfMs": None, "firstPerfText": "",
            "totalSeats": None, "availSeats": None,
            "status": "unknown", "festival": True, "festivalStatus": "available",
        },
        "blocks": {},
    }


def _festival_snapshot(event_code, lang="iw"):
    """Full festival labels payload (meta + per-type blocks) for a hub
    event, or None if this isn't a festival event. Pure HTTP. Writes the
    on-disk labels cache so get_labels() stays fresh, and an in-memory
    snapshot cache so one tick's fetch_selectable_seats + get_labels share
    a single fetch."""
    ev = str(event_code)
    hit = _fest_snap_cache.get(ev)
    if hit and (time.time() - hit[0] < FESTIVAL_SNAP_TTL):
        return hit[1]
    slug = _resolve_festival(ev)
    if not slug:
        return None
    try:
        feed = _fetch_festival_feed(slug)
    except TickchakError:
        # Known festival event but the feed is momentarily unreachable —
        # return stale data if we have it, else a minimal stub. Never fall
        # back to the legacy event-page path (it redirects to the hub and
        # yields the wrong event).
        return hit[1] if hit else _festival_stub(ev, slug, lang)

    ev_info = (feed.get("events") or {}).get(ev) or {}
    event_hash = (feed.get("hash_map") or {}).get(ev)

    blocks = {}
    total_capacity = 0
    total_available = 0
    init = _fetch_form_init(ev, event_hash) if event_hash else None
    have_counts = bool(init and isinstance(init.get("tickets"), list))
    if have_counts:
        types, total_capacity, total_available = _normalize_form_init_tickets(init)
        for key, info in types.items():
            blocks[key] = {
                "name": info["name"],
                "price": info["price"],
                "currency": info.get("currency") or "ILS",
                "amount": info["amount"],
                "sold": info["sold"],
                "available": info["available"],
                "unlimited": info.get("unlimited", False),
                "availability": "in_stock" if info["available"] > 0 else "out_of_stock",
            }

    status = _festival_status(ev_info, total_available, have_counts)

    ts = ev_info.get("timeStart")
    perf_ms = None
    perf_text = ""
    try:
        if ts:
            perf_ms = int(ts) * 1000
            perf_text = _israel_time_text(int(ts))
    except (TypeError, ValueError, OSError):
        perf_ms, perf_text = None, ""

    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": ev,
        "perf_code": "0",
        "lang": lang,
        "festival": True,
        "festival_slug": slug,
        "meta": {
            "eventName": ev_info.get("title") or "",
            "venueName": ev_info.get("venue") or "",
            "venueCity": "",
            "firstPerfMs": perf_ms,
            "firstPerfText": perf_text,
            "totalSeats": (total_capacity or None),
            "availSeats": (total_available if have_counts else None),
            "status": status,
            "festival": True,
            "festivalStatus": status,
        },
        "blocks": blocks,
    }
    try:
        _cache_path(ev, lang).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except OSError:
        pass
    _fest_snap_cache[ev] = (time.time(), payload)
    return payload


def _festival_seats(payload):
    """One status-encoded virtual seat. Encoding the status into the seat
    key (via ``block``) makes the standard add/remove diff fire a
    notification on every status transition — sold-out, last-tickets, and
    available-again — without touching the core tick loop."""
    meta = (payload or {}).get("meta") or {}
    status = meta.get("festivalStatus") or "available"
    label = {
        "available": "GA available",
        "lasttickets": "GA last tickets",
        "soldout": "GA sold out",
    }.get(status, "GA available")
    return [{
        "block": label,
        "row": "GA",
        "seat": "1",
        "festival": True,
        "status": status,
        "qty_available": meta.get("availSeats"),
        "price": None,
    }]


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
    """Return one normalized "seat" dict per active in-stock ticket type.

    Three signals stacked:

      1. ``form_button.enabled`` (from the live cached event JS) — when
         "0", the buy button is disabled site-wide; return []
         regardless of what the per-type API claims, because users can't
         actually buy through the public flow.
      2. ``/ajax/form/init`` per-type quantities — the same endpoint the
         iframe form uses to populate the ticket picker. Has real
         ``amount_avaliable``, ``amount``, ``sold`` per type.
      3. JSON-LD as fallback when form/init is unreachable.

    We emit ONE virtual seat per active type with available > 0. The
    diff is therefore type-level, not unit-level — going from 43 → 50
    units of the same type doesn't trigger a notification, only a type
    flipping from 0 → >0 does. That suits tickchak's GA-style sale model
    (no per-seat selection in the booking flow) and keeps notification
    spam tolerable.
    """
    # Festival/hub events redirect away from the normal event page — serve
    # them from the hub feed instead (one status-encoded seat).
    fest = _festival_snapshot(event_code)
    if fest is not None:
        return _festival_seats(fest)

    html = _http_get_html(perf_url(event_code))
    fb, event_hash = _fetch_event_meta_from_static_js(event_code, html)
    fb = fb or {}
    if str(fb.get("enabled", "")).strip() != "1":
        return []

    init = _fetch_form_init(event_code, event_hash)
    if init and isinstance(init.get("tickets"), list):
        types, _cap, _avail = _normalize_form_init_tickets(init)
        out = []
        for key, info in types.items():
            if (info.get("available") or 0) <= 0:
                continue
            out.append({
                "block": key,
                "row": "GA",
                "seat": "1",
                "price": info.get("price"),
                "qty_available": info.get("available"),
                "raw": {"tid": info.get("tid"), "title": info.get("name")},
            })
        if out:
            return out
        # Fall through to JSON-LD if form/init reported zero but the
        # button is live — better to ping than miss a drop.

    # Fallback: JSON-LD Offers (used to be the primary, kept as backup
    # for when form/init returns nothing parseable).
    ld = _extract_jsonld_event(html) or {}
    out = []
    for o in _normalize_offers(ld.get("offers")):
        avail = o.get("availability")
        if avail and not _availability_in_stock(avail):
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
    if not out:
        # Button is live but we have no per-type data at all — emit a
        # single opaque entry so the watcher still pings on the flip.
        out.append({
            "block": (fb.get("original_title") or "כרטיסים").strip() or "tickets",
            "row": "GA",
            "seat": "1",
            "price": None,
            "raw": fb,
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
    filter modal don't need per-source branching.

    Prefer /ajax/form/init for accurate per-type capacity + availability
    counts. Falls back to JSON-LD Offers if form/init is unreachable.
    The dashboard reads ``meta.totalSeats`` (capacity) and
    ``meta.availSeats`` (capped current quantity) from this payload to
    show real "X / Y" ratios in the watchers table.
    """
    # Festival/hub events: counts + status come from the hub feed +
    # anonymous form/init, not the (redirecting) event page.
    fest = _festival_snapshot(event_code, lang)
    if fest is not None:
        return fest

    html = _http_get_html(perf_url(event_code))
    ld = _extract_jsonld_event(html) or {}
    fb, event_hash = _fetch_event_meta_from_static_js(event_code, html)
    fb = fb or {}
    button_enabled = str(fb.get("enabled", "")).strip() == "1"

    blocks = {}
    total_capacity = 0
    total_available = 0
    init = _fetch_form_init(event_code, event_hash)
    if init and isinstance(init.get("tickets"), list):
        types, total_capacity, raw_avail = _normalize_form_init_tickets(init)
        # Even when the per-type API reports availability, the global
        # form_button is the authoritative public-buyability gate. Sold-out
        # events sometimes still expose `amount_avaliable > 0` for held
        # seats that aren't actually for sale.
        total_available = raw_avail if button_enabled else 0
        for key, info in types.items():
            available = info["available"] if button_enabled else 0
            blocks[key] = {
                "name": info["name"],
                "price": info["price"],
                "currency": info.get("currency") or "ILS",
                "amount": info["amount"],
                "sold": info["sold"],
                "available": available,
                "availability": "in_stock" if available > 0 else "out_of_stock",
            }
    else:
        # Legacy path: JSON-LD only. No real seat counts; report ticket-type
        # totals as before (one virtual seat per type) so the UI still has
        # something useful to display.
        for o in _normalize_offers(ld.get("offers")):
            name = (o.get("name") or "").strip() or "כרטיס"
            avail_raw = o.get("availability")
            ld_in_stock = _availability_in_stock(avail_raw) if avail_raw else True
            in_stock = button_enabled and ld_in_stock
            price = _parse_price(o.get("price"))
            key = _block_label(name, price)
            existing = blocks.get(key)
            if existing and existing.get("availability") == "in_stock" and not in_stock:
                continue
            blocks[key] = {
                "name": name,
                "price": price,
                "currency": o.get("priceCurrency") or "ILS",
                "availability": "in_stock" if in_stock else "out_of_stock",
            }
        # Approximate totals when form/init unavailable.
        total_capacity = len(blocks)
        total_available = sum(1 for b in blocks.values() if b.get("availability") == "in_stock")

    any_in_stock = total_available > 0

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
            # Real seat counts when /ajax/form/init is reachable
            # (sum of per-type ``amount`` for capacity, capped sum of
            # ``amount_avaliable`` for current). The watchers table
            # reads availSeats / totalSeats to render "5000 / 6495".
            # When form/init isn't available we fall back to
            # ticket-type counts (e.g. "26 / 28").
            "totalSeats": (total_capacity or None),
            "availSeats": total_available,
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
