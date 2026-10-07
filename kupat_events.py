"""kupat.co.il new-event monitor — pure-HTTP event-listing fetcher.

Companion to pacha_events.py / tm_events.py; the diff-and-ping loop lives in
app.py (`run_il_events`). Anonymous endpoints, two different back-ends:

  GET https://tickets.kupat.co.il/api/features
      → the ENTIRE current catalog (~70-80 features) as anonymous JSON.
        Unlike the seats/presentation endpoints (403 outside the in-page
        fetch chain — see kupat.py's _browse_capture), this listing endpoint
        answers plain urllib. Each feature has id, name, additionalName,
        urlName, code, categories (numeric ids), featureTypeId, and
        closestPresentationDateTime ("YYYY-MM-DD HH:MM", venue-local).
        Events often show up here DAYS before the sale is official — that
        early visibility is the point of the 'new' ping.

  GET https://www.kupat.co.il/api/*  (the marketing site's Strapi CMS)
      → the "officially promoted" signal. www.kupat.co.il was rebuilt from
        WordPress to a client-rendered Next.js app around 2026-09; the old
        `<article class="item-show">` tile scrape now parses to zero tiles
        and took the homepage/on-sale/graphic signals down with it. The
        homepage is assembled in the browser from a handful of Strapi
        collections, all of which answer plain urllib:

          /api/homepage?populate[Homepage_Sliders][populate]=*
              the ordered list of homepage rows: {Label, Type} where Type
              is Hot_Now_Slider | Custom_Slider | Venue_Shows |
              Category_Shows | Venue_Logo_Slider.
          /api/homepage?populate[Category_A]…      the hero carousel's shows
          /api/sliders?populate[shows]…            Custom_Slider members
          /api/hot-now-sliders?populate[Shows]…    Hot_Now_Slider members
          /api/venues?filters[Slug][$in]…          Venue_Shows members
          /api/shows?fields=…                      every CMS show (name,
              slug, Categories) — resolves Category_Shows rows and supplies
              the slug/name for every promoted feature.

        Every CMS show carries `Show_ID`, which IS the ticketing catalog's
        feature id, so homepage membership joins to the catalog on an exact
        integer — no more normalized-slug/fuzzy-token matching.

  GET https://www.kupat.co.il/api/events?fields=…
      → per-performance sale state straight from the CMS mirror of the
        ticketing back-end: Ticket_Sale_Start (the exact moment the sale
        opens — publicly readable BEFORE the drop), Soldout, Avail_Ratio,
        Min_Price, Queue_It. This is what powers the 'salesoon' /
        'salelive' pings; ~619 rows over 7 pages of 100.

  HEAD https://tickets.kupat.co.il/api/features/<id>/media/contentDesktopImage?raw=1
      → the promoted show's banner graphic. The endpoint serves no ETag and
        no Last-Modified, so a change is detected by Content-Length from a
        bodyless HEAD (one per promoted show, no image bytes downloaded).

`on_sale` means "promoted on the kupat.co.il homepage": a feature that's
only in the catalog pings 'new' (marked not-yet-official), and its later
homepage debut pings 'onsale' — with the banner graphic attached. When the
CMS fetch fails, on_sale is returned as None and check_on_sale raises, so
the diff loop keeps each event's stored state instead of mis-recording a
flip.

CLI probe:  python kupat_events.py [--json]
            python kupat_events.py --perfs [<feature id>] [--json]
            python kupat_events.py --home           (homepage composition)
            python kupat_events.py --sales          (sale-start table)
"""
import gzip
import json
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
SITE_BASE = "https://www.kupat.co.il"
CMS_BASE = SITE_BASE + "/api"
# The homepage banner the site itself renders for a promoted show. Keys
# come from the Next bundle: sliderImage / contentMobileImage /
# contentDesktopImage / contentDesktopWithTextImage.
MEDIA_URL = kupat.API_BASE + "/features/{fid}/media/contentDesktopImage?raw=1"

# Homepage row types that actually carry shows (Venue_Logo_Slider is a strip
# of venue logos — no shows, nothing to ping).
_SHOW_ROW_TYPES = ("Hot_Now_Slider", "Custom_Slider", "Venue_Shows", "Category_Shows")


