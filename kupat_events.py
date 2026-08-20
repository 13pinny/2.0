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

  GET https://www.kupat.co.il/  (the homepage; override with
                                 KARTIS_KUPAT_HOME_URL)
      → the "officially on sale" signal. Parsed by a two-strategy chain so
        a redesign degrades instead of blinding the monitor:

          'cards' — the WordPress card grid: <article class="item-show …
             show_artist-<slug>" aria-label="<name>"> holding an
             <a href="…/show/<slug>"> and an <img class="item-image">
             banner. Precise, and the layout as of 2026-07.
          'links' — layout-agnostic fallback used only when 'cards' finds
             nothing: every /show/<slug> link anywhere on the page, with
             the name and banner lifted from the surrounding markup. Any
             kupat.co.il subdomain and relative hrefs both count, since
             the regional sites (2207., jerusalem., …) share the scheme.

        A catalog feature is matched to a tile by normalized urlName vs
        the tile's /show/ slug or artist class ("peer tasi" → "peertasi"),
        with the Hebrew name vs aria-label as fallback. No per-show page
        fetches needed. `last_warning()` reports when the fallback carried
        the tick, so run_il_events can surface "layout changed" on
        /api/il-events/status instead of it only reaching stdout.

`on_sale` therefore means "promoted on the kupat.co.il homepage": a feature
that's only in the catalog pings 'new' (marked not-yet-official), and its
later homepage debut pings 'onsale' — with the tile's banner graphic
attached. When the homepage fetch fails, on_sale is returned as None and
check_on_sale raises, so the diff loop keeps each event's stored state
instead of mis-recording a flip.

CLI probe:  python kupat_events.py [--json]
            python kupat_events.py --home        (homepage tiles only)
            python scripts/probe_kupat_site.py   (full site fingerprint)
