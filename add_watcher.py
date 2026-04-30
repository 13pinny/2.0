"""CLI helper for the headless watcher: add or list watchers without the dashboard.

Usage:
    python add_watcher.py <ticketmaster URL or EVENT/PERF>      # add
    python add_watcher.py --list                                # list
    python add_watcher.py --remove <id>                         # delete
"""
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

import db
import labels as tm_labels
import ticketmaster

load_dotenv()
db.init()


def cmd_add(url):
    try:
        event_code, perf_code = ticketmaster.parse_url(url)
    except ticketmaster.TicketmasterError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    for existing in db.tm_all_watchers():
        if existing["event_code"] == event_code and existing["perf_code"] == perf_code:
            print(f"already watching {event_code}/{perf_code} (id={existing['id']})")
            return
    try:
        lbls = tm_labels.get_labels(event_code, perf_code, lang="iw")
        label = tm_labels.event_summary(lbls) or f"{event_code}/{perf_code}"
    except Exception:
        label = f"{event_code}/{perf_code}"
    wid = "tmw-" + uuid.uuid4().hex[:12]
    db.tm_insert_watcher({
        "id": wid, "label": label,
        "event_code": event_code, "perf_code": perf_code, "paused": 0,
    }, datetime.now(timezone.utc).isoformat())
    print(f"added {event_code}/{perf_code} :: {label} (id={wid})")
    # Capture the baseline so the next tick doesn't report existing seats as a drop
    seats = ticketmaster.fetch_selectable_seats(event_code, perf_code)
    db.tm_replace_seat_state(wid, seats)
    db.tm_update_watcher(wid, {
        "last_check_at": datetime.now(timezone.utc).isoformat(),
        "last_seat_count": len(seats),
    })
    print(f"baseline: {len(seats)} selectable seats currently")


def cmd_list():
    rows = db.tm_all_watchers()
    if not rows:
        print("(no watchers)")
        return
    for w in rows:
        flag = "PAUSED" if w["paused"] else "ACTIVE"
        last = w.get("last_check_at") or "—"
        cnt = w.get("last_seat_count")
        cnt_s = "—" if cnt is None else str(cnt)
        err = w.get("last_check_error") or ""
        print(f"  [{flag}] {w['event_code']}/{w['perf_code']:>3}  seats={cnt_s:>4}  last={last}  id={w['id']}")
        print(f"          {w.get('label')}")
        if err:
            print(f"          ERR: {err}")


def cmd_remove(wid):
    if not db.tm_get_watcher(wid):
        print(f"no watcher with id {wid}", file=sys.stderr)
        sys.exit(2)
    db.tm_delete_watcher(wid)
    print(f"removed {wid}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    if args[0] == "--list":
        cmd_list()
    elif args[0] == "--remove" and len(args) >= 2:
        cmd_remove(args[1])
    else:
        cmd_add(args[0])


if __name__ == "__main__":
    main()
