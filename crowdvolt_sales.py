"""CrowdVolt sales tracking via CrowdVolt's own JSON API.

Replaces the /selling DOM scrape, which had broken twice on redesigns and
finally stopped returning anything at all: since the CrowdVolt Chrome split
(kartis-chrome-cv on :9223, residential proxy) the login lives in
user_data_cv, while scraper.py was still driving the main :9222 Chrome —
datacenter IP, no CV session, so the page sat on a Cloudflare interstitial
and the table "never rendered". Both halves of that are fixed here: the
fetch runs through crowdvolt_pricer's CvSession (which already knows how to
reach the CV Chrome and settle a challenge), and it reads JSON instead of
a table.

Endpoints (harvested from the /selling route's Next.js chunk, 2026-08-20 —
the same `buy_sell_history` family the pricer's sell_active call belongs to,
so they share auth, paging and sort params):

    GET api.crowdvolt.com/api/buy_sell_history/sell_delivered  -- "Completed" tab
    GET api.crowdvolt.com/api/buy_sell_history/sell_incomplete -- "Incomplete" tab
    GET api.crowdvolt.com/api/buy_sell_history/sell_active     -- unsold asks (pricer)
    GET api.crowdvolt.com/api/buy_sell_history/sell_all        -- all of the above
    ?limit=N&offset=M&sort_key=date&sort_desc=true

We page delivered + incomplete and deliberately NOT sell_all: "all" includes
the still-unsold active asks, which are listings, not sales, and would
inflate revenue. A sale that hasn't been handed over yet is still a sale —
that's the `incomplete` half, and it's why the old scrape (Completed tab
only) under-reported fresh sales.

Row shape, per the page's own row mapper: event_name, event_date,
venue_name, ticket_type_name (defaults "GA"), num_tickets, price_per,
earnings_amount ?? payout_amount, order_number, transaction_date. Field
names are read through alias tuples and every row is kept verbatim in
`raw_cells`, so if CrowdVolt renames one the damage is a NULL column plus a
visible raw row rather than a silently empty sync — run
`python crowdvolt_sales.py --raw` to see what the API actually returned.

CLI (needs the CV Chrome up and logged in):

    .venv\\Scripts\\python crowdvolt_sales.py            # dry-run, print rows
    .venv\\Scripts\\python crowdvolt_sales.py --raw      # dump raw JSON per tab
    .venv\\Scripts\\python crowdvolt_sales.py --write    # persist like a real sync
"""
import json
import re
from datetime import datetime, timezone

import db

API = "https://api.crowdvolt.com"
PAGE_LIMIT = 25
# Guard against an endpoint that ignores offset and pages forever.
MAX_PAGES = 40

# (status we store, endpoint). Order matters only for dedup: a row that
# somehow appears in both tabs keeps the first (delivered) status.
SALE_TABS = (
    ("delivered", "sell_delivered"),
    ("incomplete", "sell_incomplete"),
)

# Field aliases — first present (and non-empty) wins.
_F_ORDER = ("order_number", "order_uqid", "order_id")
_F_EVENT = ("event_name",)
_F_EVENT_DATE = ("event_date", "event_datetime", "pretty_event_date")
_F_VENUE = ("venue_name", "event_location", "venue")
_F_QTY = ("num_tickets", "quantity", "qty", "qty_left")
_F_TYPE = ("ticket_type_name", "ticket_type")
_F_PRICE_PER = ("price_per", "price")
_F_PAYOUT = ("earnings_amount", "payout_amount")
_F_SALE_DATE = ("transaction_date", "sale_date", "created_at", "date")

RAW_LIMIT = 4000


class CvSalesError(RuntimeError):
    pass