"""
import gzip
import html as html_mod
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import kupat

SOURCE_NAME = "kupat"
FEATURES_URL = kupat.API_BASE + "/features"
# Site-wide per-performance catalog — the same endpoint market.py reaches
# through a BrowserSession, but it answers plain urllib too (verified
# 2026-07). ~1 MB gzipped; feeds the "new date under a known event" diff.
PRESENTATIONS_URL = kupat.API_BASE + "/presentations/?locationId=0&isHold=0"
# The homepage host is a setting, not a constant: kupat runs regional
# sites on the same codebase (2207., jerusalem., …) and a redesign has
# moved the canonical landing page before.
HOME_URL = os.environ.get("KARTIS_KUPAT_HOME_URL", "").strip() or "https://www.kupat.co.il/"

# A show link on ANY kupat.co.il host, absolute or relative — group 1 is
# the href up to the slug, group 2 the slug itself. Slugs are sometimes
# percent-encoded Hebrew, so the character class can't be [a-z0-9_-].
_SHOW_HREF_RE = re.compile(
    r'href="((?:https?://[^"/]*kupat\.co\.il)?/show/([^"/?#]+))/?(?:[?#][^"]*)?"',
    re.IGNORECASE)
# Card container: <article>/<div>/<li class="… item-show …">. Nested
# matches are harmless — tiles are deduped by slug.
_CARD_RE = re.compile(r'<(?:article|div|li)[^>]+class="[^"]*item-show[^"]*"[^>]*>',
                      re.IGNORECASE)
_TILE_ARTIST_RE = re.compile(r'show_artist-([a-z0-9_\-]+)')
_TILE_IMG_RE = re.compile(r'<img[^>]+class="[^"]*item-image[^"]*"[^>]+src="([^"]+)"', re.IGNORECASE)
# Any <img> — the fallback strategy can't assume the 'item-image' class.
# Split in two so every source attribute on a tag is visible: one regex
# finds the tags, the other every candidate URL inside one (a single
# combined pattern only ever yields the first attribute per tag, which is
# exactly the placeholder on a lazy-loaded grid).
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_ATTR_RE = re.compile(
    r'(?:data-lazy-src|data-original|data-src|srcset|src)="([^"\s]+)', re.IGNORECASE)
_ARIA_RE = re.compile(r'aria-label="([^"]*)"')
_IMG_ALT_RE = re.compile(r'<img[^>]+alt="([^"]+)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Where one tile stops and the next begins, for the fallback's forward
# window. Deliberately excludes <div>, which wraps everything.
_TILE_BOUNDARY_RE = re.compile(
    r'<a[\s>]|<(?:article|li|section)[\s>]|</(?:article|li|section)>', re.IGNORECASE)

# Guard for the layout-agnostic fallback: a mega-menu or a sitemap block
# could otherwise turn hundreds of links into pseudo-events.
MAX_HOMEPAGE_TILES = 200

# Populated by fetch_homepage_tiles; read by last_warning() so a layout
# change surfaces on /api/il-events/status, not just in the log.
_LAST_HOMEPAGE = {"strategy": None, "tiles": 0, "error": None}


class KupatEventsError(RuntimeError):
    pass


# kupat.REQUEST_HEADERS asks for JSON, which is right for the API but can
# make a content-negotiating front end serve something other than the page
# a browser sees. The homepage fetch gets a browser's document headers.
_HTML_HEADERS = dict(kupat.REQUEST_HEADERS, **{
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Upgrade-Insecure-Requests": "1",
})


def _get(url, timeout=None, headers=None, with_meta=False):
    req = urllib.request.Request(url, headers=headers or kupat.REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout or kupat.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            if not with_meta:
                return raw
            return raw, {
                "status": resp.status,
                "url": resp.url,          # after redirects
                "headers": {k.lower(): v for k, v in resp.headers.items()},
            }
    except urllib.error.HTTPError as e:
        raise KupatEventsError(f"{url} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise KupatEventsError(f"{url} unreachable: {e.reason}") from e


def _norm(s):
    """Slug-insensitive key: lowercase, strip everything but letters/digits
    (Hebrew included). 'peer tasi' == 'peer-tasi' == 'peertasi'."""
    return re.sub(r"[\W_]+", "", str(s or "").lower(), flags=re.UNICODE)


def _tokens(s):
    """Word tokens for fuzzy name matching (lowercased, punctuation-free)."""
    return {t for t in re.split(r"[\W_]+", str(s or "").lower(), flags=re.UNICODE)
            if len(t) > 1}


def _tokens_subsume(a, b):
    """True when one token set contains the other (both non-empty) — matches
    'ששון שאולוב' to 'ששון איפרם שאולוב' and 'ECHOES' to
    'ECHOES - PINK FLOYD THE WALL LIVE'."""
    return bool(a) and bool(b) and (a <= b or b <= a)


_TILE_TITLE_RE = re.compile(r'<h2[^>]+class="[^"]*item-title[^"]*"[^>]*>\s*<span>([^<]*)</span>',
                            re.IGNORECASE)


def _text(fragment, limit=120):
    """Visible text of an HTML fragment: tags stripped, entities decoded,
    whitespace collapsed."""
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub(" ", fragment or ""))).strip()[:limit]


def _clean(value, limit=120):
    """An attribute value (aria-label, alt) as display text."""
    return _WS_RE.sub(" ", html_mod.unescape(value or "")).strip()[:limit]


def _abs_url(href):
    return urllib.parse.urljoin(HOME_URL, href or "")


def _find_image(fragment):
    """First real image URL in a fragment. Lazy-loading grids park an inline
    `data:` placeholder (a 1x1 gif, a blur SVG) in `src` and put the real
    banner in `data-src`/`srcset`, and the placeholder often comes FIRST in
    attribute order — so read every source attribute on every <img> and take
    the first that isn't a data URI, falling back to the placeholder only if
    that's genuinely all there is."""
    first = ""
    for tag in _IMG_TAG_RE.finditer(fragment):
        for m in _IMG_SRC_ATTR_RE.finditer(tag.group(0)):
            url = m.group(1)
            if not url.lower().startswith("data:"):
                return url
            first = first or url
    return first


