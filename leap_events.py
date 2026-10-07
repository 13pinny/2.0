"""events.leapevents.com single-event tracker — pure-HTTP tier/price fetcher.

Leap Events (LED Presents' San Diego festivals — the tracked event is
TEMPER FESTIVAL at Petco Park) runs on a Showclix white-label. Tickets sell
in numbered TIERS ("GA Floor Tier 1/2/3", "GA Bowl Tier 1"), each priced
above the last, so — like Pacha's releases and Posh's waves — the headline
price steps up as tiers sell through.

How the fetch works: one anonymous GET of the event page. Plain urllib with
browser headers gets HTTP 200 (Cloudflare fronts the site but does not
challenge this request). Two blobs in the server-rendered HTML carry
everything:

- `<script type="application/ld+json">` — schema.org Event with name,
  startDate/endDate, location, and an `offers` ARRAY: one Offer per tier
  with `name`, `price` (all-in) and `availability`
  ("http://schema.org/InStock" | ".../SoldOut"). This is the tier feed.
- `var EVENT = {...}` — Showclix's own JS blob: event_id, venue_id,
  seller_id, currency. Used for the numeric event id and currency.

NO INVENTORY COUNTS. Showclix's admin API (api.showclix.com/Event/<id>)
answers 401 to anonymous callers, and nothing on the public page publishes
a remaining count, so every tier's `available` stays None and the tracker
is status + price only for this source — the same deal zappa_events.py has
with Eventim-IL. Downstream, None must keep meaning "unknown", never 0:
the low-stock alert simply never fires for leap events, and a sold-out
flip is detected from `availability`, not from a count hitting zero.

TRAPS
- `offers` is an ARRAY here (one per tier). Other Showclix skins emit a
  single AggregateOffer object instead; _offers() handles both so a
  quieter event doesn't parse to zero tiers.
- The ld+json `price` is the all-in price, and Showclix does NOT publish
  the face/fee split anywhere on the page — face_price stays None (unlike
  posh and eventim, which both break the fee out).
- A canonical <link> pointing at a DIFFERENT slug means the event was
  renamed/redirected; we keep tracking the slug we were given (the
  redirect resolves server-side) but the parsed name is authoritative.

CLI probe:  python leap_events.py [<url-or-slug>] [--json]
"""
import json
import re
import sys

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "leap"
SITE_BASE = "https://events.leapevents.com"

_URL_RE = re.compile(r"leapevents\.com/event/([A-Za-z0-9._~-]+)", re.I)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_LDJSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)
_EVENT_VAR_RE = re.compile(r"var\s+EVENT\s*=\s*(\{.*?\})\s*;", re.S)


def parse_url(url):
    """leapevents event URL (or bare slug) → slug. Raises ValueError."""
    s = (url or "").strip()
    m = _URL_RE.search(s)
    if m:
        return m.group(1)
    if "/" not in s and "." not in s and _SLUG_RE.match(s):
        return s
    raise ValueError(f"not an events.leapevents.com event URL: {url!r}")


def event_page_url(slug):
    return f"{SITE_BASE}/event/{slug}"


def _ld_events(html):
    """Every schema.org Event object in the page's ld+json blocks."""
    out = []
    for m in _LDJSON_RE.finditer(html):
        try:
            obj = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for cand in (obj if isinstance(obj, list) else [obj]):
            if isinstance(cand, dict) and "Event" in str(cand.get("@type") or ""):
                out.append(cand)
    return out


def _event_var(html):
    m = _EVENT_VAR_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def _offers(ld):
    """Normalize `offers` to a list — it is an array of per-tier Offers on
    this skin, but a lone AggregateOffer on others."""
    offers = ld.get("offers")
    if isinstance(offers, list):
        return [o for o in offers if isinstance(o, dict)]
    if isinstance(offers, dict):
        return [offers]
    return []


def _is_sold_out(offer):
    return "soldout" in str(offer.get("availability") or "").lower().replace(" ", "")


def _tiers(ld):
    out = []
    for o in _offers(ld):
        name = o.get("name") or o.get("description") or "Tickets"
        agg = "aggregate" in str(o.get("@type") or "").lower()
        if agg:
            # Fallback skin: no per-tier data, just a price band. Emit one
            # tier at lowPrice so the event still tracks price + status.
            price = edm_common.parse_money(o.get("lowPrice") or o.get("price"))
            out.append(make_tier("Tickets", price, sold_out=_is_sold_out(o)))
            continue
        out.append(make_tier(
            name,
            edm_common.parse_money(o.get("price")),
            sold_out=_is_sold_out(o),
            # Showclix publishes no counts to anonymous callers — available
            # and quantity stay None (unknown), never 0.
        ))
    return out


def _date_text(ld):
    """'Wed, Dec 30, 2026 · 3:00 PM' from the ld+json local startDate."""
    from datetime import datetime
    raw = ld.get("startDate")
    if not raw:
        return None
    txt = str(raw)
    # '2026-12-30T15:00:00-0800' — fromisoformat wants '-08:00' before 3.11.
    m = re.match(r"^(.*T[\d:]+)([+-]\d{2})(\d{2})$", txt)
    if m:
        txt = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    return dt.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ")


def fetch_event(slug):
    """One leapevents event, normalized (see edm_common's module docstring)."""
    slug = parse_url(slug)
    url = event_page_url(slug)
    html = edm_common.fetch_text(url)
    lds = _ld_events(html)
    if not lds:
        raise EdmEventsError(f"leap: no ld+json Event on {url} "
                             "— layout change or event removed?")
    ld = lds[0]
    evar = _event_var(html)

    tiers = _tiers(ld)
    if not tiers:
        raise EdmEventsError(f"leap: {slug} parsed to 0 ticket tiers "
                             "— layout change or event unpublished?")
    venue = None
    loc = ld.get("location")
    if isinstance(loc, dict):
        venue = (loc.get("name") or "").strip() or None

    ev = {
        "source": SOURCE_NAME,
        "event_key": slug,
        "name": (ld.get("name") or evar.get("event") or slug).strip(),
        "venue": venue,
        "date_text": _date_text(ld),
        "start_date": ld.get("startDate"),
        "page_url": url,
        "currency": (evar.get("currency") or "USD").strip() or "USD",
        "tiers": tiers,
        "total_sold": None,
        "showclix_event_id": evar.get("event_id"),
        "seller_id": evar.get("seller_id"),
    }
    return rollup(ev)


DEFAULT_TARGET = "led-presents-temper-festival"


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    ev = fetch_event(args[0] if args else DEFAULT_TARGET)
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    print("  (Showclix publishes no remaining counts — status + price only)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
