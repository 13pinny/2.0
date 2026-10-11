"""shotgun.live single-event tracker — pure-HTTP tier/price/inventory fetcher.

Shotgun (shotgun.live) is a US/EU club-and-rave ticketing platform; the
tracked event is Nü Androids Presents: Jigitz at A.I. Warehouse, DC.
Shotgun calls ticket types "deals", and — like Posh's waves and Leap's
tiers — they open and sell through in sequence, so the headline price
steps up as the cheap ones empty.

How the fetch works: one anonymous GET of the event page. The Next.js
server render embeds BOTH of the things we need:

- `<script type="application/ld+json">` MusicEvent — name, startDate,
  location, and an `offers` array (per-deal name / price / availability).
- a `"deals":[…]` array inside the React flight payload — the SAME deals
  but with **`quantityLeft`**, i.e. real remaining inventory, plus `id`,
  `shotgunableFrom` / `shotgunableBy` and an event-level `"isSoldOut"`
  bool. Alongside it sit `startTime` / `endTime` (unix seconds) and
  `timezone` (an IANA name), which is the only place the event's LOCAL
  clock time is published.

We parse the deals array, because it is a strict superset of the ld+json
offers: it is what gives Shotgun posh-grade counts instead of leap-grade
status-only data. The ld+json is still read for the event metadata.

TRAPS
- **The deals array is ESCAPED in the page HTML.** It lives inside a
  `self.__next_f.push([1,"…"])` string literal, so the raw HTML contains
  `\"deals\":[{\"id\":…`, and a regex for `"deals":` finds nothing. The
  payload must be decoded first (same trick as pacha_events/posh_events)
  — `_flight_payload()` below. Fetching with an `RSC: 1` header happens to
  return the array unescaped, but that is not needed and the parser
  accepts either form. (Adding `Next-Router-Prefetch: 1` on top returns a
  459-byte stub with no deals at all — a tempting-looking dead end.)
- **Prices here are FACE, not all-in.** Both the deals array and the
  ld+json publish the pre-fee price ($35 for General Admission) while the
  page displays $39.60 at a ~13% booking fee. That fee rate is computed
  client-side and appears NOWHERE in any server payload, so this module
  reports face value and says so (`price` == `face_price`). Price-MOVE
  detection is unaffected — face and all-in move together — but Shotgun's
  absolute numbers are not directly comparable with posh/leap/eventim,
  which all report all-in. Do not "fix" this by hardcoding a fee rate:
  a silently-stale multiplier is worse than an honest face price.
- **`startDate` in the ld+json is UTC**, so formatting it directly turns a
  Sat 10:00 PM doors time into "Sun 2:00 AM". Use `startTime` + `timezone`
  from the flight payload for anything a human reads.
- `quantityLeft: 0` means that deal sold out but it stays listed, so the
  deals array is a full release history, not just what is buyable.
- `shotgunableBy` equals the event's `endTime` on this event, i.e. it is a
  "sales close when the event ends" bound, not an early cutoff.
- Event-level `isSoldOut` is authoritative for "nothing left at all" and
  is trusted over the per-deal rollup.

CLI probe:  python shotgun_events.py [<url-or-slug>] [--json]
            python shotgun_events.py --venues [--json]   (venue pages)

Venue pages are also watched, announce-only — see the VENUES block below.
"""
import html as _html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "shotgun"
SITE_BASE = "https://shotgun.live"
DEFAULT_LOCALE = "en"

# https://shotgun.live/<locale>/events/<slug>   (locale optional/any)
_URL_RE = re.compile(r"shotgun\.live/(?:([a-z]{2})/)?events/([A-Za-z0-9._~-]+)", re.I)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_LDJSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
_DEALS_RE = re.compile(r'"deals"\s*:\s*\[')
_SOLD_OUT_RE = re.compile(r'"isSoldOut"\s*:\s*(true|false)')
_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"')
_START_TIME_RE = re.compile(r'"startTime"\s*:\s*(\d{9,12})')
_TIMEZONE_RE = re.compile(r'"timezone"\s*:\s*"([A-Za-z]+/[A-Za-z_+\-/]+)"')


