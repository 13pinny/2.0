"""Tao Group venue tracker — pure-HTTP catalog discovery + tier/price fetcher.

Two halves, because Tao Group splits its content across two hosts:

- **Catalog** (`discover()`) — taogroup.com is WordPress, and its venue
  event pages (`/venues/<slug>/events/`) render an empty
  `<div id="react-events">` that a React bundle fills from the site's OWN
  public REST route, `GET /wp-json/wp/v2/events?event_venue=<term_id>`.
  Anonymous, no nonce, no Cloudflare challenge. That is the new-event
  radar: it is where an added show first appears, usually before anything
  is on sale. This is the analogue of pacha_events.fetch_events().
- **Prices** (`fetch_event()`) — every event's "Buy Tickets" link points at
  tickets.taogroup.com (an "eventservice" white-label, CakePHP + Stripe),
  whose `/e/<slug>/tickets` page is server-rendered and carries the ticket
  types, prices and sale state. That page is what climbs: GA is sold in
  numbered releases ("General Admission - Tier 1" -> "Tier 2" -> ...) and
  the headline price steps up as each sells through, exactly like pacha's
  release ladder. Skydeck adds a parallel "GA Fast Pass" and, on some
  shows, "Mastercard VIP".

The two are joined by the ACF `links` array on the catalog record: the
first tickets.taogroup.com URL there is the event's ticket page, and its
slug is this source's `event_key` (so `tao:marquee-new-york-9-11-26`).
Events that sell only through SevenRooms / speakeasygo (table
reservations) have no such link and are skipped — there is no tier data to
watch.

The venues watched are in VENUES below; adding another Tao Group room is
one row (the term_id comes from `window.reactEventsCalender` on any of
their venue pages).

TRAPS
- **The ld+json `offers` array on the ticket page is NOT the live state.**
  It is the configured price ladder: every tier says
  `availability: InStock` even when the whole event is sold out (verified
  on a fully sold-out Skydeck show), its prices are FACE not all-in, and
  it lists rows the page does not sell — future releases plus $0
  "Guest List" holds that would win the cheapest-tier slot outright. It is
  read here for event NAME / START / VENUE only; every price, count and
  sale state comes from the rendered ticket rows.
- **Prices come in both flavours and the page headlines all-in.**
  `data-default-all-in-price-each` is what the buyer pays ($130.00);
  `data-default-price-each` is face ($125.00). We report all-in as `price`
  (matching posh/leap/eventim) and face as `face_price`. The fee is not a
  fixed rate — most rooms add a flat $5, but one Skydeck show ran a
  percentage fee ($13.88 on $100) — so never reconstruct one from the
  other.
- **Two "unavailable" states, and they mean different things.**
  `not-available-sold-out` ("Sold Out") is genuinely gone;
  `not-available-inactive` is a row that exists but is not selling here
  (its cell holds an external Purchase link, e.g. a SevenRooms table). The
  first is `sold_out`, the second is `closed`; conflating them would
  report a table upsell as a sell-out.
- **Parse only inside `#ticket-types-content`.** On events with an
  access-code section the page also ships the JavaScript template that
  BUILDS ticket rows, and it contains the literal string
  `<div class="ticket-type-item pure-g">` — scanning the whole document
  finds those and yields garbage tiers.
- **No inventory is published.** The quantity `<select>` tops out at the
  per-order limit (4 for GA, 1 for a table share), so a high `max_qty` says
  nothing, and there is no per-tier limit published to compare it against
  — so `available` stays None (unknown) except at 0, where the sold-out
  marker makes it certain. This is the leap/eventim asymmetry: `low_stock`
  never fires for tao, and None must never be read as "sold out".

CLI probe:
    python tao_events.py                     # the whole catalog, both venues
    python tao_events.py --discover          # catalog only, no ticket fetches
    python tao_events.py <url-or-slug>       # one ticket page
"""
import json
import re
import sys
import time
from datetime import datetime, timezone

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "tao"
SITE_BASE = "https://taogroup.com"
TICKETS_BASE = "https://tickets.taogroup.com"
CATALOG_URL = SITE_BASE + "/wp-json/wp/v2/events"

# The venue calendars this tracker watches. `term_id` is the event_venue
# taxonomy id the React calendar filters on — every Tao venue page ships
# the full list in `window.reactEventsCalender.filters`, so adding a room
# is one row here.
VENUES = {
    "marquee-new-york": {
        "term_id": 90,
        "name": "Marquee New York",
        "calendar_url": SITE_BASE + "/venues/marquee-new-york/events/",
    },
    "marquee-skydeck": {
        "term_id": 939,
        "name": "Marquee Skydeck",
        "calendar_url": (SITE_BASE + "/venues/"
                         "marquee-skydeck-edge-hudson-yards-new-york/event-calendar/"),
    },
}

