"""posh.vip single-event tracker — pure-HTTP tier/price/inventory fetcher.

Posh (posh.vip) is the ticketing platform behind a lot of US club EDM
promoters (the tracked event is RedEye Boston's BOYS NOIZE night). Tickets
sell in named WAVES — "Early Bird", "Wave 1", "Wave 2" — and, exactly like
Pacha's releases, each wave is priced above the last, so the headline price
climbs as the event sells through. This module fetches one event's current
waves; the diff-and-ping loop lives in app.py (`run_edm_events`).

How the fetch works — two anonymous GETs, no login, no browser:

1. The event page https://posh.vip/e/<slug> is Next.js and server-embeds
   its metadata as React flight payload (`self.__next_f.push([1,"..."])`
   chunks, same decoding trick as pacha_events._flight_payload). The
   `"eventResponse"` object inside carries eventData.eventId (24-hex) plus
   name / venueName / start / timezone. That id is CACHED under tm_cache/
   so the steady-state tick is one request, not two — see _meta().

2. The waves come from the site's own tRPC endpoint, anonymously:
       https://posh.vip/next-bff/events.getEventTickets?input={"eventId":...}
   Response: result.data.ticketGroups[].tickets[] with per-wave `name`,
   `price` (face), `totalPrice` (all-in, the number the page headlines),
   `quantityAvailable` (REAL remaining inventory — Posh is the only one of
   the three EDM sources that publishes it) and `closed`; plus
   result.data.totalTicketsSold, a live sold counter for the whole event.

TRAPS
- The tRPC mount is `/next-bff`, NOT `/api` or `/api/trpc`. posh.vip/api is
  a different gateway that 404s every procedure with a JSON-RPC envelope,
  which is easy to misread as "wrong procedure name".
- The waves are NOT in the page HTML — the page ships an empty shell and
  the client calls getEventTickets on mount. Scraping the HTML for prices
  finds only the ld+json `offers.price` (the cheapest all-in price), never
  the per-wave breakdown.
- `quantityAvailable: 0` means that wave sold out; the wave keeps being
  listed afterwards (Early Bird and Wave 1 both sit at 0 today), so the
  tier list is a full release history, not just what's buyable.
- Passing `includeEventPageButtonInfo: true` is what makes the response
  carry eventPageButtonInfo; without it that key is absent. We ask for it
  because its `text` ("Buy tickets from $28.49" / "Sales closed for this
  event") is the site's own summary of buyability.

- PASSWORD-GATED events (presales): the page serves only an
  EventPagePasswordProtectedForm and no eventId. The site unlocks it with
  POST /next-bff/events.unlockEvent {eventUrl: <slug>, password}, which
  answers {"success": true} and sets the cookie `event_<slug>_password=<pw>`;
  the page fetched with that cookie carries eventId as usual, and from then
  on getEventTickets needs no cookie at all. So the password only matters
  when the id is (re)scraped. It rides in the event key, "<slug>@<code>",
  like eventim's affiliate key — paste "<posh url> <code>" (or
  ...?password=<code>) on /edm. Such an event can legitimately list ZERO
  waves before its sale opens (Palm Tree 2027: ticketGroups [] behind
  PALM2027 on 2026-10-21), so 0 tiers is a quiet "not released yet" there
  instead of an error; the first wave then arrives as a new_tier ping.

CLI probe:  python posh_events.py [<url-or-slug>[@code]] [--json]
"""
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "posh"
SITE_BASE = "https://posh.vip"
BFF_BASE = SITE_BASE + "/next-bff"
CACHE_DIR = os.getenv("KARTIS_TM_CACHE_DIR") or "tm_cache"
CACHE_PATH = os.path.join(CACHE_DIR, "posh_events.json")
# The page is only re-read this often; between refreshes a tick is one
# tRPC call. Event NAME/venue/date barely ever change — waves change all
# the time, and those come from the API.
META_TTL_SECONDS = int(os.getenv("KARTIS_POSH_META_TTL_SECONDS") or 6 * 3600)

_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"')
_EVENT_ID_RE = re.compile(r'"eventId":"([a-f0-9]{24})"')

# https://posh.vip/e/<slug>  (also accepts a bare slug)
_URL_RE = re.compile(r"posh\.vip/(?:e|events?)/([A-Za-z0-9._~-]+)", re.I)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


_PW_QUERY_RE = re.compile(r"[?&#](?:password|code|pw)=([^&#\s]+)", re.I)


