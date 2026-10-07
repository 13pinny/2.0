"""tixr.com single-event tracker — status + price, via a real browser.

Tixr is the ticketing platform behind a lot of US venue-run EDM shows (the
tracked event is ISOxo presents: Hardcore Diva at Aon Grand Ballroom, Navy
Pier — the Chicago date of the same tour whose Austin night is tracked
through eventim_events). Tickets are grouped into SALES (GA, TABLE, …),
each holding one or more priced TIERS.

THIS IS THE ONE EDM SOURCE THAT NEEDS CHROMIUM. Tixr fronts everything
with DataDome: plain urllib gets HTTP 403 on both the event page AND
`/api/events/<id>`, with or without a full browser header set, because the
block keys on the TLS/JA3 fingerprint rather than headers. (Contrast
eventim_events, where the right headers alone are enough.) A freshly
launched HEADLESS Chromium does not get through either — it is handed a
DataDome CAPTCHA (403 on the document, redirect to
geo.captcha-delivery.com). So this module attaches over CDP to the
manually-logged-in Chrome that login.py opens for the Lysted / Viagogo /
CrowdVolt scrapers, loads the event page there, and harvests the page's
OWN `/api/events/<id>` XHR response.

That makes Tixr the only EDM source pinned to the dashboard machine —
everything else in this tracker is pure HTTP and runs happily on the VPS.
Because `edm_events.fetch_tracked` isolates per-event failures, a box with
no CDP Chrome simply records an error on the Tixr rows and keeps polling
the other sources normally; pause the Tixr row there
(`edm_events.py --pause tixr:<id>`) to stop the noise.

NO REMAINING COUNTS. The event API's tier objects carry only
`id / name / info / price / startDate / endDate / active / priceLevels /
priceVariants / priceListEntry / minPrice / maxPrice` — there is no
quantity, remaining, or sold field anywhere in the ~37KB payload, and the
captured XHR log for a Tixr event page shows `/api/events/<id>` is the
only data endpoint (everything else is analytics, New Relic and DataDome
itself). The lone count-shaped field is `venue.capacity`, which is the
room's size, not availability. So this source is status + price only,
like leap_events — `available` stays None everywhere and the low-stock
alert never fires for it. Do not infer a count from capacity.

What we DO get, and what the tracker keys on: per-sale `state`
(OPEN / SOLD_OUT / …), per-tier `price` and `active`, and the sale window
— enough for price-move, on-sale, sold-out and restock pings.

TRAPS
- `https://tixr.com/e/<id>` is a canonical short URL that resolves from
  the numeric id alone, which is why event_key can stay just the id; the
  API response's own `url` field supplies the pretty link for Discord.
- `startDate` is epoch MILLISECONDS and the event's local clock comes from
  a separate `timeZone: {name, offset}` object (offset in ms), not from an
  ISO string with a zone.
- Prices are floats and `feeInclusive` varies per event, so a Tixr price
  is not reliably comparable with the all-in prices posh/leap/eventim
  report — treated the same way as shotgun's face prices.

CLI probe:  python tixr_events.py [<url-or-id>] [--json]
"""
import json
import os
import re
import sys
import time

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "tixr"
SITE_BASE = "https://www.tixr.com"
SHORT_URL = "https://tixr.com/e/{}"
PAGE_TIMEOUT_MS = 45000
# How long to wait for the page's own /api/events/<id> XHR after load.
CAPTURE_WAIT_SECONDS = 20

# .../events/<slug>-<id>  or  tixr.com/e/<id>  or a bare id
_URL_ID_RE = re.compile(r"tixr\.com/(?:.*?[/-])?(\d{4,})(?:[/?#]|$)", re.I)
_EVENTS_RE = re.compile(r"tixr\.com/groups/[^/]+/events/[A-Za-z0-9._~-]*?(\d{4,})\b", re.I)
_ID_RE = re.compile(r"^\d{4,}$")


def parse_url(url):
    """Tixr event URL (or bare numeric id) → event id. Raises ValueError."""
    s = (url or "").strip()
    if _ID_RE.match(s):
        return s
    m = _EVENTS_RE.search(s) or _URL_ID_RE.search(s)
    if m:
        return m.group(1)
    raise ValueError(f"not a tixr.com event URL: {url!r}")


def event_page_url(event_id):
    return SHORT_URL.format(event_id)


