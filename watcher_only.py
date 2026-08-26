"""Headless Ticketmaster.co.il drop checker — for an always-on home server.

Runs the same per-minute watcher tick used by the Kartis dashboard
(`run_tm_check` in app.py) but without booting Flask or the Lysted
scrapers. The dashboard isn't reachable on this process — you manage
watchers either by running `python add_watcher.py <url>` (creates one
on disk in the local kartis.db), or by running the full app.py
elsewhere and copying its kartis.db here.

Why a separate entry point: the main app.py also imports the patchright
scraper, which attaches to a Chrome instance on localhost:9222. On a
headless server you don't have that Chrome and don't want app.py to
fail on the missing browser at scrape-tick time. This file only pulls
in `ticketmaster`, `notify`, `labels`, and `db` — no browser deps.
"""
import contextlib
import json
import signal
import time
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

import barby
import db
import dice
import discord_bot
import filters as watcher_filters
import haku
import kupat
import notify
import tickchak
import ticketmaster
import tm_discover

# Load .env from the repo root next to this script, not the current working
# directory — so notify creds resolve even when launched from elsewhere.
load_dotenv(Path(__file__).parent / ".env")
db.init()

SOURCES = {"ticketmaster": ticketmaster, "kupat": kupat, "tickchak": tickchak,
           "barby": barby, "dice": dice, "haku": haku,
           "tmdiscover": tm_discover}

import os
INTERVAL = int(os.getenv("TM_CHECK_INTERVAL_SECONDS") or 60)
# Per-seat notification cool-down (minutes) — a flapping seat that cycles in
# and out of the buyable feed would otherwise re-ping every few minutes.
# 0 disables. (Keep in sync with app.py.)
SEAT_COOLDOWN_SECONDS = int(os.getenv("KARTIS_SEAT_COOLDOWN_MINUTES") or 30) * 60

_lock = threading.Lock()
_state = {"at": None, "checked": 0, "drops": 0, "errors": 0}


def _diff_seats_set(prev_keys, curr_seats, key_fn):
    prev = set(prev_keys)
    by_key = {}
    for s in curr_seats:
        by_key.setdefault(key_fn(s), s)
    curr = set(by_key)
    return [by_key[k] for k in curr - prev], list(prev - curr)


