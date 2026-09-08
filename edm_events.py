"""US EDM event-tracker registry — source routing, the tracked-event list,
and the CLI for managing it.

The per-platform fetchers (posh_events / leap_events / eventim_events /
shotgun_events) each watch ONE event and return the same normalized dict
(the contract is in edm_common.py); this module decides which
fetcher a URL belongs to, keeps the list of events being polled in
edm_tracked_events, and fetches them all for one tick of app.py's
`run_edm_events`.

Adding another event from any supported platform is data, not code:

    python edm_events.py --add "https://posh.vip/e/some-slug"
    python edm_events.py --list
    python edm_events.py --remove posh:some-slug
    python edm_events.py --pause posh:some-slug     (--resume to undo)
    python edm_events.py --probe "<url>"            (fetch, don't store)

`--add` validates by actually fetching the event first, so a typo'd or
unsupported URL fails at the prompt instead of silently erroring once a
minute in the scheduler.

To support a NEW platform, write a module exposing SOURCE_NAME /
parse_url(url) / fetch_event(key) and add one row to SOURCES plus one to
_HOST_HINTS below — nothing else in the stack needs to change.
"""
import argparse
import json
import sys
from datetime import datetime, timezone

import db
import edm_common
import eventim_events
import leap_events
import posh_events
import shotgun_events
import tao_events
import tixr_events
from edm_common import EdmEventsError

SOURCES = {
    posh_events.SOURCE_NAME: posh_events,
    leap_events.SOURCE_NAME: leap_events,
    eventim_events.SOURCE_NAME: eventim_events,
    shotgun_events.SOURCE_NAME: shotgun_events,
    tixr_events.SOURCE_NAME: tixr_events,
    tao_events.SOURCE_NAME: tao_events,
}

# Host fragment → source. Checked before the parse_url fallback so a URL is
# routed by domain rather than by whichever regex happens to match first.
_HOST_HINTS = (
    ("posh.vip", "posh"),
    ("leapevents.com", "leap"),
    ("eventim.us", "eventim"),
    ("shotgun.live", "shotgun"),
    ("tixr.com", "tixr"),
    ("tickets.taogroup.com", "tao"),
)

# The events this tracker was built for. Seeded once, on a
# fresh DB only (db.edm_seed_tracked latches a setting), so removing one
# doesn't bring it back on the next restart.
DEFAULT_TRACKED = [
    ("posh", "hello-48",
     "https://posh.vip/e/hello-48"),
    ("leap", "led-presents-temper-festival",
     "https://events.leapevents.com/event/led-presents-temper-festival"),
    ("eventim", "690917@TheConcourseProject",
     "https://wl.eventim.us/event/"
     "ISOxo-pres-Hardcore-Diva-Night-1-at-The-Concourse-Project/690917"
     "?afflky=TheConcourseProject"),
    ("shotgun", "jigitz0912",
     "https://shotgun.live/en/events/jigitz0912"),
    # Needs the CDP Chrome (DataDome) — errors harmlessly on a box without it.
    ("tixr", "189343",
     "https://www.tixr.com/groups/navypiereventcenter/events/"
     "isoxo-presents-hardcore-diva-189343"),
]


def detect_source(url):
    """Source name for a URL, or None. Host match first, then each module's
    own parse_url as a fallback (which also accepts bare keys)."""
    s = (url or "").strip().lower()
    for frag, name in _HOST_HINTS:
        if frag in s:
            return name
    for name, mod in SOURCES.items():
        try:
            mod.parse_url(url)
            return name
        except ValueError:
            continue
    return None


def parse_target(url):
    """URL (or '<source>:<key>') → (source, event_key). Raises ValueError."""
    s = (url or "").strip()
    if ":" in s and "//" not in s:
        head, _, tail = s.partition(":")
        if head in SOURCES and tail:
            return head, SOURCES[head].parse_url(tail)
    source = detect_source(s)
    if not source:
        raise ValueError(
            f"unsupported EDM event URL: {url!r} — expected posh.vip, "
            "events.leapevents.com, wl.eventim.us, shotgun.live, tixr.com "
            "or tickets.taogroup.com")
    return source, SOURCES[source].parse_url(s)


def fetch_one(source, event_key):
    """Fetch and normalize one event. Raises EdmEventsError / ValueError."""
    mod = SOURCES.get(source)
    if mod is None:
        raise EdmEventsError(f"unknown EDM source {source!r}")
    return mod.fetch_event(event_key)


