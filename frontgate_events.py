"""Frontgate Tickets single-event tracker — pure-HTTP per-section stock.

Frontgate (<label>.frontgatetickets.com, a Live Nation white-label used by
a lot of US promoters, e.g. mellowcircus-la) shows no counts on the event
page, but its seating-chart code reads a public, anonymous, read-only
endpoint that does:

    GET https://<sub>.frontgatetickets.com/section/<eventHubId>/info

    {"error":0, "msg":"section overview", "data":{"sectionInfo":{
       "397662":{"sectionId":"397662","eventHubId":"174488","is_ga":"1",
                 "totalSeats":"2550","availableSeats":"472",
                 "minPrice":94.87,"maxPrice":94.87,
                 "ticketTypes":"General Admission", ...},
       "397661":{... "totalSeats":"350","availableSeats":"0" ...}}}}

So, unlike every other EDM source except posh/shotgun, this one has REAL
released / left counts, per section. Each section becomes one tier:
quantity = totalSeats (released), available = availableSeats (left),
used = quantity - available (sold, or sitting in someone's cart).

Notes / traps
- Counts are strings; prices are numbers (ALL-IN — $94.87 is the $85 face
  plus $9.87 fees, the number the page headlines).
- A sold-out section loses its `minPrice` and `ticketTypes` (397661 above),
  so its label has to be remembered from when it was selling, or a tier
  would be renamed on sell-out and fake a 'new_tier' ping. Labels are
  cached per sectionId in tm_cache/frontgate_events.json; a section first
  seen already sold out is just "Section <id>".
- "Left" excludes seats held in other buyers' carts (a 10-minute hold),
  so a sold-out section can come back for a few minutes when carts
  expire — that is the 'restock' ping, and it's real.
- The eventHubId is NOT the slug in the event URL (`/event/qdukuv5kq3ehokcw`)
  and the page's visible text doesn't show it; it sits in the page's
  script/markup. It's resolved once from the HTML and cached. If the
  regexes ever stop finding it, paste the /section/<id>/info URL instead —
  parse_url accepts it, and so does the "<sub>/<hubId>" shorthand.

Storefronts
- A promoter's bare storefront (https://givethanks.frontgatetickets.com/)
  lists several event pages (Give Thanks: 3-day / single-day GA and VIP).
  Paste one on /frontgate and every /event/<slug> it links is tracked, and
  events it adds later get picked up: this module is also an EDM catalog
  source (discover(), see edm_events.CATALOG_SOURCES). The storefront list
  is data, kept in the `frontgate_storefronts` setting, not in code.
- PRUNE = False: an event that drops off its storefront (sold out, or the
  promoter tidied up) stays tracked — a restock is exactly what we'd miss.
  Remove it by hand on /frontgate.

    python frontgate_events.py "<event URL | section info URL | sub/hubId>"  [--json]
    python frontgate_events.py --storefront <storefront URL>   (list its events)
"""
import html as _html
import json
import os
import re
import sys
import time
from datetime import datetime

import db
import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "frontgate"

CACHE_DIR = os.getenv("KARTIS_TM_CACHE_DIR") or "tm_cache"
CACHE_PATH = os.path.join(CACHE_DIR, "frontgate_events.json")
# Event metadata (name / venue / date) barely changes; re-read the page
# this often. The section endpoint is fetched every tick regardless.
META_TTL_SECONDS = int(os.getenv("KARTIS_FRONTGATE_META_TTL_SECONDS") or 6 * 3600)

_HOST_RE = re.compile(r"(?:https?://)?([a-z0-9][a-z0-9-]*)\.frontgatetickets\.com"
                      r"(/[^\s?#]*)?", re.I)
_EVENT_PATH_RE = re.compile(r"^/event/([A-Za-z0-9_-]+)")
_SECTION_PATH_RE = re.compile(r"^/section/(\d+)")
_SHORT_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)/([A-Za-z0-9_-]+)$", re.I)

