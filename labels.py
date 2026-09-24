"""Hebrew event/section labels + ILS prices for the Ticketmaster drop checker.

These come from two cached endpoints under /wbtxapi:
  - getEventDetail/{event}/{channel}/{lang}      — event name, venue, dates
  - getPriceByProfiles/{event}/{perf}/{ch}/{lang} — block code → name + price

The data rarely changes during an event's lifetime, so we keep a tiny on-disk
JSON cache per (event, perf, lang) keyed under tm_cache/. Cache is refreshed
automatically once per hour or whenever a watcher's notification reveals a
block code that isn't in the cached map (a hot block could appear after a new
section was opened by the venue).
"""
import json
import gzip
import urllib.request
import urllib.error
import time
from pathlib import Path

WBTX_HOST = "https://www.ticketmaster.co.il/wbtxapi"
CHANNEL = "INTERNET"
CACHE_DIR = Path(__file__).parent / "tm_cache"
CACHE_TTL_SECONDS = 3600

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "iw",
    "Accept-Encoding": "gzip, deflate",
    "CHANNEL": CHANNEL,
    "CPU": "32100",
    "CLIENT_APP_ENVIRONMENT": "prod",
    "Origin": "https://www.ticketmaster.co.il",
    "Referer": "https://www.ticketmaster.co.il/",
}


