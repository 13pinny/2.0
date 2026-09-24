"""Shared plumbing for the US EDM event trackers (posh / leap / eventim).

The three fetchers in posh_events.py, leap_events.py and eventim_events.py
each watch ONE event page and return the same normalized dict, so
app.py:run_edm_events can diff them all with one code path (the Pacha
monitor's shape, generalized past a single venue).

Normalized event dict — every fetcher returns exactly these keys:

    source          "posh" | "leap" | "eventim"
    event_key       the source's own id (posh slug, leap slug, eventim id)
    event_id        "<source>:<event_key>" — unique ACROSS sources, so one
                    edm_seen_events table can hold all three
    name, venue, date_text, start_date (ISO or None)
    page_url        the human buy page (what a Discord ping links to)
    currency        always "USD" here
    tiers           [ {name, group, price, face_price, available, quantity,
                       used, sold_out, closed, max_qty} ]  cheapest first
                    price = ALL-IN (what the buyer actually pays), which is
                    what every one of these sites headlines; face_price is
                    the pre-fee number when the site splits it out.
    on_sale         at least one tier is buyable right now
    sold_out        tiers exist and NONE is buyable
    min_price       cheapest buyable all-in price (None when sold out)
    lead_tier/_price/_available
                    the cheapest BUYABLE tier — the "current release", the
                    analogue of pacha's GA release. Its price is the one
                    that climbs when a wave sells through, and its count is
                    what the low-stock alert watches.
    total_available / total_quantity / total_sold
                    rollups; None when the site publishes no counts (leap
                    and eventim publish none — see their docstrings), which
                    must stay None rather than 0 so "no data" never reads
                    as "sold out".

Counts are the big asymmetry between the three: only Posh exposes real
remaining inventory. Anything downstream that consumes `available` has to
treat None as unknown.
"""
import gzip
import html
import json
import re
import urllib.error
import urllib.request


REQUEST_TIMEOUT = 25

# The full Chrome header set, sec-ch-ua and Sec-Fetch-* included. This is
# NOT cosmetic: wl.eventim.us sits behind Cloudflare and answers 403 to the
# short "UA + Accept" header set the other pure-HTTP modules in this repo
# use — adding the client hints and fetch metadata is what makes it pass
# (verified 2026-08-16; an earlier probe that only set UA/Accept saved a
# 670KB challenge page and read it as the event page). posh and leap do not
# need them, but sending one honest header set everywhere keeps the three
# fetchers identical and costs nothing.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
               "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Not;A=Brand";v="99", "Chromium";v="139", "Google Chrome";v="139"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}


class EdmEventsError(RuntimeError):
    """Any fetch/parse failure. Callers treat it as 'no data this tick' and
    must never fall through to an empty-but-successful result — a blocked
    or redesigned page has to read as an error, not as 'sold out'."""


