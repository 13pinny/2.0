"""UrVenue table-inventory tracker — pure-HTTP per-table-type stock.

LIV Miami (and other Fontainebleau / UrVenue clubs) book VIP tables through
UrVenue, embedded in the club's own WordPress site by the
`wp-urvenue-webservices` plugin. Its `uwsinventory.js` makes one anonymous,
read-only call on page load:

    GET https://<club host>/wp-admin/admin-ajax.php?action=uvpx
        &uvaction=uwspx_inventoryinit&eventcode=<EVE…>&cartcode=
        &homeeventcode=&homename=&returntempl=1

    {"items": {"<itemcode>": {itemname, listprice (USD min spend), capacity
               (guests), totalstock, locstock, locids, state, inactive,
               itemtype "table", paynow, ...}},
     "eventdata": {name, date, dstarttime, venuename, ticketsurl, flyers, ...},
     "html": "<rendered booking widget>", ...}

One tier per item: price = listprice (a minimum spend, not a ticket price),
available = totalstock. Event codes read EVE<venue id><3 digits><YYYYMMDD>
(LIV Miami: EVE1121562 000 20261203); the page slug after it is cosmetic
and repeats weekly ("liv-on-sunday").

Notes / traps
- `stock` is 1 on every row and equals `maxqty` — it is a per-booking cap,
  NOT inventory. `totalstock` is the count we read as "tables left". That
  reading comes from one snapshot (2026-10-19) and is UNCONFIRMED: the
  Owner's Table read totalstock 4 against a single location. `locstock`
  (= number of physical table locations) is kept as `locations` but never
  used as "released", since it can be below totalstock. So quantity/used
  stay None: no sold count, only left (and its history on /liv).
- `eventdata.ticketsurl` is the event's GA ticket link — on LIV it is a
  universe.com listing, which is how /liv pairs a night's tables with its
  Universe GA tiers (universe_events.py).
- livnightclub.com sits behind Cloudflare and 403-challenges the VPS's
  Hetzner address (2026-10-19), while a residential IP passes with a plain
  UA. So requests go through KARTIS_URVENUE_PROXY (http://user:pass@host:
  port); when that is unset the DICE residential proxy (KARTIS_DICE_PROXY)
  is reused, and "off" forces a direct request. A challenge page is HTML,
  so it fails JSON parsing and surfaces as last_error — never as 'no
  tables'. Don't add anything that tries to solve the challenge.

    python urvenue_events.py "<club event URL | host/EVE…>"  [--json]
"""
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "urvenue"

_PROXY_ENV = os.environ.get("KARTIS_URVENUE_PROXY", "").strip()
PROXY = ("" if _PROXY_ENV.lower() in ("off", "0", "none", "direct")
         else _PROXY_ENV or os.environ.get("KARTIS_DICE_PROXY", "").strip())

_EVE_RE = re.compile(r"\b(EVE\d{10,20})\b")
_HOST_RE = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(/[^\s]*)?", re.I)
_EVENT_PATH_RE = re.compile(r"(/[^\s]*?)?/event/(EVE\d{10,20})(?:/([^/?#\s]*))?", re.I)

_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        handlers = []
        if PROXY:
            handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        _opener = urllib.request.build_opener(*handlers)
    return _opener


def parse_url(url):
    """Club event URL (https://<host>/<city>/event/EVE…/<slug>/) or the
    "<host>/EVE…" shorthand → event_key "<host>/EVE…"."""
    s = (url or "").strip()
    m = _HOST_RE.match(s)
    eve = _EVE_RE.search(s)
    if m and eve and "." in m.group(1):
        return f"{m.group(1).lower()}/{eve.group(1)}"
    raise ValueError(f"not an UrVenue club event link: {url!r}")


def split_key(key):
    host, _, eve = parse_url(key).partition("/")
    return host, eve


