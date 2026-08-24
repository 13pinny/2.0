"""discover.ticketmaster.co.il drop checker — TM-IL "series" landing pages.

TM-IL builds a marketing page per big multi-date production (NEXT 2026 at
discover.ticketmaster.co.il/event/next2026 is the one this was written for).
The page lists EVERY date of the series with a colored status box:

    gray   "הכרטיסים אזלו"        sold_out          not buyable
    gray   "אין כרטיסים זמינים"   no_tickets        not buyable
    gray   "בקרוב"                soon              sale hasn't opened
    orange "כרטיסים אחרונים"      low_availability  BUYABLE — last tickets
    green  "כרטיסים"              onsale (default)  BUYABLE
    green  "מופע חדש"             new_event         BUYABLE

Watching the series page rather than N per-performance watchers is the whole
point: it is one request for every date, it needs no seat map (a sold-out
performance has nothing to poll), it picks up dates TM adds later, and it
covers the entries that carry no eventcode yet — two of NEXT's Jerusalem
dates are listed with `eventcode: null`, so they are invisible to every
other checker in this repo until TM wires them to a performance.

The status comes from the site's OWN uncached JSON:

  GET https://discover.ticketmaster.co.il/public/v1/pages/<slug>
      → {"slug", "hero", "entries": [{id, eventcode, performance_code,
         sale_status, starts_at, venue_name, city, ticket_url, ...}], ...}
      Answers plain urllib, `cache-control: no-store` (~13KB for NEXT).

The page HTML embeds the same payload in a <script id="discover-bootstrap">
tag, but behind a 60s CDN cache — it is used only as a fallback if the JSON
route ever moves, so a route change degrades to slightly-stale data instead
of a dead watcher. Both parses raise on an unrecognizable body: a blocked or
rewritten response must never look like "no dates are on sale".

Seats: ONE pseudo-seat per BUYABLE date, with the status text riding in the
`block` (the barby/tickchak trick — the core add/remove diff then fires on
every transition without the tick loop knowing anything about this source):

    sold out → last tickets / on sale   seat appears  → ping (routes to drops)
    last tickets ↔ on sale              key changes   → ping (supply moved)
    on sale → sold out                  seat vanishes → silent (removal only)

Sell-outs are deliberately silent: a 14-date series would otherwise ping
twice per flip, and "it's gone again" is not actionable. The seats are
PHYSICAL (no `ga`/`festival` flag) for the same reason haku's are — a
GA-flagged set collapses to a single status line in notify.py, which would
hide WHICH date opened, and the per-seat cool-down (KARTIS_SEAT_COOLDOWN_
MINUTES) only damps physical seats, which is exactly what a date flapping
at TM's low-availability threshold needs.

perf_code is always "0" — the watcher covers the whole series, so the slug
alone identifies it (same convention as barby/tickchak).

CLI probe:  python tm_discover.py [<url-or-slug>]   (add --json)
"""
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SOURCE_NAME = "tmdiscover"
SITE_BASE = "https://discover.ticketmaster.co.il"
API_PATH = "/public/v1/pages/"
DEFAULT_SLUG = "next2026"
CACHE_DIR = Path(__file__).parent / "tm_cache"  # shared cache dir; files are namespaced
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 25

# One pseudo-seat per buyable date, so adjacency filtering makes no sense
# (same opt-out tickchak / dice / haku take).
DEFAULT_FILTERS = {"min_group_size": 1}

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip",
    "Referer": SITE_BASE + "/",
}

# Mirrors the SPA bundle: `Cr(t) = t.sale_status || "onsale"` and
# `Bs(t) = ["onsale","low_availability","new_event"].includes(Cr(t))`.
DEFAULT_STATUS = "onsale"
BUYABLE_STATUSES = {"onsale", "low_availability", "new_event"}
# The exact box text the page renders per status (function Jy in the bundle).
STATUS_TEXT = {
    "onsale": "כרטיסים",
    "low_availability": "כרטיסים אחרונים",
    "new_event": "מופע חדש",
    "sold_out": "הכרטיסים אזלו",
    "no_tickets": "אין כרטיסים זמינים",
    "soon": "בקרוב",
}
STATUS_EMOJI = {"onsale": "🟢", "low_availability": "🟠", "new_event": "🆕"}