def check_one(w, now_iso):
    wid = w["id"]
    label = w.get("label") or f"{w['event_code']}/{w['perf_code']}"
    src_name = w.get("source") or "ticketmaster"
    src = SOURCES.get(src_name, ticketmaster)

    # Event-level watchers (ticketmaster only) aggregate seats across every
    # active performance under the event; perf-level watchers hit one perf.
    event_level = src is ticketmaster and ticketmaster.is_event_level(w)
    if event_level:
        perf_url = ticketmaster.event_url(w["event_code"])
        key_fn = ticketmaster.event_seat_key
    else:
        perf_url = src.perf_url(w["event_code"], w["perf_code"])
        key_fn = src.seat_key

    try:
        if event_level:
            seats, perf_errors = ticketmaster.fetch_event_seats(w["event_code"])
            if perf_errors:
                print(f"[{now_iso}] {label}: per-perf errors {perf_errors}", flush=True)
        else:
            seats = src.fetch_selectable_seats(w["event_code"], w["perf_code"])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        db.tm_update_watcher(wid, {"last_check_at": now_iso, "last_check_error": err})
        return 0, err

    prev_keys = db.tm_get_seat_keys(wid)
    is_baseline = not prev_keys and not w.get("last_check_at")
    was_empty = (w.get("last_seat_count") or 0) == 0
    added, removed = _diff_seats_set(prev_keys, seats, key_fn)
    db.tm_replace_seat_state(wid, seats)
    # Unconfirmed (sold-out perf) seats: only perfs already under tracking may
    # ping — the tick that STARTS tracking a perf sees its whole standing
    # unsold-seat set as "added", which is inventory, not a drop. The per-perf
    # `_tracking` sentinel in prev state marks tracking as established.
    # (Keep in sync with app.py._check_one_watcher.)
    if any(s.get("unconfirmed") for s in added):
        _tracked = {k.split("|", 2)[1] for k in prev_keys if k.startswith("U|")}
        added = [s for s in added
                 if not s.get("_tracking")
                 and (not s.get("unconfirmed") or (s.get("_perf") or "") in _tracked)]
    db.tm_update_watcher(wid, {
        "last_check_at": now_iso,
        "last_check_error": None,
        "last_seat_count": len(seats),
    })
    if is_baseline:
        return 0, None

    if added:
        # Probe block: use the first added REAL seat's block (and its perf for
        # event-level watchers — labels are perf-scoped on TM's API). Status
        # pseudo-seats carry a synthetic block label that would force a
        # labels refetch every flip.
        probe = next(
            (s.get("block") or s.get("b") for s in added
             if (s.get("block") or s.get("b")) and not (s.get("festival") or s.get("ga"))),
            None,
        )
        if event_level:
            probe_perf = next((s.get("_perf") for s in added if s.get("_perf")), None)
            try:
                lbls = src.get_labels(w["event_code"], probe_perf, lang="iw", missing_block=probe) if probe_perf else None
            except Exception:
                lbls = None
        else:
            try:
                lbls = src.get_labels(w["event_code"], w["perf_code"], lang="iw", missing_block=probe)
            except Exception:
                lbls = None
        matched = watcher_filters.apply(added, seats, w.get("filters"), labels=lbls)
        # Mixed venues (a seat map PLUS a GA lawn/pit) also carry a GA status
        # pseudo-seat whose soldout↔available flips are noise next to real
        # per-seat drops. A watcher whose snapshot has actual seats only
        # pings on those; GA-ONLY events keep their status pings.
        # (Keep in sync with app.py._check_one_watcher.)
        if any(not (s.get("festival") or s.get("ga")) for s in seats):
            matched = [s for s in matched if not s.get("ga")]
        # Per-seat cool-down: drop physical seats that already pinged inside
        # the window so a flapping VIP seat can't re-ping all day. Status
        # pseudo-seats are never cooled. (Keep in sync with app.py.)
        if matched and SEAT_COOLDOWN_SECONDS > 0:
            phys_keys = [key_fn(s) for s in matched if not (s.get("festival") or s.get("ga"))]
            cooled = db.tm_seat_cooldown_active(wid, phys_keys, SEAT_COOLDOWN_SECONDS, now_iso)
            if cooled:
                matched = [s for s in matched
                           if (s.get("festival") or s.get("ga")) or key_fn(s) not in cooled]
        master_muted = db.setting_get_bool("master_muted", default=False)
        watcher_muted = bool(w.get("muted"))
        channels_csv = (w.get("notify_channels") or "discord,email").strip().lower()
        enabled = {c for c in (s.strip() for s in channels_csv.split(",")) if c}
        if master_muted or watcher_muted:
            enabled = set()
        # Status flip — fires on every sold-out → available transition (since
        # last_seat_count returns to 0 between flips), satisfying the
        # "keep notifying" requirement for event-level watchers.
        status_flipped = event_level and was_empty and len(seats) > 0
        headline = f"🎟️ {w['event_code']} — tickets just opened" if status_flipped and matched else None
        # Festival/hub + kupat-GA watchers — and TM event-level per-perf
        # status seats — carry a status flag per tick (the seat key encodes
        # it), so any transition shows up as an `added` seat — phrase the
        # ping for the new status. (Keep in sync with app.py.)
        # Scan matched (not added) so a GA pseudo-seat stripped above can't
        # slap a status headline onto a real-seat ping.
        _fest = next((s for s in matched if s.get("festival") or s.get("ga")), None)
        if _fest:
            _nm = label or w["event_code"]
            headline = {
                "soldout": f"❌ Sold out — {_nm}",
                "lasttickets": f"⚠️ Last tickets — {_nm}",
                "available": f"🎟️ Available again — {_nm}",
                "closed": f"⛔ Sales closed — {_nm}",
            }.get(_fest.get("status"), headline)
        # DICE restock snipe — loud, one-tap (perf_url opens the DICE app).
        # (Keep in sync with app.py._check_one_watcher.)
        if src_name == "dice" and matched:
            _dnm = ((lbls or {}).get("meta") or {}).get("eventName") or label or w["event_code"]
            headline = f"🎯 BUY NOW — {_dnm}" + (" · restocked" if was_empty else "")
        # haku: every ping is registration news — spots back at a charity or
        # the sold-out prose changing. (Keep in sync with app.py.)
        if src_name == "haku" and matched:
            _hnm = ((lbls or {}).get("meta") or {}).get("eventName") or label or w["event_code"]
            if any(s.get("kind") == "rfar" for s in matched):
                headline = f"🏃 Charity entries — {_hnm}"
            else:
                headline = f"🏃 Registration update — {_hnm}"
        # tmdiscover: every matched seat is a date whose status box just
        # turned buyable (sold out / not-yet-open → last tickets or on sale),
        # so headline the flip instead of "N new seats". A date going the
        # other way is a silent removal and never reaches here.
        # (Keep in sync with app.py._check_one_watcher.)
        if src_name == "tmdiscover" and matched:
            _snm = ((lbls or {}).get("meta") or {}).get("eventName") or label or w["event_code"]
            _last = all((s.get("status") or "") == "low_availability" for s in matched)
            _n = len(matched)
            _icon, _tail = ("⚠️", " (כרטיסים אחרונים)") if _last else ("🎟️", "")
            headline = f"{_icon} {_n} date{'s' if _n != 1 else ''} just opened{_tail} — {_snm}"

        if enabled and matched:
            result = notify.notify_drop(
                label=label, perf_url=perf_url,
                added_seats=matched, removed_count=len(removed),
                total_now=len(seats), labels=lbls, channels=enabled,
                headline=headline,
                discord_override=discord_bot.webhook_for(w.get("discord_channel")),
            )
            # Stamp the physical seats we just pinged so they cool down.
            if SEAT_COOLDOWN_SECONDS > 0:
                db.tm_seat_cooldown_mark(
                    wid,
                    [key_fn(s) for s in matched if not (s.get("festival") or s.get("ga"))],
                    now_iso,
                )
        elif not matched:
            result = {"discord": "skipped (filtered)", "email": "skipped (filtered)"}
        else:
            reason = "master-muted" if master_muted else ("watcher-muted" if watcher_muted else "channels-empty")
            result = {"discord": f"skipped ({reason})", "email": f"skipped ({reason})"}
        db.tm_record_drop(
            wid, len(added), len(removed),
            json.dumps(added, ensure_ascii=False)[:8000],
            json.dumps(result)[:1000],
            now_iso,
            notify_count=len(matched),
        )
    return len(added), None