def parse_url(url):
    """shotgun.live event URL (or bare slug) → slug. Raises ValueError."""
    s = (url or "").strip()
    m = _URL_RE.search(s)
    if m:
        return m.group(2)
    if "/" not in s and "." not in s and _SLUG_RE.match(s):
        return s
    raise ValueError(f"not a shotgun.live event URL: {url!r}")


def event_page_url(slug, locale=DEFAULT_LOCALE):
    return f"{SITE_BASE}/{locale}/events/{slug}"


def _flight_payload(html):
    """Concatenate the decoded string args of every __next_f.push chunk.
    This is what turns the escaped `\\"deals\\":[…]` in the raw HTML into
    real JSON we can parse."""
    dec = json.JSONDecoder()
    chunks = []
    for m in _FLIGHT_PUSH_RE.finditer(html):
        try:
            s, _ = dec.raw_decode(html, m.end() - 1)
        except ValueError:
            continue
        chunks.append(s)
    return "".join(chunks)


def _ld_event(html):
    """The MusicEvent/Event object among the page's ld+json blocks (the page
    also ships Brand / WebSite / BreadcrumbList blocks — skip those)."""
    for m in _LDJSON_RE.finditer(html):
        try:
            obj = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for cand in (obj if isinstance(obj, list) else [obj]):
            if isinstance(cand, dict) and "Event" in str(cand.get("@type") or ""):
                return cand
    return {}


def parse_deals(html):
    """The `"deals":[…]` array — the count-bearing feed. Returns [] when the
    key is absent (caller decides whether that's fatal)."""
    dec = json.JSONDecoder()
    best = []
    for m in _DEALS_RE.finditer(html):
        try:
            arr, _ = dec.raw_decode(html, html.index("[", m.start()))
        except ValueError:
            continue
        if isinstance(arr, list) and len(arr) > len(best) and any(
                isinstance(d, dict) and "title" in d for d in arr):
            best = arr
    return best


def _tiers(deals, now_ts):
    out = []
    for d in deals:
        if not isinstance(d, dict) or not d.get("title"):
            continue
        left = d.get("quantityLeft")
        # Sale window: shotgunableFrom in the future = not open yet;
        # shotgunableBy in the past = sales ended. Both are unix seconds and
        # either may be null (meaning "unbounded"), never 0.
        frm, by = d.get("shotgunableFrom"), d.get("shotgunableBy")
        closed = bool((frm and now_ts < frm) or (by and now_ts > by))
        price = d.get("price")
        out.append(make_tier(
            d.get("title"),
            price,
            # Face == price here: Shotgun publishes no all-in figure
            # server-side (see the module docstring).
            face_price=price,
            available=left,
            sold_out=(left == 0),
            closed=closed,
        ))
    return out


def _date_text(payload, ld):
    """Human date in the EVENT's local clock. Prefers the flight payload's
    startTime+timezone; falls back to the ld+json startDate, which is UTC
    and so is labelled as such rather than quietly showing the wrong day."""
    from datetime import datetime, timezone as _tz
    tm = _START_TIME_RE.search(payload or "")
    zm = _TIMEZONE_RE.search(payload or "")
    if tm:
        dt = datetime.fromtimestamp(int(tm.group(1)), _tz.utc)
        suffix = " UTC"
        if zm:
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo(zm.group(1)))
                suffix = ""
            except Exception:
                pass  # unknown tz name / no tzdata — keep the UTC reading
        return dt.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ") + suffix
    raw = (ld or {}).get("startDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%a, %b %d, %Y · %I:%M %p UTC").replace(" 0", " ")