class KupatEventsError(RuntimeError):
    pass


def _get(url, timeout=None, method=None):
    req = urllib.request.Request(url, headers=kupat.REQUEST_HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or kupat.REQUEST_TIMEOUT) as resp:
            if method == "HEAD":
                return resp.headers
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw
    except urllib.error.HTTPError as e:
        raise KupatEventsError(f"{url} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise KupatEventsError(f"{url} unreachable: {e.reason}") from e


def _cms(path, params=(), timeout=45):
    """GET one Strapi collection/single-type off www.kupat.co.il and return
    the decoded body. `params` is a list of (key, value) pairs — Strapi's
    bracket syntax (`populate[Shows][fields][0]`) has to survive
    urlencode, which quote_plus handles fine."""
    url = f"{CMS_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(list(params))
    raw = _get(url, timeout=timeout)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KupatEventsError(f"non-JSON from /api/{path} — CMS change?") from e
    if not isinstance(body, dict) or "data" not in body:
        raise KupatEventsError(f"unexpected /api/{path} shape — CMS change?")
    return body


def _attrs(node):
    """One Strapi entry → its attribute dict. Tolerates both the v4 shape
    ({id, attributes:{…}}) and the flattened v5 one ({id, Field:…}), which
    this CMS mixes depending on the endpoint."""
    if not isinstance(node, dict):
        return {}
    inner = node.get("attributes")
    return inner if isinstance(inner, dict) else node


def _rel(value):
    """One Strapi relation → a list of attribute dicts. Handles
    {"data": [...]}, {"data": {...}}, a bare list, a bare dict and None."""
    if value is None:
        return []
    if isinstance(value, dict):
        if "data" in value:
            return _rel(value["data"])
        return [_attrs(value)]
    if isinstance(value, list):
        return [_attrs(v) for v in value]
    return []


def _fields(prefix, names):
    """(key, value) pairs selecting `names` under a Strapi populate path —
    `_fields("populate[Shows]", ["Show_ID"])` → populate[Shows][fields][0]."""
    return [(f"{prefix}[fields][{i}]", n) for i, n in enumerate(names)]


def _show_id(attrs):
    """A CMS show's Show_ID as the catalog's string feature id, or None."""
    sid = attrs.get("Show_ID")
    return str(sid) if sid not in (None, "") else None


def _homepage_rows():
    """The ordered homepage composition: [{"label", "type", "hot_now_id",
    "slider_name", "venue_slug", "category_name"}] for the rows that carry
    shows. Mirrors the filter the Next bundle applies — a row whose target
    didn't resolve is dropped by the site too, so it's dropped here."""
    body = _cms("homepage", [("populate[Homepage_Sliders][populate]", "*")])
    rows = []
    for comp in _attrs(body.get("data")).get("Homepage_Sliders") or []:
        rtype = comp.get("Type")
        if rtype not in _SHOW_ROW_TYPES:
            continue
        hot = next(iter(_rel(comp.get("Hot_Now_Slider"))), {})
        custom = next(iter(_rel(comp.get("Custom_Slider"))), {})
        venue = next(iter(_rel(comp.get("Venue"))), {})
        category = next(iter(_rel(comp.get("Category"))), {})
        rows.append({
            "label": (comp.get("Label") or "").strip(),
            "type": rtype,
            "hot_now_title": (hot.get("Title") or "").strip(),
            "slider_name": (custom.get("Sliders_name") or "").strip(),
            "venue_slug": (venue.get("Slug") or "").strip(),
            "category_name": (category.get("Feature_Category_Name") or "").strip(),
        })
    return rows


def _cms_shows():
    """Every show the marketing CMS publishes, keyed by feature id:
    ``{fid: {"name", "slug", "categories": [ids]}}``. Paged (65-ish rows at
    25/page by default; the CMS silently truncates a fields-less page, so
    the field list is explicit)."""
    out, page = {}, 1
    while True:
        body = _cms("shows", [
            ("pagination[pageSize]", "100"), ("pagination[page]", str(page)),
            *_fields("", ["Show_ID", "Name", "Slug", "Categories"]),
        ])
        rows = body.get("data") or []
        for node in rows:
            a = _attrs(node)
            fid = _show_id(a)
            if fid:
                out[fid] = {
                    "name": (a.get("Name") or "").strip(),
                    "slug": (a.get("Slug") or "").strip(),
                    "categories": [str(c) for c in (a.get("Categories") or [])],
                }
        pg = (body.get("meta") or {}).get("pagination") or {}
        if page >= (pg.get("pageCount") or 1) or not rows:
            break
        page += 1
    if not out:
        raise KupatEventsError("/api/shows parsed to 0 shows — CMS change?")
    return out


def _hero_feature_ids():
    """Feature ids in the homepage hero carousel (Category_A + the pinned
    hero slides)."""
    body = _cms("homepage", [
        *_fields("populate[Category_A]", ["Show_ID", "Name", "Slug"]),
        *_fields("populate[Hero_Pinned_Slides]", ["Show_ID", "Name", "Slug"]),
    ])
    a = _attrs(body.get("data"))
    ids = []
    for key in ("Category_A", "Hero_Pinned_Slides"):
        for show in _rel(a.get(key)):
            fid = _show_id(show)
            if fid:
                ids.append(fid)
    return ids


def _slider_members():
    """``{slider name: [feature id]}`` for every Custom_Slider."""
    body = _cms("sliders", [
        ("pagination[pageSize]", "100"),
        *_fields("populate[shows]", ["Show_ID"]),
    ])
    out = {}
    for node in body.get("data") or []:
        a = _attrs(node)
        name = (a.get("Sliders_name") or "").strip()
        if not name:
            continue
        out[name] = [f for f in (_show_id(s) for s in _rel(a.get("shows"))) if f]
    return out


def _hot_now_members():
    """``{hot-now slider title: [feature id]}`` — Pinned_Shows first, the
    way the site renders them."""
    body = _cms("hot-now-sliders", [
        ("pagination[pageSize]", "100"),
        *_fields("populate[Shows]", ["Show_ID"]),
        *_fields("populate[Pinned_Shows]", ["Show_ID"]),
    ])
    out = {}
    for node in body.get("data") or []:
        a = _attrs(node)
        title = (a.get("Title") or "").strip()
        if not title:
            continue
        ids = [_show_id(s) for s in _rel(a.get("Pinned_Shows"))]
        ids += [_show_id(s) for s in _rel(a.get("Shows"))]
        out[title] = [f for f in ids if f]
    return out


def _category_ids():
    """``{Feature_Category_Name: Feature_Category_ID}`` — a Category_Shows
    homepage row names its category, while a CMS show carries only numeric
    category ids, so the row can't be resolved without this map."""
    body = _cms("feature-categories", [
        ("pagination[pageSize]", "100"),
        *_fields("", ["Feature_Category_ID", "Feature_Category_Name"]),
    ])
    out = {}
    for node in body.get("data") or []:
        a = _attrs(node)
        name = (a.get("Feature_Category_Name") or "").strip()
        cid = a.get("Feature_Category_ID")
        if name and cid not in (None, ""):
            out[name] = str(cid)
    return out


def _venue_members(slugs):
    """``{venue slug: [feature id]}`` for the Venue_Shows rows — the shows
    behind each venue's upcoming events."""
    if not slugs:
        return {}
    params = [(f"filters[Slug][$in][{i}]", s) for i, s in enumerate(sorted(slugs))]
    params += [("pagination[pageSize]", "100"),
               *_fields("", ["Slug"]),
               *_fields("populate[Events][populate][Show]", ["Show_ID"])]
    body = _cms("venues", params)
    out = {}
    for node in body.get("data") or []:
        a = _attrs(node)
        slug = (a.get("Slug") or "").strip()
        ids = []
        for ev in _rel(a.get("Events")):
            for show in _rel(ev.get("Show")):
                fid = _show_id(show)
                if fid:
                    ids.append(fid)
        out[slug] = ids
    return out


def fetch_homepage_shows():
    """What the kupat.co.il homepage is promoting right now:
    ``{feature_id: {"name", "slug", "sections": [row label, …]}}``.

    Assembled from the CMS exactly the way the browser assembles the page —
    hero carousel plus every show-carrying slider row, in homepage order.
    Raises when the composition or the show catalog can't be read, or when
    the whole thing resolves to zero shows (a CMS change must read as a
    fetch failure, never as 'nothing is promoted'); an individual row that
    fails to resolve is skipped with a note so one broken slider can't
    blank the signal."""
    rows = _homepage_rows()
    shows = _cms_shows()

    need_sliders = any(r["type"] == "Custom_Slider" for r in rows)
    need_hot = any(r["type"] == "Hot_Now_Slider" for r in rows)
    need_cats = any(r["type"] == "Category_Shows" for r in rows)
    venue_slugs = {r["venue_slug"] for r in rows
                   if r["type"] == "Venue_Shows" and r["venue_slug"]}

    def _soft(fn, *args):
        try:
            return fn(*args)
        except Exception as e:
            print(f"[kupat_events] homepage row source {fn.__name__} failed: {e}")
            return {}

    sliders = _soft(_slider_members) if need_sliders else {}
    hot_now = _soft(_hot_now_members) if need_hot else {}
    venues = _soft(_venue_members, venue_slugs) if venue_slugs else {}
    categories = _soft(_category_ids) if need_cats else {}

    promoted = {}

    def _add(fid, label):
        if not fid:
            return
        info = promoted.setdefault(fid, {
            "name": shows.get(fid, {}).get("name", ""),
            "slug": shows.get(fid, {}).get("slug", ""),
            "sections": [],
        })
        if label and label not in info["sections"]:
            info["sections"].append(label)

    for fid in _soft(_hero_feature_ids) or []:
        _add(fid, "hero")

    for row in rows:
        label = row["label"] or row["type"]
        if row["type"] == "Custom_Slider":
            ids = sliders.get(row["slider_name"], [])
        elif row["type"] == "Hot_Now_Slider":
            ids = hot_now.get(row["hot_now_title"] or row["label"], [])
        elif row["type"] == "Venue_Shows":
            ids = venues.get(row["venue_slug"], [])
        elif row["type"] == "Category_Shows":
            want = categories.get(row["category_name"])
            ids = [fid for fid, s in shows.items()
                   if want and want in s["categories"]]
        else:
            ids = []
        for fid in ids:
            _add(fid, label)

    if not promoted:
        raise KupatEventsError("homepage resolved to 0 promoted shows — CMS change?")
    return promoted


def graphic_url(feature_id):
    """The homepage banner the site renders for one promoted feature."""
    return MEDIA_URL.format(fid=feature_id)


def _graphic_sig(feature_id):
    """Signature of one feature's banner, or None when it has none / the
    HEAD fails. The endpoint serves no ETag and no Last-Modified, so the
    body length is the signature — a re-crop or a new artwork changes it,
    a byte-identical re-upload does not (acceptable: that isn't a new
    graphic)."""
    try:
        headers = _get(graphic_url(feature_id), timeout=20, method="HEAD")
    except KupatEventsError:
        return None
    length = headers.get("Content-Length")
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip()
    if not length:
        return None
    return f"{ctype or 'image'}:{length}"


def fetch_graphic_sigs(feature_ids, workers=8):
    """``{feature_id: signature}`` for the given features, from bodyless
    HEADs (no image bytes are downloaded — the banners run 50-450 KB
    each). Features whose HEAD failed or that have no banner are simply
    absent, and the diff loop skips them rather than reading a missing
    entry as 'the graphic was removed'."""
    ids = [str(f) for f in feature_ids]
    if not ids:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(ids))) as pool:
        for fid, sig in zip(ids, pool.map(_graphic_sig, ids)):
            if sig:
                out[fid] = sig
    return out


