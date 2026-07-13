"""pacha-nyc.com new-event monitor — pure-HTTP event-listing fetcher.

Pacha NYC (Brooklyn club, ticketing backend = FourVenues, same outfit as
pacha_tickets.py's email extractor) adds events to https://pacha-nyc.com/events
at random times, and ticket prices climb through releases as they sell.
This module fetches the CURRENT event list; the diff-and-ping loop lives in
app.py (`run_pacha_events`).

How the fetch works: the site is Next.js, but the full event list is
server-embedded in the /events HTML as React flight payload — a series of
`self.__next_f.push([1,"..."])` script chunks whose second element is a JSON
string literal. Decoding each chunk with json.JSONDecoder().raw_decode,
concatenating, and raw_decode-ing the array after the single
`"initialEvents":` marker yields every event object. Plain urllib with a
Chrome UA gets HTTP 200 — no browser, no Cloudflare/DataDome challenge
(verified from both a residential IP and the Hetzner box).

Each raw event carries: event_id (32-char), name, slug
("black-coffee-05-07-2026"), date (human string), start_date/end_date (ISO,
America/New_York), iswaitlist, hide_general_ticket, iframe (standalone buy
URL https://fourvenues.com/iframe/pacha-new-york/XXXX) and a prices dict —
prices.general_access is the headline "BUY TICKETS FROM $X" number that
jumps between releases (the price-increase signal), prices.tickets[] has
per-tier price/quantity/used/available.

The per-event public page is https://pacha-nyc.com/event/<slug> (singular;
/events/<slug> is a 404).

CLI probe:  python pacha_events.py [--json]
"""
import gzip
import json
import re
import sys
import urllib.error
import urllib.request


SOURCE_NAME = "pacha"
SITE_BASE = "https://pacha-nyc.com"
EVENTS_URL = SITE_BASE + "/events"
REQUEST_TIMEOUT = 20

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}

_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"')


class PachaEventsError(RuntimeError):
    pass


def event_page_url(slug):
    return f"{SITE_BASE}/event/{slug}" if slug else EVENTS_URL


def fetch_html(url=EVENTS_URL):
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise PachaEventsError(f"pacha events page returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise PachaEventsError(f"pacha events page unreachable: {e.reason}") from e
    return raw.decode("utf-8", errors="replace")


def _flight_payload(html):
    """Concatenate the decoded string args of every __next_f.push chunk."""
    dec = json.JSONDecoder()
    chunks = []
    for m in _FLIGHT_PUSH_RE.finditer(html):
        try:
            s, _ = dec.raw_decode(html, m.end() - 1)
        except ValueError:
            continue
        chunks.append(s)
    return "".join(chunks)


def _fallback_scan(payload):
    """Last-resort per-object scan if the initialEvents marker moves."""
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(r'\{"event_id":"[a-z0-9]{20,40}"', payload):
        try:
            obj, _ = dec.raw_decode(payload, m.start())
        except ValueError:
            continue
        out.append(obj)
    # the same object can appear in more than one flight row — dedupe
    seen, uniq = set(), []
    for o in out:
        if o["event_id"] not in seen:
            seen.add(o["event_id"])
            uniq.append(o)
    return uniq


def parse_events(html):
    """Raw event dicts from the /events page HTML."""
    payload = _flight_payload(html)
    i = payload.find('"initialEvents":')
    if i >= 0:
        try:
            arr, _ = json.JSONDecoder().raw_decode(payload, payload.index("[", i))
            if isinstance(arr, list) and arr:
                return arr
        except ValueError:
            pass
    return _fallback_scan(payload)


_GA_TIER_RE = re.compile(r"general\s*(access|admission)|early\s*entry", re.I)


def _pick_ga_tier(tiers, ga_price):
    """The 'general access'-family tier whose count backs the site's
    "only N left" banner. Prefer a GA-named tier at the headline price,
    then any GA-named tier, then the tier priced at the headline. Tiers
    named VIP never qualify."""
    non_vip = [t for t in tiers if "vip" not in (t["name"] or "").lower()]
    ga_named = [t for t in non_vip if _GA_TIER_RE.search(t["name"] or "")]
    for pool in (ga_named, non_vip):
        for t in pool:
            if ga_price is not None and t.get("price") == ga_price and t.get("available") is not None:
                return t
    for t in ga_named:
        if t.get("available") is not None:
            return t
    return None


def _normalize(e):
    prices = e.get("prices") or {}
    slug = e.get("slug") or ""
    iswaitlist = bool(e.get("iswaitlist"))
    hide_ga = bool(e.get("hide_general_ticket"))
    tiers = []
    for t in prices.get("tickets") or []:
        cp = t.get("current_price") or {}
        tiers.append({
            "name": (t.get("name") or cp.get("name") or "").strip(),
            "price": cp.get("price"),
            "quantity": cp.get("quantity"),
            "used": cp.get("used"),
            "available": cp.get("available"),
        })
    ga_price = prices.get("general_access")
    ga_tier = _pick_ga_tier(tiers, ga_price)
    return {
        "event_id": e.get("event_id"),
        "name": (e.get("name") or "").strip(),
        "slug": slug,
        "date_text": (e.get("date") or "").strip(),
        "start_date": e.get("start_date"),
        "iswaitlist": iswaitlist,
        "on_sale": not iswaitlist and not hide_ga,
        "ga_price": ga_price,
        "ga_sold_out": bool(prices.get("general_access_sold_out")),
        "vip_price": prices.get("vip_experiences"),
        "buy_url": e.get("iframe") or "",
        "page_url": event_page_url(slug),
        "tiers": tiers,
        # The release whose count backs the site's "only N left" banner.
        "ga_release": ga_tier["name"] if ga_tier else None,
        "ga_available": ga_tier.get("available") if ga_tier else None,
        "ga_quantity": ga_tier.get("quantity") if ga_tier else None,
        # Whole-event rollup across current tiers (each release resets these,
        # so treat as "currently purchasable", not lifetime totals).
        "total_available": _sum_or_none(tiers, "available"),
        "total_quantity": _sum_or_none(tiers, "quantity"),
        "total_used": _sum_or_none(tiers, "used"),
    }


def _sum_or_none(tiers, key):
    vals = [t[key] for t in tiers if t.get(key) is not None]
    return sum(vals) if vals else None


def fetch_events():
    """Current pacha-nyc.com event list, normalized. Raises PachaEventsError
    on network trouble or when a 200 page parses to zero events (a site
    redesign must read as a fetch failure, never as 'all events removed')."""
    html = fetch_html()
    raw = parse_events(html)
    events = [_normalize(e) for e in raw if e.get("event_id")]
    if not events:
        raise PachaEventsError("page parsed to 0 events — layout change?")
    return events


def main(argv):
    as_json = "--json" in argv
    events = fetch_events()
    if as_json:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    print(f"{len(events)} events on {EVENTS_URL}\n")
    for ev in events:
        ga = f"${ev['ga_price']}" if ev["ga_price"] is not None else "—"
        flags = []
        if ev["iswaitlist"]:
            flags.append("WAITLIST")
        if ev["ga_sold_out"]:
            flags.append("GA SOLD OUT")
        avail = ", ".join(f"{t['name']}: {t['available']} left"
                          for t in ev["tiers"] if t.get("available") is not None)
        print(f"  {ev['name']:<55.55} {ev['date_text']:<38.38} GA {ga:<6}"
              f" {' '.join(flags)}")
        if avail:
            print(f"      {avail}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
