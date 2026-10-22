"""Market-wide viagogo sales tracker (the /vgsales page's fetcher).

Empirical facts (probe: scripts/probe_viagogo_market_sales.py, 2026-07-14):

* ``POST https://inv.viagogo.com/Listings/MarketDataV3 {eventId,
  latestServerStamp: 0}`` — the magnifier popup — returns ONE html body
  containing both tabs: the competitor listings grid (parsed by
  viagogo_pricer) and ``<div id="marketTransactionsGrid">`` — "the past ten
  sales on the website and app" for the event, ALL sellers. The Sales tab
  is a client-side toggle; no extra XHR exists.
* Works for events we hold no listing on (watchlist support), and the
  header carries event name / date / venue.
* Sale rows: Section | Row | Seats | Quantity | Price (USD per ticket —
  same unit price observed across qty-2 and qty-4 sales of one batch).
  NO dates, NO ids; our own sales carry tr class "owned" / "current".
* The USD prices are DISPLAY QUOTES, not transaction records: viagogo
  recomputes them from the sale's original currency at the current fx
  rate, so the same sale's shown price drifts (~+0.4% overnight observed
  on prod, 2026-07-16) — price must never anchor row identity.
* latestServerStamp is not a cursor — the response never echoes a stamp.

Because rows are dateless and id-less (and identical rows are common — one
probe window held four "GA 2 $66.73"), newness is decided by ALIGNING the
ordered window against the previous tick's window (stored in
viagogo_sales_events.window_json), on price-LESS keys (section/row/seats/
qty/is_ours):

  cur == prev (incl. price)     -> no new sales
  cur == prev (price-less)      -> "repriced": fx quote moved, nothing new
  cur[k:]  == prev[:len-k]      -> k new rows at the head (newest-first)
  cur[:len-k] == prev[k:]       -> k new rows at the tail (newest-last)
  cur contiguous inside prev    -> rows removed (refund) — nothing new
  same multiset, new order      -> "reorder": nothing new
  no overlap                    -> "overflow": >= WINDOW_DEPTH sales since
                                   the last tick; the visible rows all
                                   count, the true total is unknowable.

The first-ever window per event is a "baseline": stored (they are real
sales) but flagged so velocity windows ignore them — their age is unknown.
A sale's observed_at is the tick that first saw it, accurate to the poll
interval — which is why the default interval is 30 minutes, and why the
pricer's 15-minute MarketDataV3 fetch also feeds ingest_window() (pricer
piggyback), tightening resolution on pricer-enabled events for free.

CLI:
    python viagogo_market_sales.py --event <id> [--json] [--write]
    python viagogo_market_sales.py [--json] [--write]     # whole tick
Dry-run by default; --write persists like a real tick.
"""
import argparse
import io
import json
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import db
import viagogo_listing
from patchright.sync_api import sync_playwright

MARKET_DATA_URL = "https://inv.viagogo.com/Listings/MarketDataV3"
WINDOW_DEPTH = 10          # what viagogo shows; informational only
MAX_EVENTS_PER_TICK = 60   # round-robin resume covers the rest next tick
INTERVAL_MINUTES = int(os.getenv("KARTIS_VGSALES_INTERVAL_MINUTES") or 30)
RETENTION_DAYS = int(os.getenv("KARTIS_VGSALES_RETENTION_DAYS") or 90)
LISTING_FRESH_HOURS = 48   # scrape-mirror staleness before an event drops out


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------- parsing ----------------------------------------------------

# The transactions table sits inside id="marketTransactionsGrid"; cut before
# the jquery-tmpl <script> so the template's placeholder <tr> is not parsed.
TRANS_GRID_RE = re.compile(
    r'id="marketTransactionsGrid".*?<table[^>]*>(.*?)(?:<script|</table>)', re.S)
TR_RE = re.compile(r'<tr class="([^"]*)"[^>]*>(.*?)</tr>', re.S)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
HEADER_NAME_RE = re.compile(r'class="h l m0 mbxs cMag2 p0">([^<]*)<')
HEADER_DATE_RE = re.compile(r'class="h s cGry2">([^<]*)<')
HEADER_VENUE_RE = re.compile(r'<span class="ibk">([^<]*)</span>')


