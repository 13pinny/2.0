"""wl.eventim.us single-event tracker — pure-HTTP tier/price fetcher.

Eventim USA's white-label storefront (wl.eventim.us) is what venues like
The Concourse Project in Austin sell through — the tracked event is ISOxo
pres: Hardcore Diva (Night 1). Ticket types are grouped ("Night 1 General
Admission (18+)", "Night 1 VIP (Backstage) Admission (21+)", table service)
and each shows an all-in price broken into face value + service fee.

How the fetch works: one anonymous GET of the event page, server-rendered
HTML parsed with regex (no bs4 in this project). Per group the page emits

    <div class="grouping-dropdown">
      <div class="grouping-dropdown-title"><a ...>GROUP NAME</a></div>
      ...<ul class="ticket-list">
           <li> <div class="ticket-type ...">NAME</div>
                <div class="price ...">
                  <span id="subtotal_<ttid>">$52.60</span>      <- all-in
                  (<span id="price_<ttid>">$45.00</span> face value
                   + $7.60 service fee)
                </div>
                <select tickettypeid="<ttid>">...</select>      <- buyable
           </li>
           <li> <div class="ticket-type ...">NAME</div>
                <div class="purchprice tix-not-avail">This ticket type is
                sold out.</div>                                 <- not buyable
           </li>
         </ul>

plus a schema.org ld+json Event with name/startDate/location.

TWO TRAPS, both of which silently produce a WRONG answer rather than an
error, so they are guarded explicitly:

1. CLOUDFLARE. wl.eventim.us answers HTTP 403 to the short "UA + Accept"
   header set the other pure-HTTP modules here use. It passes with the full
   Chrome header set (sec-ch-ua + Sec-Fetch-* + Upgrade-Insecure-Requests)
   — that is why edm_common.BROWSER_HEADERS exists. The 403 body is a 670KB
   challenge page, i.e. big and HTML-looking, so a parser that shrugs at it
   reports "0 tiers" instead of "blocked".

2. THE AFFILIATE KEY IS LOAD-BEARING. `?afflky=<key>` is not tracking
   decoration: fetched WITHOUT it the same event id returns a 41KB page
   with NO ticket types at all; WITH it, 67KB including every type, price
   and sold-out flag. Dropping the parameter would therefore look exactly
   like "everything sold out". The key is part of the event identity here,
   so event_key is "<id>@<afflky>" and parse_url preserves it.

INVENTORY: Eventim publishes no remaining counts. What it does leak is the
quantity <select> — its highest option is min(remaining, per-order limit),
so once a type drops under the order cap (6 here) `max_qty` becomes a real
"N left" reading. It is exposed as max_qty and, only when it is strictly
below the event's order limit, as `available` — a genuine tail-end
low-stock signal. Above that it stays None (unknown), never a fake count.

CLI probe:  python eventim_events.py [<url>] [--json]
"""
import json
import re
import sys

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "eventim"
SITE_BASE = "https://wl.eventim.us"

# https://wl.eventim.us/event/<slug>/<id>?afflky=<key>   (slug is cosmetic —
# /event/_/<id> serves the same page — but <id> and afflky are not)
_URL_RE = re.compile(r"eventim\.us/event/([^/]+)/(\d+)", re.I)
_AFFLKY_RE = re.compile(r"[?&]afflky=([A-Za-z0-9._~-]+)", re.I)
_KEY_RE = re.compile(r"^(\d+)(?:@([A-Za-z0-9._~-]+))?$")

_LDJSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
_CANONICAL_RE = re.compile(r'rel="canonical"\s+href="([^"]+)"', re.I)
_GROUP_SPLIT_RE = re.compile(r'<div class="grouping-dropdown">', re.I)
_GROUP_TITLE_RE = re.compile(
    r'<div class="grouping-dropdown-title">\s*<a[^>]*>(.*?)</a>', re.I | re.S)
_TICKET_LIST_RE = re.compile(r'<ul class="ticket-list">(.*?)</ul>', re.I | re.S)
_LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
_NAME_RE = re.compile(r'<div class="ticket-type[^"]*">(.*?)</div>\s*<div class="(?:price|purchprice)',
                      re.I | re.S)
_NAME_FALLBACK_RE = re.compile(r'<div class="ticket-type[^"]*">(.*)', re.I | re.S)
_SUBTOTAL_RE = re.compile(r'<span id="subtotal_(\d+)"[^>]*>(.*?)</span>', re.I | re.S)
_FACE_RE = re.compile(r'<span id="price_(\d+)"[^>]*>(.*?)</span>', re.I | re.S)
_NOTAVAIL_RE = re.compile(r'<div class="purchprice tix-not-avail">(.*?)</div>', re.I | re.S)
_SELECT_RE = re.compile(r'<select[^>]*tickettypeid="(\d+)"(.*?)</select>', re.I | re.S)
_OPTION_VAL_RE = re.compile(r'<option value="(\d+)"', re.I)
_LIMIT_RE = re.compile(r"There is a (\d+) ticket limit", re.I)


def parse_url(url):
    """wl.eventim.us event URL → "<id>@<afflky>" (or "<id>" when the URL
    carries no affiliate key). Also accepts an already-built key. The
    affiliate key is retained deliberately — see the module docstring."""
    s = (url or "").strip()
    m = _KEY_RE.match(s)
    if m:
        return s
    m = _URL_RE.search(s)
    if not m:
        raise ValueError(f"not a wl.eventim.us event URL: {url!r}")
    event_id = m.group(2)
    a = _AFFLKY_RE.search(s)
    return f"{event_id}@{a.group(1)}" if a else event_id