# --- Live cross-check against ticketmaster.co.il --------------------------
#
# `sale_status` is a CMS field on TM-IL's marketing site, not the box office.
# It goes stale, and when it does this watcher reports a date as gray while
# tickets are on sale behind it. Measured on NEXT 2026, 2026-08-24:
#
#     date    perf        discover sale_status   TM perf status      seats
#     10.10   MRG17/001   sold_out               s01_onsale          1,195
#     06.12   MAS03/001   sold_out               s01_onsale              1
#     10.12   MAS04/001   sold_out               s01_onsale            114
#
# Three of fourteen dates wrong, one of them holding 1,195 live seats — a
# miss this source cannot see on its own, because the CMS is its only input.
# So every non-buyable entry that carries an eventcode gets a second opinion
# from ticketmaster.list_performances (the same live, uncached perf list the
# drop checker gates on).
#
# The cross-check may only ever ADD buyability, never remove it: TM being
# unreachable, slow, or disagreeing the other way leaves the CMS verdict
# exactly as it stands, so the worst case is the behaviour this source had
# before. Entries with `eventcode: null` (two of NEXT's Jerusalem dates) have
# nothing to cross-check against and stay CMS-only.
CROSSCHECK_ENABLED = (os.getenv("KARTIS_TMDISCOVER_CROSSCHECK") or "1").strip().lower()     not in ("0", "false", "no", "off")

# TM perf-list status → the discover status it corresponds to. Mirrors
# ticketmaster._ON_SALE_STATUSES — keep the two together.
_TM_STATUS_TO_DISCOVER = {
    "s01_onsale": "onsale",
    "s18_low_availability": "low_availability",
}


class TmDiscoverError(RuntimeError):
    pass


# --- HTTP ----------------------------------------------------------------

# The tick calls fetch_selectable_seats() and (only when something changed)
# get_labels() back to back; a few seconds of memo makes that one request.
_MEMO = {}
_MEMO_TTL_SECONDS = 10


def _get(url, accept_json=True):
    headers = dict(REQUEST_HEADERS)
    if not accept_json:
        headers["Accept"] = "text/html,application/xhtml+xml"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise TmDiscoverError(f"discover {url} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise TmDiscoverError(f"discover {url} unreachable: {e.reason}") from e
    return raw.decode("utf-8", errors="replace")


_BOOTSTRAP_RE = re.compile(
    r'<script[^>]*id="discover-bootstrap"[^>]*>(.*?)</script>', re.DOTALL)


def _fetch_page(slug, use_memo=True):
    """The page payload dict for a slug (live JSON API, HTML bootstrap as
    fallback). Raises rather than returning a partial payload — an empty
    `entries` from a challenge page would read as 'everything sold out'."""
    now = time.time()
    if use_memo:
        hit = _MEMO.get(slug)
        if hit and now - hit[0] < _MEMO_TTL_SECONDS:
            return hit[1]
    try:
        raw = _get(SITE_BASE + API_PATH + urllib.request.quote(str(slug)))
        payload = json.loads(raw)
    except (TmDiscoverError, json.JSONDecodeError) as api_err:
        # Fall back to the server-rendered page (60s CDN cache, same shape).
        try:
            html = _get(f"{SITE_BASE}/event/{slug}", accept_json=False)
            m = _BOOTSTRAP_RE.search(html)
            if not m:
                raise TmDiscoverError(
                    f"no discover-bootstrap payload on /event/{slug} — page layout change?")
            payload = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise TmDiscoverError(
                f"unparsable discover-bootstrap on /event/{slug}") from e
        except TmDiscoverError:
            raise api_err
    if not isinstance(payload, dict) or "entries" not in payload:
        raise TmDiscoverError(
            f"unexpected payload shape for '{slug}' — no `entries` key (API change?)")
    if not isinstance(payload.get("entries"), list):
        raise TmDiscoverError(f"`entries` is not a list for '{slug}' — API change?")
    _MEMO[slug] = (now, payload)
    return payload


# --- URL parsing ---------------------------------------------------------

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def parse_url(url):
    """Extract (slug, "0") from a discover.ticketmaster.co.il URL.

    Accepts https://discover.ticketmaster.co.il/event/next2026 (with or
    without scheme / trailing slash / query), the shorthand
    "discover/next2026", and a bare slug "next2026" when routed here
    explicitly (a bare slug on its own is ambiguous with a tickchak slug,
    so _detect_source only sends the URL forms here — paste the full URL).
    """
    if not url:
        raise TmDiscoverError("URL is empty")
    s = url.strip().split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"discover\.ticketmaster\.co\.il/(?:event/)?([A-Za-z0-9_-]+)", s, re.I)
    if m:
        return m.group(1), "0"
    m = re.fullmatch(r"\s*(?:discover|tmdiscover)\s*/\s*([A-Za-z0-9_-]+)\s*(?:/\s*0\s*)?", s, re.I)
    if m:
        return m.group(1), "0"
    if _SLUG_RE.fullmatch(s):
        return s, "0"
    raise TmDiscoverError(
        "URL must look like https://discover.ticketmaster.co.il/event/<slug>")