def _pick(row, names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", []):
            return v
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        if m:
            return float(m.group(0))
    return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _iso_date(v):
    """ISO-8601 date (YYYY-MM-DD) out of whatever the API hands us: an ISO
    timestamp, an epoch in seconds or milliseconds, or a display string.
    Returns None rather than guessing — a wrong date is worse than a blank
    one, since sale_date_iso keys the dedupe against JeruJam rows."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        ts = float(v)
        if ts > 1e11:          # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.split("•")[0].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _normalize(row, status):
    """One API row -> one crowdvolt_sales row. Returns None for a row with no
    usable id (nothing to key the upsert on)."""
    order = _pick(row, _F_ORDER)
    order = str(order).strip() if order is not None else None
    if not order:
        return None

    qty = _int(_pick(row, _F_QTY))
    price_per = _num(_pick(row, _F_PRICE_PER))
    payout = _num(_pick(row, _F_PAYOUT))
    if payout is None and price_per is not None and qty:
        # Fall back to gross. Downstream (app.py) reads sale_price as the
        # payout, so prefer CrowdVolt's own earnings figure when it's there.
        payout = round(price_per * qty, 2)

    sale_raw = _pick(row, _F_SALE_DATE)
    event_raw = _pick(row, _F_EVENT_DATE)
    raw = json.dumps(row, separators=(",", ":"), default=str)

    return {
        "id": order,
        "order_id": order,
        "sale_date": str(sale_raw) if sale_raw is not None else None,
        "sale_date_iso": _iso_date(sale_raw),
        "event_name": _pick(row, _F_EVENT),
        "event_date": str(event_raw) if event_raw is not None else None,
        "event_date_iso": _iso_date(event_raw),
        "venue": _pick(row, _F_VENUE),
        "qty": qty,
        "ticket_type": _pick(row, _F_TYPE) or "GA",
        "price_per_ticket": price_per,
        "sale_price": payout,
        "status": status,
        "raw_cells": raw[:RAW_LIMIT],
    }


def _page_tab(session, endpoint):
    """Every row of one tab, following offset paging."""
    rows, offset = [], 0
    for _ in range(MAX_PAGES):
        data = session.api(
            f"{API}/api/buy_sell_history/{endpoint}"
            f"?limit={PAGE_LIMIT}&offset={offset}&sort_key=date&sort_desc=true")
        if not isinstance(data, list):
            raise CvSalesError(
                f"{endpoint} returned {type(data).__name__}, expected a list: "
                f"{json.dumps(data)[:200]}")
        rows.extend(data)
        if len(data) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    else:
        print(f"[kartis] crowdvolt {endpoint}: stopped at the {MAX_PAGES}-page "
              f"cap ({len(rows)} rows) — paging may be ignoring offset")
    return rows


def fetch_sales(session):
    """Normalized sales rows, newest tabs first, deduped on order number."""
    out, seen = [], set()
    for status, endpoint in SALE_TABS:
        for raw in _page_tab(session, endpoint):
            if not isinstance(raw, dict):
                continue
            row = _normalize(raw, status)
            if row and row["id"] not in seen:
                seen.add(row["id"])
                out.append(row)
    return out


def fetch_raw(session):
    """{tab: [raw rows]} — the probe path, for diagnosing field drift."""
    return {endpoint: _page_tab(session, endpoint) for _, endpoint in SALE_TABS}


def _open_session(p):
    """Session on the CrowdVolt Chrome. Lives in crowdvolt_pricer so the
    Cloudflare-challenge handling and dead-tab reopen have exactly one
    implementation."""
    import crowdvolt_pricer
    return crowdvolt_pricer.open_session(p)


def fetch_via_cdp():
    """Standalone fetch — opens its own playwright + CV Chrome session. The
    hourly scrape does NOT use this (it reuses the context it already has);
    this is for the CLI and any caller without a live context."""
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        session = _open_session(p)
        try:
            return fetch_sales(session)
        finally:
            try:
                session.page.close()
            except Exception:
                pass


def _main():
    import sys
    args = sys.argv[1:]
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        session = _open_session(p)
        try:
            if "--raw" in args:
                print(json.dumps(fetch_raw(session), indent=2, default=str))
                return
            rows = fetch_sales(session)
        finally:
            try:
                session.page.close()
            except Exception:
                pass
    if "--json" in args:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['sale_date_iso'] or '????-??-??'}  {r['status']:<10} "
                  f"#{r['order_id']:<12} {(r['event_name'] or '?')[:38]:<38} "
                  f"{r['qty'] or '?'}x {(r['ticket_type'] or '')[:14]:<14} "
                  f"${r['sale_price'] if r['sale_price'] is not None else '?'}")
        print(f"\n{len(rows)} sales")
    if "--write" in args:
        db.init()
        db.upsert_crowdvolt_sales(rows, datetime.now(timezone.utc).isoformat())
        print(f"wrote {len(rows)} rows to crowdvolt_sales")


if __name__ == "__main__":
    _main()