def split_key(event_key):
    m = _KEY_RE.match((event_key or "").strip())
    if not m:
        raise ValueError(f"bad eventim key: {event_key!r}")
    return m.group(1), m.group(2)


def fetch_url(event_key):
    """The URL actually fetched — slug-independent, affiliate preserved."""
    event_id, afflky = split_key(event_key)
    url = f"{SITE_BASE}/event/_/{event_id}"
    return f"{url}?afflky={afflky}" if afflky else url


def event_page_url(event_key, slug=None):
    event_id, afflky = split_key(event_key)
    url = f"{SITE_BASE}/event/{slug or '_'}/{event_id}"
    return f"{url}?afflky={afflky}" if afflky else url


def _ld_event(html):
    for m in _LDJSON_RE.finditer(html):
        blob = m.group(1).strip()
        try:
            obj = json.loads(blob)
        except ValueError:
            continue
        for cand in (obj if isinstance(obj, list) else [obj]):
            if isinstance(cand, dict) and "Event" in str(cand.get("@type") or ""):
                return cand
    return {}


def _li_tier(li, group, order_limit):
    """One <li> of a ticket-list → a normalized tier, or None if it isn't a
    ticket row."""
    m = _NAME_RE.search(li) or _NAME_FALLBACK_RE.search(li)
    name = edm_common.strip_tags(m.group(1)) if m else ""
    if not name:
        return None

    notavail = _NOTAVAIL_RE.search(li)
    sub = _SUBTOTAL_RE.search(li)
    face = _FACE_RE.search(li)
    sel = _SELECT_RE.search(li)

    price = edm_common.parse_money(sub.group(2)) if sub else None
    face_price = edm_common.parse_money(face.group(2)) if face else None

    sold_out = closed = False
    if notavail:
        txt = edm_common.strip_tags(notavail.group(1))
        if "sold out" in txt.lower():
            sold_out = True
        else:
            # An informational row ("Click here to purchase Night 2
            # tickets.") — a cross-sell pointer, not inventory. Not buyable,
            # but must not be reported as a sell-out.
            closed = True
    elif not sel:
        # No quantity picker and no explicit notice: nothing to buy.
        closed = True

    max_qty = None
    if sel:
        vals = [int(v) for v in _OPTION_VAL_RE.findall(sel.group(2))]
        max_qty = max(vals) if vals else None

    # The quantity dropdown is capped at the per-order limit, so it only
    # reveals inventory once fewer than that many remain. Strictly-below is
    # the guard: max_qty == limit means "at least `limit` left", unknown.
    available = None
    if max_qty is not None and order_limit and max_qty < order_limit:
        available = max_qty
    elif sold_out:
        available = 0

    return make_tier(name, price, group=group, face_price=face_price,
                     available=available, sold_out=sold_out, closed=closed,
                     max_qty=max_qty)


def parse_tiers(html):
    limit_m = _LIMIT_RE.search(html)
    order_limit = int(limit_m.group(1)) if limit_m else None
    tiers = []
    for chunk in _GROUP_SPLIT_RE.split(html)[1:]:
        tm = _GROUP_TITLE_RE.search(chunk)
        group = edm_common.strip_tags(tm.group(1)) if tm else None
        lm = _TICKET_LIST_RE.search(chunk)
        if not lm:
            continue
        for li in _LI_RE.findall(lm.group(1)):
            tier = _li_tier(li, group, order_limit)
            if tier:
                tiers.append(tier)
    return tiers, order_limit


def _date_text(ld):
    from datetime import datetime
    raw = ld.get("startDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ")


def fetch_event(event_key):
    """One wl.eventim.us event, normalized (see edm_common's docstring)."""
    event_key = parse_url(event_key)
    url = fetch_url(event_key)
    html = edm_common.fetch_text(url)

    # A Cloudflare interstitial is a big HTML page with none of our markup;
    # make that read as a fetch failure, never as an empty tier list.
    if "grouping-dropdown" not in html and "ticket-list" not in html:
        if "challenge" in html.lower() or "cf-" in html.lower():
            raise EdmEventsError(
                f"eventim: {url} looks like a Cloudflare challenge, not the "
                "event page — headers may need refreshing")
        raise EdmEventsError(f"eventim: no ticket markup on {url} "
                             "— layout change, or the affiliate key is missing")

    tiers, order_limit = parse_tiers(html)
    if not tiers:
        raise EdmEventsError(f"eventim: {event_key} parsed to 0 ticket tiers "
                             "— layout change?")
    ld = _ld_event(html)

    slug = None
    cm = _CANONICAL_RE.search(html)
    if cm:
        sm = _URL_RE.search(cm.group(1))
        if sm:
            slug = sm.group(1)

    venue = None
    loc = ld.get("location")
    if isinstance(loc, dict):
        venue = (loc.get("name") or "").strip() or None

    ev = {
        "source": SOURCE_NAME,
        "event_key": event_key,
        "name": (ld.get("name") or "").strip() or f"Eventim event {event_key}",
        "venue": venue,
        "date_text": _date_text(ld),
        "start_date": ld.get("startDate"),
        "page_url": event_page_url(event_key, slug),
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
        "order_limit": order_limit,
    }
    return rollup(ev)


DEFAULT_TARGET = ("https://wl.eventim.us/event/"
                  "ISOxo-pres-Hardcore-Diva-Night-1-at-The-Concourse-Project/"
                  "690917?afflky=TheConcourseProject")


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    ev = fetch_event(args[0] if args else DEFAULT_TARGET)
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    if ev.get("order_limit"):
        print(f"  ({ev['order_limit']}-ticket order limit — a count only "
              "appears once fewer than that remain)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
