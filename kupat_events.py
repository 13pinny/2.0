"""kupat.co.il new-event monitor — pure-HTTP event-listing fetcher.

Companion to pacha_events.py / tm_events.py; the diff-and-ping loop lives in
app.py (`run_il_events`). Two anonymous endpoints:

  GET https://tickets.kupat.co.il/api/features
      → the ENTIRE current catalog (~70-80 features) as anonymous JSON.
        Unlike the seats/presentation endpoints (403 outside the in-page
        fetch chain — see kupat.py's _browse_capture), this listing endpoint
        answers plain urllib. Each feature has id, name, additionalName,
        urlName, code, categories (numeric ids), featureTypeId, and
        closestPresentationDateTime ("YYYY-MM-DD HH:MM", venue-local).
        Events often show up here DAYS before the sale is official — that
        early visibility is the point of the 'new' ping.

  GET https://www.kupat.co.il/  (the WordPress homepage)
      → the "officially on sale" signal. Promoted shows appear as
        <article class="item-show … show_artist-<slug>" aria-label="<name>">
        tiles with an <a href="…/show/<slug>"> link and an <img
        class="item-image"> banner. A catalog feature is matched to a tile
        by normalized urlName vs the tile's /show/ slug or artist class
        ("peer tasi" → "peertasi"), with the Hebrew name vs aria-label as
        fallback. No per-show page fetches needed.

`on_sale` therefore means "promoted on the kupat.co.il homepage": a feature
that's only in the catalog pings 'new' (marked not-yet-official), and its
later homepage debut pings 'onsale' — with the tile's banner graphic
attached. When the homepage fetch fails, on_sale is returned as None and
check_on_sale raises, so the diff loop keeps each event's stored state
instead of mis-recording a flip.

CLI probe:  python kupat_events.py [--json]
"""
import gzip
import json
import re
import sys
import urllib.error
import urllib.request

import kupat

SOURCE_NAME = "kupat"
FEATURES_URL = kupat.API_BASE + "/features"
HOME_URL = "https://www.kupat.co.il/"

_TILE_SLUG_RE = re.compile(r'href="https://www\.kupat\.co\.il/show/([a-z0-9_\-]+)"', re.IGNORECASE)
_TILE_ARTIST_RE = re.compile(r'show_artist-([a-z0-9_\-]+)')
_TILE_IMG_RE = re.compile(r'<img[^>]+class="[^"]*item-image[^"]*"[^>]+src="([^"]+)"', re.IGNORECASE)
_ARIA_RE = re.compile(r'aria-label="([^"]*)"')


class KupatEventsError(RuntimeError):
    pass


def _get(url):
    req = urllib.request.Request(url, headers=kupat.REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=kupat.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw
    except urllib.error.HTTPError as e:
        raise KupatEventsError(f"{url} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise KupatEventsError(f"{url} unreachable: {e.reason}") from e


def _norm(s):
    """Slug-insensitive key: lowercase, strip everything but letters/digits
    (Hebrew included). 'peer tasi' == 'peer-tasi' == 'peertasi'."""
    return re.sub(r"[\W_]+", "", str(s or "").lower(), flags=re.UNICODE)


def fetch_homepage_tiles():
    """Parse the kupat.co.il homepage into
    ``{match_key: image_url}`` — one entry per normalized slug / artist
    class / aria-label of every promoted show tile. Raises on network
    trouble or when a 200 page parses to zero tiles (a site redesign must
    read as a fetch failure, never as 'nothing is promoted')."""
    html = _get(HOME_URL).decode("utf-8", errors="replace")
    # The tile's own <article> block ends before the img on some layouts —
    # scan a window past the match so the banner img is always in scope.
    keys = {}
    tiles = 0
    for m in re.finditer(r'<article[^>]+class="[^"]*item-show[^"]*"[^>]*>', html):
        block = html[m.start(): m.start() + 2500]
        slug_m = _TILE_SLUG_RE.search(block)
        if not slug_m:
            continue
        tiles += 1
        img_m = _TILE_IMG_RE.search(block)
        img = img_m.group(1) if img_m else ""
        for key_m in (slug_m.group(1),
                      *( [a.group(1)] if (a := _TILE_ARTIST_RE.search(block)) else [] ),
                      *( [l.group(1)] if (l := _ARIA_RE.search(block)) else [] )):
            k = _norm(key_m)
            if k and (k not in keys or img):
                keys[k] = img
    if not tiles:
        raise KupatEventsError("homepage parsed to 0 show tiles — layout change?")
    return keys


def fetch_events():
    """Current kupat catalog, normalized, with homepage-promotion state.
    Raises KupatEventsError on catalog trouble or a zero-feature parse (an
    API change must read as a fetch failure, never as 'all events
    removed'). A homepage failure alone degrades softly: events come back
    with on_sale=None and the diff loop keeps stored state."""
    raw = _get(FEATURES_URL)
    try:
        feats = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KupatEventsError("non-JSON from /api/features — API change?") from e
    if not isinstance(feats, list):
        raise KupatEventsError("unexpected /api/features shape — API change?")

    try:
        home = fetch_homepage_tiles()
    except Exception as e:
        print(f"[kupat_events] homepage fetch failed (catalog ok): {e}")
        home = None

    events = []
    for f in feats:
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        fid = str(f["id"])
        name = (f.get("name") or "").strip()
        extra = (f.get("additionalName") or "").strip()
        if extra:
            name = f"{name} — {extra}" if name else extra
        dt = (f.get("closestPresentationDateTime") or "").strip()

        if home is None:
            on_sale, image = None, ""
        else:
            match = next((k for k in (_norm(f.get("urlName")), _norm(f.get("name")))
                          if k and k in home), None)
            on_sale = match is not None
            image = home.get(match, "") if match else ""

        events.append({
            "source": SOURCE_NAME,
            "event_key": fid,
            "name": name,
            "venue": "",  # not in the features payload
            "date_text": dt,
            "first_date_ms": None,
            # True = promoted on the kupat.co.il homepage (officially on
            # sale); False = catalog-only so far; None = homepage unknown.
            "on_sale": on_sale,
            "image": image,
            "url": f"{kupat.SITE_BASE}/booking/features/{fid}",
        })
    if not events:
        raise KupatEventsError("features endpoint parsed to 0 events — API change?")
    return events


def check_on_sale(ev):
    """Called by the diff loop only when fetch_events returned on_sale=None
    (homepage fetch failed). Raising makes the loop fall back to each
    event's stored state instead of guessing."""
    raise KupatEventsError("kupat homepage unavailable this tick")


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    events = fetch_events()
    if "--json" in argv:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    n_home = sum(1 for e in events if e["on_sale"])
    print(f"{len(events)} features on {FEATURES_URL} — {n_home} promoted on the homepage\n")
    for ev in events:
        mark = "HOME" if ev["on_sale"] else ("?   " if ev["on_sale"] is None else "    ")
        img = "img" if ev.get("image") else "   "
        print(f"  {mark} {img} {ev['event_key']:>6}  {ev['name']:<45.45} {ev['date_text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
