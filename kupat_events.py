"""kupat.co.il new-event monitor — pure-HTTP event-listing fetcher.

Companion to pacha_events.py / tm_events.py; the diff-and-ping loop lives in
app.py (`run_il_events`). Three anonymous endpoints, two hosts:

  GET https://tickets.kupat.co.il/api/features
      → the ENTIRE current catalog (~65 features) as anonymous JSON.
        Unlike the seats/presentation endpoints (403 outside the in-page
        fetch chain — see kupat.py's _browse_capture), this listing endpoint
        answers plain urllib. Each feature has id, name, additionalName,
        urlName, code, categories (numeric ids), featureTypeId, and
        closestPresentationDateTime ("YYYY-MM-DD HH:MM", venue-local).
        Events often show up here DAYS before the sale is official — that
        early visibility is the point of the 'new' ping.

  GET https://tickets.kupat.co.il/api/presentations/
      → every performance on the site in one call; powers the 'newdate'
        diff (see fetch_presentations).

  GET https://www.kupat.co.il/api/shows
      → the "officially on sale" signal. www.kupat.co.il was rebuilt in
        2026-08 as a client-rendered Next.js front end over a **Strapi v4
        CMS**, so the old WordPress homepage scrape is dead: the HTML now
        carries zero show markup (the flight payload holds only chunk
        refs) and every tile is fetched client-side from this API. The
        collection is the marketing catalog — a strict SUBSET of
        /api/features that excludes internal rows (test features, closed
        army/corporate shows). Crucially each record carries **Show_ID**,
        which IS the feature id, so the fuzzy urlName/aria-label tile
        matching the old parser needed is gone — the join is exact.

`on_sale` no longer means "promoted on the homepage". That was only ever a
PROXY for "the sale is officially open", because the old WordPress site
published nothing better. The Strapi records nest their performances under
`Events`, each with a real **Ticket_Sale_Start** timestamp (venue-local,
populated on every event as of 2026-08), so on_sale is now the honest
thing: the feature has a published show whose earliest ticket-sale start
has passed. A catalog feature with no show record, or one whose sale
starts in the future, is `on_sale=False` and pings 'new' as
not-yet-official; the 'onsale' ping then fires on the tick the sale window
actually opens, rather than whenever marketing happened to rotate the show
onto the homepage.

Because that basis changed, `ONSALE_BASIS` is bumped — run_il_events
re-baselines kupat's on_sale column silently on the first tick after
deploy instead of firing an 'onsale' burst for every event the old
homepage never happened to promote.

Homepage teaser pseudo-events (`home:<slug>`) are gone with the scrape:
every show the new homepage renders comes out of this same Strapi
collection and therefore always has a catalog link, so the "graphic up but
no tickets yet" state the teasers modelled can no longer occur. Rows
stored under the old `home:` keys simply stop being seen (the diff only
pings on appearance, never on disappearance).

When the CMS fetch fails, on_sale is returned as None and check_on_sale
raises, so the diff loop keeps each event's stored state instead of
mis-recording a flip.

CLI probe:  python kupat_events.py [--json]
"""
import gzip
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import kupat

SOURCE_NAME = "kupat"
# Bumped whenever the meaning of `on_sale` changes. run_il_events compares
# this against the stored basis and, on a mismatch, stores the new values
# without firing 'onsale' pings for the difference.
ONSALE_BASIS = "ticket_sale_start_v2"

FEATURES_URL = kupat.API_BASE + "/features"
# Site-wide per-performance catalog — the same endpoint market.py reaches
# through a BrowserSession, but it answers plain urllib too (verified
# 2026-07). ~1 MB gzipped; feeds the "new date under a known event" diff.
PRESENTATIONS_URL = kupat.API_BASE + "/presentations/?locationId=0&isHold=0"

# The marketing front end (Next.js + Strapi v4), a DIFFERENT host from the
# ticketing backend kupat.API_BASE points at.
CMS_BASE = "https://www.kupat.co.il"
SHOWS_URL = CMS_BASE + "/api/shows"
# Strapi caps pageSize at 100 regardless of what's asked for, so paginate.
_CMS_PAGE_SIZE = 100
_CMS_MAX_PAGES = 20