def _kupat_session_for(watchers):
    """Shared kupat browser for a tick, or a no-op when no kupat watcher is
    in the list. (Keep in sync with app.py._kupat_session_for.)"""
    if any((w.get("source") or "") == "kupat" for w in watchers):
        return kupat.shared_session()
    return contextlib.nullcontext()


def tick():
    if not _lock.acquire(blocking=False):
        return
    try:
        if db.setting_get_bool("master_paused", default=False):
            now_iso = datetime.now(timezone.utc).isoformat()
            _state.update(at=now_iso, checked=0, drops=0, errors=0, paused=True)
            print(f"[{now_iso}] master_paused — skipping tick", flush=True)
            return
        watchers = db.tm_active_watchers()
        now_iso = datetime.now(timezone.utc).isoformat()
        drops = errors = 0
        # One Chromium for every kupat watcher in this tick instead of one
        # each — see kupat.shared_session. No-op when none are due.
        # (Keep in sync with app.py.run_tm_check.)
        with _kupat_session_for(watchers):
            for w in watchers:
                try:
                    added, err = check_one(w, now_iso)
                    if err:
                        errors += 1
                    if added:
                        drops += 1
                except Exception:
                    errors += 1
                    traceback.print_exc()
        _state.update(at=now_iso, checked=len(watchers), drops=drops, errors=errors, paused=False)
        print(f"[{now_iso}] checked={len(watchers)} drops={drops} errors={errors}", flush=True)
    finally:
        _lock.release()