def fetch_text(url, headers=None, timeout=REQUEST_TIMEOUT):
    """GET → decoded text. Raises EdmEventsError on any network/HTTP problem."""
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise EdmEventsError(f"{url} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise EdmEventsError(f"{url} unreachable: {e.reason}") from e
    except OSError as e:  # socket timeouts, connection resets
        raise EdmEventsError(f"{url} failed: {e}") from e
    return raw.decode("utf-8", errors="replace")


def fetch_json(url, headers=None, timeout=REQUEST_TIMEOUT):
    txt = fetch_text(url, headers=headers, timeout=timeout)
    try:
        return json.loads(txt)
    except ValueError as e:
        raise EdmEventsError(f"{url} returned non-JSON ({txt[:120]!r})") from e


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(fragment):
    """Visible text of an HTML fragment, entities decoded, runs of
    whitespace collapsed. The eventim parser leans on this because its
    ticket rows are nested divs with no stable text node."""
    if not fragment:
        return ""
    txt = _TAG_RE.sub(" ", fragment)
    return _WS_RE.sub(" ", html.unescape(txt)).strip()


_MONEY_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_money(text):
    """First number in `text` as a float ('$1,234.50' → 1234.5). None when
    there is no number — callers must not coerce that to 0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _MONEY_RE.search(str(text).replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def tier_is_buyable(tier):
    """Can a buyer put this tier in a cart right now? A tier with no count
    published (available is None) is buyable unless the page says otherwise
    — that is the normal state for leap and eventim."""
    if tier.get("sold_out") or tier.get("closed"):
        return False
    return tier.get("available") is None or tier["available"] > 0


def _sum_or_none(tiers, key):
    """Sum of a count across tiers, or None when no tier publishes it.
    None (unknown) must never collapse to 0 (sold out)."""
    vals = [t[key] for t in tiers if t.get(key) is not None]
    return sum(vals) if vals else None


def rollup(ev):
    """Fill the derived fields on a normalized dict whose `tiers` list is
    already populated. Mutates and returns `ev`.

    lead_* is the cheapest BUYABLE tier — the release currently selling.
    Sorting is by all-in price with unpriced tiers last, so a free/hidden
    row can't win the lead slot away from a real one."""
    tiers = ev.get("tiers") or []
    tiers.sort(key=lambda t: (t.get("price") is None, t.get("price") or 0))
    buyable = [t for t in tiers if tier_is_buyable(t)]

    ev["on_sale"] = bool(buyable)
    # Only claim SOLD OUT when we actually saw tiers; an event whose tier
    # list failed to parse must not be reported as sold out.
    ev["sold_out"] = bool(tiers) and not buyable

    priced = [t for t in buyable if t.get("price") is not None]
    lead = priced[0] if priced else (buyable[0] if buyable else None)
    ev["min_price"] = lead.get("price") if lead else None
    ev["lead_tier"] = lead.get("name") if lead else None
    ev["lead_price"] = lead.get("price") if lead else None
    ev["lead_available"] = lead.get("available") if lead else None

    ev["total_available"] = _sum_or_none(buyable, "available")
    ev["total_quantity"] = _sum_or_none(tiers, "quantity")
    if ev.get("total_sold") is None:
        ev["total_sold"] = _sum_or_none(tiers, "used")
    ev.setdefault("currency", "USD")
    ev["event_id"] = f"{ev['source']}:{ev['event_key']}"
    return ev


def make_tier(name, price, *, group=None, face_price=None, available=None,
              quantity=None, used=None, sold_out=False, closed=False,
              max_qty=None):
    """One ticket tier in the normalized shape (keeps the three fetchers
    from drifting on key names)."""
    return {
        "name": (name or "").strip() or "Tickets",
        "group": (group or "").strip() or None,
        "price": price,
        "face_price": face_price,
        "available": available,
        "quantity": quantity,
        "used": used,
        "sold_out": bool(sold_out),
        "closed": bool(closed),
        "max_qty": max_qty,
    }


def print_event(ev):
    """Shared CLI probe output for the three modules' __main__ blocks."""
    flags = []
    if ev.get("sold_out"):
        flags.append("SOLD OUT")
    elif not ev.get("on_sale"):
        flags.append("NOT ON SALE")
    print(f"{ev['name']}  [{ev['source']}]")
    print(f"  {ev.get('venue') or '—'} · {ev.get('date_text') or ev.get('start_date') or '—'}"
          f"  {' '.join(flags)}")
    print(f"  {ev['page_url']}")
    lead = ev.get("lead_tier")
    if lead:
        left = ev.get("lead_available")
        print(f"  current release: {lead} @ ${ev['lead_price']:,.2f}"
              + (f" — {left} left" if left is not None else " — count not published"))
    for t in ev.get("tiers") or []:
        state = "SOLD OUT" if t["sold_out"] else ("CLOSED" if t["closed"] else "on sale")
        price = f"${t['price']:,.2f}" if t.get("price") is not None else "—"
        face = f" (face ${t['face_price']:,.2f})" if t.get("face_price") is not None else ""
        left = f"  {t['available']} left" if t.get("available") is not None else ""
        grp = f"[{t['group']}] " if t.get("group") else ""
        print(f"    {grp}{t['name']:<44.44} {price:>10}{face:<18} {state:<9}{left}")
    tot = ev.get("total_available")
    if tot is not None:
        print(f"  total available: {tot}")
    if ev.get("total_sold") is not None:
        print(f"  total sold: {ev['total_sold']}")
