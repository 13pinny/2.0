"""Market-wide event tracker — feeds the /market page (app.py run_market_sweep).

Discovers every trackable event/performance on the Israeli ticketing sites
and snapshots its availability so the dashboard can rank events by sales
velocity ("sold in the last 1h/6h/24h/3d/7d"), the same math the /ga and
/festival pages use for watched events — but market-wide, for spotting hot
events worth buying inventory for.

Per-source strategy (each adapter returns normalized entity dicts):

  kupat      GET /api/presentations/?locationId=0&isHold=0 — the WHOLE
             site's presentations in one call (name, venue, date, soldout,
             minPrice) — then exact availSeats per presentation via
             parallel in-page detail fetches. All through one shared
             kupat.BrowserSession (the CDN only challenges page loads;
             in-page fetch() sails through). ~581 presentations in ~2 min.
  ticketmaster
             tm_events.fetch_events() homepage catalog (featured events
             only), then ticketmaster.fetch_event_seats() per event — it
             already handles perf enumeration, past-perf skipping, and the
             salesOptions buyability gate. available = selectable-seat
             count per perf; min price from the cached labels blocks.
  tickchak   No global catalog exists. The user pastes event/hub URLs on
             the /market page (market_manual table); hubs are expanded to
             their member events every sweep via the anonymous hub feed +
             /ajax/form/init per-type counts.
  zappa      zappa_events.fetch_events() — Eventim-IL white-label; needs a
             HEADFUL off-screen Chromium (see that module's docstring).
             Eventim publishes no ticket counts, so zappa rows carry
             status + from-price only (no sold-per-window velocity), and
             new-event / on-sale Discord pings happen HERE (source
             'zappa' in site_seen_events) — unlike kupat/tm, the VPS-side
             il_events job can't fetch zappa (no browser there).

Entity dict shape (market_events row minus bookkeeping):
    {source, event_code, perf_code, name, venue, date_text, first_date_ms,
     url, status, capacity, available, min_price, last_error}

`source` values match the watcher SOURCES keys (kupat / ticketmaster /
tickchak) ON PURPOSE: availability history goes into the same
tickchak_sales_snapshots table under the same keys, so an event that also
has a watcher shares one denser series (the watcher's 10-min run_sales_sync
plus this hourly sweep).

This sweep must NOT write site_seen_events for kupat/tm — the 10-minute
il_events job diffs that table for its new-event pings, and pre-seeding a
row would swallow the ping forever.

CLI probe (house convention):
    python market.py --source kupat|tm|tickchak|all [--json] [--write]
Default is a dry-run table; --write persists entities + snapshots like a
real sweep tick.
"""
import json
import sys
import time
from datetime import datetime, timezone, timedelta

import db
import kupat
import kupat_events  # noqa: F401  (il_events owns discovery; imported for parity/debug)
import notify
import tickchak
import ticketmaster
import tm_events
import zappa_events

# How long after showtime a row keeps being tracked before it's flagged
# 'past' and dropped from fetching + the page. Mirrors ticketmaster.PAST_PERF_GRACE_MS.
PAST_GRACE_MS = 6 * 3600 * 1000

# Politeness gap between per-event request bursts on ticketmaster/tickchak.
REQUEST_SPACING_SECONDS = 0.2

_ISRAEL_TZ = None


def _israel_tz():
    global _ISRAEL_TZ
    if _ISRAEL_TZ is None:
        from zoneinfo import ZoneInfo
        _ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
    return _ISRAEL_TZ


def _kupat_dt_ms(date_text):
    """'YYYY-MM-DD HH:MM[:SS]' venue-local → epoch-ms (Israel time)."""
    if not date_text:
        return None
    try:
        dt = datetime.strptime(date_text[:16], "%Y-%m-%d %H:%M")
        return int(dt.replace(tzinfo=_israel_tz()).timestamp() * 1000)
    except Exception:
        return None


def _entity(source, event_code, perf_code, **kw):
    return {
        "source": source,
        "event_code": str(event_code),
        "perf_code": str(perf_code or "0"),
        "name": kw.get("name"),
        "venue": kw.get("venue"),
        "date_text": kw.get("date_text"),
        "first_date_ms": kw.get("first_date_ms"),
        "url": kw.get("url"),
        "status": kw.get("status") or "unknown",
        "capacity": kw.get("capacity"),
        "available": kw.get("available"),
        "min_price": kw.get("min_price"),
        "manual": kw.get("manual", False),
        "last_error": kw.get("last_error"),
    }


