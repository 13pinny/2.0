"""CLI helper for the headless watcher: add or list watchers without the dashboard.

Usage:
    python add_watcher.py <URL or shorthand>                  # add (auto-detects source)
    python add_watcher.py --list                              # list
    python add_watcher.py --remove <id>                       # delete

Source is detected from the URL (kupat.co.il or ticketmaster.co.il). For
shorthand FORMAT/PERF, alpha-prefixed event codes route to ticketmaster
and digit-only ones route to kupat.
"""
import re
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

import barby
import db
import dice
import kupat
import tickchak
import ticketmaster

load_dotenv()
db.init()

SOURCES = {"ticketmaster": ticketmaster, "kupat": kupat, "tickchak": tickchak,
           "barby": barby, "dice": dice}


def detect_source(url):
    s = (url or "").strip().lower()
    if "barby.co.il" in s:
        return "barby", barby
    if "dice.fm" in s or re.fullmatch(r"\s*[a-f0-9]{24}\s*", s or ""):
        return "dice", dice
    if "tickchak.co.il" in s:
        return "tickchak", tickchak
    if "kupat.co.il" in s:
        return "kupat", kupat
    if "ticketmaster.co.il" in s:
        return "ticketmaster", ticketmaster
    if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", s or ""):
        return "kupat", kupat
    return "ticketmaster", ticketmaster


def cmd_add(url):
    src_name, src = detect_source(url)
    try:
        event_code, perf_code = src.parse_url(url)
    except Exception as e:
        print(f"error: {src_name}: {e}", file=sys.stderr)
        sys.exit(2)
    for existing in db.tm_all_watchers():
        if (
            (existing.get("source") or "ticketmaster") == src_name
            and existing["event_code"] == event_code
            and existing["perf_code"] == perf_code
        ):
            print(f"already watching {src_name} {event_code}/{perf_code} (id={existing['id']})")
            return
    # Event-level (perf_code='ALL', ticketmaster only) watchers cover every
    # performance under the event and use a different fetch path.
    event_level = src is ticketmaster and perf_code == ticketmaster.EVENT_LEVEL_PERF
    try:
        if event_level:
            perfs = ticketmaster.list_performances(event_code)
            probe_perf = str((perfs[0] or {}).get("performanceCode") or "")
            lbls = src.get_labels(event_code, probe_perf, lang="iw") if probe_perf else None
            base = src.event_summary(lbls) if lbls else event_code
            label = f"{base} — all dates" if base else f"{event_code} — all dates"
        else:
            lbls = src.get_labels(event_code, perf_code, lang="iw")
            label = src.event_summary(lbls) or f"{event_code}/{perf_code}"
    except Exception:
        label = f"{event_code}/{perf_code}"
    wid = "tmw-" + uuid.uuid4().hex[:12]
    db.tm_insert_watcher({
        "id": wid, "label": label, "source": src_name,
        "event_code": event_code, "perf_code": perf_code,
        "paused": 0, "muted": 0, "notify_channels": "discord,email",
    }, datetime.now(timezone.utc).isoformat())
    print(f"added [{src_name}] {event_code}/{perf_code} :: {label} (id={wid})")
    if event_level:
        seats, perf_errors = ticketmaster.fetch_event_seats(event_code)
        if perf_errors:
            print(f"  per-perf errors at baseline: {perf_errors}")
    else:
        seats = src.fetch_selectable_seats(event_code, perf_code)
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
        flags = []
        if w.get("paused"):
            flags.append("PAUSED")
        if w.get("muted"):
            flags.append("MUTED")
        if w.get("last_check_error"):
            flags.append("ERROR")
        if not flags:
            flags = ["ACTIVE"]
        flag = ",".join(flags)
        src = w.get("source") or "ticketmaster"
        last = w.get("last_check_at") or "—"
        cnt = w.get("last_seat_count")
        cnt_s = "—" if cnt is None else str(cnt)
        chans = w.get("notify_channels") or "discord,email"
        err = w.get("last_check_error") or ""
        print(f"  [{flag:<14}] {src:<12} {w['event_code']}/{w['perf_code']:<5}  seats={cnt_s:>4}  channels={chans}  last={last}  id={w['id']}")
        print(f"                                  {w.get('label')}")
        if err:
            print(f"                                  ERR: {err}")


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