# Only the fields the radar needs — the untrimmed record carries the full
# rendered post plus yoast metadata (~60KB each), and _fields brings the
# whole two-venue sweep under 20KB gzipped.
_CATALOG_FIELDS = ("id,slug,link,status,acf.event_title,acf.start_epoch,"
                   "acf.event_start_date,acf.links")
_CATALOG_PAGE_SIZE = 100
_CATALOG_MAX_PAGES = 20          # 2000 events — a runaway-paging backstop

# https://tickets.taogroup.com/e/<slug>/tickets
_URL_RE = re.compile(r"tickets\.taogroup\.com/e/([A-Za-z0-9._~-]+)", re.I)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

_LDJSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
_TYPES_ANCHOR_RE = re.compile(r'id="ticket-types-content"')
_TYPES_END_RE = re.compile(r'<div id="ticketSelected"')
_ITEM_RE = re.compile(r'<div class="ticket-type-item[^"]*"[^>]*data-sort=')
_NAME_RE = re.compile(r'<h3 class="name[^"]*">(.*?)</h3>', re.S)
_ALL_IN_RE = re.compile(r'data-default-all-in-price-each="([\d.]+)"')
_FACE_RE = re.compile(r'data-default-price-each="([\d.]+)"')
_PRICE_CELL_RE = re.compile(r'class="content[^"]*\bprice\b[^"]*">(.*?)</div>', re.S)
_FEE_RE = re.compile(r'fee-notice">(.*?)<', re.S)
_OPTION_RE = re.compile(r'<option value="(\d+)"')
_SOLD_OUT_RE = re.compile(r"not-available-sold-out")
_INACTIVE_RE = re.compile(r"not-available-inactive")


