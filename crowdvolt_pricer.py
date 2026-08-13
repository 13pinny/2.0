"""CrowdVolt auto-pricer: keep our asks just under the cheapest competitor
for the same event + ticket type.

Empirical facts (probed 2026-07-22 through the CDP Chrome, cv_probe/):

* Everything is a JSON API, but Cloudflare 503s any out-of-browser request —
  so ALL calls run as in-page fetch() from a page parked on www.crowdvolt.com
  (same trick as kupat's in-page harvest, but cleaner: no UI clicking at all,
  including the price WRITE).

* Auth = `Authorization: Bearer <stytch_session cookie value>` plus the
  `x-fingerprint-id` (localStorage `fingerprint_id`) and `x-pokedex` headers.
  what.crowdvolt.com (the book) 503s without the fingerprint headers;
  api.crowdvolt.com 302s to /signup without the bearer. All three values are
  read from the live page at call time, so token rotation is free.

* Our active asks: GET api.crowdvolt.com/api/buy_sell_history/sell_active
  ?limit=N&offset=M — [{ask_uqid, base_card:{event_uqid, event_name,
  ticket_type, price, qty_left, all_in_price (TOTAL, not per-ticket), ...},
  ask_status: 'unfilled'}].

* The order book (ALL sellers): GET what.crowdvolt.com/api/book/get
  ?event_uqid=X — {buy: [...bids], sell: [...asks]}, each row carrying
  uqid (== ask_uqid for our own rows!), user_uqid, price (seller's price),
  all_in_price (buyer-facing, integer), qty, ticket_type, ticket_type_uqid.
  Own rows are excluded by matching uqid against our sell_active set.

* Buyers see all-in prices sorted ascending; all_in = round(price * (1+fee))
  per event (52→54, 94→98, 95→99...; fee ~4%). Within one event+type the
  seller→all-in mapping is monotonic, so undercutting the competitor's
  SELLER price undercuts their all-in too. Displayed all-ins are integers,
  hence the default undercut of $1 (setting cv_pricer_undercut) — a cent
  undercut can round to the same displayed all-in. GET api/fee_calc/get
  ?price=&qty=&is_buy=false&event_uqid=&ask_uqid= returns {fee, buyer_price}
  if exact all-in math is ever needed.

* Price write: POST api.crowdvolt.com/api/market/edit_sell
  {ask_uqid, price, qty, ignore_low_price_check: false} →
  {"status": "success"}. qty = the ask's current qty_left (the modal always
  sends it). Verified live 52→53→52. ignore_low_price_check stays false so
  CrowdVolt's own too-cheap guard can still reject a fat-finger target.

Pricing rules mirror viagogo_pricer: undercut the cheapest competitor of the
same event + ticket type; tighten when our lead is thinner than the
undercut; floors mandatory; lower-only (raise architecture present, off);
sliding-window drop cap (cv_pricer_max_drop_pct / cv_pricer_drop_window_
hours); external price changes adopted as the new baseline (dollar-scale
moves ping); dry-run default ON; master kill switch default OFF. All prices
USD.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from patchright.sync_api import sync_playwright

import db
import notify

CDP_URL = os.environ.get("KARTIS_CDP_URL", "http://localhost:9222")
HOME_URL = "https://www.crowdvolt.com/"
API = "https://api.crowdvolt.com"
WHAT = "https://what.crowdvolt.com"

UNDERCUT_DEFAULT = 1.00   # displayed all-ins are integers — see docstring
MAX_CHANGES_PER_TICK = 20
PRICE_EPSILON = 0.005
PAGE_LIMIT = 25           # sell_active page size we request

# x-pokedex is a build constant the SPA sends on every API call (0376 as of
# 2026-07-22). We read it from the page's own traffic when possible and fall
# back to this pin; if CV rotates it and fetches start failing, sniff a new
# one with cv_probe/probe_headers.py.
POKEDEX_FALLBACK = "0376"


class CvPricerError(RuntimeError):
    pass


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _norm_type(s):
    return " ".join((s or "").split()).casefold()


# --- settings ------------------------------------------------------------

def undercut_amount():
    try:
        return float(db.setting_get("cv_pricer_undercut") or UNDERCUT_DEFAULT)
    except (TypeError, ValueError):
        return UNDERCUT_DEFAULT


def master_enabled():
    return db.setting_get_bool("cv_pricer_master_enabled", False)


def dry_run_enabled():
    return db.setting_get_bool("cv_pricer_dry_run", True)


def allow_raise_global():
    return db.setting_get_bool("cv_pricer_allow_raise_global", False)


def ignore_single_competitors():
    return db.setting_get_bool("cv_pricer_ignore_singles", True)


def drop_cap_enabled():
    return db.setting_get_bool("cv_pricer_drop_cap_enabled", True)


def max_drop_pct():
    try:
        return float(db.setting_get("cv_pricer_max_drop_pct") or 15.0)
    except (TypeError, ValueError):
        return 15.0


def drop_window_hours():
    try:
        return float(db.setting_get("cv_pricer_drop_window_hours") or 12.0)
    except (TypeError, ValueError):
        return 12.0


def drop_guard(ask_uqid, current_price):
    """(min_allowed_price, baseline) — same sliding-window reconstruction as
    viagogo_pricer.drop_guard, backed by cv_pricer_log."""
    since = (datetime.now(timezone.utc)
             - timedelta(hours=drop_window_hours())).isoformat()
    writes = db.cv_pricer_writes_since(str(ask_uqid), since)
    baseline = None
    for w in writes:
        if w.get("action") == "manual_adopt" and w.get("new_price"):
            baseline = w["new_price"]
        elif baseline is None and w.get("old_price"):
            baseline = w["old_price"]
    if not baseline:
        baseline = current_price
    return baseline * (1.0 - max_drop_pct() / 100.0), baseline


# --- in-page API session --------------------------------------------------

_FETCH_JS = """async (arg) => {
  const tok = (document.cookie.split('; ')
      .find(c => c.startsWith('stytch_session=')) || '').split('=')[1];
  if (!tok) return {status: -1, body: 'no stytch_session cookie - not logged in?'};
  const headers = Object.assign({
    'Authorization': 'Bearer ' + tok,
    'x-fingerprint-id': localStorage.getItem('fingerprint_id') || '',
    'x-pokedex': arg.pokedex,
  }, arg.body != null ? {'Content-Type': 'application/json'} : {});
  try {
    const r = await fetch(arg.url, {
      method: arg.method || 'GET',
      headers,
      body: arg.body != null ? JSON.stringify(arg.body) : undefined,
    });
    return {status: r.status, body: await r.text()};
  } catch (e) {
    return {status: -1, body: String(e)};
  }
}"""


class CvSession:
    """A page parked on www.crowdvolt.com whose in-page fetch() carries the
    logged-in Stytch session. One per tick. The SPA occasionally closes a
    duplicate tab out from under us (posthog primary-window logic), so a
    dead page is reopened once per call instead of failing the tick."""

    def __init__(self, page):
        self.page = page

    def _reopen(self):
        context = self.page.context
        try:
            self.page.close()
        except Exception:
            pass
        self.page = context.new_page()
        self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_timeout(3000)
        _settle_challenge(self.page)

    def api(self, url, method="GET", body=None):
        try:
            res = self.page.evaluate(_FETCH_JS, {
                "url": url, "method": method, "body": body,
                "pokedex": POKEDEX_FALLBACK,
            })
        except Exception:
            if self.page.is_closed():
                self._reopen()
                res = self.page.evaluate(_FETCH_JS, {
                    "url": url, "method": method, "body": body,
                    "pokedex": POKEDEX_FALLBACK,
                })
            else:
                raise
        if res["status"] != 200:
            raise CvPricerError(
                f"{method} {url.split('?')[0]} -> {res['status']}: "
                f"{(res['body'] or '')[:200]}")
        try:
            return json.loads(res["body"])
        except (TypeError, ValueError) as e:
            raise CvPricerError(f"bad JSON from {url.split('?')[0]}: {e}")

    # -- reads --

    def fetch_active_listings(self):
        """All unfilled asks, normalized for cv_active_listings. base_card's
        all_in_price is the listing TOTAL; we keep the per-ticket price as
        the pricing unit throughout."""
        out, offset = [], 0
        while True:
            data = self.api(
                f"{API}/api/buy_sell_history/sell_active"
                f"?limit={PAGE_LIMIT}&offset={offset}&sort_key=date&sort_desc=true")
            if not isinstance(data, list) or not data:
                break
            for row in data:
                card = row.get("base_card") or {}
                out.append({
                    "ask_uqid": row.get("ask_uqid"),
                    "event_uqid": card.get("event_uqid"),
                    "event_name": card.get("event_name"),
                    "event_date": row.get("pretty_event_date") or card.get("event_date"),
                    "event_location": card.get("event_location"),
                    "ticket_type": card.get("ticket_type"),
                    "price": card.get("price"),
                    "qty": card.get("qty_left"),
                    "all_in_price": card.get("all_in_price"),
                })
            if len(data) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
        return [r for r in out if r["ask_uqid"] and r["event_uqid"]]

    def fetch_book(self, event_uqid):
        """The public order book's SELL side, simplified. `uqid` is the ask
        id — our own rows are identified by matching it against our
        sell_active set (is_ours stamped by the caller)."""
        data = self.api(f"{WHAT}/api/book/get?event_uqid={event_uqid}")
        rows = []
        for r in (data.get("sell") or []):
            rows.append({
                "uqid": r.get("uqid"),
                "user_uqid": r.get("user_uqid"),
                "ticket_type": r.get("ticket_type"),
                "price": r.get("price"),
                "all_in_price": r.get("all_in_price"),
                "qty": r.get("qty"),
                "seller": r.get("user_first"),
            })
        return rows

    # -- write --

    def edit_sell(self, ask_uqid, price, qty):
        data = self.api(f"{API}/api/market/edit_sell", method="POST", body={
            "ask_uqid": str(ask_uqid),
            "price": price,
            "qty": qty,
            "ignore_low_price_check": False,
        })
        if (data or {}).get("status") != "success":
            raise CvPricerError(f"edit_sell rejected: {json.dumps(data)[:200]}")
        return data


def _looks_challenged(page):
    """True while the tab is sitting on Cloudflare's interstitial."""
    try:
        title = (page.title() or "").strip().lower()
    except Exception:
        return False
    return "just a moment" in title or "attention required" in title


