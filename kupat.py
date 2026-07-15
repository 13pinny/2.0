"""kupat.co.il drop checker (Israeli ticketing platform — Caesarea, Habima, …).

The booking SPA at tickets.kupat.co.il fetches seat availability from a
small REST API. Three endpoints matter:

  GET /api/seats/seats-status?presentationId={pid}&venueTypeId=1&isReserved=1&isHold=0
      → returns the array of currently-buyable seats (~140 entries even
        when the show has thousands of total seats).
  GET /api/presentations/feature/{featureId}/{presentationId}?isHold=0
      → event metadata: featureName, venueName, dateTime, priceLevels,
        seatplanId, venueId.
  GET /api/seats/seatplan?seatplanId={spId}&venueId={vId}
      → full venue layout (~370 KB) — we only use it for the
        sectionId → SectionName map. Cached on disk per (seatplan, venue).

The seats-status response is a positional array per seat:
  [sectionId, ?, seatLabel, rowLabel, -1, ticketGroupId, ?, seatLabel, rowLabel]

We only use sectionId, rowLabel, seatLabel, ticketGroupId. The repeated
positions appear to be a serialization quirk of the kupat backend.

Two traps that silently blind the watcher if you get them wrong:

  * The booking SPA IGNORES the `prsntId` URL param. It auto-selects its own
    default date (highest-availability) and rewrites the URL to match, so on
    a multi-date event the seats-status XHR only ever fires for that default
    — every other date looks empty. You must click the date's row
    (`#presentation_<pid>`) to make the SPA fetch it. Verified July 2026.
  * `isGA` is NOT a boolean despite the name. It carries small ints (1 / 2 /
    24 …) on ordinary seated events, so `if presentation["isGA"]` marks most
    seated shows as general admission and collapses their whole seat map into
    one pseudo-seat. GA is instead "venue has no seated sections" — see
    _has_seated_capacity. (`isReserved` is likewise an int, not a flag.)

seats-status is also bot-protected: unlike the catalog/detail endpoints it
403s an in-page fetch(), so it can only be harvested by capturing the SPA's
own XHR (BrowserSession.capture) — never via api_get.

No login, no captcha — these endpoints are anonymous (the Cloudflare
Turnstile + vee-crm POSTs that show up in the SPA traffic guard the
checkout flow, not the listing endpoints).

Two more endpoints power the market sweep (market.py):

  GET /api/presentations/?locationId=0&isHold=0
      → EVERY presentation on the site in one ~1 MB response (581 rows as
        of July 2026): featureId, featureName, venueName, venueCity,
        dateTime, soldout, availRatio, minPrice, maxPrice. Omitting
        featureIds is what makes it site-wide; availRatio is a 4-decimal
        fraction, not a count.
  GET /api/presentations/feature/{featureId}/{presentationId}?isHold=0
      → the same presentation detail the booking page uses; carries the
        exact `availSeats` integer.

The CDN's bot filter only challenges *document* loads — once any kupat
page is open in a real browser, plain fetch() calls issued from its JS
context are answered normally (verified July 2026). BrowserSession
exploits that: one page-load, then arbitrarily many cheap in-page API
calls (parallel batches of ~10 run in ~2s).
"""
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://tickets.kupat.co.il/api"
SITE_BASE = "https://tickets.kupat.co.il"
CACHE_DIR = Path(__file__).parent / "tm_cache"  # shared cache dir; files are namespaced
CACHE_TTL_SECONDS = 3600

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Origin": SITE_BASE,
    "Referer": SITE_BASE + "/",
}

REQUEST_TIMEOUT = 20

SOURCE_NAME = "kupat"

# kupat GA (standing) events expose only tickets-left (`availSeats`), no
# per-seat map and no total capacity. We track them like tickchak festival
# shows — a single status-encoded "seat" — and derive a low-stock flag from
# a threshold since kupat has no "last tickets" signal of its own.
GA_LOW_THRESHOLD = int(os.getenv("KARTIS_KUPAT_GA_LOW") or 25)