def _http_get_json(url):
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    resp = urllib.request.urlopen(req, timeout=20)
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _cache_path(event_code, perf_code, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{event_code}_{perf_code}_{lang}.json"


def _fetch_event_meta(event_code, lang):
    url = f"{WBTX_HOST}/api/v1/bxcached/event/getEventDetail/{event_code}/{CHANNEL}/{lang}"
    payload = _http_get_json(url)
    d = payload.get("data") or {}
    return {
        "eventName": (d.get("eventName") or "").strip(),
        "eventGroupName": (d.get("eventGroupName") or "").strip(),
        "venueName": (d.get("venueName") or "").strip(),
        "venueCity": (d.get("venueCity") or "").strip(),
        "venueId": d.get("venueId"),
        "firstPerfMs": d.get("firstPerformanceDate"),
        "lastPerfMs": d.get("lastPerformanceDate"),
        "status": d.get("status"),
    }


def _fetch_block_labels(event_code, perf_code, lang):
    """Returns {block_code: {name, price (ILS), priceLevel, profileId}}.

    We pick the first PUBLIC profile's prices when available; otherwise we
    take the first block entry from any profile (still gives a name + price).
    """
    # Profiles tell us which profile is PUBLIC vs TOKEN/MEMBER
    profiles_url = f"{WBTX_HOST}/api/v1/bxcached/event/getEventPerfProfiles/{event_code}/{perf_code}/{CHANNEL}/{lang}"
    public_pids = []
    try:
        prof = _http_get_json(profiles_url).get("data") or []
        public_pids = [str(p.get("profileId")) for p in prof if p.get("profileType") == "PUBLIC"]
    except Exception:
        pass

    prices_url = f"{WBTX_HOST}/api/v1/bxcached/event/getPriceByProfiles/{event_code}/{perf_code}/{CHANNEL}/{lang}"
    prices = _http_get_json(prices_url).get("data") or {}

    # Walk profiles in priority order: public ones first, then everything else
    ordered_pids = list(dict.fromkeys(public_pids + list(prices.keys())))
    blocks = {}
    for pid in ordered_pids:
        for tier in prices.get(pid, []):
            value = tier.get("value")
            level = tier.get("priceLevel")
            for b in tier.get("blocks", []) or []:
                code = b.get("code")
                if not code or code in blocks:
                    continue
                blocks[code] = {
                    "name": (b.get("description") or "").strip(),
                    "price": round(value / 100, 2) if value is not None else None,
                    "priceLevel": level,
                    "profileId": pid,
                }
    return blocks


_ISM_HOST = "https://www.ticketmaster.co.il/ismapi/api/v1/seatPlans"
_INTERNET = "INTERNET"


def _discover_extra_blocks(event_code, perf_code):
    """Discover every block code in the venue, plus the seated capacity and
    GA (unnumbered) allocations.

    Returns (extras, totalSeated, gaRows). `extras` is the set of block
    codes seen in getAllBlockSeats / getAllGaBlock. `totalSeated` is the
    dot-count of the seat plan — every entry in getAllBlockSeats is one
    physical seat regardless of state, so the length of the array IS the
    venue's seated capacity for this performance. `gaRows` is the raw
    getAllGaBlock list — one row per (block, price-profile) allocation with
    `t` (capacity) and `a` (remaining); [] for fully-seated shows.

    Cached for an hour as part of the labels payload.
    """
    extras = set()
    total_seated = 0
    ga_rows = []
    try:
        payload = _http_get_json(f"{_ISM_HOST}/getAllBlockSeats/{event_code}/{perf_code}")
        data = payload.get("data") or []
        if isinstance(data, list):
            total_seated = len(data)
            for s in data:
                if isinstance(s, dict) and s.get("b"):
                    extras.add(s["b"])
    except Exception:
        pass
    try:
        payload = _http_get_json(f"{_ISM_HOST}/getAllGaBlock/{event_code}/{perf_code}")
        data = payload.get("data") or []
        if isinstance(data, list):
            for s in data:
                if isinstance(s, dict) and s.get("b"):
                    extras.add(s["b"])
                    if s.get("ga"):
                        ga_rows.append(s)
    except Exception:
        pass
    return extras, total_seated, ga_rows


def fetch_fresh(event_code, perf_code, lang="iw"):
    """Force a refresh — bypasses cache. Used when a notification surfaces an
    unknown block code (rare but possible if a new section opens up)."""
    meta = _fetch_event_meta(event_code, lang)
    try:
        blocks = _fetch_block_labels(event_code, perf_code, lang)
    except Exception:
        blocks = {}
    # Backfill block codes that exist in the venue layout but didn't
    # appear in getPriceByProfiles — typically restricted/accessibility
    # tiers and standing blocks. Also captures the total seated capacity.
    total_seated = 0
    ga_rows = []
    try:
        extras, total_seated, ga_rows = _discover_extra_blocks(event_code, perf_code)
        for code in extras:
            blocks.setdefault(code, {
                "name": code,
                "price": None,
                "priceLevel": None,
                "profileId": None,
            })
    except Exception:
        pass
    if total_seated:
        meta["totalSeats"] = total_seated
    if ga_rows:
        # GA (unnumbered) show — expose counts + status for the GA Tracker
        # page, mirroring kupat's meta keys (ga / gaStatus / availSeats /
        # totalSeats / firstPerfText). Lazy import: ticketmaster.py only
        # imports labels inside a function, so there's no import cycle.
        import ticketmaster as _tm
        avail = _tm.ga_available(ga_rows)
        meta["ga"] = True
        meta["gaStatus"] = _tm.ga_status(avail)
        meta["availSeats"] = avail
        meta["totalSeats"] = (_tm.ga_total(ga_rows) or 0) + total_seated or None
    if meta.get("firstPerfMs"):
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            meta["firstPerfText"] = datetime.fromtimestamp(
                meta["firstPerfMs"] / 1000, ZoneInfo("Asia/Jerusalem")
            ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    payload = {
        "_fetched_at": time.time(),
        "event_code": event_code,
        "perf_code": perf_code,
        "lang": lang,
        "meta": meta,
        "blocks": blocks,
    }
    _cache_path(event_code, perf_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return payload


def get_labels(event_code, perf_code, lang="iw", force=False, missing_block=None):
    """Returns the full label payload, refreshing the cache if it's stale or
    if `missing_block` is set and not present in the cached map.

    Soft-fails: if the network is unreachable and there's no cache, returns
    a stub payload so callers don't crash. The notification just falls back
    to raw block codes in that case.
    """
    path = _cache_path(event_code, perf_code, lang)
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
        or (missing_block and missing_block not in (cached.get("blocks") or {}))
    )
    if not fresh_needed:
        return cached
    try:
        return fetch_fresh(event_code, perf_code, lang)
    except Exception:
        return cached or {
            "event_code": event_code, "perf_code": perf_code, "lang": lang,
            "meta": {}, "blocks": {},
        }


def block_label(labels, code):
    """Hebrew name for a block code, or the raw code if unknown."""
    info = (labels.get("blocks") if labels else None) or {}
    entry = info.get(code) or {}
    return entry.get("name") or code


def block_price(labels, code):
    """Public ILS price for a block, or None."""
    info = (labels.get("blocks") if labels else None) or {}
    return (info.get(code) or {}).get("price")


def event_summary(labels):
    """Return a short '<eventName> · <venueName> · <date>' string for headers."""
    meta = (labels or {}).get("meta") or {}
    parts = []
    if meta.get("eventName"):
        parts.append(meta["eventName"])
    if meta.get("venueName"):
        parts.append(meta["venueName"].strip())
    if meta.get("firstPerfMs"):
        try:
            from datetime import datetime, timezone
            d = datetime.fromtimestamp(meta["firstPerfMs"] / 1000, tz=timezone.utc)
            parts.append(d.strftime("%Y-%m-%d %H:%M"))
        except Exception:
            pass
    return " · ".join(parts)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    ev = sys.argv[1] if len(sys.argv) > 1 else "MBP19"
    pf = sys.argv[2] if len(sys.argv) > 2 else "001"
    payload = fetch_fresh(ev, pf)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
