"""zappa-club.co.il event fetcher — the Zappa club chain (TLV Midtown,
Herzliya, Haifa, Jerusalem, Live Park, amphitheaters), which is an
Eventim Israel white-label storefront.

Access facts (recon 2026-07-07 — hard-won, don't re-derive):

  - zappa.co.il is a PARKED domain (ad squatter). The real site is
    https://www.zappa-club.co.il.
  - Akamai Bot Manager guards everything: direct HTTP dies at the TLS
    fingerprint (urllib/curl hang forever), and **headless** Chromium gets
    ERR_HTTP2_PROTOCOL_ERROR. A HEADFUL Chromium window passes; we park it
    off-screen. Once any zappa page is open, in-page fetch() works against
    both zappa's own /api/ endpoints and public-api.eventim.com (which
    403s when called directly).
  - Catalog: the Eventim websearch API the site itself uses —
      GET public-api.eventim.com/websearch/search/api/exploration/v2
          /productGroups?webId=web__eventim-co-il&language=he
          &retail_partner=ZPE&categories=<cat>&categories=null
          &sort=DateAsc&tags=DISABLE_FBS&page=N
    20 productGroups per page + totalResults. The odd `categories=null` +
    `tags=DISABLE_FBS` duo is REQUIRED — omit either and the API answers
    400 "Insufficient parameter given". A product = one dated show at one
    venue (productId == the evid in event URLs).
  - Status + price: batched per-event info from zappa's own domain —
      GET www.zappa-club.co.il/api/event/v2/additionalInfo?language=he
          &affiliate=ZPE&withCrossedOutPrices=false&withEventMarkup=false
          &evid=A&evid=B...
    → {"informationMap": {evid: {eventStatus, fromPrice, message}}}.
    eventStatus vocabulary seen so far: AVAILABLE, CANCELLED (Eventim also
    uses SOLD_OUT / LAST_TICKETS tiers — mapped defensively below).
  - NO ticket counts exist anywhere (Eventim doesn't publish them), so
    this source is catalog + status + from-price only — the market page
    shows no sold-per-window velocity for zappa.

Returns rows shaped like kupat_events/tm_events fetch_events() so the
same site_seen_events diff-and-ping machinery works, plus extra fields
(status, min_price, venue_name, city) for the market sweep.

CLI probe:  python zappa_events.py [--json] [--category "הופעות חיות"]
"""
import json
import math
import os
import re
import sys
import time
from datetime import datetime

SOURCE_NAME = "zappa"
SITE_BASE = "https://www.zappa-club.co.il"
SEARCH_BASE = ("https://public-api.eventim.com/websearch/search/api/"
               "exploration/v2/productGroups")

# Which Eventim categories to sweep. "הופעות חיות" (live shows) is the
# resale-relevant one; pipe-separate to add more (e.g. "|סטנדאפ").
CATEGORIES = [c for c in (os.getenv("KARTIS_ZAPPA_CATEGORIES") or "הופעות חיות").split("|") if c.strip()]

PAGE_SIZE = 20          # fixed by the API
MAX_PAGES = 30          # runaway guard
INFO_BATCH = 20         # evids per additionalInfo call

_JS_FETCH = """
async (url) => {
  const r = await fetch(url, {headers: {"Accept": "application/json"}});
  return {status: r.status, body: await r.text()};
}
"""


class ZappaError(RuntimeError):
    pass


