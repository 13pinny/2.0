"""haku (hakuapp.com) registration drop checker — built for the Houston Marathon.

Watches a haku-hosted race's registration availability and pings when spots
come back. Two signals, both diffed as pseudo-seats by the generic tick:

1. REGISTRATION OPTION CARDS — the server-rendered page
   https://events.hakuapp.com/events/<key>/registration_options lists one
   card per registration path (Open Registration / Run for a Reason charity
   program / Legacy / Training Challenge), each with free-text status prose
   ("Chevron Houston Marathon & Aramco Houston Half Marathon are sold out.
   This also includes our Run for a Reason Charity Program Entries.").
   Each card becomes one pseudo-seat whose key includes a hash of the card
   text, so ANY wording change (e.g. the sold-out sentence disappearing)
   reads as removed+added and pings with the new text in the seat line.

2. RFAR CHARITY AVAILABILITY — the charity index at
   /events/<key>/charity_partners exposes an anonymous search endpoint,
   POST /events/<key>/beneficiaries_search, whose
   `is_available_for_registration=true` + `event_sub_types[]=<distance id>`
   filter returns exactly the charities that currently have entries for
   that distance. One pseudo-seat per (distance, charity): a charity
   getting marathon/half spots back = a new seat = a drops ping naming the
   charity. As of 2026-08 the marathon and half queries return ZERO
   charities (matches the site's sold-out notice) and the 5K returns 24,
   so the filter genuinely discriminates per distance.

Empirical facts (probed 2026-08-08):
- Everything is plain HTTP — no Cloudflare/Akamai challenge, no cookies, no
  login, works from urllib on any machine (VPS-safe, no Chrome).
- The `Accept` header is REQUIRED: Rails content negotiation answers
  406 Not Acceptable when it's missing. X-Requested-With marks the search
  request as the AJAX the SPA sends; keep both.
- beneficiaries_search takes a form-encoded body; the `pagination_key`
  query param the in-page JS appends is optional. `per_page=100` avoids
  pagination (Houston has ~70 charities total).
- Distance ids (`event_sub_types[]`): 1 = 5K, 4 = Half Marathon,
  5 = Marathon, 17 = Other — read off the filter checkboxes on the charity
  index. We watch Marathon + Half only: the 5K is generally open anyway and
  its two dozen available charities would just be baseline noise.
- An EMPTY search result is a structured fragment containing
  "We couldn't find charity partners matching your search." /
  cp-no-results-img-container; real hits contain beneficiary-block divs.
  Anything else (challenge page, error) raises HakuError so a blocked
  response can never fake "nothing available" (state stays untouched and
  the recovery tick can't spray phantom re-adds).
- haku.ly short links 302 to register.hakuapp.com/?event=<key>&... —
  parse_url follows them once to learn the event key.

Seat model: every seat here is a PHYSICAL pseudo-seat (no `ga`/`festival`
flag) on purpose — the mixed-venue GA strip in the tick would silence card
pings whenever a charity seat exists in the snapshot, and the per-seat
cool-down (which only covers physical seats) is exactly the guard we want
against a charity's availability flapping at the last-spot boundary.
Everything routes to the `drops` channel; a registration watcher has no
non-actionable state worth a separate status ping.

seat_key MUST stay f"{block}|{row}|{seat}" — db.tm_replace_seat_state
rebuilds keys from those three fields itself and anything else never
matches (every tick would re-ping the whole snapshot).

CLI probe:  .venv\\Scripts\\python haku.py <url-or-20-hex-key>
"""

import gzip
import hashlib
import html as _html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE_BASE = "https://events.hakuapp.com"

# The 2027 Houston Marathon Weekend of Events. Any *houstonmarathon.com URL
# pasted into the add-watcher box maps here so the user never has to dig the
# haku key out of the site's REGISTER button.
HOUSTON_EVENT_KEY = "cd380df4bf4a0ee67276"

# Distances whose RFAR charity availability we watch (label, event_sub_type).
RFAR_DISTANCES = (("Marathon", 5), ("Half Marathon", 4))

CACHE_DIR = Path(__file__).resolve().parent / "tm_cache"
CACHE_TTL_SECONDS = 3600

# Pseudo-seats are 1-per-signal; the group-size default of 2 would eat them.
DEFAULT_FILTERS = {"min_group_size": 1}

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    # Required — the Rails backend 406s requests with no Accept header.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}