def _make_tile(slug, href, name, image):
    """One homepage show tile. ``keys`` is the set of normalized strings a
    catalog feature may match on (slug as written, slug percent-decoded,
    the artist class, the display name)."""
    keys = {k for k in (_norm(slug), _norm(urllib.parse.unquote(slug)), _norm(name)) if k}
    return {"slug": slug, "name": name, "image": _abs_url(image) if image else "",
            "url": _abs_url(href), "keys": keys}


def _parse_tiles_structured(html):
    """Strategy 'cards' — the WordPress '.item-show' grid. Precise: each
    tile is bounded by the next card so the aria/title/img regexes can't
    bleed into a neighbour."""
    tiles, seen = [], set()
    starts = [m.start() for m in _CARD_RE.finditer(html)]
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else min(len(html), start + 4000)
        block = html[start:end]
        href_m = _SHOW_HREF_RE.search(block)
        if not href_m:
            continue
        slug = href_m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        img_m = _TILE_IMG_RE.search(block)
        image = img_m.group(1) if img_m else _find_image(block)
        aria = _ARIA_RE.search(block)
        title = _TILE_TITLE_RE.search(block)
        name = _clean(aria.group(1) if aria else "") or _clean(title.group(1) if title else "")
        tile = _make_tile(slug, href_m.group(1), name, image)
        artist_m = _TILE_ARTIST_RE.search(block)
        if artist_m:
            tile["keys"].add(_norm(artist_m.group(1)))
        tile["keys"].discard("")
        tiles.append(tile)
    return tiles


def _parse_tiles_anchors(html):
    """Strategy 'links' — layout-agnostic fallback: every /show/<slug>
    link on the page, with the name and banner lifted from the markup
    around it. Used only when the card grid finds nothing, so a redesign
    costs precision rather than the whole on-sale signal.

    Everything is read from the anchor itself plus a FORWARD window that
    stops at the next anchor or card boundary. Nothing is read backwards
    and nothing crosses a boundary: a loose window makes a bare nav link
    inherit the neighbouring card's title and banner, which would then ping
    as a brand-new show graphic under the wrong name. A card that puts its
    title after the link ('<a><img></a><h3>name</h3>') is the reason the
    forward window exists at all.

    Name preference, most to least trustworthy: the anchor's own
    aria-label, its inner text, a heading just after it, then image alt
    text (often a file name or a bare artist tag)."""
    tiles, by_slug = [], {}
    for m in _SHOW_HREF_RE.finditer(html):
        slug = m.group(2)
        # The anchor's own markup: back to its '<a', forward to '</a>'.
        a_open = html.rfind("<a", max(0, m.start() - 400), m.start())
        a_start = a_open if a_open != -1 else m.start()
        a_close = html.find("</a>", m.end())
        a_end = a_close + 4 if a_close != -1 and a_close - m.end() < 4000 else m.end()
        anchor = html[a_start:a_end]
        # Forward window for a trailing title/banner, bounded by whatever
        # starts the next tile.
        bound = _TILE_BOUNDARY_RE.search(html, a_end, min(len(html), a_end + 900))
        after = html[a_end:bound.start() if bound else min(len(html), a_end + 900)]

        aria = _ARIA_RE.search(anchor)
        heading = re.search(r"<h[1-6][^>]*>(.*?)</h[1-6]>", after, re.IGNORECASE | re.DOTALL)
        alt = _IMG_ALT_RE.search(anchor) or _IMG_ALT_RE.search(after)
        name = (_clean(aria.group(1) if aria else "")
                or _text(anchor[anchor.find(">") + 1:])
                or _text(heading.group(1) if heading else "")
                or _clean(alt.group(1) if alt else ""))
        image = _find_image(anchor) or _find_image(after)

        prev = by_slug.get(slug)
        if prev is None:
            tile = _make_tile(slug, m.group(1), name, image)
            by_slug[slug] = tile
            tiles.append(tile)
        else:
            # Same show linked twice (banner link + title link): keep the
            # richest version rather than whichever came first.
            if image and not prev["image"]:
                prev["image"] = _abs_url(image)
            if name and len(name) > len(prev["name"]):
                prev["name"] = name
                prev["keys"].add(_norm(name))
                prev["keys"].discard("")
        if len(tiles) >= MAX_HOMEPAGE_TILES:
            break
    return tiles


