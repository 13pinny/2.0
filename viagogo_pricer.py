"""Viagogo auto-pricer: keep our listings just under the cheapest competitor.

Empirical facts (confirmed 2026-07-02 against Hanan Ben Ari 30 Jul, event
161441108, listing 13216838386 — scripts/probe_viagogo_public.py and
scripts/probe_viagogo_edit_price.py):

* Public event URL — the short forms (/E-<id>, /ww/E-<id>) all bounce to the
  homepage. Only the fully-slugged path resolves, e.g.
  https://www.viagogo.com/Concert-Tickets/Other-Concerts/Hanan-Ben-Ari-Tickets/E-161441108
  The slug is NOT guessable (Hanan Ben Ari sits under "Other-Concerts", not
  "Rock-and-Pop"), so we discover it by typing the event name into the
  homepage search box and reading the /search/groupedsearch JSON the page
  fires — its event entries carry absolute URLs ending in /E-<id>.
  Discovered URLs are cached in tm_cache/viagogo_public_urls.json.

* The event page embeds its full listing grid server-side in
  <script id="index-data" type="application/json"> — parse it from the raw
  document response (React strips the tag from the DOM after hydration).
  grid.items[]: id (== our inv listing id!), section, sectionId,
  availableTickets, rawPrice, listingCurrencyCode, buyerCurrencyCode.
  No listings XHR fires on load; the doc is the data source.

* rawPrice is the fee-less per-ticket price in BUYER currency (ILS for
  Israeli events): our $98.99 listing showed rawPrice 295.54 / "₪296"
  (fx ≈ 2.986, viagogo's internal rate — NOT the market rate). We derive
  fx per event from our own listing (rawPrice / our USD WebsitePrice) and
  fall back to the last-known fx stored in settings.

* ?Quantity=0 requests the grid unfiltered ("Any"); the page otherwise
  defaults to a quantity=2 filter that hides non-matching listings
  (grid.quantity / grid.totalFilteredListings vs totalListings).

* Grid page 1 has pageSize 10. If totalFilteredListings > pageSize we may
  not see every listing; the tick logs that and still acts on what it sees
  (cheapest listings sort first).

* Price edit — the /Listings/SaveListing form REPLAY does NOT work: the
  returned form HTML doesn't carry <select> selected values or the real
  FaceValue, so a raw replay fails server validation ("CurrencyCode is
  required", "SplitType is required", "'true' is not valid for FaceValue").
  What works (probe-confirmed live, $98.99→$98.98→$98.99) is the UI modal:
  click the listing row's td.editCol pencil, fill()
  input[name="Listing.WebsitePrice"] + input[name="Listing.Proceeds"], click
  #btnSaveDetails — the app's own JS assembles a valid save POST. Same
  philosophy as viagogo_listing.create_draft_listing: real input events,
  never raw JS value-setting. Proceeds = price * PROCEEDS_RATE (0.90).
  We still read the Details form first for the CanEditPrice guard.

Pricing rules (user requirements):
  * target = cheapest COMPETITOR (USD, own listings excluded by id) minus
    the undercut (setting pricer_undercut, default $0.04 ≈ keeps the
    buyer-currency display clearly below after fx rounding)
  * this applies even when we are already the section's cheapest: if our
    lead over the next competitor is thinner than the undercut we TIGHTEN
    down to competitor - undercut (seen live on Ishay Ribo/Amir Dadon:
    16 GA listings with a competitor exactly 1 buyer-cent above ours).
    A comfortable lead computes a raise target and is skipped silently
    while raising is off.
  * never below the per-listing floor; floor is mandatory to enable
  * sliding-window drop cap: at most pricer_max_drop_pct (default 15%)
    below the listing's price at the start of the last
    pricer_drop_window_hours (default 12h). Partial room clamps the write
    (action rate_clamp); no room skips it (rate_limited) and notifies once
    so the user can take over from /pricer. Capacity returns as old drops
    age out of the window.
  * lower-only for now; raise needs BOTH pricer_allow_raise_global and the
    listing's allow_raise flag (architecture in place, off at launch)
  * no competitors visible in the section: do nothing (skip_alone)
  * single-ticket listings don't count as competitors while
    pricer_ignore_singles is on (default ON) — a lone single undercutting
    us isn't real competition for a multi-ticket listing
  * manual price change on inv.viagogo (live price != last_set_price)
    pauses the listing + notifies; resume from the dashboard re-adopts
  * dry-run mode (pricer_dry_run, default ON) logs/notifies without writing
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

from patchright.sync_api import sync_playwright

import db
import notify
import scraper
import viagogo_listing
from viagogo_listing import PROCEEDS_RATE, _exclusive_browser

DETAILS_URL = "https://inv.viagogo.com/Listings/Details"
PUBLIC_HOME = "https://www.viagogo.com/"

UNDERCUT_DEFAULT = 0.04
MAX_CHANGES_PER_TICK = 20
PRICE_EPSILON = 0.005  # floats within half a cent are "the same price"

URL_CACHE_PATH = os.path.join("tm_cache", "viagogo_public_urls.json")
INDEX_DATA_RE = re.compile(
    r'<script id="index-data" type="application/json">\s*(.*?)\s*</script>',
    re.S,
)


class PricerError(RuntimeError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm_section(s):
    """Match inv section names against public grid section names.

    inv renders seated sections multi-line ('H\\nRow 11 , Seat …'); the grid
    uses just the section ('H'). First line, collapsed whitespace, casefold.
    """
    first = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return re.sub(r"\s+", " ", first).strip().casefold()


# --- settings ------------------------------------------------------------

def undercut_amount():
    try:
        return float(db.setting_get("pricer_undercut") or UNDERCUT_DEFAULT)
    except (TypeError, ValueError):
        return UNDERCUT_DEFAULT


def master_enabled():
    return db.setting_get_bool("pricer_master_enabled", False)


def dry_run_enabled():
    return db.setting_get_bool("pricer_dry_run", True)


def allow_raise_global():
    return db.setting_get_bool("pricer_allow_raise_global", False)


def ignore_single_competitors():
    """When on (default), listings with only 1 ticket available don't count
    as competitors — a lone single undercutting us isn't real competition
    for a multi-ticket listing, and chasing it just burns margin."""
    return db.setting_get_bool("pricer_ignore_singles", True)


def max_drop_pct():
    """Hard brake: max total decrease per listing inside the drop window."""
    try:
        return float(db.setting_get("pricer_max_drop_pct") or 15.0)
    except (TypeError, ValueError):
        return 15.0


def drop_window_hours():
    try:
        return float(db.setting_get("pricer_drop_window_hours") or 12.0)
    except (TypeError, ValueError):
        return 12.0


def drop_guard(listing_id, current_price):
    """Return (min_allowed_price, baseline) for the sliding-window drop cap.

    baseline = the listing's price at the start of the window (old_price of
    the first real write inside it; current price if the pricer hasn't
    written in the window). min_allowed = baseline * (1 - max_drop_pct%).
    The window slides, so capacity comes back as old drops age out.
    """
    since = (datetime.now(timezone.utc)
             - timedelta(hours=drop_window_hours())).isoformat()
    writes = db.pricer_writes_since(str(listing_id), since)
    baseline = None
    if writes and writes[0].get("old_price"):
        baseline = writes[0]["old_price"]
    if not baseline:
        baseline = current_price
    return baseline * (1.0 - max_drop_pct() / 100.0), baseline


# --- live inventory read -------------------------------------------------

def read_live_listings(page):
    """Re-scrape /Listings on an already-warmed page (viagogo_listing.
    _open_listings_page). The 15-min tick always works from live prices —
    the hourly scrape mirror is up to an hour stale."""
    page.wait_for_selector("tr.eventRow", timeout=30000)
    page.wait_for_timeout(1500)
    return scraper._extract_viagogo_rows(page)


# --- public event page ---------------------------------------------------

def _load_url_cache():
    try:
        with open(URL_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_url_cache(cache):
    os.makedirs(os.path.dirname(URL_CACHE_PATH), exist_ok=True)
    with open(URL_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)


def discover_public_url(context, event_id, event_name):
    """Find the public www.viagogo.com event URL for an inv event id by
    driving the homepage search box (real keystrokes — same philosophy as
    viagogo_listing) and reading the groupedsearch JSON responses."""
    cache = _load_url_cache()
    hit = cache.get(str(event_id))
    if hit:
        return hit

    page = context.new_page()
    captured = []
    page.on(
        "response",
        lambda r: captured.append(r) if "groupedsearch" in r.url else None,
    )
    try:
        page.goto(PUBLIC_HOME, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        box = page.query_selector("input[placeholder*=earch]")
        if not box:
            raise PricerError("viagogo homepage search box not found")
        box.click()
        page.keyboard.type(event_name, delay=60)
        page.wait_for_timeout(5000)

        needle = f"/E-{event_id}"
        for resp in captured:
            try:
                body = resp.text()
            except Exception:
                continue
            for m in re.finditer(r'"url":"([^"]+)"', body):
                url = m.group(1).replace("\\/", "/")
                if needle in url:
                    url = url.split("?")[0]
                    if url.startswith("/"):
                        url = "https://www.viagogo.com" + url
                    cache[str(event_id)] = url
                    _save_url_cache(cache)
                    return url
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_event_grid(context, public_url):
    """Load the public event page (quantity filter off) and return the parsed
    index-data blob, or None on failure. Parsed from the raw document
    response — the script tag does not survive hydration."""
    page = context.new_page()
    doc_bodies = []

    def on_response(resp):
        try:
            if resp.request.resource_type == "document" and "viagogo.com" in resp.url:
                doc_bodies.append(resp)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(public_url + "?Quantity=0", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if f"/E-" not in page.url:
            # bounced to artist/home page — event no longer publicly listed
            return None
        for resp in doc_bodies:
            try:
                m = INDEX_DATA_RE.search(resp.text())
            except Exception:
                continue
            if m:
                return json.loads(m.group(1))
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def grid_section_prices(index_data, our_listing_ids, min_competitor_qty=1):
    """Digest a grid into per-section price info.

    Returns (sections, meta) where sections maps normalized section name ->
    {"cheapest_raw": float (overall), "cheapest_is_ours": bool,
     "cheapest_competitor_raw": float|None (cheapest NON-ours; the pricing
     anchor — None means we're alone in the section),
     "our_raw": float|None (cheapest of OUR listings in that section)}
    and meta carries fx-calibration inputs and coverage counts.

    min_competitor_qty filters the competitor anchor: with 2, a listing
    holding a single ticket (grid availableTickets == 1) doesn't count as
    competition. Unknown quantities are kept (safer to compete than to
    ignore a real rival).
    """
    grid = (index_data or {}).get("grid") or {}
    items = grid.get("items") or []
    ours = {str(i) for i in our_listing_ids}
    sections = {}
    our_items = []
    for it in items:
        sec = _norm_section(it.get("section"))
        raw = it.get("rawPrice")
        if raw is None:
            continue
        lid = str(it.get("id"))
        is_ours = lid in ours
        if is_ours:
            our_items.append(it)
        cur = sections.setdefault(sec, {
            "cheapest_raw": None, "cheapest_is_ours": False,
            "cheapest_competitor_raw": None, "our_raw": None,
        })
        if cur["cheapest_raw"] is None or raw < cur["cheapest_raw"]:
            cur.update(cheapest_raw=raw, cheapest_is_ours=is_ours)
        qty = it.get("availableTickets")
        big_enough = qty is None or qty >= min_competitor_qty
        if not is_ours and big_enough and (cur["cheapest_competitor_raw"] is None
                                           or raw < cur["cheapest_competitor_raw"]):
            cur["cheapest_competitor_raw"] = raw
        if is_ours and (cur["our_raw"] is None or raw < cur["our_raw"]):
            cur["our_raw"] = raw
    meta = {
        "total_listings": index_data.get("totalListings"),
        "filtered": grid.get("totalFilteredListings"),
        "page_size": grid.get("pageSize"),
        "buyer_currency": (items[0].get("buyerCurrencyCode") if items else None),
        "our_items": our_items,
    }
    return sections, meta


def derive_fx(meta, live_by_id):
    """viagogo's internal buyer-currency-per-USD rate, from one of OUR
    listings present in the grid (rawPrice / our USD WebsitePrice). Persisted
    to settings as pricer_last_fx for events where none of ours are visible."""
    for it in meta.get("our_items") or []:
        lid = str(it.get("id"))
        our = live_by_id.get(lid)
        raw = it.get("rawPrice")
        if our and our.get("price") and raw:
            fx = raw / our["price"]
            if 0.1 < fx < 100:
                db.setting_set("pricer_last_fx", f"{fx:.6f}", _now_iso())
                return fx, "calibrated"
    try:
        fx = float(db.setting_get("pricer_last_fx") or 0)
        if fx > 0:
            return fx, "cached"
    except (TypeError, ValueError):
        pass
    return None, "unavailable"


# --- pricing rule ---------------------------------------------------------

def compute_target(cheapest_competitor, current_price, floor, allow_raise,
                   undercut):
    """THE pricing rule, pure and unit-testable.

    All prices in USD (our listing currency). Returns (target|None, action):
    action in {'lower', 'raise', 'floor_clamp', 'noop', 'skip_raise'}.
    None target = nothing to write. A floor at/above our current price comes
    out as 'noop' (already there) or 'skip_raise' (would have to go up).
    """
    target = round(cheapest_competitor - undercut, 2)
    clamped = False
    if floor is not None and target < floor:
        target = round(floor, 2)
        clamped = True
    if abs(target - current_price) < PRICE_EPSILON:
        return None, "noop"
    if target > current_price:
        if not allow_raise:
            return None, "skip_raise"
        return target, "raise"
    return target, ("floor_clamp" if clamped else "lower")


# --- price write ----------------------------------------------------------

def _parse_edit_form(html):
    """Ordered [(name, value, type, checked)] + form action from the
    /Listings/Details HTML. Order and duplicates preserved — see module
    docstring on the IsPublishToViagogo checkbox pair."""
    fields = []
    for m in re.finditer(r"<(input|select|textarea)\b[^>]*>", html, re.I):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        typ = re.search(r'type="([^"]*)"', tag)
        fields.append({
            "name": name.group(1),
            "value": value.group(1) if value else "",
            "type": (typ.group(1) if typ else m.group(1)).lower(),
            "checked": bool(re.search(r"\bchecked\b", tag, re.I)),
        })
    action = None
    fm = re.search(r'<form\b[^>]*action="([^"]*)"', html, re.I)
    if fm:
        action = fm.group(1)
    return action, fields


def set_listing_price(page, listing_id, new_price):
    """Change one listing's price via the row's edit modal (see module
    docstring — the raw form replay fails validation; the app's own modal JS
    is the only proven writer). Returns the price that actually stuck
    (re-read from the reloaded Listings page) — the caller stores THAT as
    last_set_price so server-side rounding can't false-trigger manual-change
    detection next tick. `page` must be an open, rendered /Listings page."""
    resp = page.request.post(DETAILS_URL, form={"listingId": str(listing_id)})
    if not resp.ok:
        raise PricerError(f"Details POST {resp.status} for {listing_id}")
    _, fields = _parse_edit_form(resp.text())
    can_edit = next((f["value"] for f in fields
                     if f["name"] == "Listing.CanEditPrice"), "")
    if can_edit.lower() != "true":
        raise PricerError(f"listing {listing_id} is not price-editable")

    proceeds = round(new_price * PROCEEDS_RATE, 2)

    clicked = page.evaluate(
        """(id) => {
  const tr = document.querySelector(`tr[data-id="${id}"]`);
  if (!tr) return false;
  const cell = tr.querySelector('td.editCol, td.deets');
  if (!cell) return false;
  cell.click();
  return true;
}""",
        str(listing_id),
    )
    if not clicked:
        raise PricerError(f"edit cell not found for listing {listing_id}")
    try:
        page.wait_for_selector('input[name="Listing.WebsitePrice"]',
                               state="visible", timeout=15000)
    except Exception:
        raise PricerError(f"edit modal never opened for {listing_id}")
    page.fill('input[name="Listing.WebsitePrice"]', f"{new_price:.2f}")
    page.fill('input[name="Listing.Proceeds"]', f"{proceeds:.2f}")
    save = page.query_selector("#btnSaveDetails")
    if not save:
        raise PricerError(f"save button not found in edit modal for {listing_id}")
    save.click()
    page.wait_for_timeout(3000)

    # Verify against the re-rendered row; return what actually stuck.
    page.reload(wait_until="domcontentloaded")
    rows = read_live_listings(page)
    row = next((r for r in rows if str(r["id"]) == str(listing_id)), None)
    if row is None:
        raise PricerError(f"listing {listing_id} vanished after save")
    stuck = row.get("price")
    if stuck is None or abs(stuck - new_price) > 0.05:
        raise PricerError(
            f"price did not stick for {listing_id}: wanted {new_price}, "
            f"row shows {stuck}"
        )
    return stuck


# --- the tick -------------------------------------------------------------

def _log(entry):
    db.pricer_log_add(entry, _now_iso())


def run_pricer_tick(dry_run=None):
    """One full pricing pass. Returns counters for the status dict."""
    counters = {"changed": 0, "paused": 0, "skipped": 0, "errors": 0,
                "dry_run": None, "eligible": 0}
    if not master_enabled():
        return counters
    if dry_run is None:
        dry_run = dry_run_enabled()
    counters["dry_run"] = bool(dry_run)

    configs = {lid: c for lid, c in db.pricer_config_all().items()
               if c.get("enabled") and not c.get("paused")}
    if not configs:
        return counters

    undercut = undercut_amount()
    raise_global = allow_raise_global()
    now = _now_iso()

    with _exclusive_browser(), sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        context = page.context
        try:
            live = read_live_listings(page)
            live_by_id = {str(r["id"]): r for r in live}
            try:
                db.upsert_viagogo(live, now)  # freshen the dashboard mirror
            except Exception as e:
                print(f"[pricer] mirror upsert failed (non-fatal): {e}")

            # -- manual-change pass (before any competitor work) --
            eligible = {}
            for lid, cfg in configs.items():
                row = live_by_id.get(lid)
                if row is None or not row.get("price"):
                    _log({"listing_id": lid, "action": "fetch_failed",
                          "detail": "listing not on inv Listings page"})
                    counters["skipped"] += 1
                    continue
                last_set = cfg.get("last_set_price")
                if last_set is not None and abs(row["price"] - last_set) >= PRICE_EPSILON:
                    db.pricer_config_set(lid, {
                        "paused": 1, "paused_reason": "manual_change",
                        "paused_at": now,
                    }, now)
                    _log({"listing_id": lid, "event_id": row.get("event_id"),
                          "section": row.get("section"),
                          "old_price": last_set, "new_price": row["price"],
                          "action": "pause_manual",
                          "detail": "price on inv differs from last auto-set"})
                    try:
                        notify.notify_pricer_paused({
                            "event_name": row.get("event_name"),
                            "section": row.get("section"),
                            "listing_id": lid,
                            "expected": last_set,
                            "seen": row["price"],
                        })
                    except Exception as e:
                        print(f"[pricer] pause notify failed: {e}")
                    counters["paused"] += 1
                    continue
                eligible[lid] = (cfg, row)
            counters["eligible"] = len(eligible)

            # -- group by event, one public fetch per event --
            by_event = {}
            for lid, (cfg, row) in eligible.items():
                by_event.setdefault(str(row["event_id"]), []).append((lid, cfg, row))

            for event_id, members in by_event.items():
                if counters["changed"] >= MAX_CHANGES_PER_TICK:
                    _log({"listing_id": "-", "event_id": event_id,
                          "action": "error",
                          "detail": "circuit breaker: max changes per tick"})
                    counters["errors"] += 1
                    break

                event_name = members[0][2].get("event_name") or ""
                our_ids_event = [str(r["id"]) for r in live
                                 if str(r.get("event_id")) == event_id]
                try:
                    public_url = discover_public_url(context, event_id, event_name)
                except Exception as e:
                    public_url = None
                    print(f"[pricer] url discovery failed for {event_id}: {e}")
                if not public_url:
                    for lid, cfg, row in members:
                        _log({"listing_id": lid, "event_id": event_id,
                              "section": row.get("section"),
                              "action": "fetch_failed",
                              "detail": "no public event URL"})
                    counters["skipped"] += len(members)
                    continue

                try:
                    index_data = fetch_event_grid(context, public_url)
                except Exception as e:
                    index_data = None
                    print(f"[pricer] grid fetch failed for {event_id}: {e}")
                if not index_data:
                    for lid, cfg, row in members:
                        _log({"listing_id": lid, "event_id": event_id,
                              "section": row.get("section"),
                              "action": "fetch_failed",
                              "detail": f"no grid data at {public_url}"})
                    counters["skipped"] += len(members)
                    continue

                min_qty = 2 if ignore_single_competitors() else 1
                sections, meta = grid_section_prices(index_data, our_ids_event,
                                                     min_competitor_qty=min_qty)
                if meta["filtered"] and meta["page_size"] and \
                        meta["filtered"] > meta["page_size"]:
                    print(f"[pricer] event {event_id}: grid shows "
                          f"{meta['page_size']} of {meta['filtered']} listings "
                          "— cheapest sort first, coverage partial")

                fx, fx_source = derive_fx(meta, live_by_id)
                if not fx:
                    for lid, cfg, row in members:
                        _log({"listing_id": lid, "event_id": event_id,
                              "section": row.get("section"),
                              "action": "fetch_failed",
                              "detail": "no fx calibration available"})
                    counters["skipped"] += len(members)
                    continue

                # -- shared target per (event, section) group --
                by_section = {}
                for lid, cfg, row in members:
                    by_section.setdefault(_norm_section(row["section"]), []).append((lid, cfg, row))

                for sec, group in by_section.items():
                    info = sections.get(sec)
                    if not info:
                        for lid, cfg, row in group:
                            _log({"listing_id": lid, "event_id": event_id,
                                  "section": row.get("section"),
                                  "action": "skip_alone",
                                  "detail": "section not in public grid "
                                            "(no visible listings)"})
                        counters["skipped"] += len(group)
                        continue
                    if info["cheapest_competitor_raw"] is None:
                        # every visible listing in the section is ours
                        for lid, cfg, row in group:
                            _log({"listing_id": lid, "event_id": event_id,
                                  "section": row.get("section"),
                                  "action": "skip_alone",
                                  "detail": "no competitors in section"})
                        counters["skipped"] += len(group)
                        continue

                    # Anchor on the cheapest NON-ours listing. If we already
                    # lead but by less than the undercut, this tightens our
                    # price to a clear lead; a comfortable lead computes a
                    # raise target, which stays skipped while raising is off.
                    competitor_usd = info["cheapest_competitor_raw"] / fx
                    for lid, cfg, row in group:
                        if counters["changed"] >= MAX_CHANGES_PER_TICK:
                            break
                        allow_raise = bool(raise_global and cfg.get("allow_raise"))
                        target, action = compute_target(
                            competitor_usd, row["price"], cfg.get("floor_price"),
                            allow_raise, undercut,
                        )
                        base = {
                            "listing_id": lid, "event_id": event_id,
                            "section": row.get("section"),
                            "old_price": row["price"],
                            "competitor_price": round(competitor_usd, 2),
                        }
                        if target is None:
                            # noop and skip_raise recur every tick for
                            # healthy listings — count them but don't write
                            # a log row each time
                            counters["skipped"] += 1
                            continue
                        if target < row["price"]:
                            # sliding-window drop cap (default 15% per 12h)
                            min_allowed, baseline = drop_guard(lid, row["price"])
                            if target < min_allowed - PRICE_EPSILON:
                                clamped = round(min_allowed, 2)
                                if clamped >= row["price"] - PRICE_EPSILON:
                                    # no room left to move this window
                                    prev = db.pricer_log_recent(1, lid)
                                    _log({**base, "new_price": target,
                                          "action": "rate_limited",
                                          "detail": f"drop cap {max_drop_pct():g}%/"
                                                    f"{drop_window_hours():g}h hit "
                                                    f"(baseline ${baseline:.2f})"})
                                    if not (prev and prev[0]["action"] == "rate_limited"):
                                        try:
                                            notify.notify_pricer_rate_limited({
                                                "event_name": row.get("event_name"),
                                                "section": row.get("section"),
                                                "listing_id": lid,
                                                "current": row["price"],
                                                "wanted": target,
                                                "baseline": baseline,
                                                "pct": max_drop_pct(),
                                                "hours": drop_window_hours(),
                                            })
                                        except Exception as e:
                                            print(f"[pricer] rate-limit notify failed: {e}")
                                    counters["skipped"] += 1
                                    continue
                                target = clamped
                                action = "rate_clamp"
                        if dry_run:
                            _log({**base, "new_price": target,
                                  "action": "dry_run", "dry_run": 1,
                                  "detail": f"would {action} (fx {fx:.4f} {fx_source})"})
                            try:
                                notify.notify_pricer_change({
                                    "event_name": row.get("event_name"),
                                    "section": row.get("section"),
                                    "listing_id": lid,
                                    "old_price": row["price"],
                                    "new_price": target,
                                    "competitor_price": round(competitor_usd, 2),
                                    "floor": cfg.get("floor_price"),
                                    "dry_run": True,
                                })
                            except Exception as e:
                                print(f"[pricer] notify failed: {e}")
                            counters["changed"] += 1
                            continue
                        if not master_enabled():  # kill switch is live mid-tick
                            return counters
                        try:
                            stuck = set_listing_price(page, lid, target)
                        except Exception as e:
                            _log({**base, "new_price": target, "action": "error",
                                  "detail": str(e)[:300]})
                            counters["errors"] += 1
                            continue
                        db.pricer_config_set(lid, {
                            "last_set_price": stuck, "last_set_at": _now_iso(),
                        }, _now_iso())
                        _log({**base, "new_price": stuck, "action": action,
                              "detail": f"fx {fx:.4f} {fx_source}"})
                        try:
                            notify.notify_pricer_change({
                                "event_name": row.get("event_name"),
                                "section": row.get("section"),
                                "listing_id": lid,
                                "old_price": row["price"],
                                "new_price": stuck,
                                "competitor_price": round(competitor_usd, 2),
                                "floor": cfg.get("floor_price"),
                                "dry_run": False,
                            })
                        except Exception as e:
                            print(f"[pricer] notify failed: {e}")
                        counters["changed"] += 1
        finally:
            try:
                page.close()
            except Exception:
                pass
    return counters