# ---------------------------------------------------------------- kupat ----

def sweep_kupat(detail_concurrency=10):
    """One BrowserSession: site-wide presentations catalog, then exact
    availSeats per future presentation via parallel in-page fetches."""
    now_ms = time.time() * 1000
    entities = []
    with kupat.BrowserSession() as session:
        rows = kupat.fetch_all_presentations(session)
        future = []
        for p in rows:
            if not isinstance(p, dict) or not p.get("id") or not p.get("featureId"):
                continue
            ms = _kupat_dt_ms(p.get("dateTime") or "")
            if ms is not None and ms < now_ms - PAST_GRACE_MS:
                continue
            future.append((p, ms))
        details = kupat.fetch_presentation_details(
            session,
            [(p["featureId"], p["id"]) for p, _ in future],
            concurrency=detail_concurrency,
        )
    for p, ms in future:
        pid = str(p["id"])
        det = details.get(pid)
        avail = det.get("availSeats") if det else None
        if det:
            status = kupat._ga_status(det)
        else:
            status = "soldout" if p.get("soldout") else "unknown"
        name = (p.get("featureName") or "").strip()
        extra = (p.get("featureAdditionalName") or "").strip()
        if extra:
            name = f"{name} — {extra}"
        entities.append(_entity(
            "kupat", p["featureId"], pid,
            name=name,
            venue=" · ".join(v for v in ((p.get("venueName") or "").strip(),
                                         (p.get("venueCity") or "").strip()) if v),
            date_text=(p.get("dateTime") or "")[:16],
            first_date_ms=ms,
            url=kupat.perf_url(p["featureId"], pid),
            status=status,
            capacity=None,  # seatplan capacity is misleading for GA; skip
            available=int(avail) if isinstance(avail, (int, float)) else None,
            min_price=p.get("minPrice"),
            last_error=None if det else "detail fetch failed",
        ))
    return entities


# --------------------------------------------------------- ticketmaster ----

def _tm_min_price(event_code, perf_code, perf_seats):
    """Cheapest label price among the blocks this perf actually has seats
    in; falls back to the cheapest block overall. Labels are disk-cached
    (tm_cache/, 1h TTL) so this usually costs nothing within a sweep."""
    try:
        labels = ticketmaster.get_labels(event_code, perf_code)
    except Exception:
        return None
    blocks = (labels or {}).get("blocks") or {}
    seat_blocks = {s.get("block") for s in perf_seats}
    prices = [b.get("price") for c, b in blocks.items()
              if b.get("price") is not None and c in seat_blocks]
    if not prices:
        prices = [b.get("price") for b in blocks.values() if b.get("price") is not None]
    return min(prices) if prices else None


# Perf-list status strings meaning tickets are buyable RIGHT NOW (unlike
# tm_events._SALE_OPENED_STATUSES this excludes s02_soldout — that set asks
# "did the sale ever open", we ask "can I buy"). We gate on the status
# string, not getPerformanceDetail's salesOptions: as of 2026-07-07 that
# endpoint returns salesOptions=0 for every perf on the site (verified
# against s01_onsale perfs with hundreds of selectable seats), so it can no
# longer tell selling from closed.
_BUYABLE_STATUSES = {"s01_onsale", "s18_low_availability", "s06_boxoffice"}