def _settle_challenge(page):
    """A managed CF challenge usually clears itself in the real browser
    within a few seconds — give it that chance, then one reload. An
    interactive Turnstile never auto-clears; that needs a human in noVNC,
    so fail the tick with instructions instead of hammering or wedging."""
    if not _looks_challenged(page):
        return
    page.wait_for_timeout(8000)
    if not _looks_challenged(page):
        return
    try:
        page.reload(wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    page.wait_for_timeout(8000)
    if _looks_challenged(page):
        raise CvPricerError(
            "cloudflare challenge on crowdvolt.com — open noVNC, click "
            "through the check in the CrowdVolt tab, then sign in if needed")


def open_session(p):
    # scraper's hardened connect: pre-closes stray ticketing tabs (a parked
    # tab stuck on a CF challenge has a self-navigating iframe that can
    # crash or WEDGE the attach — the 2026-08-12 outage), real timeout,
    # retries. Lazy import keeps the CLI dry-run light.
    import scraper
    browser = scraper._connect_over_cdp(p, CDP_URL)
    context = browser.contexts[0]
    page = context.new_page()
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(3000)
    _settle_challenge(page)
    return CvSession(page)


# --- pricing rule ---------------------------------------------------------

def cheapest_competitor(book_rows, ticket_type, our_ask_uqids,
                        min_competitor_qty=1):
    """Cheapest non-ours seller price for one ticket type. None = alone."""
    want = _norm_type(ticket_type)
    best = None
    for r in book_rows:
        if r.get("uqid") in our_ask_uqids or r.get("price") is None:
            continue
        if _norm_type(r.get("ticket_type")) != want:
            continue
        if r.get("qty") is not None and r["qty"] < min_competitor_qty:
            continue
        if best is None or r["price"] < best:
            best = r["price"]
    return best


def compute_target(cheapest_competitor_price, current_price, floor,
                   allow_raise, undercut):
    """Identical semantics to viagogo_pricer.compute_target."""
    target = round(cheapest_competitor_price - undercut, 2)
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


# --- the tick -------------------------------------------------------------

def _log(entry):
    db.cv_pricer_log_add(entry, _now_iso())


def refresh_book_snapshot(event_uqid):
    """On-demand book fetch for the /cvpricer market panel. Opens its own
    page (~10s)."""
    with sync_playwright() as p:
        sess = open_session(p)
        try:
            rows = sess.fetch_book(event_uqid)
        finally:
            try:
                sess.page.close()
            except Exception:
                pass
    ours = {r["ask_uqid"] for r in db.cv_listings_all()}
    for r in rows:
        r["is_ours"] = r.get("uqid") in ours
    db.cv_market_snapshot_set(event_uqid, json.dumps(rows), _now_iso())
    return rows


def run_cv_pricer_tick(dry_run=None):
    """One full pass. Returns counters for the status dict. Always refreshes
    the cv_active_listings mirror (even with master off nothing would run —
    so the mirror refresh is gated on master too; the run-now button and the
    15-min tick both come through here)."""
    counters = {"changed": 0, "paused": 0, "skipped": 0, "errors": 0,
                "dry_run": None, "eligible": 0, "listings": 0}
    if dry_run is None:
        dry_run = dry_run_enabled()
    counters["dry_run"] = bool(dry_run)

    undercut = undercut_amount()
    raise_global = allow_raise_global()
    min_qty = 2 if ignore_single_competitors() else 1
    now = _now_iso()

    with sync_playwright() as p:
        sess = open_session(p)
        try:
            live = sess.fetch_active_listings()
            counters["listings"] = len(live)
            db.cv_listings_upsert(live, now)
            live_by_id = {r["ask_uqid"]: r for r in live}
            our_ask_uqids = set(live_by_id)

            if not master_enabled():
                return counters

            configs = {aid: c for aid, c in db.cv_pricer_config_all().items()
                       if c.get("enabled") and not c.get("paused")}
            if not configs:
                return counters

            # -- manual-change pass --
            eligible = {}
            for aid, cfg in configs.items():
                row = live_by_id.get(aid)
                if row is None or not row.get("price"):
                    # Not in sell_active any more = sold or deleted on CV.
                    db.cv_pricer_config_set(aid, {
                        "enabled": 0, "paused": 0, "paused_reason": None,
                    }, now)
                    _log({"ask_uqid": aid, "action": "disabled_gone",
                          "detail": "ask no longer active on CrowdVolt — auto-disabled"})
                    counters["skipped"] += 1
                    continue
                last_set = cfg.get("last_set_price")
                if last_set is not None and abs(row["price"] - last_set) >= PRICE_EPSILON:
                    # External change (user edited on the site): adopt as the
                    # new baseline, same as the viagogo pricer's default mode.
                    db.cv_pricer_config_set(aid, {
                        "last_set_price": row["price"], "last_set_at": now,
                    }, now)
                    _log({"ask_uqid": aid, "event_uqid": row["event_uqid"],
                          "ticket_type": row.get("ticket_type"),
                          "old_price": last_set, "new_price": row["price"],
                          "action": "manual_adopt",
                          "detail": "external price change adopted as baseline"})
                    if abs(row["price"] - last_set) >= 0.50:
                        try:
                            notify.notify_pricer_adopted({
                                "event_name": f"[CV] {row.get('event_name')}",
                                "section": row.get("ticket_type"),
                                "listing_id": aid,
                                "expected": last_set,
                                "seen": row["price"],
                            })
                        except Exception as e:
                            print(f"[cvpricer] adopt notify failed: {e}")
                eligible[aid] = (cfg, row)
            counters["eligible"] = len(eligible)

            # -- one book fetch per event --
            by_event = {}
            for aid, (cfg, row) in eligible.items():
                by_event.setdefault(row["event_uqid"], []).append((aid, cfg, row))

            for event_uqid, members in by_event.items():
                if counters["changed"] >= MAX_CHANGES_PER_TICK:
                    _log({"ask_uqid": "-", "event_uqid": event_uqid,
                          "action": "error",
                          "detail": "circuit breaker: max changes per tick"})
                    counters["errors"] += 1
                    break
                try:
                    book = sess.fetch_book(event_uqid)
                except Exception as e:
                    for aid, cfg, row in members:
                        _log({"ask_uqid": aid, "event_uqid": event_uqid,
                              "ticket_type": row.get("ticket_type"),
                              "action": "fetch_failed",
                              "detail": f"book fetch failed: {str(e)[:200]}"})
                    counters["skipped"] += len(members)
                    continue
                for r in book:
                    r["is_ours"] = r.get("uqid") in our_ask_uqids
                try:
                    db.cv_market_snapshot_set(event_uqid, json.dumps(book), now)
                except Exception as e:
                    print(f"[cvpricer] snapshot write failed (non-fatal): {e}")

                for aid, cfg, row in members:
                    if counters["changed"] >= MAX_CHANGES_PER_TICK:
                        break
                    competitor = cheapest_competitor(
                        book, row.get("ticket_type"), our_ask_uqids,
                        min_competitor_qty=min_qty)
                    if competitor is None:
                        _log({"ask_uqid": aid, "event_uqid": event_uqid,
                              "ticket_type": row.get("ticket_type"),
                              "action": "skip_alone",
                              "detail": "no qualifying competitors for this ticket type"})
                        counters["skipped"] += 1
                        continue
                    allow_raise = bool(raise_global and cfg.get("allow_raise"))
                    target, action = compute_target(
                        competitor, row["price"], cfg.get("floor_price"),
                        allow_raise, undercut)
                    base = {
                        "ask_uqid": aid, "event_uqid": event_uqid,
                        "ticket_type": row.get("ticket_type"),
                        "old_price": row["price"],
                        "competitor_price": round(competitor, 2),
                    }
                    if target is None:
                        counters["skipped"] += 1
                        continue
                    if target < row["price"] and drop_cap_enabled() \
                            and not cfg.get("no_drop_cap"):
                        min_allowed, baseline = drop_guard(aid, row["price"])
                        if target < min_allowed - PRICE_EPSILON:
                            clamped = round(min_allowed, 2)
                            if clamped >= row["price"] - PRICE_EPSILON:
                                prev = db.cv_pricer_log_recent(1, aid)
                                _log({**base, "new_price": target,
                                      "action": "rate_limited",
                                      "detail": f"drop cap {max_drop_pct():g}%/"
                                                f"{drop_window_hours():g}h hit "
                                                f"(baseline ${baseline:.2f})"})
                                if not (prev and prev[0]["action"] == "rate_limited"):
                                    try:
                                        notify.notify_pricer_rate_limited({
                                            "event_name": f"[CV] {row.get('event_name')}",
                                            "section": row.get("ticket_type"),
                                            "listing_id": aid,
                                            "current": row["price"],
                                            "wanted": target,
                                            "baseline": baseline,
                                            "pct": max_drop_pct(),
                                            "hours": drop_window_hours(),
                                        })
                                    except Exception as e:
                                        print(f"[cvpricer] rate-limit notify failed: {e}")
                                counters["skipped"] += 1
                                continue
                            target = clamped
                            action = "rate_clamp"
                    if dry_run:
                        _log({**base, "new_price": target,
                              "action": "dry_run", "dry_run": 1,
                              "detail": f"would {action}"})
                        try:
                            notify.notify_pricer_change({
                                "event_name": f"[CV] {row.get('event_name')}",
                                "section": row.get("ticket_type"),
                                "listing_id": aid,
                                "old_price": row["price"],
                                "new_price": target,
                                "competitor_price": round(competitor, 2),
                                "floor": cfg.get("floor_price"),
                                "dry_run": True,
                            })
                        except Exception as e:
                            print(f"[cvpricer] notify failed: {e}")
                        counters["changed"] += 1
                        continue
                    if not master_enabled():  # kill switch live mid-tick
                        return counters
                    try:
                        sess.edit_sell(aid, target, row.get("qty") or 1)
                    except Exception as e:
                        _log({**base, "new_price": target, "action": "error",
                              "detail": str(e)[:300]})
                        counters["errors"] += 1
                        continue
                    db.cv_pricer_config_set(aid, {
                        "last_set_price": target, "last_set_at": _now_iso(),
                    }, _now_iso())
                    _log({**base, "new_price": target, "action": action})
                    try:
                        notify.notify_pricer_change({
                            "event_name": f"[CV] {row.get('event_name')}",
                            "section": row.get("ticket_type"),
                            "listing_id": aid,
                            "old_price": row["price"],
                            "new_price": target,
                            "competitor_price": round(competitor, 2),
                            "floor": cfg.get("floor_price"),
                            "dry_run": False,
                        })
                    except Exception as e:
                        print(f"[cvpricer] notify failed: {e}")
                    counters["changed"] += 1
        finally:
            try:
                sess.page.close()
            except Exception:
                pass
    return counters


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--book":
        print(json.dumps(refresh_book_snapshot(sys.argv[2]), indent=1))
    else:
        db.init()
        print(json.dumps(run_cv_pricer_tick(dry_run=True), indent=1))