def fetch_event(slug):
    """One shotgun.live event, normalized (see edm_common's docstring)."""
    slug = parse_url(slug)
    url = event_page_url(slug)
    html = edm_common.fetch_text(url)

    # The deals array is escaped inside the flight payload on a normal page
    # load, and unescaped when the page is fetched as RSC — look in the
    # decoded payload first, then fall back to the raw HTML so both work.
    payload = _flight_payload(html)
    deals = parse_deals(payload) or parse_deals(html)
    ld = _ld_event(html)
    if not deals and not ld:
        raise EdmEventsError(f"shotgun: neither deals nor ld+json on {url} "
                             "— layout change or event removed?")

    tiers = _tiers(deals, time.time())
    if not tiers:
        # Fall back to the ld+json offers, which carry no counts but do
        # carry price + availability. Better a status-only reading than a
        # blank one; still an error if BOTH are empty.
        for o in (ld.get("offers") if isinstance(ld.get("offers"), list) else []):
            if not isinstance(o, dict):
                continue
            price = edm_common.parse_money(o.get("price"))
            tiers.append(make_tier(
                o.get("name"), price, face_price=price,
                sold_out="soldout" in str(o.get("availability") or "").lower()
                         .replace(" ", "")))
    if not tiers:
        raise EdmEventsError(f"shotgun: {slug} parsed to 0 ticket tiers "
                             "— layout change or event unpublished?")

    venue = None
    loc = ld.get("location")
    if isinstance(loc, dict):
        venue = (loc.get("name") or "").strip() or None

    ev = {
        "source": SOURCE_NAME,
        "event_key": slug,
        "name": (ld.get("name") or "").strip() or slug,
        "venue": venue,
        "date_text": _date_text(payload, ld),
        "start_date": ld.get("startDate"),
        "page_url": url,
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
        # Prices are pre-fee — surfaced so callers/dashboards can say so.
        "price_basis": "face",
    }
    ev = rollup(ev)

    # The event-level flag is authoritative for a total sell-out; trust it
    # over the per-deal rollup (a deal can sit at quantityLeft>0 while the
    # event is closed, e.g. a table hold).
    m = _SOLD_OUT_RE.search(payload) or _SOLD_OUT_RE.search(html)
    if m and m.group(1) == "true":
        ev["sold_out"] = True
        ev["on_sale"] = False
    return ev


# ---------------------------------------------------------------- venues
#
# Venue/organizer pages (shotgun.live/<locale>/venues/<slug>) list every
# upcoming event as a card. These are watched ANNOUNCE-ONLY: a show that
# appears on the page gets one 'new' ping in the venue's own channel and
# is NOT added to the poll list — the point is "they just announced X",
# and polling ~50 shows for price moves would bury that in noise. See
# edm_events.sync_catalogs (entries with track=False).
#
# Traps:
# - shotgun.live sits behind a Vercel bot checkpoint ("Vercel Security
#   Checkpoint", HTTP 429) that blocks plain HTTP from datacenter IPs and
#   headless browsers, and since 2026-10 the r.jina.ai reader too. So the
#   relay (eventim_relay.py, source "shotgun") loads the venue page in its
#   real Chrome and POSTs the HTML to /api/edm/eventim-relay, exactly like
#   tao; _load_venue_html prefers that copy and only tries its own fetch
#   when the copy is missing or stale.
# - The page renders only the first 24 upcoming events; the rest sit behind
#   a "See more" button that calls a Next server action (no public API —
#   api.shotgun.live wants an organizer API key). The relay clicks it until
#   it's gone. A page that still shows the button is TRUNCATED and is
#   treated as a fetch failure, never as the full list: otherwise events
#   25+ would look brand new the first time a complete copy arrives.
# - Cards under "Past events" use the same markup, so only the section
#   between the "Upcoming events" and "Past events" headings is read.
# - <time datetime> is UTC; date_text is rendered in the venue's "tz".
VENUES = {
    "nu-androids": {
        "name": "Nu Androids",
        "slug": "nu-androids",
        "tz": "America/New_York",
    },
}

RELAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "tm_cache", "shotgun_relay")
RELAY_MAX_AGE_SECONDS = int(os.getenv("KARTIS_SHOTGUN_RELAY_MAX_AGE_SECONDS")
                            or 1800)
VENUE_RELAY_PREFIX = "venue:"