def _israel_now():
    """Venue-local wall clock — every kupat date string, Ticket_Sale_Start
    included, is Israel time with no offset attached."""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Jerusalem")).replace(tzinfo=None)
    except Exception:
        # No tzdata (bare Windows venv): fall back to UTC+3/+2 rather than
        # local time, which on the VPS is UTC and would read every pending
        # sale as already open up to 3 hours early.
        utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return utc + datetime.timedelta(hours=3 if 3 <= utc.month <= 10 else 2)


def sale_start_passed(sale_start):
    """True when a stored `Ticket_Sale_Start` has arrived. The diff loop
    needs this to tell the two ways a pending sale leaves the feed apart:
    its minute came (the 'salelive' ping) versus kupat pulling or
    rescheduling it (nothing to say). Unparseable text reads as NOT
    passed — a bad timestamp must not fake a sale opening."""
    try:
        import datetime
        when = datetime.datetime.strptime(str(sale_start).strip()[:19],
                                          "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return when <= _israel_now()


def fetch_pending_sales():
    """Performances whose sale has NOT opened yet, grouped by feature:
    ``{feature_id: {perf_key: {"sale_start", "date_text", "venue",
    "min_price", "queue"}}}``.

    One filtered request against the CMS's mirror of the box office
    (`Ticket_Sale_Start > now`), so a drop is visible with its exact
    opening minute for as long as kupat has scheduled it — typically hours
    to days ahead. `run_il_events` pings 'salesoon' when one first appears
    (or moves) and 'salelive' once the stored time passes.

    Raises on network/parse trouble so the diff loop keeps stored state; an
    EMPTY result is legitimate (nothing scheduled) and is returned as {}."""
    fields = ["Event_ID", "Feature_ID", "Feature_Name", "Date_Time",
              "Location_Name", "Ticket_Sale_Start", "Min_Price", "Queue_It"]
    params = [
        ("filters[Ticket_Sale_Start][$gt]", _israel_now().strftime("%Y-%m-%d %H:%M:%S")),
        ("pagination[pageSize]", "100"),
        ("sort", "Ticket_Sale_Start:asc"),
        *_fields("", fields),
    ]
    body = _cms("events", params)
    out = {}
    for node in body.get("data") or []:
        a = _attrs(node)
        fid, perf = a.get("Feature_ID"), a.get("Event_ID")
        start = (a.get("Ticket_Sale_Start") or "").strip()
        if fid in (None, "") or perf in (None, "") or not start:
            continue
        out.setdefault(str(fid), {})[str(perf)] = {
            "sale_start": start,
            "date_text": (a.get("Date_Time") or "").strip(),
            "venue": (a.get("Location_Name") or "").strip(),
            "min_price": a.get("Min_Price"),
            "queue": bool(a.get("Queue_It")),
            "name": (a.get("Feature_Name") or "").strip(),
        }
    return out


def _has_banner(sigs, feature_id):
    """Whether to attach the banner to this event's Discord embed. `sigs`
    is None when the HEAD sweep never ran (assume the banner is there, as
    before); otherwise a feature absent from it has no banner and linking
    one would embed a 404 — which happens for real on a homepage teaser
    whose ticketing feature doesn't exist yet."""
    return sigs is None or feature_id in sigs


def fetch_events():
    """Current kupat catalog, normalized, with homepage-promotion state.
    Raises KupatEventsError on catalog trouble or a zero-feature parse (an
    API change must read as a fetch failure, never as 'all events
    removed'). A homepage failure alone degrades softly: events come back
    with on_sale=None and the diff loop keeps stored state.

    Each event carries, beyond the shared fields:
      on_sale            True = promoted on the kupat.co.il homepage
      homepage_sections  which homepage rows it sits in (hero, slider names)
      image              the banner graphic, when promoted
      image_sig          that banner's change signature (see _graphic_sig)
      homepage_teaser    promoted but absent from the ticketing catalog"""
    raw = _get(FEATURES_URL)
    try:
        feats = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KupatEventsError("non-JSON from /api/features — API change?") from e
    if not isinstance(feats, list):
        raise KupatEventsError("unexpected /api/features shape — API change?")

    try:
        promoted = fetch_homepage_shows()
    except Exception as e:
        print(f"[kupat_events] homepage fetch failed (catalog ok): {e}")
        promoted = None

    # None = the banner sweep didn't run at all (keep showing the graphic,
    # we just can't say whether it changed); a dict = it ran, and a feature
    # missing from it genuinely has no banner to embed.
    sigs = None
    if promoted:
        try:
            sigs = fetch_graphic_sigs(promoted)
        except Exception as e:
            print(f"[kupat_events] banner HEADs failed (promotion state ok): {e}")

    events = []
    seen_ids = set()
    for f in feats:
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        fid = str(f["id"])
        seen_ids.add(fid)
        name = (f.get("name") or "").strip()
        extra = (f.get("additionalName") or "").strip()
        if extra:
            name = f"{name} — {extra}" if name else extra
        hp = (promoted or {}).get(fid)
        events.append({
            "source": SOURCE_NAME,
            "event_key": fid,
            "name": name,
            "venue": "",  # not in the features payload
            "date_text": (f.get("closestPresentationDateTime") or "").strip(),
            "first_date_ms": None,
            # True = promoted on the kupat.co.il homepage (officially on
            # sale); False = catalog-only so far; None = homepage unknown.
            "on_sale": None if promoted is None else bool(hp),
            "homepage_sections": list(hp["sections"]) if hp else [],
            "image": graphic_url(fid) if hp and _has_banner(sigs, fid) else "",
            "image_sig": (sigs or {}).get(fid) if hp else None,
            "url": f"{kupat.SITE_BASE}/booking/features/{fid}",
        })
    if not events:
        raise KupatEventsError("features endpoint parsed to 0 events — API change?")

    # Teaser graphics: a show the marketing site promotes that the
    # ticketing catalog doesn't carry yet — announced, no ticket link.
    # Keyed by the SAME feature id the catalog will use, so its later
    # catalog debut updates this row instead of pinging twice.
    for fid, info in (promoted or {}).items():
        if fid in seen_ids:
            continue
        slug = info.get("slug") or ""
        events.append({
            "source": SOURCE_NAME,
            "event_key": fid,
            "name": info.get("name") or fid,
            "venue": "",
            "date_text": "",
            "first_date_ms": None,
            "on_sale": True,   # promoted by definition
            "homepage_sections": list(info["sections"]),
            "image": graphic_url(fid) if _has_banner(sigs, fid) else "",
            "image_sig": (sigs or {}).get(fid),
            "homepage_teaser": True,
            "url": (f"{SITE_BASE}/show/{slug}" if slug
                    else f"{kupat.SITE_BASE}/booking/features/{fid}"),
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


def _main_home(argv):
    promoted = fetch_homepage_shows()
    if "--json" in argv:
        print(json.dumps(promoted, indent=1, ensure_ascii=False))
        return 0
    sections = {}
    for fid, info in promoted.items():
        for s in info["sections"]:
            sections.setdefault(s, []).append((fid, info["name"]))
    print(f"{len(promoted)} shows promoted across {len(sections)} homepage rows\n")
    for label, rows in sections.items():
        print(f"  {label}  ({len(rows)})")
        for fid, name in rows:
            print(f"      {fid:>6}  {name:<44.44}")
    return 0


def _main_sales(argv):
    pending = fetch_pending_sales()
    if "--json" in argv:
        print(json.dumps(pending, indent=1, ensure_ascii=False))
        return 0
    total = sum(len(v) for v in pending.values())
    print(f"now (Israel): {_israel_now():%Y-%m-%d %H:%M}")
    print(f"{total} performances across {len(pending)} events have a sale that "
          f"has not opened yet\n")
    for fid, perfs in pending.items():
        for pk, p in sorted(perfs.items(), key=lambda kv: kv[1]["sale_start"]):
            q = " QUEUE-IT" if p["queue"] else ""
            print(f"  {fid:>6}/{pk:<7} opens {p['sale_start']}  "
                  f"{p['name']:<26.26} {p['date_text']:<17} {p['venue']:<24.24}{q}")
    return 0


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if "--home" in argv:
        return _main_home(argv)
    if "--sales" in argv:
        return _main_sales(argv)
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
        img = "img" if ev.get("image_sig") else "   "
        rows = ", ".join(ev.get("homepage_sections") or [])
        print(f"  {mark} {img} {ev['event_key']:>6}  {ev['name']:<34.34} "
              f"{ev['date_text']:<17} {rows:<40.40}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