class HakuError(RuntimeError):
    pass


def _http(url, data=None, extra_headers=None, timeout=30):
    """GET (or POST when data is given) returning decoded text + final URL."""
    headers = dict(REQUEST_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return raw.decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        raise HakuError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise HakuError(f"network error for {url}: {e.reason}") from e


_EVENT_KEY_RE = re.compile(r"[a-f0-9]{20}")


def parse_url(url):
    """Accept any URL that identifies a haku event and return (event_key, "0").

    Handles events.hakuapp.com/events/<key>/..., register/donate.hakuapp.com
    ?event=<key>, haku.ly short links (followed once), *houstonmarathon.com
    (mapped to the known key), and a bare 20-hex key.
    """
    s = (url or "").strip()
    if not s:
        raise HakuError("empty URL")
    low = s.lower()

    if re.fullmatch(r"[a-f0-9]{20}", low):
        return low, "0"
    if "houstonmarathon" in low:
        return HOUSTON_EVENT_KEY, "0"

    m = re.search(r"hakuapp\.com/events/([a-f0-9]{20})", low)
    if m:
        return m.group(1), "0"
    m = re.search(r"[?&]event=([a-f0-9]{20})", low)
    if m:
        return m.group(1), "0"

    if "haku.ly/" in low:
        # Short link — follow the redirect chain and read the key off the
        # final URL. One-time cost at watcher-create; the tick never pays it.
        if not low.startswith("http"):
            s = "https://" + s
        _, final_url = _http(s)
        m = _EVENT_KEY_RE.search(final_url)
        if m:
            return m.group(0), "0"
        raise HakuError(f"no event key in haku.ly redirect target {final_url}")

    raise HakuError(f"unrecognized haku URL: {url}")


def perf_url(event_code, perf_code="0"):
    return f"{SITE_BASE}/events/{event_code}/registration_options"


def _collapse(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_tags(fragment):
    return _collapse(_html.unescape(re.sub(r"<[^>]+>", " ", fragment)))


# One card = one <a class="... registration-option-links"> wrapping a title
# span and a description-text paragraph.
_CARD_RE = re.compile(
    r'<a[^>]+class="[^"]*registration-option-links[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_CARD_TITLE_RE = re.compile(
    r'<span[^>]*class="[^"]*text-uppercase[^"]*"[^>]*>(.*?)</span>', re.DOTALL
)
_CARD_TEXT_RE = re.compile(
    r'<p[^>]*class="[^"]*description-text[^"]*"[^>]*>(.*?)</p\s*>\s*</div>', re.DOTALL
)


def fetch_option_cards(event_code):
    """[(title, text), ...] for each registration-path card on the event's
    registration_options page. Raises if the page doesn't look like haku."""
    page, _ = _http(perf_url(event_code))
    if "registration-option-links" not in page:
        if "Registration Options" in page or "registration_options" in page:
            return []  # legit haku page that currently has no cards
        raise HakuError("registration_options page did not look like haku "
                        "(challenge or error page?)")
    cards = []
    for m in _CARD_RE.finditer(page):
        body = m.group(1)
        t = _CARD_TITLE_RE.search(body)
        title = _strip_tags(t.group(1)) if t else "Registration"
        x = _CARD_TEXT_RE.search(body)
        text = _strip_tags(x.group(1)) if x else _strip_tags(body)
        cards.append((title.rstrip(":"), text))
    return cards


_NO_RESULTS_MARKERS = ("cp-no-results", "find charity partners")
_BENEFICIARY_RE = re.compile(r"beneficiary_key=([a-f0-9]+)")
_CHARITY_NAME_RE = re.compile(r'cp-list-text">([^<]+)<')


def fetch_available_charities(event_code, sub_type_id):
    """Charities that currently have entries for one distance:
    [(beneficiary_key, name), ...]. Raises on any unrecognizable response."""
    body = urllib.parse.urlencode(
        [
            ("q", ""),
            ("is_local", "true"),
            ("is_national", "true"),
            ("is_international", "true"),
            ("event_sub_types[]", sub_type_id),
            ("is_available_for_registration", "true"),
            ("store_last_path", "false"),
            ("per_page", 100),
            ("page", 1),
        ]
    ).encode()
    frag, _ = _http(
        f"{SITE_BASE}/events/{event_code}/beneficiaries_search",
        data=body,
        extra_headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{SITE_BASE}/events/{event_code}/charity_partners",
        },
    )
    keys = _BENEFICIARY_RE.findall(frag)
    if not keys:
        if any(mk in frag for mk in _NO_RESULTS_MARKERS):
            return []
        raise HakuError("beneficiaries_search returned neither charities nor "
                        "the no-results fragment (blocked?)")
    names = [_html.unescape(n).strip() for n in _CHARITY_NAME_RE.findall(frag)]
    out, seen = [], set()
    for i, key in enumerate(keys):
        if key in seen:
            continue
        seen.add(key)
        name = names[i] if i < len(names) else key
        out.append((key, name))
    return out


def fetch_selectable_seats(event_code, perf_code="0"):
    """One pseudo-seat per registration-option card (keyed on its text) plus
    one per charity that has entries for a watched distance. All physical
    (no `ga` flag) — see the module docstring for why."""
    seats = []
    for title, text in fetch_option_cards(event_code):
        digest = hashlib.sha1(f"{title}|{text}".encode()).hexdigest()[:8]
        label = f"{title}: {text}" if text else title
        if len(label) > 160:
            label = label[:157] + "..."
        seats.append({
            "block": label,
            "row": digest,
            "seat": "1",
            "price": None,
            "kind": "card",
        })
    for dist_name, sub_id in RFAR_DISTANCES:
        for bkey, name in fetch_available_charities(event_code, sub_id):
            seats.append({
                "block": f"RFAR {dist_name}: {name}",
                "row": bkey[:8],
                "seat": "1",
                "price": None,
                "kind": "rfar",
                "beneficiary_key": bkey,
            })
    return seats


def seat_key(seat):
    # Must mirror db.tm_replace_seat_state's block|row|seat rebuild exactly.
    return f"{seat.get('block', '')}|{seat.get('row', '')}|{seat.get('seat', '')}"


def _cache_path(event_code, lang):
    return CACHE_DIR / f"haku_{event_code}_{lang}.json"


_EVENT_NAME_RE = re.compile(r"registering for the\s+(.+?)\s*[.<]", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)


def fetch_fresh(event_code, lang="iw"):
    page, _ = _http(perf_url(event_code))
    m = _EVENT_NAME_RE.search(page)
    if m:
        event_name = _strip_tags(m.group(1))
    else:
        t = _TITLE_RE.search(page)
        event_name = _strip_tags(t.group(1)) if t else f"haku {event_code}"
    payload = {
        "meta": {
            "eventName": event_name,
            "venueName": "hakuapp.com",
            "venueCity": "",
            "seatType": "registration",
            # firstPerfMs stays None on purpose: _purge_past_watchers would
            # auto-delete the watcher once the parsed date passes, and a
            # registration watch should outlive nothing but the user's
            # interest. firstPerfText is non-ISO so the fallback parse fails.
            "firstPerfMs": None,
            "firstPerfText": "registration watch",
            "status": "",
            "availSeats": None,
            "totalSeats": None,
        },
        # Block labels ARE the display strings (card text / charity names),
        # so there is nothing to translate — notify._block_info falls back
        # to the raw block label, which is exactly what we want shown.
        "blocks": {},
        "fetched_at": time.time(),
    }
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(event_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    """Labels payload (cached 1h). `missing_block` is ignored on purpose:
    blocks is intentionally empty (labels fall back to the raw block string),
    so honoring it would force a refetch on every single ping."""
    path = _cache_path(event_code, lang)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    fresh_enough = cached and (time.time() - cached.get("fetched_at", 0)) < CACHE_TTL_SECONDS
    if fresh_enough and not force:
        return cached
    try:
        return fetch_fresh(event_code, lang)
    except Exception:
        if cached:
            return cached
        return {"meta": {}, "blocks": {}}


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    name = meta.get("eventName") or "haku event"
    return f"{name} — registration"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1])
        sys.exit(1)
    ev, perf = parse_url(sys.argv[1])
    print(f"event_key={ev}")
    labels = get_labels(ev, perf, force=True)
    print(f"event: {event_summary(labels)}")
    print(f"url:   {perf_url(ev, perf)}")
    seats = fetch_selectable_seats(ev, perf)
    print(f"\n{len(seats)} pseudo-seat(s):")
    for s in seats:
        print(f"  [{s['kind']}] {s['block']}")
        print(f"         key = {seat_key(s)}")