def parse_url(url):
    """posh.vip event URL (or bare slug) → event key: the slug, or
    "<slug>@<code>" for a password-gated event. The code comes from a
    trailing "@code", a ?password=/?code= param, or a second word after the
    URL ("https://posh.vip/e/x PALM2027"). Raises ValueError."""
    s = (url or "").strip()
    pw = None
    head, _, rest = s.partition(" ")
    if rest.strip() and " " not in rest.strip():
        s, pw = head, rest.strip()
    m = _PW_QUERY_RE.search(s)
    if m:
        pw = pw or urllib.parse.unquote(m.group(1))
    m = _URL_RE.search(s)
    if m:
        slug = m.group(1)
    elif "/" not in s and "." not in s and _SLUG_RE.match(s.split("@")[0]):
        slug = s
    else:
        raise ValueError(f"not a posh.vip event URL: {url!r}")
    if "@" in slug:
        slug, _, at_pw = slug.partition("@")
        pw = pw or at_pw
    if not slug:
        raise ValueError(f"not a posh.vip event URL: {url!r}")
    return f"{slug}@{pw}" if pw else slug


def split_key(key):
    """event key → (slug, password or None)."""
    slug, _, pw = (key or "").partition("@")
    return slug, (pw or None)


def event_page_url(slug):
    return f"{SITE_BASE}/e/{slug}"


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


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(cache):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=1)
    except OSError:
        pass  # a cache we can't write just means we re-read the page