def fetch_home_html():
    """Raw homepage bytes plus response metadata (status, final URL,
    headers) — the probe script reports on these; fetch_homepage_tiles
    only wants the body."""
    return _get(HOME_URL, headers=_HTML_HEADERS, with_meta=True)


def fetch_homepage_tiles():
    """Parse the kupat homepage into a list of show tiles:
    ``[{"slug", "name", "image", "url", "keys"}]`` — ``keys`` is the set of
    normalized match keys. Tries the precise card grid first and falls back
    to a generic /show/ link scan; raises on network trouble or when a 200
    page yields no shows under EITHER strategy (a site redesign must read
    as a fetch failure, never as 'nothing is promoted')."""
    raw, _meta = fetch_home_html()
    html = raw.decode("utf-8", errors="replace")
    tiles, strategy = _parse_tiles_structured(html), "cards"
    if not tiles:
        tiles, strategy = _parse_tiles_anchors(html), "links"
    if not tiles:
        _LAST_HOMEPAGE.update(strategy=None, tiles=0,
                              error="parsed to 0 show tiles — layout change?")
        raise KupatEventsError("homepage parsed to 0 show tiles — layout change?")
    _LAST_HOMEPAGE.update(strategy=strategy, tiles=len(tiles), error=None)
    if strategy == "links":
        print(f"[kupat_events] homepage card markup not found — generic "
              f"/show/ link scan found {len(tiles)} tiles; layout may have changed")
    return tiles