# kupat.REQUEST_HEADERS pins Origin/Referer to tickets.kupat.co.il, which
# is the wrong site for these calls.
CMS_HEADERS = {
    "User-Agent": kupat.REQUEST_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": CMS_BASE + "/",
}

_ISRAEL_TZ = None


class KupatEventsError(RuntimeError):
    pass


def _israel_now():
    """Venue-local 'YYYY-MM-DD HH:MM:SS' — the timezone every kupat
    timestamp is expressed in, so sale-start comparisons are plain string
    compares. Falls back to system local time when tzdata is missing (a
    stock Windows Python without the `tzdata` wheel); that only skews the
    sale-open moment on a machine not already set to Israel time."""
    global _ISRAEL_TZ
    if _ISRAEL_TZ is None:
        try:
            from zoneinfo import ZoneInfo
            _ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
        except Exception:
            _ISRAEL_TZ = False
    now = datetime.now(_ISRAEL_TZ) if _ISRAEL_TZ else datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _get(url, timeout=None, headers=None):
    req = urllib.request.Request(url, headers=headers or kupat.REQUEST_HEADERS)
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


def _attrs(node):
    """Strapi v4 wraps relations as {"data": {...}} or {"data": [...]}, and
    entities as {"id":…, "attributes": {…}}. Unwrap to the attribute dict
    (or [] / {} when the relation is empty)."""
    if isinstance(node, dict) and "data" in node:
        node = node["data"]
    if isinstance(node, list):
        return [_attrs(x) for x in node]
    if isinstance(node, dict):
        return node.get("attributes") if "attributes" in node else node
    return {}


def _media_url(node):
    """Absolute URL for a Strapi media relation. Uploads are served through
    /api/media/proxy?key=… paths that are relative to the CMS host."""
    a = _attrs(node)
    url = (a or {}).get("url") if isinstance(a, dict) else None
    if not url:
        return ""
    return url if url.startswith("http") else CMS_BASE + url


def _shows_query(page):
    """Strapi field/populate params. Mirrors what the site's own homepage
    bundle asks for, minus the presentation fluff — keeping the field list
    tight holds the response near 90 KB instead of pulling every show's
    full HTML synopsis."""
    qs = {}
    for i, f in enumerate(("Name", "Show_ID", "Slug", "Additional_Name",
                           "Closest_Presentation_Date_Time")):
        qs[f"fields[{i}]"] = f
    for i, f in enumerate(("Date_Time", "Location_Name", "Event_ID", "Soldout",
                           "Ticket_Sale_Start", "Ticket_Sale_Stop")):
        qs[f"populate[Events][fields][{i}]"] = f
    qs["populate[Show_Image][populate]"] = "*"
    qs["pagination[pageSize]"] = str(_CMS_PAGE_SIZE)
    qs["pagination[page]"] = str(page)
    return urllib.parse.urlencode(qs)


def fetch_shows():
    """The www.kupat.co.il Strapi show catalog, keyed by feature id (str):
    ``{show_id: {"name", "slug", "image", "sale_start", "on_sale"}}``.

    ``sale_start`` is the EARLIEST Ticket_Sale_Start across the show's
    performances (venue-local 'YYYY-MM-DD HH:MM:SS', or None when the CMS
    publishes none) and ``on_sale`` is True once it has passed. A published
    show carrying no sale-start at all counts as on sale — it is in the
    public marketing catalog, which is the same thing the old homepage-tile
    signal meant, so an unpopulated field degrades to the previous
    behaviour rather than withholding the ping forever.

    Raises on network trouble or a zero-row parse: a further redesign must
    read as a fetch failure, never as 'nothing is on sale'."""
    now = _israel_now()
    shows, page, pages = {}, 1, 1
    while page <= pages and page <= _CMS_MAX_PAGES:
        raw = _get(f"{SHOWS_URL}?{_shows_query(page)}", timeout=60,
                   headers=CMS_HEADERS)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as e:
            raise KupatEventsError("non-JSON from /api/shows — CMS change?") from e
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise KupatEventsError("unexpected /api/shows shape — CMS change?")
        pages = (((body.get("meta") or {}).get("pagination") or {})
                 .get("pageCount") or 1)
        for rec in body["data"]:
            a = _attrs(rec)
            if not isinstance(a, dict) or a.get("Show_ID") is None:
                continue
            evs = _attrs(a.get("Events")) or []
            starts = sorted(e.get("Ticket_Sale_Start") for e in evs
                            if isinstance(e, dict) and e.get("Ticket_Sale_Start"))
            img = a.get("Show_Image") or {}
            shows[str(a["Show_ID"])] = {
                "name": (a.get("Name") or "").strip(),
                "slug": (a.get("Slug") or "").strip(),
                "image": (_media_url(img.get("Web_Image"))
                          or _media_url(img.get("Mobile_Image"))),
                "sale_start": starts[0] if starts else None,
                # No sale-start published anywhere → treat the show's mere
                # presence in the public catalog as the sale being open.
                "on_sale": (starts[0] <= now) if starts else True,
            }
        page += 1
    if not shows:
        raise KupatEventsError("/api/shows parsed to 0 shows — CMS change?")
    return shows


