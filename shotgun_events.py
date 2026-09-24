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
"""
import json
import re
import sys
import time

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


DEFAULT_TARGET = "jigitz0912"


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
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