def sweep_tm():
    """Homepage catalog → per-event perf sweep. Skips grp: rows (their
    member events appear as their own ev: rows). Seat counts are only
    fetched for buyable perfs — sold-out perfs still serve phantom
    "AVAILABLE" seats (the MKS26 case in ticketmaster.fetch_event_seats),
    so their count is pinned to 0 here."""
    events = tm_events.fetch_events()
    entities = []
    now_ms = time.time() * 1000
    for ev in events:
        key = ev["event_key"]
        if not key.startswith("ev:"):
            continue
        code = key[3:]
        time.sleep(REQUEST_SPACING_SECONDS)
        try:
            perfs = ticketmaster.list_performances(code)
        except ticketmaster.TicketmasterError as e:
            if "no performances" in str(e):
                continue  # listed on the homepage but nothing scheduled yet
            entities.append(_entity(
                "ticketmaster", code, "0",
                name=ev.get("name"), venue=ev.get("venue"),
                date_text=ev.get("date_text"), first_date_ms=ev.get("first_date_ms"),
                url=ev.get("url"), status="unknown", last_error=str(e)[:200],
            ))
            continue
        for p in perfs:
            pc = str(p.get("performanceCode") or "")
            if not pc:
                continue
            ms = p.get("performanceDate")
            ms = int(ms) if isinstance(ms, (int, float)) else None
            if ms and ms < now_ms - PAST_GRACE_MS:
                continue
            raw = str(p.get("status") or "")
            last_error = None
            perf_seats = []
            if raw in _BUYABLE_STATUSES:
                status = "lasttickets" if raw == "s18_low_availability" else "available"
                try:
                    perf_seats = ticketmaster.fetch_selectable_seats(code, pc)
                except Exception as e:
                    last_error = f"seats: {e}"[:200]
                # 0 selectable while on sale ⇒ GA / no public seat map:
                # count unknown, not zero.
                available = len(perf_seats) if perf_seats else None
            elif "soldout" in raw.lower():
                status, available = "soldout", 0
            else:
                status, available = "closed", 0
            date_text = ""
            if ms:
                try:
                    date_text = datetime.fromtimestamp(
                        ms / 1000, _israel_tz()).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    date_text = ""
            entities.append(_entity(
                "ticketmaster", code, pc,
                name=ev.get("name"),
                venue=(p.get("venueName") or "").strip() or ev.get("venue"),
                date_text=date_text or ev.get("date_text"),
                first_date_ms=ms or ev.get("first_date_ms"),
                url=ticketmaster.perf_url(code, pc),
                status=status,
                capacity=None,
                available=available,
                min_price=_tm_min_price(code, pc, perf_seats) if perf_seats else None,
                last_error=last_error,
            ))
    return entities


# ------------------------------------------------------------- tickchak ----

def _tickchak_hub_entities(slug):
    """Expand one hub (festival) page into per-member-event entities using
    the anonymous feed + form/init counts — same signals the festival
    watchers use, without the per-watcher slug-resolution machinery."""
    feed = tickchak._fetch_festival_feed(slug)
    hub_url = f"https://home.tickchak.co.il/{slug}"
    entities = []
    for eid, info in (feed.get("events") or {}).items():
        time.sleep(REQUEST_SPACING_SECONDS)
        event_hash = (feed.get("hash_map") or {}).get(eid)
        capacity = available = None
        min_price = None
        have_counts = False
        last_error = None
        if event_hash:
            try:
                init = tickchak._fetch_form_init(eid, event_hash)
                if init and isinstance(init.get("tickets"), list):
                    types, capacity, available = tickchak._normalize_form_init_tickets(init)
                    have_counts = True
                    prices = [t.get("price") for t in types.values()
                              if t.get("price") and t.get("available", 0) > 0]
                    min_price = min(prices) if prices else None
            except Exception as e:
                last_error = f"form_init: {e}"[:200]
        status = tickchak._festival_status(info, available or 0, have_counts)
        ts = info.get("timeStart")
        ms, date_text = None, ""
        try:
            if ts:
                ms = int(ts) * 1000
                date_text = tickchak._israel_time_text(int(ts))
        except (TypeError, ValueError, OSError):
            pass
        entities.append(_entity(
            "tickchak", eid, "0",
            name=info.get("title"), venue=info.get("venue"),
            date_text=date_text, first_date_ms=ms, url=hub_url,
            status=status,
            capacity=capacity if have_counts else None,
            available=available if have_counts else None,
            min_price=min_price, manual=True, last_error=last_error,
        ))
    return entities