def fetch_events():
    """Current kupat catalog, normalized, with sale state from the CMS.
    Raises KupatEventsError on catalog trouble or a zero-feature parse (an
    API change must read as a fetch failure, never as 'all events
    removed'). A CMS failure alone degrades softly: events come back with
    on_sale=None and the diff loop keeps stored state."""
    raw = _get(FEATURES_URL)
    try:
        feats = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KupatEventsError("non-JSON from /api/features — API change?") from e
    if not isinstance(feats, list):
        raise KupatEventsError("unexpected /api/features shape — API change?")

    try:
        shows = fetch_shows()
    except Exception as e:
        print(f"[kupat_events] CMS fetch failed (catalog ok): {e}")
        shows = None

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
        # A feature absent from the CMS is in the ticketing backend only —
        # a test row, a closed corporate/army show, or a sale that hasn't
        # been announced yet. Either way it is not publicly on sale.
        show = shows.get(fid) if shows is not None else None
        events.append({
            "source": SOURCE_NAME,
            "event_key": fid,
            "name": name,
            "venue": "",  # not in the features payload
            "date_text": dt,
            "first_date_ms": None,
            # True = the published sale window has opened; False = listed
            # but not selling yet; None = CMS unknown this tick.
            "on_sale": None if shows is None else bool(show and show["on_sale"]),
            # Venue-local sale-open timestamp when the CMS publishes one and
            # it is still in the future — the 'new' ping shows it so a
            # pre-announced sale is actionable before it opens.
            "sale_start": (show or {}).get("sale_start") if show and not show["on_sale"] else None,
            "image": (show or {}).get("image") or "",
            "url": f"{kupat.SITE_BASE}/booking/features/{fid}",
        })
    if not events:
        raise KupatEventsError("features endpoint parsed to 0 events — API change?")
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
    (the CMS fetch failed). Raising makes the loop fall back to each
    event's stored state instead of guessing."""
    raise KupatEventsError("kupat CMS unavailable this tick")




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
    if "--shows" in argv:
        shows = fetch_shows()
        if "--json" in argv:
            print(json.dumps(shows, indent=1, ensure_ascii=False))
            return 0
        print(f"{len(shows)} CMS shows on {SHOWS_URL} (now {_israel_now()} Israel)\n")
        for sid in sorted(shows, key=int):
            s = shows[sid]
            mark = "ONSALE" if s["on_sale"] else "SOON  "
            img = "img" if s["image"] else "   "
            print(f"  {mark} {img} {sid:>6}  {s['name']:<38.38} "
                  f"{s['sale_start'] or ''}")
        return 0
    events = fetch_events()
    if "--json" in argv:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    n_sale = sum(1 for e in events if e["on_sale"])
    n_soon = sum(1 for e in events if e["on_sale"] is False)
    print(f"{len(events)} catalog features — {n_sale} on sale, "
          f"{n_soon} listed but not selling yet\n")
    for ev in events:
        if ev["on_sale"] is None:
            mark = "?     "
        elif ev["on_sale"]:
            mark = "ONSALE"
        else:
            mark = "      "
        img = "img" if ev.get("image") else "   "
        extra = f"  sale opens {ev['sale_start']}" if ev.get("sale_start") else ""
        print(f"  {mark} {img} {ev['event_key']:>14}  {ev['name']:<42.42} "
              f"{ev['date_text']}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