class Browser:
    """Headful off-screen Chromium with an anchor page on zappa-club.co.il,
    exposing in-page JSON GETs. Context-manager scoped."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            raise ZappaError(
                "zappa fetcher needs patchright + Chromium. Run "
                "`.venv\\Scripts\\python -m patchright install chromium`."
            ) from e
        self._pw = sync_playwright().start()
        # Headless gets ERR_HTTP2_PROTOCOL_ERROR from the Akamai edge;
        # headful passes. Park the window far off-screen.
        self._browser = self._pw.chromium.launch(
            headless=False, args=["--window-position=-2400,-2400"])
        self._page = self._browser.new_context(locale="he-IL").new_page()
        try:
            self._page.goto(SITE_BASE + "/", wait_until="domcontentloaded",
                            timeout=45000)
        except Exception as e:
            self.__exit__(None, None, None)
            raise ZappaError(f"failed to open zappa anchor page: {e}") from e
        self._page.wait_for_timeout(2000)
        return self

    def __exit__(self, exc_type, exc, tb):
        for close in (
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                close()
            except Exception:
                pass
        self._pw = self._browser = self._page = None
        return False

    def get_json(self, url):
        res = self._page.evaluate(_JS_FETCH, url)
        if res.get("status") != 200:
            raise ZappaError(f"HTTP {res.get('status')} from {url[:120]}")
        try:
            return json.loads(res.get("body") or "null")
        except json.JSONDecodeError as e:
            raise ZappaError(f"non-JSON from {url[:120]}") from e


def _quote(s):
    from urllib.parse import quote
    return quote(s, safe="")


def _search_url(category, page):
    return (f"{SEARCH_BASE}?webId=web__eventim-co-il&language=he"
            f"&retail_partner=ZPE&categories={_quote(category)}"
            f"&categories=null&sort=DateAsc&tags=DISABLE_FBS&page={page}")


def _info_url(evids):
    q = "&".join(f"evid={e}" for e in evids)
    return (f"{SITE_BASE}/api/event/v2/additionalInfo?language=he"
            f"&affiliate=ZPE&withCrossedOutPrices=false"
            f"&withEventMarkup=false&{q}")


def _status(event_status, product_status):
    s = (event_status or product_status or "").upper()
    if "CANCEL" in s:
        return "closed"
    if "SOLD" in s:
        return "soldout"
    if "LAST" in s or "FEW" in s:
        return "lasttickets"
    if "AVAILABLE" in s:
        return "available"
    return "unknown"


def _parse_price(s):
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"[\d.,]+", str(s or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _event_url(product):
    """Prefer the zappa-club.co.il URL over the eventim.co.il product link
    (same path structure, minus the affiliate param)."""
    path = ((product.get("url") or {}).get("path") or "").split("?")[0]
    if path:
        return SITE_BASE + path
    link = (product.get("link") or "").split("?")[0]
    return link.replace("https://www.eventim.co.il", SITE_BASE) if link else SITE_BASE


def _date_ms(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return None


def _date_text(iso):
    return (iso or "").replace("T", " ")[:16]


def fetch_events(categories=None, with_info=True):
    """One full catalog sweep. Returns a list of normalized rows, one per
    product (= dated show):
        {source, event_key, name, venue, date_text, first_date_ms,
         on_sale, url, status, min_price}
    Raises ZappaError if the catalog yields zero products (a layout/param
    change must read as a failure, never as 'all events removed')."""
    products = {}
    with Browser() as br:
        for cat in (categories or CATEGORIES):
            page1 = br.get_json(_search_url(cat, 1))
            total = page1.get("totalResults") or 0
            pages = min(MAX_PAGES, max(1, math.ceil(total / PAGE_SIZE)))
            payloads = [page1]
            for p in range(2, pages + 1):
                time.sleep(0.2)
                payloads.append(br.get_json(_search_url(cat, p)))
            for payload in payloads:
                for group in payload.get("productGroups") or []:
                    gname = (group.get("name") or "").strip()
                    for prod in group.get("products") or []:
                        pid = str(prod.get("productId") or "")
                        if not pid or pid in products:
                            continue
                        le = ((prod.get("typeAttributes") or {})
                              .get("liveEntertainment") or {})
                        loc = le.get("location") or {}
                        venue = " · ".join(v for v in (
                            (loc.get("name") or "").strip(),
                            (loc.get("city") or "").strip()) if v)
                        start = le.get("startDate")
                        products[pid] = {
                            "source": SOURCE_NAME,
                            "event_key": pid,
                            "name": (prod.get("name") or gname or "").strip(),
                            "venue": venue,
                            "date_text": _date_text(start),
                            "first_date_ms": _date_ms(start),
                            "on_sale": None,
                            "url": _event_url(prod),
                            "status": _status(None, prod.get("status")),
                            "min_price": None,
                        }
        if not products:
            raise ZappaError("zappa catalog parsed to 0 products — API change?")

        if with_info:
            evids = list(products)
            for i in range(0, len(evids), INFO_BATCH):
                chunk = evids[i:i + INFO_BATCH]
                time.sleep(0.2)
                try:
                    info = (br.get_json(_info_url(chunk)) or {}).get("informationMap") or {}
                except ZappaError:
                    continue  # status stays whatever the catalog said
                for eid in chunk:
                    row = info.get(eid)
                    if not row:
                        continue
                    ev = products[eid]
                    ev["status"] = _status(row.get("eventStatus"), None) or ev["status"]
                    ev["min_price"] = _parse_price(row.get("fromPrice")) or ev["min_price"]

    for ev in products.values():
        ev["on_sale"] = ev["status"] in ("available", "lasttickets")
    return list(products.values())


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    as_json = "--json" in argv
    cats = None
    if "--category" in argv:
        i = argv.index("--category")
        cats = [argv[i + 1]] if i + 1 < len(argv) else None
    events = fetch_events(categories=cats)
    if as_json:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    print(f"{len(events)} zappa products\n")
    for ev in sorted(events, key=lambda e: e.get("first_date_ms") or 0):
        price = f"₪{ev['min_price']:g}" if ev.get("min_price") else ""
        print(f"  {ev['event_key']:<10} {ev['name']:<42.42} {ev['date_text']:<17} "
              f"{ev['venue']:<34.34} {ev['status']:<12} {price}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