def parse_url(url):
    """tickets.taogroup.com URL (or bare slug) -> slug. Raises ValueError."""
    s = (url or "").strip()
    m = _URL_RE.search(s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s and _SLUG_RE.match(s):
        return s
    raise ValueError(f"not a tickets.taogroup.com event URL: {url!r}")


def event_page_url(slug):
    return f"{TICKETS_BASE}/e/{slug}/tickets"


# ---------------------------------------------------------------- catalog

def _catalog_page(term_id, page):
    url = (f"{CATALOG_URL}?event_venue={term_id}&per_page={_CATALOG_PAGE_SIZE}"
           f"&page={page}&_fields={_CATALOG_FIELDS}")
    data = edm_common.fetch_json(url)
    if not isinstance(data, list):
        # WP answers a past-the-end page with an error object, not [].
        raise EdmEventsError(f"tao catalog page {page} for venue {term_id} "
                             f"returned {str(data)[:120]}")
    return data


def fetch_catalog(venue_key):
    """Every event record the WP calendar holds for one venue, all pages.
    Raises EdmEventsError on network trouble or a 200 that parses to zero
    events (a redesign must read as a fetch failure, never as an empty
    calendar)."""
    venue = VENUES.get(venue_key)
    if venue is None:
        raise EdmEventsError(f"unknown tao venue {venue_key!r}")
    out = []
    for page in range(1, _CATALOG_MAX_PAGES + 1):
        try:
            batch = _catalog_page(venue["term_id"], page)
        except EdmEventsError:
            if page == 1:
                raise
            break            # past the last page — WP 400s rather than []
        if not batch:
            break
        out.extend(batch)
        if len(batch) < _CATALOG_PAGE_SIZE:
            break
    if not out:
        raise EdmEventsError(
            f"tao catalog for {venue_key} parsed to 0 events — layout change?")
    return out


def _ticket_link(record):
    """The tickets.taogroup.com URL in an ACF links array, or None. Events
    that sell only through SevenRooms / speakeasygo have no such link."""
    for entry in ((record.get("acf") or {}).get("links") or []):
        link = (entry or {}).get("link") or {}
        url = (link.get("url") or "").strip()
        if "tickets.taogroup.com" in url:
            return url
    return None


def _catalog_entry(record, venue_key):
    acf = record.get("acf") or {}
    try:
        start_epoch = int(acf.get("start_epoch") or 0)
    except (TypeError, ValueError):
        start_epoch = 0
    url = _ticket_link(record)
    return {
        "venue_key": venue_key,
        "venue": VENUES[venue_key]["name"],
        "post_id": record.get("id"),
        "name": ((acf.get("event_title") or {}).get("display_title")
                 or "").strip() or None,
        "start_epoch": start_epoch,
        "date_text": (acf.get("event_start_date") or "").strip() or None,
        "info_url": record.get("link"),
        "ticket_url": url,
        "event_key": parse_url(url) if url else None,
    }


def discover(venue_keys=None, now_ts=None):
    """Upcoming events across the watched venues, soonest first.

    Returns (entries, errors): entries are catalog dicts with `event_key`
    set for the ones that have a tickets.taogroup.com page (the pollable
    ones) and None for reservation-only shows; errors is
    {venue_key: message}. One venue's calendar being down must not sink the
    other, so failures are reported rather than raised."""
    now_ts = int(now_ts if now_ts is not None else time.time())
    entries, errors = [], {}
    for venue_key in (venue_keys or VENUES):
        try:
            records = fetch_catalog(venue_key)
        except (EdmEventsError, ValueError) as e:
            errors[venue_key] = f"{type(e).__name__}: {e}"
            continue
        for record in records:
            if (record.get("status") or "publish") != "publish":
                continue
            entry = _catalog_entry(record, venue_key)
            # start_epoch 0 means the ACF date never got filled in; treat it
            # as "not a real upcoming show" rather than as 1970.
            if entry["start_epoch"] and entry["start_epoch"] >= now_ts:
                entries.append(entry)
    entries.sort(key=lambda e: (e["start_epoch"], e["name"] or ""))
    return entries, errors


# ------------------------------------------------------------ ticket page

def _ld_event(html):
    """The MusicEvent object on a ticket page. Metadata ONLY — its `offers`
    array is the configured ladder, not the live state (see docstring)."""
    for m in _LDJSON_RE.finditer(html):
        try:
            obj = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for cand in (obj if isinstance(obj, list) else [obj]):
            if isinstance(cand, dict) and "Event" in str(cand.get("@type") or ""):
                return cand
    return {}


def _ticket_rows(html):
    """The rendered ticket-type rows, as raw HTML fragments.

    Scoped to `#ticket-types-content` ... `#ticketSelected` on purpose: the
    access-code flow's inline JS builds rows from a template that contains
    the same `ticket-type-item` markup as a string, and a document-wide
    scan picks those up as tiers."""
    anchor = _TYPES_ANCHOR_RE.search(html)
    if not anchor:
        return []
    end = _TYPES_END_RE.search(html, anchor.end())
    region = html[anchor.end():end.start() if end else len(html)]
    starts = [m.start() for m in _ITEM_RE.finditer(region)]
    return [region[s:(starts[i + 1] if i + 1 < len(starts) else len(region))]
            for i, s in enumerate(starts)]


def _row_tier(row):
    """One ticket row -> (normalized tier, fee notice or None)."""
    name_m = _NAME_RE.search(row)
    name = edm_common.strip_tags(name_m.group(1)) if name_m else None

    all_in = _ALL_IN_RE.search(row)
    face = _FACE_RE.search(row)
    price = float(all_in.group(1)) if all_in else None
    face_price = float(face.group(1)) if face else None
    if price is None:
        # No hidden input (a row rendered as pure text, e.g. a free hold) —
        # fall back to the visible price cell.
        cell = _PRICE_CELL_RE.search(row)
        price = edm_common.parse_money(edm_common.strip_tags(cell.group(1))
                                       if cell else None)

    sold_out = bool(_SOLD_OUT_RE.search(row))
    closed = bool(_INACTIVE_RE.search(row))
    # The dropdown is capped at the per-order limit, and the limit itself is
    # not published, so a positive max_qty is NOT a remaining count. Only 0
    # is certain — and a row whose only option is 0 while carrying no
    # "Sold Out" marker is still nothing you can buy.
    qtys = [int(v) for v in _OPTION_RE.findall(row)]
    max_qty = max(qtys) if qtys else None
    if max_qty == 0 and not closed:
        sold_out = True

    fee_m = _FEE_RE.search(row)
    return make_tier(
        name, price,
        face_price=face_price,
        available=0 if sold_out else None,
        sold_out=sold_out,
        closed=closed,
        max_qty=max_qty,
    ), (edm_common.strip_tags(fee_m.group(1)) if fee_m else None)


def _venue_key_for(venue_name, slug):
    """Which room in VENUES this event belongs to, or None.

    The ticket page names the room (ld+json `location.name`), and the slug
    is the tiebreaker when it doesn't. Worth the belt and braces: the
    per-venue Discord routing keys off this, and a missing venue name
    defaulting Skydeck shows into the Marquee channel would be a silent
    mis-route rather than a visible failure."""
    n = (venue_name or "").strip().lower()
    s = (slug or "").strip().lower()
    for key, v in VENUES.items():
        if v["name"].lower() in n:
            return key
    if "skydeck" in n or "skydeck" in s:
        return "marquee-skydeck"
    if "marquee" in n or s.startswith("marquee-new-york"):
        return "marquee-new-york"
    return None


def _date_text(ld):
    """Human date in the venue's local clock. `startDate` here carries a
    real UTC offset (...T23:00:00-04:00), so it needs no timezone lookup —
    unlike shotgun's, which is UTC and shifts a Fri 11pm show into Sat."""
    raw = (ld or {}).get("startDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ")


def fetch_event(slug):
    """One tickets.taogroup.com event, normalized (see edm_common)."""
    slug = parse_url(slug)
    url = event_page_url(slug)
    html = edm_common.fetch_text(url)

    ld = _ld_event(html)
    rows = _ticket_rows(html)
    if not rows and not ld:
        raise EdmEventsError(f"tao: no ticket rows and no ld+json on {url} "
                             "— layout change or event removed?")
    tiers, fees = [], []
    for row in rows:
        tier, fee = _row_tier(row)
        if tier["price"] is None and not _NAME_RE.search(row):
            continue
        tiers.append(tier)
        if fee:
            fees.append(fee)
    if not tiers:
        # Deliberately an error, not "sold out": a page that renders no rows
        # at all is an unpublished / not-yet-on-sale event or a redesign,
        # and the last good state must survive it.
        raise EdmEventsError(f"tao: {slug} parsed to 0 ticket tiers — not yet "
                             "on sale, unpublished, or a layout change?")

    venue = None
    loc = ld.get("location")
    if isinstance(loc, dict):
        venue = (loc.get("name") or "").strip() or None

    ev = {
        "source": SOURCE_NAME,
        "event_key": slug,
        "name": (ld.get("name") or "").strip() or slug,
        "venue": venue,
        # Which VENUES room — notify.py routes the per-venue "new events"
        # Discord channels off this (Marquee NY and Skydeck get their own).
        "venue_key": _venue_key_for(venue, slug),
        "date_text": _date_text(ld),
        "start_date": ld.get("startDate"),
        "page_url": url,
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
        # Reported prices are what the buyer pays at checkout.
        "price_basis": "all-in",
        "fee_notice": fees[0] if fees else None,
        # This source finds its own events (see discover()), so a 'new' ping
        # here means the venue just announced a show — not that someone
        # pasted a URL. notify_edm_event words it accordingly.
        "catalog": True,
    }
    return rollup(ev)


# ------------------------------------------------------------------- CLI

def _print_catalog(entries, errors):
    print(f"{len(entries)} upcoming event(s) across {len(VENUES)} Tao venue(s)\n")
    for e in entries:
        # date_text is the ACF door time in the venue's own clock; the epoch
        # would need the venue tz to print the same day.
        when = (e["date_text"] or "")[:10]
        key = e["event_key"] or "— reservations only"
        print(f"  {when:<10}  {e['venue']:<18.18} "
              f"{(e['name'] or '?'):<42.42} {key}")
    for venue_key, msg in errors.items():
        print(f"ERROR {venue_key}: {msg}")


def main(argv):
    as_json = "--json" in argv
    discover_only = "--discover" in argv
    args = [a for a in argv if not a.startswith("--")]

    if args:
        ev = fetch_event(args[0])
        if as_json:
            print(json.dumps(ev, indent=1, ensure_ascii=False))
        else:
            edm_common.print_event(ev)
            if ev.get("fee_notice"):
                print(f"  ({ev['fee_notice']} — prices shown are all-in)")
        return 0

    entries, errors = discover()
    if discover_only:
        if as_json:
            print(json.dumps({"events": entries, "errors": errors},
                             indent=1, ensure_ascii=False))
        else:
            _print_catalog(entries, errors)
        return 1 if errors and not entries else 0

    events, fetch_errors = [], dict(errors)
    for e in entries:
        if not e["event_key"]:
            continue
        try:
            events.append(fetch_event(e["event_key"]))
        except (EdmEventsError, ValueError) as err:
            fetch_errors[e["event_key"]] = f"{type(err).__name__}: {err}"
        time.sleep(0.3)
    if as_json:
        print(json.dumps({"events": events, "errors": fetch_errors},
                         indent=1, ensure_ascii=False))
        return 0
    for ev in events:
        edm_common.print_event(ev)
        print()
    for key, msg in fetch_errors.items():
        print(f"ERROR {key}: {msg}")
    print(f"{len(events)} ok, {len(fetch_errors)} failed")
    return 1 if fetch_errors and not events else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