def _has_seated_capacity(presentation, seatplan):
    """True when this presentation's venue has a real per-seat map.

    kupat's `isGA` field looks like a flag but is actually a small integer
    (1 / 2 / 24 …) present on ordinary seated events, so it can't be trusted
    to mean "general admission". A GA/standing event instead has *no seated
    sections* — that's the venue-level fact we key on (mirrors the commit
    that added GA tracking: GA = "no per-seat map and no total capacity").
    Returns True if the seatplan reports any seated capacity.
    """
    _, total_seated = _build_block_map(presentation, seatplan)
    return total_seated > 0


def _ga_status(presentation):
    """available | lasttickets | soldout for a GA presentation."""
    avail = presentation.get("availSeats")
    if presentation.get("soldout") or (isinstance(avail, (int, float)) and avail <= 0):
        return "soldout"
    if isinstance(avail, (int, float)) and avail <= GA_LOW_THRESHOLD:
        return "lasttickets"
    return "available"


class KupatError(RuntimeError):
    pass


class BrowserSession:
    """One headless Chromium shared across many kupat fetches.

    Two access modes:
      - capture(url, matchers): load a booking page and harvest matching
        XHR responses — the only way to trigger the SPA's own call chain.
      - api_get / api_get_many: in-page fetch() from a lazily-opened
        anchor page (site root). The CDN bot filter only challenges
        document loads, so these behave like a plain HTTP client once the
        anchor is up. api_get_many runs batches via Promise.all.

    Use as a context manager. The one-shot helpers below (_browse_capture)
    wrap it so single-watcher callers keep their old cost profile.
    """

    def __init__(self):
        self._pw = None
        self._browser = None
        self._ctx = None
        self._anchor = None

    def __enter__(self):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            raise KupatError(
                "kupat watcher needs patchright + Chromium. Run "
                "`.venv\\Scripts\\python -m patchright install chromium`."
            ) from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(locale="he-IL")
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
        self._pw = self._browser = self._ctx = self._anchor = None
        return False

    def capture(self, url, matchers, wait=12, select_pid=None):
        """Load `url` in a fresh page and return {key: resp.json()} for every
        response whose URL satisfies matchers[key] (a str predicate). Keys
        that never matched are simply absent — callers decide severity.

        `select_pid` (a presentation id): after the page loads, click the
        matching date row (`#presentation_<pid>`). The booking SPA IGNORES
        the `prsntId` URL param and auto-selects a default date (the last /
        highest-availability one), so for a multi-date event the seats-status
        XHR only ever fires for the SPA's default — never for the date we
        actually watch. Clicking the date row forces the SPA to fetch that
        presentation's seats. Harmless when the target already is the default
        (its XHR is captured on load and the loop below short-circuits).
        """
        page = self._ctx.new_page()
        captured = {}

        def on_response(resp):
            try:
                u = resp.url
                for key, pred in matchers.items():
                    if key not in captured and pred(u):
                        captured[key] = resp.json()
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            try:
                page.close()
            except Exception:
                pass
            raise KupatError(f"failed to load kupat booking page: {e}") from e
        if select_pid is not None:
            try:
                page.wait_for_selector(f"#presentation_{select_pid}", timeout=8000)
                page.evaluate(
                    """(pid) => {
                        const e = document.querySelector('#presentation_' + pid);
                        if (e) { e.scrollIntoView(); (e.querySelector('.list-item') || e).click(); }
                    }""",
                    str(select_pid),
                )
            except Exception:
                pass
        deadline = time.time() + wait
        while time.time() < deadline:
            if all(k in captured for k in matchers):
                break
            page.wait_for_timeout(200)
        try:
            page.close()
        except Exception:
            pass
        return captured

    _JS_FETCH_MANY = """
    async (urls) => Promise.all(urls.map(async (u) => {
      try {
        const r = await fetch(u, {headers: {"Accept": "application/json"}});
        return {url: u, status: r.status, body: await r.text()};
      } catch (e) { return {url: u, status: -1, body: String(e)}; }
    }))
    """

    def _ensure_anchor(self):
        if self._anchor is None or self._anchor.is_closed():
            page = self._ctx.new_page()
            try:
                page.goto(SITE_BASE + "/", wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                try:
                    page.close()
                except Exception:
                    pass
                raise KupatError(f"failed to open kupat anchor page: {e}") from e
            page.wait_for_timeout(1000)
            self._anchor = page
        return self._anchor

    def api_get_many(self, paths, concurrency=10):
        """In-page fetch of API `paths` (each starting with /api/). Returns
        {path: parsed_json_or_None} — None for non-200 / non-JSON."""
        page = self._ensure_anchor()
        out = {}
        for i in range(0, len(paths), concurrency):
            chunk = paths[i:i + concurrency]
            results = page.evaluate(self._JS_FETCH_MANY, [SITE_BASE + p for p in chunk])
            for p, res in zip(chunk, results):
                body = None
                if res.get("status") == 200:
                    try:
                        body = json.loads(res.get("body") or "null")
                    except Exception:
                        body = None
                out[p] = body
        return out

    def api_get(self, path):
        return self.api_get_many([path])[path]


def _browse_capture(feature_id, presentation_id, want):
    """Single page-load that captures whichever of the SPA's XHRs you ask for.

    `want` is a subset of {"seats", "presentation", "seatplan"}. Returns a
    dict with whatever was captured before timeout, `_missing` listing any
    keys that never arrived (soft-fail; caller decides whether to error).
    Launches and closes its own browser — ~5-10s per call (the date-row click
    below has to wait for a second seats-status round-trip); batch callers
    should hold a BrowserSession instead.
    """
    matchers = {}
    if "seats" in want:
        matchers["seats"] = (
            lambda u: "/seats-status" in u and f"presentationId={presentation_id}" in u
        )
    if "presentation" in want:
        matchers["presentation"] = (
            lambda u: f"/presentations/feature/{feature_id}/{presentation_id}" in u
        )
    if "seatplan" in want:
        matchers["seatplan"] = lambda u: "/seats/seatplan" in u

    with BrowserSession() as session:
        raw = session.capture(
            perf_url(feature_id, presentation_id), matchers,
            select_pid=presentation_id,
        )

    captured = {}
    if "seats" in raw:
        captured["seats"] = (raw["seats"] or {}).get("seats") or []
    if "presentation" in raw:
        captured["presentation"] = (raw["presentation"] or {}).get("presentation") or {}
    if "seatplan" in raw:
        captured["seatplan"] = raw["seatplan"]
    missing = [k for k in want if k not in captured]
    if missing:
        captured["_missing"] = missing
    return captured


# --- Market-sweep fetchers (used by market.py via a shared BrowserSession) --

def fetch_all_presentations(session):
    """Every presentation on the site in one in-page API call.

    Returns the raw list of presentation dicts (featureId, featureName,
    venueName, venueCity, dateTime "YYYY-MM-DD HH:MM", soldout, availRatio,
    minPrice, maxPrice, id). ~1 MB response, 581 rows as of July 2026."""
    body = session.api_get("/api/presentations/?locationId=0&isHold=0")
    if not isinstance(body, dict):
        raise KupatError("presentations catalog fetch failed")
    return body.get("presentations") or []


def fetch_presentation_details(session, pairs, concurrency=10):
    """Exact availability for many presentations.

    `pairs` is [(feature_id, presentation_id), ...]. Returns
    {presentation_id(str): presentation_detail_dict} — entries whose fetch
    failed are absent. Each detail carries availSeats, soldout, isGA."""
    paths = [
        f"/api/presentations/feature/{fid}/{pid}?isHold=0"
        for fid, pid in pairs
    ]
    raw = session.api_get_many(paths, concurrency=concurrency)
    out = {}
    for (fid, pid), path in zip(pairs, paths):
        body = raw.get(path)
        pres = (body or {}).get("presentation") if isinstance(body, dict) else None
        if pres:
            out[str(pid)] = pres
    return out


def parse_url(url):
    """Extract (feature_id, presentation_id) from a kupat booking URL or
    shorthand "FEATURE/PERF" (e.g. "1358/51596"). Both ids are stringified
    digits; we keep them as strings so they slot directly into the same
    DB columns as the Ticketmaster source.
    """
    if not url:
        raise KupatError("URL is empty")
    s = url.strip()
    short = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", s)
    if short:
        return short.group(1), short.group(2)
    feature_match = re.search(r"/features/(\d+)", s)
    prsnt_match = re.search(r"[?&]prsntId=(\d+)", s)
    if not feature_match:
        raise KupatError("URL must include /features/<id>")
    if not prsnt_match:
        raise KupatError(
            "URL must include ?prsntId=<presentation id>. Open the show in your "
            "browser, click a specific date, and copy the URL with prsntId in it."
        )
    return feature_match.group(1), prsnt_match.group(1)


def perf_url(feature_id, presentation_id):
    return f"{SITE_BASE}/booking/features/{feature_id}?display=list&prsntId={presentation_id}"


def fetch_selectable_seats(feature_id, presentation_id):
    """Returns currently-buyable seats in the normalized cross-source shape.

    Each dict has block (sectionId as str), row (rowLabel), seat (seatLabel),
    plus priceLevel (the ticketGroupId) and `raw` (the original positional
    array). Costs ~5-10s per call because the kupat API rejects out-of-page
    requests; we run a headless browser to capture the seats-status XHR.
    """
    captured = _browse_capture(
        feature_id, presentation_id, want={"seats", "presentation", "seatplan"}
    )
    presentation = captured.get("presentation") or {}
    seatplan = captured.get("seatplan") or {}
    # Real per-seat availability is the ground truth: if the seats-status XHR
    # returned any per-seat rows, this is a seated event — return them.
    seats = captured.get("seats") or []
    out = []
    for s in seats:
        if not isinstance(s, list) or len(s) < 4:
            continue
        out.append({
            "block": str(s[0]),
            "row": str(s[3]),
            "seat": str(s[2]),
            "priceLevel": s[5] if len(s) > 5 else None,
            "raw": s,
        })
    if out:
        return out
    # No buyable per-seat rows. This is either a GA (standing) event — which
    # has no seat map at all, only a tickets-left count — or a seated event
    # with nothing currently available. kupat's `isGA` field is NOT a boolean
    # (it carries small ints like 1/2/24 on ordinary seated events), so we
    # decide GA the way the venue itself does: a GA event has no seated
    # sections in its seatplan. Track it as one status-encoded seat so the
    # diff fires on every sold-out / last-tickets / available-again
    # transition (mirrors the tickchak festival model).
    if not _has_seated_capacity(presentation, seatplan):
        status = _ga_status(presentation)
        label = {"available": "GA available", "lasttickets": "GA last tickets",
                 "soldout": "GA sold out"}.get(status, "GA available")
        return [{
            "block": label, "row": "GA", "seat": "1",
            "ga": True, "status": status,
            "qty_available": presentation.get("availSeats"),
            "price": None,
        }]
    return out


def seat_key(seat):
    return f"{seat.get('block','')}|{seat.get('row','')}|{seat.get('seat','')}"


def format_seat(seat):
    return f"section {seat.get('block','?')} row {seat.get('row','?')} seat {seat.get('seat','?')}"


# --- Labels (event meta + section names + ILS prices), cached on disk ----

def _cache_path(feature_id, presentation_id, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"kupat_{feature_id}_{presentation_id}_{lang}.json"


# presentation + seatplan are captured in the same page load as seats — see
# fetch_fresh below. No separate HTTP helpers; the API rejects them anyway.


def _build_block_map(presentation, seatplan):
    """Returns ({block_code: {name, price, priceLevel, ticketGroupId}}, totalSeated).

    totalSeated is the sum of every section's Capacity from the seatplan —
    i.e. the venue's seated dot-count for this layout."""
    price_by_group = {}
    for lvl in presentation.get("priceLevels") or []:
        gid = lvl.get("ticketGroupId")
        if gid is None:
            continue
        price_by_group[gid] = lvl.get("minPrice") or lvl.get("maxPrice")
    sections = (seatplan or {}).get("sections") or {}
    out = {}
    total_seated = 0
    for sid, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        section_id = str(sec.get("VenueSectionId", sid))
        name = (sec.get("SectionName") or "").strip()
        gid = sec.get("MainTicketGroupId")
        try:
            gid_int = int(gid) if gid is not None else None
        except (TypeError, ValueError):
            gid_int = None
        cap = sec.get("Capacity")
        if isinstance(cap, (int, float)):
            total_seated += int(cap)
        out[section_id] = {
            "name": name or section_id,
            "price": price_by_group.get(gid_int),
            "priceLevel": gid_int,
            "ticketGroupId": gid_int,
        }
    return out, total_seated


def fetch_fresh(feature_id, presentation_id, lang="iw"):
    captured = _browse_capture(feature_id, presentation_id, want={"presentation", "seatplan"})
    presentation = captured.get("presentation") or {}
    seatplan = captured.get("seatplan") or {}
    blocks, total_seated = _build_block_map(presentation, seatplan)
    # GA (standing) events have no seated sections — see _has_seated_capacity.
    # `isGA` is an int, not a boolean, so it can't be used here.
    is_ga = total_seated == 0
    # kupat returns dateTime as "YYYY-MM-DD HH:MM:SS" in venue-local time.
    # We keep both the raw string (for accurate display, no TZ conversion)
    # and an ms-since-epoch interpretation (for sorting). The display path
    # prefers the raw string so we don't accidentally render the time in
    # whatever timezone the watcher box happens to be in.
    dt_raw = (presentation.get("dateTime") or "").strip()
    perf_text = dt_raw[:16] if len(dt_raw) >= 16 else dt_raw
    perf_ms = None
    if dt_raw:
        try:
            from datetime import datetime
            perf_ms = int(datetime.strptime(dt_raw[:16], "%Y-%m-%d %H:%M").timestamp() * 1000)
        except Exception:
            perf_ms = None
    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(feature_id),
        "perf_code": str(presentation_id),
        "lang": lang,
        "meta": {
            "eventName": (presentation.get("featureName") or "").strip(),
            "venueName": (presentation.get("venueName") or "").strip(),
            "venueCity": (presentation.get("locationCity") or "").strip(),
            "firstPerfMs": perf_ms,
            "firstPerfText": perf_text,
            "status": "soldout" if presentation.get("soldout") else "selling",
            "availSeats": presentation.get("availSeats"),
            # For GA, the seatplan capacity is the venue's *seated* dot-count,
            # not the standing/GA allocation, so it's a misleading "total" —
            # null it and show tickets-left only. Non-GA keeps the real total.
            "totalSeats": None if is_ga else (total_seated or None),
            # GA (standing) marker + low-stock-aware status for the GA Tracker
            # page. Non-GA kupat events leave ga False and behave as before.
            "ga": is_ga,
            "gaStatus": _ga_status(presentation) if is_ga else None,
        },
        "blocks": blocks,
    }
    _cache_path(feature_id, presentation_id, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload


def get_labels(feature_id, presentation_id, lang="iw", force=False, missing_block=None):
    path = _cache_path(feature_id, presentation_id, lang)
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
        or (missing_block and str(missing_block) not in (cached.get("blocks") or {}))
    )
    if not fresh_needed:
        return cached
    try:
        return fetch_fresh(feature_id, presentation_id, lang)
    except Exception:
        return cached or {
            "source": SOURCE_NAME, "event_code": str(feature_id),
            "perf_code": str(presentation_id), "lang": lang,
            "meta": {}, "blocks": {},
        }


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    parts = []
    if meta.get("eventName"):
        parts.append(meta["eventName"])
    if meta.get("venueName"):
        parts.append(meta["venueName"])
    # Prefer the raw venue-local string from the API — avoids any timezone
    # conversion guesswork on the watcher box.
    if meta.get("firstPerfText"):
        parts.append(meta["firstPerfText"])
    return " · ".join(parts)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("usage: python kupat.py <url-or-FEATURE/PERF>")
        sys.exit(2)
    fid, pid = parse_url(sys.argv[1])
    print(f"feature={fid} perf={pid}")
    seats = fetch_selectable_seats(fid, pid)
    print(f"{len(seats)} selectable seats")
    labels = get_labels(fid, pid)
    print("meta:", labels.get("meta"))
    print("blocks:")
    for code, info in (labels.get("blocks") or {}).items():
        print(f"  {code}: {info.get('name')} — ₪{info.get('price')}")
    by_block = {}
    for s in seats:
        by_block.setdefault(s["block"], 0)
        by_block[s["block"]] += 1
    print("\\navailable by section:")
    for b, n in sorted(by_block.items(), key=lambda kv: -kv[1]):
        info = (labels.get("blocks") or {}).get(b, {})
        print(f"  {b}: {n}  ({info.get('name','?')})")