def _capture_event_json(event_id):
    """Load the event page in the CDP Chrome and return the payload of its
    own /api/events/<id> call.

    Attaches to the manually-launched Chrome on KARTIS_CDP_URL (the same
    instance login.py opens for the Lysted/Viagogo/CrowdVolt scrapers).
    That is not a stylistic choice: a freshly launched HEADLESS Chromium is
    served a DataDome CAPTCHA here — verified 2026-08-16, the document 403s
    and redirects to geo.captcha-delivery.com — while the real browser,
    with its own profile and history, loads the page normally. Same
    "a human's browser, inherited" design the rest of this repo uses.

    Falls back to launching a patchright Chromium if no CDP Chrome is up,
    which usually hits the CAPTCHA and raises — deliberately, so the tick
    records an honest error instead of silently reporting no tickets."""
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        raise EdmEventsError(
            "tixr needs patchright + Chromium (DataDome blocks plain HTTP). "
            "Run `.venv\\Scripts\\python -m patchright install chromium`."
        ) from e

    want = f"/api/events/{event_id}"
    captured = {}
    pw = browser = page = None
    owns_browser = False
    try:
        pw = sync_playwright().start()
        cdp_url = os.getenv("KARTIS_CDP_URL") or "http://localhost:9222"
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=8000)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        except Exception:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(locale="en-US")
            owns_browser = True
        page = ctx.new_page()

        def on_response(resp):
            if "body" in captured:
                return
            try:
                if want in resp.url and resp.request.resource_type in ("xhr", "fetch"):
                    captured["body"] = resp.json()
            except Exception:
                pass  # a non-JSON / already-consumed response is not fatal

        page.on("response", on_response)
        try:
            page.goto(event_page_url(event_id), wait_until="domcontentloaded",
                      timeout=PAGE_TIMEOUT_MS)
        except Exception as e:
            raise EdmEventsError(f"tixr: could not load event {event_id}: {e}") from e

        deadline = time.time() + CAPTURE_WAIT_SECONDS
        while time.time() < deadline and "body" not in captured:
            page.wait_for_timeout(250)

        if "body" not in captured:
            # Last resort: ask for it from inside the page, which now holds
            # the DataDome cookies the bare request lacks.
            try:
                captured["body"] = page.evaluate(
                    """async (u) => {
                        const r = await fetch(u, {headers: {'Accept': 'application/json'}});
                        return r.ok ? await r.json() : null;
                    }""",
                    want,
                )
            except Exception:
                captured["body"] = None
    finally:
        # Close the PAGE always, but only close the BROWSER when we launched
        # it — closing a connect_over_cdp handle would take down the user's
        # logged-in Chrome and break every other scraper in the project.
        for close in (
            lambda: page.close(),
            lambda: (browser.close() if owns_browser and browser else None),
            lambda: pw and pw.stop(),
        ):
            try:
                close()
            except Exception:
                pass

    body = captured.get("body")
    if not isinstance(body, dict) or not body.get("id"):
        raise EdmEventsError(
            f"tixr: no /api/events/{event_id} payload captured — DataDome "
            "challenge, or the event was removed")
    return body


def _tiers(data):
    out = []
    for sale in data.get("sales") or []:
        group = (sale.get("category") or "").strip() or None
        state = (sale.get("state") or "").upper()
        status = (sale.get("status") or "").upper()
        # Tixr has no counts; buyability is entirely state-driven. Anything
        # that isn't an explicitly OPEN, PUBLISHED sale is not purchasable.
        sold_out = "SOLD" in state
        closed = not sold_out and (state != "OPEN" or status not in ("PUBLISHED", ""))
        for t in sale.get("tiers") or []:
            price = t.get("price")
            if price is None:
                price = t.get("minPrice")
            out.append(make_tier(
                t.get("name") or sale.get("name") or group,
                price,
                group=group,
                # available stays None — see the module docstring.
                sold_out=sold_out,
                closed=closed or not t.get("active", True),
            ))
    return out


def _date_text(data):
    """Event start in its own local clock, from epoch-ms + the tz offset."""
    from datetime import datetime, timedelta, timezone as _tz
    ms = data.get("startDate")
    if not ms:
        return None
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000, _tz.utc)
    except (TypeError, ValueError, OverflowError):
        return None
    tzinfo = data.get("timeZone") or {}
    label = ""
    off = tzinfo.get("offset")
    if isinstance(off, (int, float)):
        dt = dt + timedelta(milliseconds=off)
        label = f" {tzinfo.get('name')}" if tzinfo.get("name") else ""
    else:
        label = " UTC"
    return dt.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ") + label


def _start_iso(data):
    from datetime import datetime, timezone as _tz
    ms = data.get("startDate")
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, _tz.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_event(event_id):
    """One tixr.com event, normalized (see edm_common's docstring)."""
    event_id = parse_url(event_id)
    data = _capture_event_json(event_id)

    tiers = _tiers(data)
    if not tiers:
        raise EdmEventsError(f"tixr: event {event_id} parsed to 0 ticket tiers "
                             "— API shape change or nothing published?")
    venue = ((data.get("venue") or {}).get("name") or "").strip() or None

    ev = {
        "source": SOURCE_NAME,
        "event_key": event_id,
        "name": (data.get("name") or "").strip() or f"Tixr event {event_id}",
        "venue": venue,
        "date_text": _date_text(data),
        "start_date": _start_iso(data),
        "page_url": data.get("url") or event_page_url(event_id),
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
        # Tixr's feeInclusive flag varies per event, so its prices are not
        # reliably all-in — flagged like shotgun's face prices.
        "price_basis": "face" if not data.get("feeInclusive") else "all-in",
    }
    return rollup(ev)


DEFAULT_TARGET = "189343"


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    ev = fetch_event(args[0] if args else DEFAULT_TARGET)
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    print("  (Tixr publishes no remaining counts — status + price only)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