def fetch_url(url):
    """Convenience: fetch straight from a URL without touching the DB."""
    source, key = parse_target(url)
    return fetch_one(source, key)


def fetch_tracked(rows=None):
    """Fetch every tracked (non-paused) event.

    Returns (events, errors) where errors is {event_id: message}. One
    source being down must never sink the tick — each event is fetched
    independently and a failure is reported, not raised, so the other
    events still diff and ping normally."""
    if rows is None:
        rows = db.edm_tracked_all(include_paused=False)
    events, errors = [], {}
    for row in rows:
        try:
            ev = fetch_one(row["source"], row["event_key"])
        except (EdmEventsError, ValueError) as e:
            errors[row["event_id"]] = f"{type(e).__name__}: {e}"
            continue
        except Exception as e:  # a parser bug must not kill the scheduler job
            errors[row["event_id"]] = f"{type(e).__name__}: {e}"
            continue
        ev["tracked_url"] = row.get("url")
        ev["label"] = row.get("label")
        events.append(ev)
    return events, errors


def _now():
    return datetime.now(timezone.utc).isoformat()


# Sources that watch a whole VENUE CALENDAR instead of a hand-picked event:
# each exposes discover() -> (entries, errors) where an entry carries
# event_key / ticket_url / venue / name (see tao_events.discover). Their
# rows in edm_tracked_events are maintained by sync_catalogs() below and
# marked with auto_source, so the poll list stays current without anyone
# pasting URLs. This is the pacha-style half of the tracker.
CATALOG_SOURCES = {
    tao_events.SOURCE_NAME: tao_events,
}


def sync_catalogs(source_names=None, now_iso=None, prune=True):
    """Reconcile the catalog sources' venue listings into the poll list.

    Adds every upcoming event that has a ticket page, prunes the auto-added
    rows the calendar no longer lists (the show happened, or it was pulled),
    and leaves hand-added rows alone in both directions.

    Returns {source: {"added": [...], "known": n, "pruned": [...],
                      "skipped": n, "baseline": bool, "errors": {...}}}.

    `baseline` is True on the very first sync for a source (nothing was
    auto-tracked yet), which means `added` is a backfill of everything
    currently on sale rather than news — the caller suppresses its pings,
    exactly as the pacha monitor's first tick does. Every sync after that,
    an entry in `added` really is a newly announced show.

    Pruning is skipped for a source whose discover() reported ANY venue
    error: a calendar that 500s comes back as "no upcoming events", and
    pruning on that would silently untrack the whole venue — the same
    reason every other fetcher here treats an empty parse as a failure."""
    now_iso = now_iso or _now()
    out = {}
    for name in (source_names or CATALOG_SOURCES):
        mod = CATALOG_SOURCES.get(name)
        if mod is None:
            continue
        try:
            entries, errors = mod.discover()
        except Exception as e:      # a parser bug must not kill the tick
            out[name] = {"added": [], "known": 0, "pruned": [], "skipped": 0,
                         "baseline": False,
                         "errors": {"discover": f"{type(e).__name__}: {e}"}}
            continue

        existing = db.edm_tracked_auto_ids(f"{name}:")
        keep, added, skipped = [], [], 0
        for entry in entries:
            if not entry.get("event_key"):
                skipped += 1        # reservations-only show, nothing to poll
                continue
            event_id = f"{name}:{entry['event_key']}"
            keep.append(event_id)
            db.edm_tracked_add(
                name, entry["event_key"], entry.get("ticket_url"), now_iso,
                label=entry.get("name"),
                auto_source=f"{name}:{entry.get('venue_key') or 'catalog'}",
                # Deleting an auto row is futile (the next sync re-adds it),
                # so pausing is how a human silences one — don't undo it.
                unpause=False)
            if event_id not in existing:
                added.append(event_id)
        pruned = (db.edm_tracked_auto_prune(f"{name}:", keep)
                  if prune and not errors else [])
        out[name] = {"added": added, "known": len(keep), "pruned": pruned,
                     "skipped": skipped, "baseline": not existing,
                     "errors": errors}
    return out


def cmd_sync_catalogs(args):
    results = sync_catalogs()
    for name, r in results.items():
        print(f"{name}: {r['known']} upcoming, {len(r['added'])} added"
              + (" (baseline)" if r["baseline"] else "")
              + f", {len(r['pruned'])} pruned, "
                f"{r['skipped']} without a ticket page")
        for eid in r["added"]:
            print(f"  + {eid}")
        for eid in r["pruned"]:
            print(f"  - {eid}")
        for where, msg in (r["errors"] or {}).items():
            print(f"  ERROR {where}: {msg}")
    return 0


