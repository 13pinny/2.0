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
# Site-wide per-performance catalog — the same endpoint market.py reaches
# through a BrowserSession, but it answers plain urllib too (verified
# 2026-07). ~1 MB gzipped; feeds the "new date under a known event" diff.
PRESENTATIONS_URL = kupat.API_BASE + "/presentations/?locationId=0&isHold=0"
HOME_URL = "https://www.kupat.co.il/"

_TILE_SLUG_RE = re.compile(r'href="https://www\.kupat\.co\.il/show/([a-z0-9_\-]+)"', re.IGNORECASE)
_TILE_ARTIST_RE = re.compile(r'show_artist-([a-z0-9_\-]+)')
_TILE_IMG_RE = re.compile(r'<img[^>]+class="[^"]*item-image[^"]*"[^>]+src="([^"]+)"', re.IGNORECASE)
_ARIA_RE = re.compile(r'aria-label="([^"]*)"')


class KupatEventsError(RuntimeError):
    pass


def _get(url, timeout=None):
    req = urllib.request.Request(url, headers=kupat.REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout or kupat.REQUEST_TIMEOUT) as resp:
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


def fetch_homepage_tiles():
    """Parse the kupat.co.il homepage into a list of show tiles:
    ``[{"slug", "name", "image", "keys"}]`` — ``keys`` is the set of
    normalized match keys (slug, show_artist class, aria-label). Raises on
    network trouble or when a 200 page parses to zero tiles (a site
    redesign must read as a fetch failure, never as 'nothing is
    promoted')."""
    html = _get(HOME_URL).decode("utf-8", errors="replace")
    tiles = []
    seen_slugs = set()
    starts = [m.start() for m in re.finditer(r'<article[^>]+class="[^"]*item-show[^"]*"[^>]*>', html)]
    for i, start in enumerate(starts):
        # Bound each tile at the next <article> so aria/title/img regexes
        # can't bleed into the neighbouring tile.
        end = starts[i + 1] if i + 1 < len(starts) else min(len(html), start + 4000)
        block = html[start:end]
        slug_m = _TILE_SLUG_RE.search(block)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        img_m = _TILE_IMG_RE.search(block)
        aria = _ARIA_RE.search(block)
        title = _TILE_TITLE_RE.search(block)
        name = ((aria.group(1) if aria else "") or (title.group(1) if title else "")).strip()
        artist_m = _TILE_ARTIST_RE.search(block)
        keys = {k for k in (
            _norm(slug),
            _norm(artist_m.group(1)) if artist_m else "",
            _norm(name),
        ) if k}
        tiles.append({"slug": slug, "name": name,
                      "image": img_m.group(1) if img_m else "", "keys": keys})
    if not tiles:
        raise KupatEventsError("homepage parsed to 0 show tiles — layout change?")
    return tiles


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
        for idx, t in enumerate(tiles):
            if idx in matched_tiles:
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
                "url": f"https://www.kupat.co.il/show/{t['slug']}",
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
    events = fetch_events()
    if "--json" in argv:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    n_home = sum(1 for e in events if e["on_sale"] and not e.get("homepage_teaser"))
    n_teaser = sum(1 for e in events if e.get("homepage_teaser"))
    print(f"{len(events)} entries — {n_home} catalog features promoted on the "
          f"homepage, {n_teaser} teaser graphics without a catalog link\n")
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
