"""Viagogo auto-pricer: keep our listings just under the cheapest competitor.

PRIMARY competitor source (confirmed 2026-07-04, scripts/
probe_viagogo_compare.py): the magnifier icon on each /Listings event row
POSTs /Listings/MarketDataV3 {eventId} and returns every listing on the
event as HTML — section/row/quantity/price in OUR account currency (USD),
our own rows marked <tr class="owned"> with the listing id. One session
POST: no public page, no URL discovery, no fx. Everything below about the
public event page is the FALLBACK path only.

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
  * the compete pool is per-listing (market panel on /pricer): own section
    always + compete_sections (whole sections, future listings included)
    + compete_include (individual rows, fingerprinted section+row+qty since
    MarketDataV3 gives competitors no id; explicit picks bypass the singles
    filter) - compete_exclude (unticked rows inside selected sections; an
    include beats an exclude). Fingerprints break when a rival's qty
    changes — stale picks silently drop until re-ticked in the panel.
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
  * external price change on inv.viagogo (live price != last_set_price):
    ADOPTED as the new baseline by default (pricer_manual_change_mode=
    'adopt') — a manual raise holds until a competitor undercuts it, then
    auto resumes; dollar-scale moves ping Discord, viagogo's own cent-
    drift adopts silently. Set the mode to 'pause' for the old
    freeze-until-resumed behavior.
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
MARKET_DATA_URL = "https://inv.viagogo.com/Listings/MarketDataV3"
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


def manual_change_mode():
    """What to do when the price on inv differs from what the pricer last
    set: 'adopt' (default) takes it as the new baseline and keeps pricing —
    a raise holds until a competitor undercuts it, then auto resumes;
    'pause' is the old freeze-until-resumed behavior. Adopt also self-heals
    the false pauses caused by viagogo's own price-reduction feature
    shaving 1-4 cents off listings (observed on 13181893170 + 13216838386)."""
    v = (db.setting_get("pricer_manual_change_mode") or "adopt").strip().lower()
    return "pause" if v == "pause" else "adopt"


def ignore_single_competitors():
    """When on (default), listings with only 1 ticket available don't count
    as competitors — a lone single undercutting us isn't real competition
    for a multi-ticket listing, and chasing it just burns margin."""
    return db.setting_get_bool("pricer_ignore_singles", True)


def drop_cap_enabled():
    """Global switch for the sliding-window drop cap. Off = every listing
    may follow the market down to its floor without rate limiting. The
    per-listing no_drop_cap flag does the same for one listing."""
    return db.setting_get_bool("pricer_drop_cap_enabled", True)


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
    for w in writes:
        if w.get("action") == "manual_adopt" and w.get("new_price"):
            # The user's own price change resets the guard: pricer drops
            # are measured from what THEY set, not the pre-manual price
            # (otherwise a big manual cut leaves the pricer rate_limited
            # against a stale baseline — seen live on 13336046972:
            # $495.96 baseline blocking a 2-cent tie-break at $165).
            baseline = w["new_price"]
        elif baseline is None and w.get("old_price"):
            baseline = w["old_price"]
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


# --- inv market data (primary competitor source) --------------------------
# The magnifier icon on each /Listings event row POSTs
# /Listings/MarketDataV3 {eventId, latestServerStamp:0} and gets back an
# HTML modal with EVERY listing on the event: section / row / quantity /
# price+proceeds — all in OUR account currency (USD), with our own rows
# marked <tr data-id="<listing_id>" class="owned">. One session POST, no
# public page, no fx. (Probe: scripts/probe_viagogo_compare.py,
# confirmed live 2026-07-04 on event 161491868 — 15 rows, ours owned.)

MARKET_ROW_RE = re.compile(r'<tr data-id="(\d*)"[^>]*class="([^"]*)"[^>]*>(.*?)</tr>',
                           re.S)
MARKET_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def _parse_market_html(html):
    """MarketDataV3 modal HTML -> [{listing_id, section, row, qty, price,
    venue_area, is_ours}]. td layout: spacer, section, row, quantity, price,
    proceeds, venue area, notes."""
    out = []
    for m in MARKET_ROW_RE.finditer(html):
        lid, cls, body = m.groups()
        tds = MARKET_TD_RE.findall(body)
        if len(tds) < 6:
            continue
        price_txt = _strip_tags(tds[4])
        pm = re.search(r"[\d,]+(?:\.\d+)?", price_txt.replace(",", ""))
        price = float(pm.group(0)) if pm else None
        qty_txt = _strip_tags(tds[3])
        qty = int(qty_txt) if qty_txt.isdigit() else None
        out.append({
            "listing_id": lid or None,
            "section": _strip_tags(tds[1]),
            "row": _strip_tags(tds[2]),
            "qty": qty,
            "price": price,
            # viagogo's own tier grouping (e.g. all VIP-x sections share a
            # "VIP" venue area) — the market panel uses it to flag
            # same-level sections
            "venue_area": _strip_tags(tds[6]) if len(tds) > 6 else "",
            "is_ours": "owned" in cls,
        })
    return out


def fetch_market_listings(page, event_id):
    """All listings for an event straight from the inv session. Returns
    parsed rows, or None on failure (caller falls back to the public page)."""
    resp = page.request.post(MARKET_DATA_URL, form={"eventId": str(event_id)})
    if not resp.ok:
        return None
    rows = _parse_market_html(resp.text())
    return rows or None


def _row_fp(r):
    """Fingerprint for an anonymous competitor row (MarketDataV3 gives no
    listing id for other sellers): section + row label + quantity. Survives
    their price changes; a qty change (partial sale) breaks the match, so
    stale picks silently drop out until re-ticked in the panel."""
    return (
        _norm_section(r.get("section")),
        re.sub(r"\s+", " ", (r.get("row") or "")).strip().casefold(),
        r.get("qty"),
    )


def _cfg_fps(cfg, key):
    """Decode a compete_include/compete_exclude JSON field into a set of
    fingerprints."""
    raw = cfg.get(key)
    if not raw:
        return set()
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    out = set()
    for it in items:
        if isinstance(it, dict):
            out.add((
                _norm_section(it.get("s")),
                re.sub(r"\s+", " ", (it.get("r") or "")).strip().casefold(),
                it.get("q"),
            ))
    return out


def cheapest_competitor(market_rows, section_set, min_competitor_qty=1,
                        include_fps=frozenset(), exclude_fps=frozenset()):
    """Cheapest NON-ours price in a listing's compete pool:
    rows in the selected sections (minus per-listing excludes, singles
    filtered) plus individually included rows from anywhere (explicit picks
    bypass the singles filter). None = no qualifying competitor."""
    best = None
    for r in market_rows:
        if r["is_ours"] or r["price"] is None:
            continue
        fp = _row_fp(r)
        in_pool = False
        if _norm_section(r["section"]) in section_set and fp not in exclude_fps:
            if r["qty"] is None or r["qty"] >= min_competitor_qty:
                in_pool = True
        if fp in include_fps:
            in_pool = True
        if in_pool and (best is None or r["price"] < best):
            best = r["price"]
    return best


def listing_section_set(cfg, row):
    """The sections a listing competes with: its own section always, plus
    any extras the user picked in the market panel (compete_sections JSON)."""
    out = {_norm_section(row.get("section"))}
    raw = cfg.get("compete_sections")
    if raw:
        try:
            extras = json.loads(raw)
            out |= {_norm_section(s) for s in extras if s}
        except (TypeError, ValueError):
            pass
    return out


def refresh_market_snapshot(event_id):
    """On-demand MarketDataV3 fetch for the /pricer market panel's Refresh
    button. Opens its own warmed page (~30s), persists the snapshot, and
    returns the parsed rows. Serialized with ticks via _exclusive_browser."""
    with _exclusive_browser(), sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        try:
            rows = fetch_market_listings(page, event_id)
        finally:
            try:
                page.close()
            except Exception:
                pass
    if rows is None:
        raise PricerError(f"MarketDataV3 returned nothing for event {event_id}")
    db.market_snapshot_set(event_id, json.dumps(rows), _now_iso())
    return rows


def market_section_prices(market_rows, min_competitor_qty=1):
    """Per-section competitor anchor from MarketDataV3 rows. Returns
    {norm_section: {"cheapest_competitor_usd": float|None}} — None means the
    section has listings but no qualifying competitor (all ours / all
    singles when min_competitor_qty=2)."""
    sections = {}
    for r in market_rows:
        sec = _norm_section(r["section"])
        cur = sections.setdefault(sec, {"cheapest_competitor_usd": None})
        if r["is_ours"] or r["price"] is None:
            continue
        if r["qty"] is not None and r["qty"] < min_competitor_qty:
            continue
        if (cur["cheapest_competitor_usd"] is None
                or r["price"] < cur["cheapest_competitor_usd"]):
            cur["cheapest_competitor_usd"] = r["price"]
    return sections


# --- public event page (fallback competitor source) ------------------------

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


def _public_sections_fallback(context, event_id, event_name, our_ids_event,
                              live_by_id, min_qty):
    """Legacy competitor source (public event page + fx calibration),
    normalized to the market_section_prices shape. Only used when
    MarketDataV3 fails. Returns {sec: {"cheapest_competitor_usd": ...}} or
    None when the whole path fails."""
    try:
        public_url = discover_public_url(context, event_id, event_name)
        if not public_url:
            return None
        index_data = fetch_event_grid(context, public_url)
        if not index_data:
            return None
        raw_sections, meta = grid_section_prices(index_data, our_ids_event,
                                                 min_competitor_qty=min_qty)
        fx, _ = derive_fx(meta, live_by_id)
        if not fx:
            return None
        return {
            sec: {"cheapest_competitor_usd": (info["cheapest_competitor_raw"] / fx
                                              if info["cheapest_competitor_raw"] else None)}
            for sec, info in raw_sections.items()
        }
    except Exception as e:
        print(f"[pricer] public fallback failed for {event_id}: {e}")
        return None


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
                    # Transient render miss vs genuinely deleted listing:
                    # the mirror's last_seen_at decides. Not seen by any of
                    # the ~100 daily reads for 6h+ = the user deleted it on
                    # inv — auto-disable so it stops ticking (and stops
                    # spamming fetch_failed).
                    mirror = db.viagogo_get(lid)
                    seen = (mirror or {}).get("last_seen_at") or ""
                    stale_cutoff = (datetime.now(timezone.utc)
                                    - timedelta(hours=6)).isoformat()
                    if not mirror or seen < stale_cutoff:
                        db.pricer_config_set(lid, {
                            "enabled": 0, "paused": 0, "paused_reason": None,
                        }, now)
                        _log({"listing_id": lid, "action": "disabled_gone",
                              "detail": "listing no longer on inv.viagogo "
                                        "(not seen 6h+) — auto-disabled"})
                    else:
                        _log({"listing_id": lid, "action": "fetch_failed",
                              "detail": "listing not on inv Listings page"})
                    counters["skipped"] += 1
                    continue
                last_set = cfg.get("last_set_price")
                if last_set is not None and abs(row["price"] - last_set) >= PRICE_EPSILON:
                    if manual_change_mode() == "pause":
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
                    # Adopt mode (default): the changed price — the user's
                    # deliberate raise, or viagogo's own cent-shaving price
                    # reduction feature — becomes the new baseline and the
                    # listing stays live. Lower-only pricing then does the
                    # right thing on a raise: hold while nobody is under
                    # us, resume undercutting the moment someone is.
                    db.pricer_config_set(lid, {
                        "last_set_price": row["price"], "last_set_at": now,
                    }, now)
                    _log({"listing_id": lid, "event_id": row.get("event_id"),
                          "section": row.get("section"),
                          "old_price": last_set, "new_price": row["price"],
                          "action": "manual_adopt",
                          "detail": "external price change adopted as baseline"})
                    if abs(row["price"] - last_set) >= 0.50:
                        # a real (dollar-scale) manual move — worth a ping;
                        # viagogo's own 1-4 cent drift stays silent
                        try:
                            notify.notify_pricer_adopted({
                                "event_name": row.get("event_name"),
                                "section": row.get("section"),
                                "listing_id": lid,
                                "expected": last_set,
                                "seen": row["price"],
                            })
                        except Exception as e:
                            print(f"[pricer] adopt notify failed: {e}")
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
                min_qty = 2 if ignore_single_competitors() else 1

                # Primary source: MarketDataV3 on the inv session — USD,
                # own rows marked, one POST. Public event page only as a
                # fallback if viagogo ever breaks/denies the endpoint.
                market = None
                sections = None       # fallback path only
                source = "market"
                try:
                    market = fetch_market_listings(page, event_id)
                except Exception as e:
                    print(f"[pricer] MarketDataV3 failed for {event_id}: {e}")
                if market:
                    try:
                        db.market_snapshot_set(event_id, json.dumps(market), now)
                    except Exception as e:
                        print(f"[pricer] snapshot write failed (non-fatal): {e}")
                else:
                    source = "public"
                    sections = _public_sections_fallback(
                        context, event_id, event_name, our_ids_event,
                        live_by_id, min_qty)
                    if sections is None:
                        for lid, cfg, row in members:
                            _log({"listing_id": lid, "event_id": event_id,
                                  "section": row.get("section"),
                                  "action": "fetch_failed",
                                  "detail": "market data and public page both failed"})
                        counters["skipped"] += len(members)
                        continue

                # -- per-listing anchor: cheapest competitor across the
                # listing's compete set (own section + panel picks). Own
                # rows are never anchors, so same-set listings of ours get
                # identical targets and never undercut each other. If we
                # already lead but by less than the undercut this tightens
                # to a clear lead; a comfortable lead computes a raise
                # target, which stays skipped while raising is off.
                for lid, cfg, row in members:
                    if counters["changed"] >= MAX_CHANGES_PER_TICK:
                        break
                    section_set = listing_section_set(cfg, row)
                    if market:
                        include_fps = _cfg_fps(cfg, "compete_include")
                        competitor_usd = cheapest_competitor(
                            market, section_set, min_competitor_qty=min_qty,
                            include_fps=include_fps,
                            exclude_fps=_cfg_fps(cfg, "compete_exclude"))
                        any_rows = any(
                            _norm_section(r["section"]) in section_set
                            or _row_fp(r) in include_fps
                            for r in market)
                    else:
                        cands = [sections[s]["cheapest_competitor_usd"]
                                 for s in section_set if s in sections]
                        cands = [c for c in cands if c is not None]
                        competitor_usd = min(cands) if cands else None
                        any_rows = any(s in sections for s in section_set)
                    if competitor_usd is None:
                        _log({"listing_id": lid, "event_id": event_id,
                              "section": row.get("section"),
                              "action": "skip_alone",
                              "detail": ("no qualifying competitors in "
                                         f"{len(section_set)} section(s)"
                                         if any_rows else
                                         "compete sections not in market data")})
                        counters["skipped"] += 1
                        continue
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
                    if target < row["price"] and drop_cap_enabled() \
                            and not cfg.get("no_drop_cap"):
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
                              "detail": f"would {action} (src {source})"})
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
                          "detail": f"src {source}"})
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