def cmd_list(args):
    tracked = db.edm_tracked_all()
    if not tracked:
        print("no EDM events tracked — add one with --add <url>")
        return 0
    seen = db.edm_all_seen()
    print(f"{len(tracked)} tracked EDM event(s):\n")
    for r in tracked:
        s = seen.get(r["event_id"]) or {}
        state = []
        if r["paused"]:
            state.append("PAUSED")
        if s.get("sold_out"):
            state.append("SOLD OUT")
        elif s.get("on_sale"):
            state.append("on sale")
        if s.get("last_error"):
            state.append("ERROR")
        price = f"${s['min_price']:,.2f}" if s.get("min_price") is not None else "—"
        print(f"  {r['event_id']}")
        print(f"      {s.get('name') or r.get('label') or '(not yet fetched)'}")
        print(f"      from {price}   {' '.join(state) or '—'}"
              + (f"   lead: {s['lead_tier']}" if s.get("lead_tier") else ""))
        if s.get("last_error"):
            print(f"      last error: {s['last_error']}")
        print(f"      {r['url']}")
    return 0


def cmd_add(args):
    source, key = parse_target(args.add)
    event_id = f"{source}:{key}"
    print(f"validating {event_id} …")
    ev = fetch_one(source, key)          # fail loudly here, not in the scheduler
    db.edm_tracked_add(source, key, args.add, _now(), label=args.label)
    print(f"tracking {event_id}: {ev['name']}")
    edm_common.print_event(ev)
    return 0


def cmd_remove(args):
    n = db.edm_tracked_remove(args.remove)
    print(f"removed {args.remove}" if n else f"not tracked: {args.remove}")
    return 0 if n else 1


def cmd_pause(event_id, paused):
    n = db.edm_tracked_set_paused(event_id, paused)
    word = "paused" if paused else "resumed"
    print(f"{word} {event_id}" if n else f"not tracked: {event_id}")
    return 0 if n else 1


def cmd_probe(args):
    ev = fetch_url(args.probe)
    if args.json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
    else:
        edm_common.print_event(ev)
    return 0


def cmd_fetch_all(args):
    events, errors = fetch_tracked()
    if args.json:
        print(json.dumps({"events": events, "errors": errors},
                         indent=1, ensure_ascii=False))
        return 0
    for ev in events:
        edm_common.print_event(ev)
        print()
    for eid, err in errors.items():
        print(f"ERROR {eid}: {err}")
    print(f"{len(events)} ok, {len(errors)} failed")
    return 1 if errors and not events else 0


def main(argv):
    p = argparse.ArgumentParser(
        description="US EDM event trackers (posh / leap / eventim / "
                    "shotgun / tixr / tao)")
    p.add_argument("--list", action="store_true", help="list tracked events")
    p.add_argument("--sync-catalogs", action="store_true", dest="sync_catalogs",
                   help="reconcile the venue-catalog sources (tao) into the "
                        "poll list: add newly announced shows, drop the ones "
                        "that have happened")
    p.add_argument("--add", metavar="URL", help="track an event URL")
    p.add_argument("--label", help="optional note to store with --add")
    p.add_argument("--remove", metavar="EVENT_ID", help="stop tracking")
    p.add_argument("--pause", metavar="EVENT_ID", help="pause polling")
    p.add_argument("--resume", metavar="EVENT_ID", help="resume polling")
    p.add_argument("--probe", metavar="URL",
                   help="fetch one URL without storing anything")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args(argv)

    # --probe is the only pure-network command; everything else reads or
    # writes the tracked list, so make sure the schema exists first (same
    # contract as add_watcher.py) and seed the original three on a fresh DB.
    if not args.probe:
        db.init()
        db.edm_seed_tracked(DEFAULT_TRACKED, _now())

    try:
        if args.add:
            return cmd_add(args)
        if args.remove:
            return cmd_remove(args)
        if args.pause:
            return cmd_pause(args.pause, True)
        if args.resume:
            return cmd_pause(args.resume, False)
        if args.probe:
            return cmd_probe(args)
        if args.sync_catalogs:
            return cmd_sync_catalogs(args)
        if args.list:
            return cmd_list(args)
        return cmd_fetch_all(args)
    except (EdmEventsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