def inventory_url(host, eve):
    return (f"https://{host}/wp-admin/admin-ajax.php?action=uvpx"
            f"&uvaction=uwspx_inventoryinit&eventcode={eve}&cartcode="
            f"&homeeventcode=&homename=&returntempl=1")


def _fetch_json(url):
    headers = dict(edm_common.BROWSER_HEADERS,
                   Accept="application/json, text/javascript, */*; q=0.01",
                   **{"X-Requested-With": "XMLHttpRequest"})
    req = urllib.request.Request(url, headers=headers)
    try:
        with _get_opener().open(req, timeout=edm_common.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        hint = " (Cloudflare challenge)" if e.headers.get("cf-mitigated") else ""
        raise EdmEventsError(f"urvenue: HTTP {e.code}{hint} from {url.split('?')[0]}") from e
    except urllib.error.URLError as e:
        raise EdmEventsError(f"urvenue: unreachable: {e.reason}") from e
    except OSError as e:
        raise EdmEventsError(f"urvenue: request failed: {e}") from e
    txt = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(txt)
    except ValueError as e:
        raise EdmEventsError(f"urvenue: non-JSON response ({txt[:100]!r})") from e


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_inventory(data):
    """inventory JSON → (eventdata dict, tiers)."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise EdmEventsError(f"urvenue: no items in response ({str(data)[:160]!r})")
    tiers = []
    for code, it in data["items"].items():
        if not isinstance(it, dict):
            continue
        left = _int(it.get("totalstock"))
        off = str(it.get("state") or "on").lower() != "on" or str(it.get("inactive")) == "1"
        price = it.get("listprice")
        price = float(price) if isinstance(price, (int, float)) else edm_common.parse_money(price)
        t = make_tier(it.get("itemname") or code, price,
                      group=(it.get("itemtype") or "").title() or None,
                      available=left, sold_out=(left == 0), closed=off and left != 0,
                      max_qty=_int(it.get("maxqty")))
        t["section_id"] = code
        t["guests"] = _int(it.get("capacity"))
        t["locations"] = _int(it.get("locstock"))
        t["note"] = (it.get("highlight") or "").strip() or None
        tiers.append(t)
    if not tiers:
        raise EdmEventsError("urvenue: inventory has no items — event not bookable yet?")
    return data.get("eventdata") or {}, tiers


def _flyer(ed):
    fl = ed.get("flyers")
    if isinstance(fl, str):
        try:
            fl = json.loads(fl.replace("'", '"'))
        except ValueError:
            m = re.search(r"https?://[^'\"\s]+\.(?:jpe?g|png|webp)", fl)
            return m.group(0) if m else None
    if isinstance(fl, dict):
        for k in ("eventpage", *fl.keys()):
            v = fl.get(k)
            if isinstance(v, dict) and (v.get("url") or v.get("full")):
                return v.get("url") or v.get("full")
    return None


def fetch_event(event_key):
    """One UrVenue event's table inventory, normalized."""
    host, eve = split_key(event_key)
    ed, tiers = parse_inventory(_fetch_json(inventory_url(host, eve)))
    start_date = ed.get("date") or None
    date_text = None
    if start_date:
        try:
            date_text = datetime.strptime(start_date, "%Y-%m-%d").strftime("%a %b %-d, %Y")
        except ValueError:
            date_text = start_date
        if ed.get("dstarttime"):
            date_text += f" · {ed['dstarttime']}"
    ev = {
        "source": SOURCE_NAME,
        "event_key": f"{host}/{eve}",
        "name": (ed.get("name") or "").strip() or eve,
        "venue": ed.get("venuename"),
        "date_text": date_text,
        "start_date": start_date,
        "page_url": ed.get("event-page-url") or ed.get("event-url") or f"https://{host}/",
        "image": _flyer(ed),
        "tickets_url": ed.get("ticketsurl") or None,
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
    }
    return rollup(ev)


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
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
    print(f"  GA tickets: {ev.get('tickets_url') or '—'}"
          f"   (proxy {'on' if PROXY else 'off'})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