def _strip_tags(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def parse_sales(body):
    """MarketDataV3 body -> ordered [{section, row, seats, qty, price,
    is_ours}] from the transactions grid, or None if the grid is missing
    entirely (page-shape change / logged out — distinct from an event that
    simply has no sales yet, which returns [])."""
    m = TRANS_GRID_RE.search(body or "")
    if not m:
        return None
    rows = []
    for cls, tr_body in TR_RE.findall(m.group(1)):
        tds = TD_RE.findall(tr_body)
        if len(tds) < 6:
            continue
        qty_txt = _strip_tags(tds[4])
        price_txt = _strip_tags(tds[5]).replace(",", "")
        pm = re.search(r"[\d.]+", price_txt)
        rows.append({
            "section": _strip_tags(tds[1]),
            "row": _strip_tags(tds[2]),
            "seats": _strip_tags(tds[3]),
            "qty": int(qty_txt) if qty_txt.isdigit() else None,
            "price": float(pm.group(0)) if pm else None,
            "is_ours": ("owned" in cls) or ("current" in cls),
        })
    return rows


def parse_header(body):
    """Event name / date text / venue from the popup header — present even
    for events we hold no listing on."""
    name = HEADER_NAME_RE.search(body or "")
    date = HEADER_DATE_RE.search(body or "")
    venue_parts = HEADER_VENUE_RE.findall(body or "")
    venue = " ".join(v.strip() for v in venue_parts).strip()
    return {
        "name": name.group(1).strip() if name else None,
        "date_text": date.group(1).strip() if date else None,
        "venue": venue or None,
    }


# ---------------- window alignment -------------------------------------------

def _key(r):
    """Full row identity incl. price — only good for the exact-equality
    fast path."""
    return (r.get("section"), r.get("row"), r.get("seats"), r.get("qty"),
            r.get("price"), bool(r.get("is_ours")))


def _rkey(r):
    """Alignment identity — NO price. The grid's USD prices are display
    quotes recomputed from the sale's original currency at the CURRENT fx
    rate, so the same sale's price drifts (~+0.4% observed at the daily fx
    update, 2026-07-16 midnight UTC, prod). With price in the key the
    drift zeroed the overlap every day and the whole window re-recorded as
    'new' — the bug this split fixes."""
    return (r.get("section"), r.get("row"), r.get("seats"), r.get("qty"),
            bool(r.get("is_ours")))


def diff_window(prev_rows, cur_rows):
    """(new_rows, mode) — see module docstring for the alignment table.
    prev_rows None means the event was never fetched: everything is
    'baseline'. Alignment runs on price-less keys (_rkey); 'repriced'
    means same sales in the same order, only the fx quote moved. Known
    miss: a full window of identical price-less rows plus one more
    identical sale looks like 'nochange' — accepted (undercounting beats
    the daily re-record)."""
    if prev_rows is None:
        return list(cur_rows), "baseline"
    if [_key(r) for r in cur_rows] == [_key(r) for r in prev_rows]:
        return [], "nochange"
    prev = [_rkey(r) for r in prev_rows]
    cur = [_rkey(r) for r in cur_rows]
    if cur == prev:
        return [], "repriced"
    n = len(cur)
    for k in range(1, n):
        if cur[k:] == prev[:n - k]:
            return list(cur_rows[:k]), "head"
    for k in range(1, n):
        if cur[:n - k] == prev[k:]:
            return list(cur_rows[n - k:]), "tail"
    if n < len(prev):
        for off in range(0, len(prev) - n + 1):
            if prev[off:off + n] == cur:
                return [], "shrunk"
    if Counter(cur) == Counter(prev):
        return [], "reorder"
    return list(cur_rows), "overflow"


# ---------------- fetch + ingest ---------------------------------------------

def fetch_event_raw(page, event_id):
    """One MarketDataV3 POST on the warmed inv session. Returns the raw
    body, or None on a non-OK response."""
    resp = page.request.post(
        MARKET_DATA_URL,
        form={"eventId": str(event_id), "latestServerStamp": "0"})
    if not resp.ok:
        return None
    return resp.text() or None


def ingest_window(event_id, body, now_iso, write=True):
    """Parse one MarketDataV3 body, align against the stored window, persist
    the new rows. Shared entry point — the vgsales tick and the pricer
    piggyback both land here. Returns {mode, new, window} or None when the
    body carries no transactions grid."""
    rows = parse_sales(body)
    if rows is None:
        return None
    hdr = parse_header(body)
    ev = db.vg_sales_event_get(event_id)
    prev = None
    if ev and ev.get("window_json"):
        try:
            prev = json.loads(ev["window_json"])
        except Exception:
            prev = None
    new_rows, mode = diff_window(prev, rows)
    if write:
        if new_rows:
            db.vg_market_sales_insert_many(
                event_id, new_rows, mode == "baseline", now_iso)
        db.vg_sales_event_upsert(
            event_id, hdr["name"], hdr["venue"], hdr["date_text"],
            json.dumps(rows, ensure_ascii=False), None, now_iso)
    return {"mode": mode, "new": len(new_rows), "window": len(rows),
            "rows": new_rows, "header": hdr}


def fetch_one(event_id, write=True, lock_timeout=180):
    """Fetch + ingest a single event end-to-end (own browser attach). Used
    by the /api/vgsales/watch inline first fetch (short lock_timeout so a
    busy pricer tick fails the request fast instead of hanging it) and the
    CLI. Raises on any failure."""
    with viagogo_listing._exclusive_browser(timeout=lock_timeout,
                                            category="pricing"), \
            sync_playwright() as p:
        page = viagogo_listing._open_listings_page(p)
        try:
            body = fetch_event_raw(page, event_id)
        finally:
            try:
                page.close()
            except Exception:
                pass
    if not body:
        raise RuntimeError("MarketDataV3 empty response (bad event id?)")
    res = ingest_window(event_id, body, _now_iso(), write=write)
    if res is None:
        raise RuntimeError("no transactions grid in response")
    return res


# ---------------- tick --------------------------------------------------------

def _event_not_past(event_date_iso, now_dt):
    if not event_date_iso:
        return True
    return event_date_iso[:10] >= (now_dt - timedelta(days=1)).date().isoformat()


def collect_targets(now_dt):
    """{event_id: {'listed': bool, 'watch': bool}} — every event we hold a
    fresh listing on, plus the watchlist."""
    fresh = (now_dt - timedelta(hours=LISTING_FRESH_HOURS)).isoformat()
    targets = {}
    for r in db.viagogo_listing_event_ids(fresh):
        if not _event_not_past(r.get("event_date_iso"), now_dt):
            continue
        targets[str(r["event_id"])] = {"listed": True, "watch": False}
    for w in db.vg_watchlist_all():
        t = targets.setdefault(str(w["event_id"]),
                               {"listed": False, "watch": False})
        t["watch"] = True
    return targets


def run_sales_tick(force=False, write=True):
    """One full pass over every tracked event. Returns counters for the
    status dict. Chrome down -> _open_listings_page raises and the caller
    records the error (same failure mode as the pricer/market jobs)."""
    now_dt = datetime.now(timezone.utc)
    summary = {"targets": 0, "fetched": 0, "skipped_fresh": 0, "deferred": 0,
               "new_sales": 0, "baselines": 0, "repriced": 0, "overflows": 0,
               "errors": 0}
    targets = collect_targets(now_dt)
    summary["targets"] = len(targets)
    if not targets:
        return summary

    last_fetch = {e["event_id"]: e.get("last_fetch_at") or ""
                  for e in db.vg_sales_events_all()}
    fresh_cutoff = (now_dt - timedelta(
        minutes=max(1, INTERVAL_MINUTES // 2))).isoformat()
    due = []
    for eid in targets:
        last = last_fetch.get(eid, "")
        if not force and last >= fresh_cutoff:
            summary["skipped_fresh"] += 1     # pricer piggyback covered it
            continue
        due.append((last, eid))
    due.sort()                                # oldest-fetched first
    if len(due) > MAX_EVENTS_PER_TICK:
        summary["deferred"] = len(due) - MAX_EVENTS_PER_TICK
        due = due[:MAX_EVENTS_PER_TICK]

    if due:
        with viagogo_listing._exclusive_browser(category="pricing"), \
                sync_playwright() as p:
            page = viagogo_listing._open_listings_page(p)
            try:
                for _, eid in due:
                    try:
                        body = fetch_event_raw(page, eid)
                        if not body:
                            raise RuntimeError("MarketDataV3 empty response")
                        res = ingest_window(eid, body, _now_iso(), write=write)
                        if res is None:
                            raise RuntimeError("no transactions grid in response")
                        summary["fetched"] += 1
                        if res["mode"] == "baseline":
                            summary["baselines"] += 1
                        else:
                            summary["new_sales"] += res["new"]
                        if res["mode"] == "repriced":
                            summary["repriced"] += 1
                        if res["mode"] == "overflow":
                            summary["overflows"] += 1
                    except Exception as e:
                        summary["errors"] += 1
                        if write:
                            db.vg_sales_event_upsert(
                                eid, None, None, None, None,
                                str(e)[:300], _now_iso())
                    time.sleep(1.0 + random.random())
            finally:
                try:
                    page.close()
                except Exception:
                    pass

    if write:
        db.vg_market_sales_prune(
            (now_dt - timedelta(days=RETENTION_DAYS)).isoformat())
    return summary


# ---------------- CLI ---------------------------------------------------------

def _print_rows(rows):
    for r in rows:
        ours = " *OURS*" if r.get("is_ours") else ""
        print(f"  {r.get('section') or '?':30} row={r.get('row') or '-':10} "
              f"seats={r.get('seats') or '-':10} qty={r.get('qty')} "
              f"${r.get('price')}{ours}")


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--event", help="probe a single viagogo event id")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true",
                    help="persist like a real tick (default: dry-run)")
    args = ap.parse_args()

    if args.event:
        res = fetch_one(args.event, write=args.write)
        hdr = res["header"]
        if args.json:
            print(json.dumps(res, indent=1, ensure_ascii=False))
        else:
            print(f"{hdr['name']} | {hdr['date_text']} | {hdr['venue']}")
            print(f"mode={res['mode']} new={res['new']} "
                  f"window={res['window']} write={args.write}")
            _print_rows(res["rows"])
    else:
        summary = run_sales_tick(force=True, write=args.write)
        print(json.dumps(summary, indent=1) if args.json else summary)


if __name__ == "__main__":
    main()
