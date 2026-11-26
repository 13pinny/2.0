"""boletosexpress.com stock tracker — the EDM-source face of boletos.py.

Same page, same parser (`boletos.parse_event`); this turns each SECTION of
a seated map (or each ticket type of a GA table) into a normalized tier so
the EDM tick can track it: one tier per section with `quantity` = seats on
the map, `available` = seats marked `open`, `used` = the rest (sold, held
or not released — the page doesn't tell them apart). Those are real counts,
so boletos rides the per-event "ping me under N left" alert
(app._LEFT_ALERT_SOURCES, kind `below`) and the `tier_restock` ping like
Frontgate. Its pings go to the show's own Discord channel (notify routes
source `boletos` to `#<event-slug>`, the channel a drop watcher on the same
show uses).

The drop-checker plugin (boletos.py) is the per-seat half: it pings each
returned seat with its row and number. Use the tracker for "how many are
left", the watcher for "a seat just came back".

Probe:  python boletos_events.py "<boletosexpress.com event URL>" [--json]
"""
import json
import sys

import boletos
import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "boletos"


def parse_url(url):
    """Event URL (or bare id) → event key: the numeric event id."""
    try:
        return boletos.parse_url(url)[0]
    except boletos.BoletosError as e:
        raise ValueError(str(e)) from e


def fetch_event(event_key):
    try:
        page = boletos.get_page(event_key)
        ev = boletos.parse_event(page)
    except boletos.BoletosError as e:
        raise EdmEventsError(str(e)) from e
    boletos._write_labels(event_key, ev)
    seated = ev["layout"] == "seated"
    runs = boletos.row_runs(ev["seats"]) if seated else {}
    tiers = []
    for s in ev["sections"]:
        q, a = s["quantity"], s["available"]
        t = make_tier(
            s["name"], s["price"], available=a, quantity=q,
            used=(q - a) if (q is not None and a is not None) else None,
            sold_out=s["sold_out"], max_qty=s["max_qty"])
        # {row: longest run of open seats side by side} — what the
        # "N seats together" alert (app._boletos_runs_kinds) counts. Rows
        # with nothing open are left out; it rides in tiers_json.
        if seated:
            t["row_runs"] = runs.get(s["name"], {})
        tiers.append(t)
    start = ev.get("start")
    out = {
        "source": SOURCE_NAME,
        "event_key": str(event_key),
        "name": ev.get("name") or f"Boletos event {event_key}",
        "venue": ev.get("venue"),
        "date_text": start.replace("T", " ") if start else None,
        "start_date": start,
        "page_url": boletos.perf_url(event_key),
        "image": ev.get("image"),
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
    }
    out = rollup(out)
    if seated:
        out["total_released"] = sum(t["quantity"] or 0 for t in tiers)
        out["total_sold"] = sum(t["used"] or 0 for t in tiers)
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print('usage: python boletos_events.py "<boletosexpress.com event URL>" [--json]')
        sys.exit(2)
    ev = fetch_event(parse_url(args[0]))
    if "--json" in sys.argv:
        print(json.dumps(ev, indent=2, ensure_ascii=False))
    else:
        edm_common.print_event(ev)
