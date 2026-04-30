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

No login, no captcha — these endpoints are anonymous (the Cloudflare
Turnstile + vee-crm POSTs that show up in the SPA traffic guard the
checkout flow, not the listing endpoints).
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


class KupatError(RuntimeError):
    pass


def _browse_capture(feature_id, presentation_id, want):
    """Single page-load that captures whichever of the SPA's XHRs you ask for.

    `want` is a subset of {"seats", "presentation", "seatplan"}. Returns a
    dict with whatever was captured before timeout. Closes the browser on
    completion. We need this because hitting the kupat API with raw urllib
    or even patchright's request context returns 403 — the API only accepts
    requests that originate from the in-page fetch chain (likely a JS
    challenge / TLS fingerprint check at the CDN). Going through a real
    browser dodges all of that for the price of ~4 seconds per call.
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        raise KupatError(
            "kupat watcher needs patchright + Chromium. Run "
            "`.venv\\Scripts\\python -m patchright install chromium`."
        ) from e

    url = perf_url(feature_id, presentation_id)
    captured = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(locale="he-IL")
        page = ctx.new_page()

        def on_response(resp):
            try:
                u = resp.url
                if "seats" in want and "/seats-status" in u and f"presentationId={presentation_id}" in u:
                    captured["seats"] = (resp.json() or {}).get("seats") or []
                elif "presentation" in want and f"/presentations/feature/{feature_id}/{presentation_id}" in u:
                    captured["presentation"] = (resp.json() or {}).get("presentation") or {}
                elif "seatplan" in want and "/seats/seatplan" in u:
                    captured["seatplan"] = resp.json()
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            raise KupatError(f"failed to load kupat booking page: {e}") from e

        # Poll briefly until everything we want is present (or 12s elapses).
        deadline = time.time() + 12
        while time.time() < deadline:
            if all(k in captured for k in want):
                break
            page.wait_for_timeout(200)

        browser.close()

    missing = [k for k in want if k not in captured]
    if missing:
        # Soft-fail: return whatever we got. Caller decides whether to error.
        captured["_missing"] = missing
    return captured


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
    array). Costs ~4s per call because the kupat API rejects out-of-page
    requests; we run a headless browser to capture the seats-status XHR.
    """
    captured = _browse_capture(feature_id, presentation_id, want={"seats"})
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
            "totalSeats": total_seated or None,
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
