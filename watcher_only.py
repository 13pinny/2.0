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
import json
import signal
import time
import threading
import traceback
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

import db
import labels as tm_labels
import notify
import ticketmaster

load_dotenv()
db.init()

import os
INTERVAL = int(os.getenv("TM_CHECK_INTERVAL_SECONDS") or 60)

_lock = threading.Lock()
_state = {"at": None, "checked": 0, "drops": 0, "errors": 0}


def check_one(w, now_iso):
    wid = w["id"]
    label = w.get("label") or f"{w['event_code']}/{w['perf_code']}"
    perf_url = ticketmaster.perf_url(w["event_code"], w["perf_code"])
    try:
        seats = ticketmaster.fetch_selectable_seats(w["event_code"], w["perf_code"])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        db.tm_update_watcher(wid, {"last_check_at": now_iso, "last_check_error": err})
        return 0, err

    prev_keys = db.tm_get_seat_keys(wid)
    is_baseline = not prev_keys and not w.get("last_check_at")
    added, removed, _ = ticketmaster.diff_seats(prev_keys, seats)
    db.tm_replace_seat_state(wid, seats)
    db.tm_update_watcher(wid, {
        "last_check_at": now_iso,
        "last_check_error": None,
        "last_seat_count": len(seats),
    })
    if is_baseline:
        return 0, None

    if added:
        probe = next((s.get("b") for s in added if s.get("b")), None)
        lbls = tm_labels.get_labels(w["event_code"], w["perf_code"], lang="iw", missing_block=probe)
        result = notify.notify_drop(
            label=label, perf_url=perf_url,
            added_seats=added, removed_count=len(removed),
            total_now=len(seats), labels=lbls,
        )
        db.tm_record_drop(
            wid, len(added), len(removed),
            json.dumps(added, ensure_ascii=False)[:8000],
            json.dumps(result)[:1000],
            now_iso,
        )
    return len(added), None


def tick():
    if not _lock.acquire(blocking=False):
        return
    try:
        watchers = db.tm_active_watchers()
        now_iso = datetime.now(timezone.utc).isoformat()
        drops = errors = 0
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
        _state.update(at=now_iso, checked=len(watchers), drops=drops, errors=errors)
        print(f"[{now_iso}] checked={len(watchers)} drops={drops} errors={errors}", flush=True)
    finally:
        _lock.release()


def main():
    print(f"[kartis-watcher] starting; interval={INTERVAL}s; db={db.DB_PATH}", flush=True)
    watchers = db.tm_active_watchers()
    print(f"[kartis-watcher] {len(watchers)} active watcher(s):", flush=True)
    for w in watchers:
        print(f"  - {w['event_code']}/{w['perf_code']} :: {w.get('label') or '(no label)'}", flush=True)
    if not watchers:
        print("  (none — add one with: python add_watcher.py <ticketmaster URL>)", flush=True)

    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    sched.add_job(tick, "interval", seconds=INTERVAL, id="tm_check")
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