def _unlock(slug, password):
    """Unlock a password-gated event page → the Cookie header value the
    page then needs. Raises EdmEventsError on a wrong code."""
    body = json.dumps({"eventUrl": slug, "password": password}).encode()
    req = urllib.request.Request(f"{BFF_BASE}/events.unlockEvent", data=body,
                                 method="POST", headers={
        **edm_common.BROWSER_HEADERS,
        "Accept": "*/*",
        "content-type": "application/json",
        "Origin": SITE_BASE,
        "Referer": event_page_url(slug),
    })
    try:
        with urllib.request.urlopen(req, timeout=edm_common.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            cookies = resp.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as e:
        raise EdmEventsError(f"posh: unlock {slug} returned HTTP {e.code} "
                             "— wrong code?") from e
    except (urllib.error.URLError, OSError) as e:
        raise EdmEventsError(f"posh: unlock {slug} failed: {e}") from e
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        data = {}
    res = ((data.get("result") or {}).get("data") if isinstance(data, dict) else None) or data
    if isinstance(res, dict) and res.get("json") is not None:
        res = res["json"]
    if not (isinstance(res, dict) and res.get("success")):
        raise EdmEventsError(f"posh: code rejected for {slug}")
    jar = [c.split(";", 1)[0] for c in cookies if "=" in c]
    jar.append(f"event_{slug}_password={urllib.parse.quote(password)}")
    return "; ".join(dict.fromkeys(jar))


def _scrape_meta(slug, password=None):
    """eventId + display metadata from the event page's flight payload."""
    headers = None
    if password:
        headers = {**edm_common.BROWSER_HEADERS, "Cookie": _unlock(slug, password)}
    html = edm_common.fetch_text(event_page_url(slug), headers=headers)
    payload = _flight_payload(html)
    data = {}
    i = payload.find('"eventResponse"')
    if i >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(payload, payload.index("{", i))
            data = (obj.get("eventData") or {}) if isinstance(obj, dict) else {}
        except ValueError:
            data = {}
    event_id = data.get("eventId")
    if not event_id:
        # Fallback: the id also appears bare in the payload (and, on some
        # renders, only there). Layout drift shouldn't cost us the tracker.
        m = _EVENT_ID_RE.search(payload) or _EVENT_ID_RE.search(html)
        event_id = m.group(1) if m else None
    if not event_id:
        if "PasswordProtected" in html or "PasswordProtected" in payload:
            raise EdmEventsError(f"posh: {event_page_url(slug)} is password-gated "
                                 + ("and the code didn't unlock it" if password else
                                    "— add it as \"<url> <code>\""))
        raise EdmEventsError(f"posh: no eventId found on {event_page_url(slug)} "
                             "— layout change or event removed?")
    return {
        "event_id": event_id,
        "name": (data.get("name") or "").strip() or slug,
        "venue": (data.get("venueName") or "").strip() or None,
        # Posh ships BOTH, and they are not the same instant: `start` is the
        # event's LOCAL wall-clock time (misleadingly stamped with a Z) and
        # `startUtc` is the real UTC instant. Keep them apart — displaying
        # startUtc turns a Fri 10pm doors time into "Sat 2:00 AM".
        "start": data.get("start") or data.get("startUtc"),
        "start_utc": data.get("startUtc") or data.get("start"),
        "timezone": data.get("timezone"),
        "fetched_at": time.time(),
    }


def _meta(slug, force=False, password=None):
    cache = _load_cache()
    hit = cache.get(slug)
    if (not force and hit and hit.get("event_id")
            and (time.time() - (hit.get("fetched_at") or 0)) < META_TTL_SECONDS):
        return hit
    meta = _scrape_meta(slug, password)
    cache[slug] = meta
    _save_cache(cache)
    return meta


def _date_text(meta):
    """'Fri, Sep 4, 2026 · 10:00 PM' from the ISO start, in the EVENT's own
    timezone when the page gave us one. Posh's `start` is already local
    wall-clock and `startUtc` is the UTC instant; we prefer `start` for
    display so a 10pm doors time never shows as next-day 2am."""
    from datetime import datetime
    raw = meta.get("start")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.strftime("%a, %b %-d, %Y · %-I:%M %p") if os.name != "nt" \
        else dt.strftime("%a, %b %d, %Y · %I:%M %p").replace(" 0", " ")


def fetch_tickets(event_id):
    """Raw result.data of events.getEventTickets for one event id."""
    inp = urllib.parse.quote(json.dumps(
        {"eventId": event_id, "includeEventPageButtonInfo": True}))
    url = f"{BFF_BASE}/events.getEventTickets?input={inp}"
    body = edm_common.fetch_json(url, headers={
        **edm_common.BROWSER_HEADERS,
        "Accept": "*/*",
        "content-type": "application/json",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer": SITE_BASE + "/",
    })
    if isinstance(body, dict) and body.get("error"):
        msg = ((body["error"].get("message") if isinstance(body["error"], dict) else None)
               or "unknown error")
        raise EdmEventsError(f"posh getEventTickets error: {msg}")
    data = ((body or {}).get("result") or {}).get("data")
    if not isinstance(data, dict):
        raise EdmEventsError("posh getEventTickets returned no result.data "
                             "— API shape change?")
    return data


def _tiers(data):
    out = []
    for group in data.get("ticketGroups") or []:
        gname = None if group.get("ungrouped") else (group.get("name") or None)
        for t in group.get("tickets") or []:
            if t.get("isHidden"):
                continue  # password/hidden tiers a normal buyer never sees
            avail = t.get("quantityAvailable")
            out.append(make_tier(
                t.get("name"),
                # All-in is what the page headlines and what the buyer pays;
                # `price` is the pre-fee face value.
                t.get("totalPrice") if t.get("totalPrice") is not None else t.get("price"),
                group=gname,
                face_price=t.get("price"),
                available=avail,
                sold_out=(avail == 0),
                closed=bool(t.get("closed") or t.get("disabled")),
            ))
    return out


def fetch_event(slug):
    """One posh event, normalized (see edm_common's module docstring)."""
    key = parse_url(slug)
    slug, password = split_key(key)
    meta = _meta(slug, password=password)
    try:
        data = fetch_tickets(meta["event_id"])
    except EdmEventsError:
        # A cached id can go stale if the organizer re-creates the event on
        # the same slug. Re-scrape once before giving up.
        meta = _meta(slug, force=True, password=password)
        data = fetch_tickets(meta["event_id"])

    tiers = _tiers(data)
    unreleased = (password and not tiers
                  and isinstance(data.get("ticketGroups"), list)
                  and not data["ticketGroups"])
    if not tiers and not unreleased:
        raise EdmEventsError(f"posh: {slug} parsed to 0 ticket tiers "
                             "— API shape change or event unpublished?")
    button = data.get("eventPageButtonInfo") or {}
    ev = {
        "source": SOURCE_NAME,
        "event_key": key,
        "name": meta.get("name") or slug,
        "venue": meta.get("venue"),
        "date_text": _date_text(meta),
        # start_date is the sortable/comparable instant (UTC); date_text is
        # the human local string.
        "start_date": meta.get("start_utc") or meta.get("start"),
        "page_url": event_page_url(slug),
        "currency": "USD",
        "tiers": tiers,
        "total_sold": data.get("totalTicketsSold"),
        "status_text": ((button.get("text") or "").strip()
                        or ("No tickets released yet" if unreleased else None)),
    }
    return rollup(ev)


DEFAULT_TARGET = "hello-48"


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    target = args[0] if args else DEFAULT_TARGET
    ev = fetch_event(target)
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    if ev.get("status_text"):
        print(f"  site says: {ev['status_text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