# Where the eventHubId has been seen to live. Tried in order; the first one
# that matches wins. All are deliberately loose about quoting.
_HUB_RES = [
    re.compile(r"/section/(\d{3,})/info"),
    re.compile(r"event_?hub_?id[\"']?\s*[:=]\s*[\"']?(\d{3,})", re.I),
    re.compile(r"data-event-?hub(?:-?id)?\s*=\s*[\"'](\d{3,})", re.I),
    re.compile(r"\bhub_?id[\"']?\s*[:=]\s*[\"']?(\d{3,})", re.I),
]
_META_RE = re.compile(
    r"<meta[^>]+(?:property|name)\s*=\s*[\"']([^\"']+)[\"'][^>]*content\s*=\s*"
    r"[\"']([^\"']*)[\"']", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DATE_RE = re.compile(
    r"(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")
_SHOWTIME_RE = re.compile(r"(?:Show|Doors)\s+at\s+(\d{1,2}(?::\d{2})?\s*[AP]M)", re.I)


def parse_storefront(url):
    """Bare storefront URL ("https://<sub>.frontgatetickets.com/") → sub,
    else None. Anything with an /event/ or /section/ path is an event."""
    m = _HOST_RE.search((url or "").strip())
    if not m or m.group(1).lower() == "www":
        return None
    path = (m.group(2) or "").rstrip("/")
    return m.group(1).lower() if path == "" else None


def parse_url(url):
    """Frontgate URL or shorthand → event_key "<sub>/<slug-or-hubId>".

    Accepts an event page (…/event/<slug>), the section endpoint
    (…/section/<hubId>/info) or "<sub>/<slug|hubId>". A numeric tail is a
    hub id; anything else is an event slug resolved on first fetch."""
    s = (url or "").strip()
    m = _HOST_RE.search(s)
    if m:
        sub, path = m.group(1).lower(), m.group(2) or ""
        if sub == "www":
            raise ValueError("paste the promoter's Frontgate link "
                             "(<name>.frontgatetickets.com/event/…)")
        pm = _SECTION_PATH_RE.match(path) or _EVENT_PATH_RE.match(path)
        if pm:
            return f"{sub}/{pm.group(1)}"
        raise ValueError(f"not a Frontgate event link: {url!r}")
    m = _SHORT_RE.match(s)
    if m and "." not in s:
        return f"{m.group(1).lower()}/{m.group(2)}"
    raise ValueError(f"not a Frontgate event link: {url!r}")


def _split(key):
    sub, _, tail = parse_url(key).partition("/")
    return sub, tail


def site_base(sub):
    return f"https://{sub}.frontgatetickets.com"


def section_info_url(sub, hub_id):
    return f"{site_base(sub)}/section/{hub_id}/info"


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
    except OSError:
        pass  # a cache we can't write just means we re-read the page


def find_hub_id(page):
    for rx in _HUB_RES:
        m = rx.search(page or "")
        if m:
            return m.group(1)
    return None


def parse_meta(page):
    """name / venue / date_text / start_date from the event page HTML."""
    metas = {}
    for k, v in _META_RE.findall(page or ""):
        metas.setdefault(k.lower(), _html.unescape(v).strip())
    title = metas.get("og:title")
    if not title:
        m = _TITLE_RE.search(page or "")
        title = _html.unescape(m.group(1)).strip() if m else ""
    # og:title reads "Mellow Circus LA - General Admission @ Ace*Mission
    # Studios - Oct 31."
    name, venue = title, None
    if " @ " in title:
        name, rest = title.split(" @ ", 1)
        venue = re.split(r"\s+-\s+", rest, maxsplit=1)[0].strip() or None
    text = edm_common.strip_tags(page)
    date_text, start_date = None, None
    dm = _DATE_RE.search(text)
    if dm:
        date_text = dm.group(0)
        try:
            start_date = datetime.strptime(
                f"{dm.group(2)} {dm.group(3)} {dm.group(4)}", "%B %d %Y").date().isoformat()
        except ValueError:
            start_date = None
        tm = _SHOWTIME_RE.search(text)
        if tm:
            date_text += f" · {tm.group(1).upper().replace(' ', '')}"
    return {"name": name.strip() or None, "venue": venue,
            "date_text": date_text, "start_date": start_date,
            "image": metas.get("og:image")}


def _resolve(event_key, cache):
    """(sub, hub_id, meta) for an event key, from cache when fresh."""
    sub, tail = _split(event_key)
    entry = cache.get(event_key) or {}
    if entry.get("hub_id") and time.time() - (entry.get("meta_at") or 0) < META_TTL_SECONDS:
        return sub, entry["hub_id"], entry
    hub_id = tail if tail.isdigit() else entry.get("hub_id")
    if not tail.isdigit():
        try:
            page = edm_common.fetch_text(f"{site_base(sub)}/event/{tail}")
        except EdmEventsError:
            if hub_id:          # metadata refresh failed; the id still works
                return sub, hub_id, entry
            raise
        hub_id = find_hub_id(page) or hub_id
        if not hub_id:
            raise EdmEventsError(
                f"frontgate: no eventHubId on {site_base(sub)}/event/{tail} — "
                "paste the …/section/<id>/info link instead")
        entry.update(parse_meta(page))
    entry.update(hub_id=hub_id, meta_at=time.time())
    cache[event_key] = entry
    return sub, hub_id, entry


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_sections(data, labels=None):
    """section-info JSON → normalized tiers. `labels` {sectionId: name} is
    updated in place with every section that currently names itself."""
    if not isinstance(data, dict) or data.get("error") not in (0, "0", None):
        raise EdmEventsError(f"frontgate: section info returned an error "
                             f"({str(data)[:160]!r})")
    info = ((data.get("data") or {}).get("sectionInfo"))
    if not isinstance(info, dict) or not info:
        raise EdmEventsError("frontgate: section info has no sections "
                             "— wrong hub id, or the event was unpublished?")
    labels = labels if labels is not None else {}
    tiers = []
    for sid, sec in info.items():
        if not isinstance(sec, dict):
            continue
        sid = str(sec.get("sectionId") or sid)
        own = next((str(sec.get(k)).strip() for k in
                    ("ticketTypes", "name", "levelName")
                    if sec.get(k) and str(sec.get(k)).strip()), None)
        if own:
            labels[sid] = own
        total, left = _int(sec.get("totalSeats")), _int(sec.get("availableSeats"))
        price = sec.get("minPrice")
        price = float(price) if isinstance(price, (int, float)) else edm_common.parse_money(price)
        t = make_tier(labels.get(sid) or f"Section {sid}", price,
                      group=sec.get("sectionGroupName"),
                      available=left, quantity=total,
                      used=(total - left) if total is not None and left is not None else None,
                      sold_out=(left == 0))
        t["section_id"] = sid
        t["max_price"] = sec.get("maxPrice")
        tiers.append(t)
    if not tiers:
        raise EdmEventsError("frontgate: section info parsed to 0 sections")
    return tiers


def fetch_event(event_key):
    """One Frontgate event, normalized (see edm_common's docstring)."""
    event_key = parse_url(event_key)
    cache = _load_cache()
    sub, hub_id, meta = _resolve(event_key, cache)
    headers = dict(edm_common.BROWSER_HEADERS,
                   Accept="application/json, text/javascript, */*; q=0.01",
                   **{"X-Requested-With": "XMLHttpRequest",
                      "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                      "Sec-Fetch-Site": "same-origin"})
    data = edm_common.fetch_json(section_info_url(sub, hub_id), headers=headers)
    labels = meta.setdefault("labels", {})
    tiers = parse_sections(data, labels)
    cache[event_key] = meta
    _save_cache(cache)

    _, tail = _split(event_key)
    page_url = (f"{site_base(sub)}/event/{tail}" if not tail.isdigit()
                else site_base(sub))
    ev = {
        "source": SOURCE_NAME,
        "event_key": event_key,
        "name": meta.get("name") or f"Frontgate event {hub_id}",
        "venue": meta.get("venue"),
        "date_text": meta.get("date_text"),
        "start_date": meta.get("start_date"),
        "page_url": page_url,
        "image": meta.get("image"),
        "hub_id": hub_id,
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
    }
    ev = rollup(ev)
    # rollup only sums `available` over buyable tiers, which is the right
    # "left" number; released/sold are whole-event figures here.
    ev["total_released"] = sum(t["quantity"] or 0 for t in tiers)
    ev["total_sold"] = sum(t["used"] or 0 for t in tiers)
    return ev


# --- storefronts (catalog source) -------------------------------------------

PRUNE = False
STOREFRONTS_SETTING = "frontgate_storefronts"
_EVENT_LINK_RE = re.compile(r"/event/([A-Za-z0-9_-]{6,})")


def storefronts():
    """[{sub, name, added_at}] — the storefronts being watched."""
    try:
        rows = json.loads(db.setting_get(STOREFRONTS_SETTING) or "[]")
    except (TypeError, ValueError):
        rows = []
    return [r for r in rows if isinstance(r, dict) and r.get("sub")]


def _storefronts_save(rows, now_iso):
    db.setting_set(STOREFRONTS_SETTING, json.dumps(rows), now_iso)


def storefront_add(sub, name, now_iso):
    rows = [r for r in storefronts() if r["sub"] != sub]
    rows.append({"sub": sub, "name": name, "added_at": now_iso})
    _storefronts_save(rows, now_iso)


def storefront_remove(sub, now_iso):
    rows = storefronts()
    _storefronts_save([r for r in rows if r["sub"] != sub], now_iso)
    return len(rows) != len(storefronts())


def fetch_storefront(sub):
    """(storefront name, [event slugs in page order]) for one storefront.
    Raises EdmEventsError when the page lists no events at all — a blocked
    or redesigned page must not read as 'the promoter has nothing on'."""
    page = edm_common.fetch_text(site_base(sub) + "/")
    slugs = []
    for slug in _EVENT_LINK_RE.findall(page):
        if slug not in slugs:
            slugs.append(slug)
    if not slugs:
        raise EdmEventsError(f"frontgate: no /event/ links on {site_base(sub)}/")
    meta = parse_meta(page)
    # og:title reads "GIVE THANKS 2026 | SAN JOSE CONVENTION CENTER | Nov. 25th…"
    name = (meta.get("name") or "").split(" | ")[0].strip()
    return (name or sub), slugs


def discover():
    """Every event on every watched storefront, in the catalog-entry shape
    edm_events.sync_catalogs expects. venue_key is the storefront's sub."""
    entries, errors = [], {}
    for sf in storefronts():
        sub = sf["sub"]
        try:
            _name, slugs = fetch_storefront(sub)
        except EdmEventsError as e:
            errors[sub] = str(e)
            continue
        for slug in slugs:
            entries.append({
                "event_key": f"{sub}/{slug}",
                "ticket_url": f"{site_base(sub)}/event/{slug}",
                "venue_key": sub,
                "name": None,
                "venue": sf.get("name"),
            })
    return entries, errors


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    if "--storefront" in argv and args:
        sub = parse_storefront(args[0]) or args[0]
        name, slugs = fetch_storefront(sub)
        print(f"{name}: {len(slugs)} event(s)")
        for slug in slugs:
            print(f"  {site_base(sub)}/event/{slug}")
        return 0
    if not args:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    try:
        ev = fetch_event(args[0])
    except (EdmEventsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    print(f"  released {ev['total_released']:,} · left {ev['total_available'] or 0:,}"
          f" · sold/in carts {ev['total_sold']:,}  (hub {ev['hub_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