def last_warning():
    """Optional source-module hook read by app.run_il_events: a short
    human-readable note about a degraded (but not failed) fetch, or None.
    Surfacing this on /api/il-events/status is what turns a silent
    homepage break into something noticed the same day."""
    if _LAST_HOMEPAGE["error"]:
        return f"homepage: {_LAST_HOMEPAGE['error']}"
    if _LAST_HOMEPAGE["strategy"] == "links":
        return (f"homepage: card markup missing, using generic /show/ link scan "
                f"({_LAST_HOMEPAGE['tiles']} tiles) — check the layout")
    return None


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
        tiles = fetch_homepage_tiles()
    except Exception as e:
        print(f"[kupat_events] homepage fetch failed (catalog ok): {e}")
        tiles = None

    rows = []  # (feature_dict, event_dict) so the match passes can annotate
    for f in feats:
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        fid = str(f["id"])
        name = (f.get("name") or "").strip()
        extra = (f.get("additionalName") or "").strip()
        if extra:
            name = f"{name} — {extra}" if name else extra
        dt = (f.get("closestPresentationDateTime") or "").strip()
        rows.append((f, {
            "source": SOURCE_NAME,
            "event_key": fid,
            "name": name,
            "venue": "",  # not in the features payload
            "date_text": dt,
            "first_date_ms": None,
            # True = promoted on the kupat.co.il homepage (officially on
            # sale); False = catalog-only so far; None = homepage unknown.
            "on_sale": None if tiles is None else False,
            "image": "",
            "url": f"{kupat.SITE_BASE}/booking/features/{fid}",
        }))
    if not rows:
        raise KupatEventsError("features endpoint parsed to 0 events — API change?")
    events = [ev for _, ev in rows]

    if tiles is not None:
        matched_tiles = set()
        key_to_tile = {}
        for idx, t in enumerate(tiles):
            for k in t["keys"]:
                if k not in key_to_tile or t["image"]:
                    key_to_tile[k] = idx

        def _mark(ev, idx):
            matched_tiles.add(idx)
            ev["on_sale"] = True
            ev["image"] = tiles[idx]["image"]

        # Pass 1 — exact normalized-key match (urlName/name vs slug/artist/
        # aria-label). Handles similar-name pairs correctly ("ריטה" vs
        # "ריטה ושירי מימון" each hit their own tile).
        pending = []
        for f, ev in rows:
            keys = {k for k in (_norm(f.get("urlName")), _norm(f.get("name"))) if k}
            idx = next((key_to_tile[k] for k in keys if k in key_to_tile), None)
            if idx is not None:
                _mark(ev, idx)
            else:
                pending.append((f, ev))

        # Pass 2 — fuzzy token-subset match for the leftovers, against
        # still-unmatched tiles only ('ששון שאולוב' tile ↔ 'ששון איפרם
        # שאולוב' feature; urlName 'BRAVO' ↔ 'BRAVO CIRCUS הרפתקאות היער
        # האבוד' tile). A single-token set only matches when the token is
        # ≥5 chars ('echoes', 'bravo') so a short bare artist name can't
        # swallow an unrelated longer title.
        def _fuzzy_ok(a, b):
            if a == b and a:
                return True
            small = min(len(a), len(b))
            if small >= 2:
                return _tokens_subsume(a, b)
            if small == 1:
                return _tokens_subsume(a, b) and all(
                    len(t) >= 5 for t in (a if len(a) == 1 else b))
            return False

        for f, ev in pending:
            ftoks = [t for t in (_tokens(f.get("name")), _tokens(f.get("urlName")))
                     if t]
            idx = next((i for i, t in enumerate(tiles)
                        if i not in matched_tiles and any(
                            _fuzzy_ok(ft, _tokens(t["name"])) for ft in ftoks)), None)
            if idx is not None:
                _mark(ev, idx)

        # Pass 3 — teaser suppression: a still-unmatched tile whose name
        # fuzzy-matches ANY catalog feature (matched or not) has a ticket
        # link somewhere, so it isn't a teaser — kupat sometimes gives
        # multi-show festivals sloppy urlNames ('RITA' on 'פסטיבל באר
        # טוביה - ריטה') that exact-match the wrong tile in pass 1 and
        # would otherwise strand the festival's own tile as a false teaser.
        all_ftoks = []
        for f, _ev in rows:
            all_ftoks.extend(t for t in (_tokens(f.get("name")), _tokens(f.get("urlName"))) if t)
        for idx, t in enumerate(tiles):
            if idx in matched_tiles:
                continue
            ttoks = _tokens(t["name"])
            if any(_fuzzy_ok(ft, ttoks) for ft in all_ftoks):
                matched_tiles.add(idx)

        # Teaser graphics: homepage tiles matching NO catalog feature — the
        # show is announced (graphic up) but its ticket link doesn't exist
        # yet (or sale opens later / sells elsewhere). Emit each as a
        # pseudo-event keyed home:<slug> so the standard diff pings its
        # first appearance; when the catalog entry shows up later it pings
        # 'new' under its own feature id as usual.
        #
        # A banner is required. The ping is literally "new show graphic",
        # and the requirement is what keeps the layout-agnostic 'links'
        # fallback safe: a redesign whose nav or footer links shows would
        # otherwise turn every one of them into a pseudo-event.
        for idx, t in enumerate(tiles):
            if idx in matched_tiles or not t["image"]:
                continue
            events.append({
                "source": SOURCE_NAME,
                "event_key": f"home:{t['slug']}",
                "name": t["name"] or t["slug"],
                "venue": "",
                "date_text": "",
                "first_date_ms": None,
                "on_sale": True,   # promoted by definition; never flips
                "image": t["image"],
                "homepage_teaser": True,
                "url": t.get("url") or f"https://www.kupat.co.il/show/{t['slug']}",
            })
    return events