def _tickchak_event_entity(code):
    """One manually-added standalone tickchak event via the labels path
    (form/init counts with JSON-LD fallback; 1h disk cache)."""
    labels = tickchak.get_labels(code, "0")
    meta = (labels or {}).get("meta") or {}
    blocks = (labels or {}).get("blocks") or {}
    prices = [b.get("price") for b in blocks.values()
              if b.get("price") and b.get("availability") == "in_stock"]
    if not prices:
        prices = [b.get("price") for b in blocks.values() if b.get("price")]
    status = {"selling": "available", "soldout": "soldout"}.get(
        meta.get("status"), meta.get("festivalStatus") or "unknown")
    return _entity(
        "tickchak", code, "0",
        name=meta.get("eventName"),
        venue=" · ".join(v for v in ((meta.get("venueName") or "").strip(),
                                     (meta.get("venueCity") or "").strip()) if v),
        date_text=meta.get("firstPerfText"),
        first_date_ms=meta.get("firstPerfMs"),
        url=tickchak.perf_url(code),
        status=status,
        capacity=meta.get("totalSeats"),
        available=meta.get("availSeats"),
        min_price=min(prices) if prices else None,
        manual=True,
    )


def sweep_tickchak(manual_rows=None):
    if manual_rows is None:
        manual_rows = [r for r in db.market_manual_all() if r["source"] == "tickchak"]
    entities = []
    for row in manual_rows:
        try:
            if row["kind"] == "hub":
                entities.extend(_tickchak_hub_entities(row["code"]))
            else:
                entities.append(_tickchak_event_entity(row["code"]))
        except Exception as e:
            entities.append(_entity(
                "tickchak", row["code"], "0",
                name=row.get("label") or row["code"],
                status="unknown", manual=True, last_error=str(e)[:200],
            ))
    return entities


# --------------------------------------------------------------- zappa ----

MAX_ZAPPA_PINGS_PER_TICK = 12


def sweep_zappa(rows=None):
    """Entities from the zappa catalog. No counts exist (Eventim), so
    available/capacity stay NULL — the page shows status + price only."""
    if rows is None:
        rows = zappa_events.fetch_events()
    return [_entity(
        "zappa", r["event_key"], "0",
        name=r.get("name"), venue=r.get("venue"),
        date_text=r.get("date_text"), first_date_ms=r.get("first_date_ms"),
        url=r.get("url"), status=r.get("status"),
        capacity=None, available=None, min_price=r.get("min_price"),
    ) for r in rows]


def _zappa_diff_and_ping(rows, now_iso):
    """New-event / on-sale Discord pings for zappa, mirroring app.py's
    run_il_events gating: first-ever tick is a stored-but-silent baseline,
    master_muted stores state without sending, ping cap per tick. Lives
    here (not il_events) because only the market machine has the browser
    zappa needs. master_paused is enforced by the caller
    (app.run_market_sweep skips the whole sweep)."""
    seen = db.site_events_all_seen("zappa")
    baseline = not seen
    muted = db.setting_get_bool("master_muted", default=False)
    pings = []
    if not baseline:
        for ev in rows:
            old = seen.get(ev["event_key"])
            if old is None:
                pings.append(("new", ev, None))
            elif not old["on_sale"] and ev["on_sale"]:
                pings.append(("onsale", ev, old))
    notified = 0
    if not muted:
        for kind, ev, old in pings[:MAX_ZAPPA_PINGS_PER_TICK]:
            res = notify.notify_site_event(kind, ev, old)
            print(f"[market] zappa {kind}: {ev['name']} ({ev.get('date_text')}) -> {res}")
            notified += 1
            time.sleep(0.5)
        if len(pings) > MAX_ZAPPA_PINGS_PER_TICK:
            print(f"[market] zappa ping cap hit — {len(pings) - MAX_ZAPPA_PINGS_PER_TICK} suppressed")
    for ev in rows:
        db.site_events_upsert_seen("zappa", ev, now_iso)
    if baseline:
        print(f"[market] zappa baseline stored: {len(rows)} events, no pings")
    return {"pings": len(pings), "notified": notified, "baseline": baseline}


# ------------------------------------------------------------ orchestrate --

ADAPTERS = {
    "kupat": sweep_kupat,
    "ticketmaster": sweep_tm,
    "tickchak": sweep_tickchak,
    "zappa": sweep_zappa,
}

# CLI aliases (the TM adapter is spelled both ways in casual use)
_SOURCE_ALIASES = {"tm": "ticketmaster"}