_CARD_RE = re.compile(
    r'<a[^>]*href="(?:https://shotgun\.live)?/(?:[a-z]{2}(?:-[a-z]{2})?/)?'
    r'events/([A-Za-z0-9._~-]+)"[^>]*>(.*?)</a>', re.S)
_CARD_TITLE_RE = re.compile(r"<p[^>]*font-bold[^>]*>(.*?)</p>", re.S)
_CARD_ALT_RE = re.compile(r'<img[^>]*alt="([^"]*)"')
_CARD_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_CARD_PLACE_RE = re.compile(r'<div class="text-muted-foreground[^"]*">(.*?)</div>', re.S)
_SEE_MORE_RE = re.compile(r">\s*See more\s*</button>", re.I)
# The paginated list's own props carry the true count, e.g.
# {"eventsToQuery":"upcoming","organizerId":220134,"eventsPerPage":6,
#  "totalEventsNumber":50,"skip":24} — escaped inside the flight payload.
_TOTAL_UPCOMING_RE = re.compile(
    r'eventsToQuery\\?"\s*:\s*\\?"upcoming\\?".{0,200}?'
    r'totalEventsNumber\\?"\s*:\s*(\d+)', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def venue_page_url(venue_key, locale=DEFAULT_LOCALE):
    return f"{SITE_BASE}/{locale}/venues/{VENUES[venue_key]['slug']}"


def _is_checkpoint(page):
    return "Vercel Security Checkpoint" in (page or "")[:4000]


def _card_text(fragment):
    return _html.unescape(_TAG_RE.sub("", fragment or "")).strip()


def parse_venue_upcoming(page, tz_name="America/New_York"):
    """Event cards under "Upcoming events" -> list of dicts. Raises
    EdmEventsError for a checkpoint page, a missing section, or a list the
    page itself says is incomplete (see the truncation trap above)."""
    if _is_checkpoint(page):
        raise EdmEventsError("shotgun venue page is the Vercel bot checkpoint")
    start = page.find(">Upcoming events<")
    if start == -1:
        if "No upcoming events" in page:
            return []
        raise EdmEventsError("shotgun venue page has no 'Upcoming events' "
                             "section — layout change?")
    end = page.find(">Past events<", start)
    section = page[start:end if end != -1 else len(page)]

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    out, seen = [], set()
    for m in _CARD_RE.finditer(section):
        slug, card = m.group(1), m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        title = _CARD_TITLE_RE.search(card) or _CARD_ALT_RE.search(card)
        when = _CARD_TIME_RE.search(card)
        place = _CARD_PLACE_RE.search(card)
        start_epoch, date_text = 0, None
        if when:
            try:
                dt = datetime.fromisoformat(when.group(1).replace("Z", "+00:00"))
                start_epoch = int(dt.timestamp())
                date_text = (dt.astimezone(tz)
                             .strftime("%a, %b %d, %Y · %I:%M %p")
                             .replace(" 0", " "))
            except ValueError:
                pass
        out.append({
            "event_key": slug,
            "name": _card_text(title.group(1)) if title else slug,
            "place": _card_text(place.group(1)) if place else None,
            "start_epoch": start_epoch,
            "date_text": date_text,
            "ticket_url": event_page_url(slug),
        })

    # The button is the test: it's gone exactly when the list is complete.
    # totalEventsNumber only feeds the message — it's the server's count at
    # first render and can disagree with what the client finally shows.
    total = _TOTAL_UPCOMING_RE.search(page)
    if _SEE_MORE_RE.search(section):
        raise EdmEventsError(
            f"shotgun venue list truncated ({len(out)} of "
            f"{total.group(1) if total else '?'} upcoming shown) — needs the "
            "relay's expanded copy (is kartis-relay running with "
            "KARTIS_RELAY_SOURCES including shotgun?)")
    return out


def _venue_relay_path(venue_key):
    return os.path.join(RELAY_DIR, f"venue-{venue_key}.html")


def relay_age(event_key):
    """Seconds since the relay last stored this page, or None. Only venue
    pages ("venue:<key>") are relayed."""
    key = str(event_key or "")
    if not key.startswith(VENUE_RELAY_PREFIX):
        return None
    try:
        return time.time() - os.path.getmtime(
            _venue_relay_path(key[len(VENUE_RELAY_PREFIX):]))
    except OSError:
        return None


def relay_store(event_key, page):
    """Keep a relay-rendered venue page ("venue:<key>") for discover().
    Refuses a checkpoint, a truncated list or an unknown venue, so a bad
    load can never overwrite a good copy. Returns the number of events."""
    key = str(event_key or "")
    if not key.startswith(VENUE_RELAY_PREFIX):
        raise EdmEventsError(f"shotgun relay: only venue pages are relayed, got {key!r}")
    venue_key = key[len(VENUE_RELAY_PREFIX):]
    venue = VENUES.get(venue_key)
    if venue is None:
        raise EdmEventsError(f"shotgun relay: unknown venue {venue_key!r}")
    entries = parse_venue_upcoming(page or "", venue.get("tz") or "America/New_York")
    os.makedirs(RELAY_DIR, exist_ok=True)
    path = _venue_relay_path(venue_key)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(path + ".tmp", path)
    return len(entries)


def relay_targets():
    """What the relay should load for this source: every watched venue page,
    with a hint to click "See more" until the whole list is on the page."""
    return [{"source": SOURCE_NAME, "event_key": VENUE_RELAY_PREFIX + vk,
             "url": venue_page_url(vk), "expand": "See more"}
            for vk in VENUES]


def _load_venue_html(venue_key):
    age = relay_age(VENUE_RELAY_PREFIX + venue_key)
    if age is not None and age <= RELAY_MAX_AGE_SECONDS:
        with open(_venue_relay_path(venue_key), encoding="utf-8") as f:
            return f.read()
    when = f"{age / 60:.0f} min ago" if age is not None else "never"
    try:
        return edm_common.fetch_text(venue_page_url(venue_key))
    except EdmEventsError as e:
        raise EdmEventsError(f"{e} — relay last sent this venue page {when}; "
                             "is kartis-relay running?") from e


def discover(venue_keys=None, now_ts=None):
    """Upcoming events across the watched venue pages, soonest first, in the
    catalog-entry shape edm_events.sync_catalogs expects. Every entry is
    track=False (announce-only). Returns (entries, errors) like
    tao_events.discover; one venue failing doesn't sink the others."""
    now_ts = int(now_ts if now_ts is not None else time.time())
    entries, errors = [], {}
    for venue_key in (venue_keys or VENUES):
        venue = VENUES[venue_key]
        try:
            cards = parse_venue_upcoming(_load_venue_html(venue_key),
                                         venue.get("tz") or "America/New_York")
        except (EdmEventsError, ValueError) as e:
            errors[venue_key] = f"{type(e).__name__}: {e}"
            continue
        for c in cards:
            # A show whose start time has passed can linger on the page for
            # the night; it isn't news.
            if c["start_epoch"] and c["start_epoch"] < now_ts:
                continue
            entries.append({
                **c,
                "venue_key": venue_key,
                "venue": c.get("place") or venue["name"],
                "track": False,
            })
    entries.sort(key=lambda e: (e["start_epoch"] or 0, e["name"] or ""))
    return entries, errors


DEFAULT_TARGET = "jigitz0912"


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    if "--venues" in argv:
        entries, errors = discover()
        if as_json:
            print(json.dumps({"entries": entries, "errors": errors},
                             indent=1, ensure_ascii=False))
            return 0
        for e in entries:
            print(f"{e['venue_key']:14} {e['date_text'] or '?':32} "
                  f"{e['name']}  ({e['venue']})  {e['ticket_url']}")
        for vk, msg in errors.items():
            print(f"ERROR {vk}: {msg}")
        return 1 if errors else 0
    ev = fetch_event(args[0] if args else DEFAULT_TARGET)
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    print("  (prices are FACE value — Shotgun adds a ~13% booking fee at "
          "checkout that it publishes nowhere server-side)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