def fetch_presentations(events=None):
    """Every presentation (event date) on the site, grouped by feature id:
    ``{feature_id: [{perf_key, date_text, venue, soldout, min_price}, …]}``
    sorted by date. Powers the "new date added under a known event" ping in
    run_il_events — the /api/features catalog only carries the CLOSEST date
    per event, so an extra show added to an existing page is invisible
    there. `events` is ignored (uniform signature with tm_events — kupat
    has one site-wide catalog call). A feature id absent from the result
    means "no dates known this tick"; the diff loop skips it rather than
    treating it as empty. Raises on network trouble or a zero-row parse
    (an API change must read as a fetch failure, never as 'all dates
    removed')."""
    # The endpoint streams the whole ~1 MB catalog slowly — 60s was hit on
    # the VPS (2026-07-20); 120s has headroom without stalling the tick.
    raw = _get(PRESENTATIONS_URL, timeout=120)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KupatEventsError("non-JSON from /api/presentations — API change?") from e
    rows = body.get("presentations") if isinstance(body, dict) else None
    if not rows:
        raise KupatEventsError("presentations endpoint parsed to 0 rows — API change?")
    by_feature = {}
    for p in rows:
        if not isinstance(p, dict) or p.get("id") is None or p.get("featureId") is None:
            continue
        venue = ", ".join(x for x in ((p.get("venueName") or "").strip(),
                                      (p.get("venueCity") or "").strip()) if x)
        by_feature.setdefault(str(p["featureId"]), []).append({
            "perf_key": str(p["id"]),
            "date_text": (p.get("dateTime") or "").strip(),
            "venue": venue,
            "soldout": bool(p.get("soldout")),
            "min_price": p.get("minPrice"),
        })
    for perfs in by_feature.values():
        perfs.sort(key=lambda x: x["date_text"])
    return by_feature


def check_on_sale(ev):
    """Called by the diff loop only when fetch_events returned on_sale=None
    (homepage fetch failed). Raising makes the loop fall back to each
    event's stored state instead of guessing."""
    raise KupatEventsError("kupat homepage unavailable this tick")


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if "--perfs" in argv:
        want = next((a for a in argv if a.isdigit()), None)
        perfs = fetch_presentations()
        if "--json" in argv:
            print(json.dumps({want: perfs[want]} if want else perfs,
                             indent=1, ensure_ascii=False))
            return 0
        total = sum(len(v) for v in perfs.values())
        print(f"{total} presentations across {len(perfs)} features on {PRESENTATIONS_URL}\n")
        for fid in sorted(perfs, key=int):
            if want and fid != want:
                continue
            for p in perfs[fid]:
                so = "SOLDOUT" if p["soldout"] else "       "
                price = f"₪{p['min_price']:g}" if p.get("min_price") else ""
                print(f"  {fid:>6}/{p['perf_key']:<6} {so} {p['date_text']:<17} {price:<8} {p['venue']}")
        return 0
    if "--home" in argv:
        tiles = fetch_homepage_tiles()
        if "--json" in argv:
            print(json.dumps([dict(t, keys=sorted(t["keys"])) for t in tiles],
                             indent=1, ensure_ascii=False))
            return 0
        print(f"{len(tiles)} tiles on {HOME_URL} via the "
              f"'{_LAST_HOMEPAGE['strategy']}' strategy\n")
        for t in tiles:
            print(f"  {'img' if t['image'] else '---'} {t['slug']:<30.30} {t['name']}")
        return 0
    events = fetch_events()
    if "--json" in argv:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    n_home = sum(1 for e in events if e["on_sale"] and not e.get("homepage_teaser"))
    n_teaser = sum(1 for e in events if e.get("homepage_teaser"))
    warn = last_warning()
    print(f"{len(events)} entries — {n_home} catalog features promoted on the "
          f"homepage, {n_teaser} teaser graphics without a catalog link")
    print(f"homepage strategy: {_LAST_HOMEPAGE['strategy'] or 'FAILED'}"
          + (f"   ⚠ {warn}" if warn else "") + "\n")
    for ev in events:
        if ev.get("homepage_teaser"):
            mark = "TEASER"
        elif ev["on_sale"]:
            mark = "HOME  "
        elif ev["on_sale"] is None:
            mark = "?     "
        else:
            mark = "      "
        img = "img" if ev.get("image") else "   "
        print(f"  {mark} {img} {ev['event_key']:>14}  {ev['name']:<42.42} {ev['date_text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