def run_sweep(sources=None, write=True):
    """One market tick. Adapters fail independently — one site being down
    must not blank the others. Returns a summary dict for /api/market/status."""
    started = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    wanted = [_SOURCE_ALIASES.get(s, s) for s in (sources or list(ADAPTERS))]
    counts, errors = {}, {}
    all_entities = []
    zappa_pings = None
    for name in wanted:
        fn = ADAPTERS.get(name)
        if fn is None:
            errors[name] = "unknown source"
            continue
        try:
            if name == "zappa":
                rows = zappa_events.fetch_events()
                ents = sweep_zappa(rows)
                if write:
                    zappa_pings = _zappa_diff_and_ping(rows, now_iso)
            else:
                ents = fn()
            counts[name] = len(ents)
            all_entities.extend(ents)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"[:300]
    if write:
        for ev in all_entities:
            db.market_upsert(ev, now_iso)
            if ev.get("available") is not None:
                cap = ev.get("capacity")
                avail = ev["available"]
                db.sales_snapshot_insert(
                    ev["source"], ev["event_code"], ev["perf_code"],
                    cap, avail,
                    (cap - avail) if isinstance(cap, int) else None,
                    now_iso,
                )
        # Retire rows past showtime (plus grace) so the page and the next
        # sweep stop caring about them. Feed-driven sources re-add anything
        # genuinely still listed.
        cutoff_ms = time.time() * 1000 - PAST_GRACE_MS
        for row in db.market_all(include_past=True):
            if (row.get("status") != "past" and row.get("first_date_ms")
                    and row["first_date_ms"] < cutoff_ms):
                db.market_set_status(row["source"], row["event_code"],
                                     row["perf_code"], "past")
    return {
        "at": now_iso,
        "counts": counts,
        "errors": errors,
        "entities": len(all_entities),
        "zappa_pings": zappa_pings,
        "duration_s": round(time.time() - started, 1),
    }


def sweep_one_manual(kind, code):
    """Fetch + persist a single just-added tickchak entry so the /market row
    appears immediately instead of after the next hourly sweep."""
    now_iso = datetime.now(timezone.utc).isoformat()
    if kind == "hub":
        entities = _tickchak_hub_entities(code)
    else:
        entities = [_tickchak_event_entity(code)]
    for ev in entities:
        db.market_upsert(ev, now_iso)
        if ev.get("available") is not None:
            cap = ev.get("capacity")
            db.sales_snapshot_insert(
                ev["source"], ev["event_code"], ev["perf_code"],
                cap, ev["available"],
                (cap - ev["available"]) if isinstance(cap, int) else None,
                now_iso,
            )
    return entities


# ------------------------------------------------------------------- CLI ---

def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    as_json = "--json" in argv
    write = "--write" in argv
    sources = None
    if "--source" in argv:
        i = argv.index("--source")
        if i + 1 >= len(argv):
            print("usage: python market.py [--source kupat|tm|tickchak|all] [--json] [--write]")
            return 2
        if argv[i + 1] != "all":
            sources = [argv[i + 1]]

    if write:
        summary = run_sweep(sources)
        print(json.dumps(summary, indent=1, ensure_ascii=False))
        return 0

    wanted = [_SOURCE_ALIASES.get(s, s) for s in (sources or list(ADAPTERS))]
    entities = []
    for name in wanted:
        fn = ADAPTERS.get(name)
        if fn is None:
            print(f"unknown source {name!r}; known: {', '.join(ADAPTERS)}")
            return 2
        t0 = time.time()
        ents = fn()
        print(f"[{name}] {len(ents)} entities in {time.time() - t0:.1f}s")
        entities.extend(ents)
    if as_json:
        print(json.dumps(entities, indent=1, ensure_ascii=False))
        return 0
    for ev in sorted(entities, key=lambda e: e.get("first_date_ms") or 0):
        avail = ev.get("available")
        cap = ev.get("capacity")
        left = "—" if avail is None else (f"{avail}/{cap}" if cap else str(avail))
        price = ev.get("min_price")
        print(f"  {ev['source']:<13} {ev['event_code']:>7}/{ev['perf_code']:<7} "
              f"{(ev.get('name') or '?'):<38.38} {(ev.get('date_text') or ''):<17} "
              f"{(ev.get('venue') or ''):<28.28} {ev['status']:<12} left={left:<9} "
              f"{'₪' + format(price, 'g') if price is not None else ''}"
              f"{'  ERR: ' + ev['last_error'] if ev.get('last_error') else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