DICE_SNIPER_SECONDS = int(os.getenv("KARTIS_DICE_SNIPER_SECONDS") or 15)
DICE_SNIPER_ENABLED = (os.getenv("KARTIS_DICE_SNIPER_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")

# Fast poll for the TM-IL sources — see the note in app.py. Pure HTTP and
# cheap, so unlike dice's sniper it is not gated on being sold out.
TM_SNIPER_SECONDS = int(os.getenv("KARTIS_TM_SNIPER_SECONDS") or 20)
TM_SNIPER_ENABLED = (os.getenv("KARTIS_TM_SNIPER_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
TM_SNIPER_SOURCES = {"ticketmaster", "tmdiscover"}


def dice_sniper():
    """Fast restock poll for SOLD-OUT dice watchers only (mirror of
    app.run_dice_sniper). Shares `_lock` with the main tick so the same
    watcher is never fetched twice at once."""
    if db.setting_get_bool("master_paused", default=False):
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        for w in db.tm_active_watchers():
            if (w.get("source") or "") != "dice" or w.get("paused"):
                continue
            if (w.get("last_seat_count") or 0) != 0:
                continue
            try:
                check_one(w, now_iso)
            except Exception:
                traceback.print_exc()
    finally:
        _lock.release()


def tm_sniper():
    """Fast poll for ticketmaster / tmdiscover watchers (mirror of
    app.run_tm_sniper). Shares `_lock` with the main tick so the same watcher
    is never fetched twice at once."""
    if not TM_SNIPER_ENABLED:
        return
    if db.setting_get_bool("master_paused", default=False):
        return
    if not _lock.acquire(blocking=False):
        return
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        for w in db.tm_active_watchers():
            if (w.get("source") or "") not in TM_SNIPER_SOURCES or w.get("paused"):
                continue
            try:
                check_one(w, now_iso)
            except Exception:
                traceback.print_exc()
    finally:
        _lock.release()


def main():
    # Watcher labels are Hebrew (TM event names); the default Windows console
    # encoding (cp1252) can't render them and crashes the startup print.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"[kartis-watcher] starting; interval={INTERVAL}s; db={db.DB_PATH}", flush=True)
    watchers = db.tm_active_watchers()
    print(f"[kartis-watcher] {len(watchers)} active watcher(s):", flush=True)
    for w in watchers:
        print(f"  - {w['event_code']}/{w['perf_code']} :: {w.get('label') or '(no label)'}", flush=True)
    if not watchers:
        print("  (none — add one with: python add_watcher.py <ticketmaster URL>)", flush=True)

    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    sched.add_job(tick, "interval", seconds=INTERVAL, id="tm_check")
    if DICE_SNIPER_ENABLED:
        sched.add_job(dice_sniper, "interval", seconds=DICE_SNIPER_SECONDS,
                      id="dice_sniper", max_instances=1)
        print(f"[kartis-watcher] dice restock-snipe on: every {DICE_SNIPER_SECONDS}s while sold out", flush=True)
    if TM_SNIPER_ENABLED:
        sched.add_job(tm_sniper, "interval", seconds=TM_SNIPER_SECONDS,
                      id="tm_sniper", max_instances=1)
        print(f"[kartis-watcher] TM-IL snipe on: every {TM_SNIPER_SECONDS}s", flush=True)
    sched.start()

    # Run one immediate tick so the user sees activity right away
    tick()

    # Wait for SIGINT/SIGTERM
    stop = threading.Event()
    def _shutdown(signum, frame):
        print(f"[kartis-watcher] caught signal {signum}, stopping…", flush=True)
        stop.set()
    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)
    try:
        while not stop.is_set():
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        print("[kartis-watcher] stopped.", flush=True)


if __name__ == "__main__":
    main()