def perf_url(event_code, perf_code="0"):
    return f"{SITE_BASE}/event/{event_code}"


# --- Entries -------------------------------------------------------------

def entry_status(entry):
    """The status the page box shows. Null/blank means on sale — the SPA
    defaults it that way, and TM leaves it null on freshly-wired dates."""
    return (entry.get("sale_status") or DEFAULT_STATUS).strip() or DEFAULT_STATUS


def is_buyable(entry):
    return entry_status(entry) in BUYABLE_STATUSES


def entry_date(entry):
    """('DD.MM.YYYY HH:MM', epoch_ms) from the entry's ISO `starts_at`.

    `starts_at` is a real offset-aware timestamp, so it is rendered in
    Asia/Jerusalem (the venue's clock) rather than UTC — a 20:15 Ramat Gan
    show would otherwise display as 17:15.
    """
    raw = (entry.get("starts_at") or "").strip()
    if not raw:
        return "", None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw, None
    try:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo("Asia/Jerusalem"))
    except Exception:
        pass
    return dt.strftime("%d.%m.%Y %H:%M"), int(dt.timestamp() * 1000)


def entry_ref(entry):
    """Stable per-date id for the seat key: the TM performance when the date
    has one, else the CMS row's uuid (NEXT lists two Jerusalem dates with
    eventcode: null — they still need to be watchable)."""
    code = (entry.get("eventcode") or "").strip()
    perf = str(entry.get("performance_code") or "").strip()
    if code:
        return code, (perf or "ALL")
    return (str(entry.get("id") or "")[:8] or "?"), (perf or "—")


def entry_url(entry):
    """Public buy URL for one date (mirrors Xy in the bundle), falling back
    to the series page when TM hasn't wired a performance yet."""
    direct = (entry.get("ticket_url") or "").strip()
    if direct:
        return direct
    code = (entry.get("eventcode") or "").strip()
    perf = str(entry.get("performance_code") or "").strip()
    if code and perf and (entry.get("ticket_target") or "") == "performance":
        return f"https://www.ticketmaster.co.il/performance/{code}/{perf}/ALL/iw"
    if code:
        return f"https://www.ticketmaster.co.il/event/{code}/ALL/iw"
    child = (entry.get("child_slug") or "").strip()
    return f"{SITE_BASE}/event/{child}" if child else ""


def _live_perf_status(entry, memo):
    """The status TM's own box office reports for this entry's performance,
    normalized to a discover status — or None when there's nothing to say.

    `memo` is a per-call {event_code: {perf_code: status}} cache, so a series
    listing several dates under one event code (NEXT lists four under MAS03)
    costs one perf-list request, not four. Never raises: every failure path
    returns None, which leaves the CMS verdict untouched.
    """
    code = (entry.get("eventcode") or "").strip()
    perf = str(entry.get("performance_code") or "").strip()
    if not code or not perf:
        return None
    if code not in memo:
        try:
            import ticketmaster  # lazy: keeps this module's CLI probe dep-free
            memo[code] = {
                str(p.get("performanceCode") or ""): str(p.get("status") or "")
                for p in ticketmaster.list_performances(code)
            }
        except Exception:
            memo[code] = {}
    return _TM_STATUS_TO_DISCOVER.get(memo[code].get(perf) or "")


def _sorted_entries(payload):
    return sorted(
        payload.get("entries") or [],
        key=lambda e: (entry_date(e)[1] or 0, str(e.get("id") or "")),
    )


def _seat_block(entry, status=None, via_tm=False):
    """The seat's block text — date, venue and the status box's own wording.
    The status is part of the key on purpose (see the module docstring).

    `status` overrides the entry's own when the live cross-check disagreed;
    `via_tm` marks the ping so it's clear the series page still shows this
    date as gray and the buy link goes straight to the box office.
    """
    date_text, _ = entry_date(entry)
    status = status or entry_status(entry)
    venue = (entry.get("venue_name") or entry.get("venue_key") or "").strip()
    emoji = STATUS_EMOJI.get(status, "🎟️")
    head = " · ".join(p for p in (date_text, venue) if p) or (entry.get("title") or "?")
    tail = " (לפי TM — הדף עוד מציג אזל)" if via_tm else ""
    return f"{emoji} {head} — {STATUS_TEXT.get(status, status)}{tail}"


def fetch_selectable_seats(event_code, perf_code="0"):
    """One physical pseudo-seat per BUYABLE date on the series page.

    Non-buyable dates (sold out / no tickets / not yet on sale) produce no
    seat, so a date selling out is a silent removal and a date opening is an
    `added` seat — which is the ping this source exists for.

    A date the CMS calls non-buyable is given a second opinion by TM's live
    perf list before being dropped (see CROSSCHECK_ENABLED) — add-only, so a
    TM failure can never turn a buyable date gray.
    """
    payload = _fetch_page(event_code)
    seats = []
    perf_memo = {}
    for entry in _sorted_entries(payload):
        status, via_tm = entry_status(entry), False
        if not is_buyable(entry):
            live = _live_perf_status(entry, perf_memo) if CROSSCHECK_ENABLED else None
            if not live:
                continue
            status, via_tm = live, True
        ref, perf = entry_ref(entry)
        seats.append({
            "block": _seat_block(entry, status=status, via_tm=via_tm),
            "row": ref,
            "seat": perf,
            "price": None,
            "status": status,
            "via_tm": via_tm,
            "entry_id": entry.get("id"),
            "url": entry_url(entry),
        })
    return seats


def seat_key(seat):
    # Must mirror db.tm_replace_seat_state's block|row|seat rebuild exactly.
    return f"{seat.get('block', '')}|{seat.get('row', '')}|{seat.get('seat', '')}"


def format_seat(seat):
    return f"{seat.get('block', '?')} ({seat.get('row', '?')}/{seat.get('seat', '?')})"


# --- Labels (event meta), cached on disk ---------------------------------

def _cache_path(event_code, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"tmdiscover_{event_code}_{lang}.json"


def fetch_fresh(event_code, perf_code="0", lang="iw"):
    payload = _fetch_page(event_code)
    entries = _sorted_entries(payload)
    hero = payload.get("hero") or {}
    name = (hero.get("title") or (payload.get("seo") or {}).get("title")
            or str(event_code)).strip()
    venues = []
    for e in entries:
        v = (e.get("venue_name") or e.get("venue_key") or "").strip()
        if v and v not in venues:
            venues.append(v)
    dated = [(entry_date(e), e) for e in entries]
    first = next(((t, ms) for (t, ms), _ in dated if ms), ("", None))
    last = next(((t, ms) for (t, ms), _ in reversed(dated) if ms), first)
    buyable = [e for e in entries if is_buyable(e)]
    meta = {
        "eventName": name,
        "venueName": " / ".join(venues[:3]),
        "venueCity": (entries[0].get("city") or "").strip() if entries else "",
        "seatType": "series",
        # The LAST date of the series, so app._purge_past_watchers only reaps
        # the watcher once every date has passed (a series watcher that dies
        # on its opening night would stop watching the other 13).
        "firstPerfMs": last[1],
        "firstPerfText": first[0],
        "lastPerfText": last[0],
        "status": "selling" if buyable else "soldout",
        "dates": len(entries),
        "datesOnSale": len(buyable),
        # TM publishes no per-date ticket counts on this page.
        "availSeats": None,
        "totalSeats": None,
        "entries": [
            {
                "id": e.get("id"),
                "date": entry_date(e)[0],
                "venue": (e.get("venue_name") or "").strip(),
                "eventcode": e.get("eventcode"),
                "performance_code": e.get("performance_code"),
                "status": entry_status(e),
                "status_text": STATUS_TEXT.get(entry_status(e), entry_status(e)),
                "buyable": is_buyable(e),
                "url": entry_url(e),
            }
            for e in entries
        ],
    }
    payload_out = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(event_code),
        "perf_code": "0",
        "lang": lang,
        "meta": meta,
        # No prices are published on the series page, so blocks stay empty —
        # notify.py falls back to rendering the block text as-is, and
        # _total_price stays None instead of inventing a total.
        "blocks": {},
    }
    _cache_path(event_code, lang).write_text(
        json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload_out


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    path = _cache_path(event_code, lang)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    fresh_needed = (
        force
        or cached is None
        or (time.time() - (cached.get("_fetched_at") or 0) > CACHE_TTL_SECONDS)
    )
    if not fresh_needed:
        return cached
    try:
        return fetch_fresh(event_code, perf_code, lang)
    except Exception:
        return cached or {
            "source": SOURCE_NAME, "event_code": str(event_code),
            "perf_code": "0", "lang": lang, "meta": {}, "blocks": {},
        }


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    parts = []
    if meta.get("eventName"):
        parts.append(meta["eventName"])
    if meta.get("venueName"):
        parts.append(meta["venueName"])
    if meta.get("dates"):
        first = meta.get("firstPerfText") or ""
        last = meta.get("lastPerfText") or ""
        span = first[:10] if first else ""
        if last and last[:10] != span:
            span = f"{span}–{last[:10]}" if span else last[:10]
        parts.append(f"{meta['dates']} dates" + (f" ({span})" if span else ""))
    return " · ".join(parts)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    slug, perf = parse_url(args[0] if args else DEFAULT_SLUG)
    payload = _fetch_page(slug, use_memo=False)
    entries = _sorted_entries(payload)
    if as_json:
        print(json.dumps({
            "slug": slug,
            "entries": [
                {"date": entry_date(e)[0], "venue": e.get("venue_name"),
                 "eventcode": e.get("eventcode"), "perf": e.get("performance_code"),
                 "status": entry_status(e), "buyable": is_buyable(e),
                 "url": entry_url(e)}
                for e in entries
            ],
        }, ensure_ascii=False, indent=2))
        sys.exit(0)
    labels = get_labels(slug, perf, force=True)
    print(f"slug={slug}  {perf_url(slug, perf)}")
    print("summary:", event_summary(labels))
    print(f"{len(entries)} dates:")
    for e in entries:
        st = entry_status(e)
        mark = "BUYABLE" if is_buyable(e) else "       "
        ref, pc = entry_ref(e)
        print(f"  {mark}  {entry_date(e)[0]:<17} {ref}/{pc:<4} "
              f"{(e.get('venue_name') or '')[:22]:<24} {st} ({STATUS_TEXT.get(st, st)})")
    seats = fetch_selectable_seats(slug, perf)
    print(f"\nselectable pseudo-seats: {len(seats)}")
    for s in seats:
        print(f"  {format_seat(s)}\n     key={seat_key(s)}")
