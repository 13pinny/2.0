"""ticketmaster.co.il new-event monitor — pure-HTTP event-listing fetcher.

Companion to pacha_events.py / kupat_events.py; the diff-and-ping loop lives
in app.py (`run_il_events`). This module fetches the CURRENT event list from
the two anonymous homepage feeds the SPA itself uses (found by sniffing the
webclient's XHRs — the homepage route is /homepage/ALL/iw):

  GET /wbtxapi/api/v1/bxcached/event/getAllTopEvent/iw
      → every homepage-carousel item. eventType EVENT rows carry
        btxEventId ("MTA65"), eventName, venueName/City, first/last
        performance epoch-ms, priceComment. eventType GROUP rows carry
        btxEventGroupId + eventGroupName (member events usually also appear
        as their own EVENT rows).
  GET /wbtxapi/api/v1/bxcached/event/getSpotLightData/ALL/ALL/iw
      → the hero banners; merged in for events not in the top list.

Both answer plain urllib with the same CHANNEL/CPU headers ticketmaster.py
already sends. This is the site's "featured" feed rather than a guaranteed
catalog, but TM-IL promotes essentially everything on the homepage.

Neither feed says whether sales are open, so `on_sale` is returned as None
(= unknown) and the caller resolves it lazily via check_on_sale():

  - EVENT rows: getPerformanceList (already in ticketmaster.py) — on sale
    iff any performance's status string says the sale opened (see
    _SALE_OPENED_STATUSES; the `active` flag lags reality and is ignored).
  - GROUP rows: getGroupPageInfo/{id}/INTERNET/iw — same status strings.

CLI probe:  python tm_events.py [--json]  (add --onsale to also resolve
the per-event sale status; ~1 extra request per event)
"""
import json
import sys
import time
from datetime import datetime

import ticketmaster

SOURCE_NAME = "tm"
API_BASE = ticketmaster.API_HOST + "/wbtxapi/api/v1/bxcached/event"

# Status codes (shared by getPerformanceList rows and getGroupPageInfo)
# meaning the sale has OPENED — including sold-out, which is "opened and
# went". A soldout→selling flip is the drop checker's ping, not this
# monitor's, so counting soldout as on_sale keeps the two from overlapping.
# The perf-list `active` flag is NOT used: it lags reality (bxcached — see
# fetch_event_seats in ticketmaster.py; MTA65 shows s01_onsale with
# active=false). Waiting states: s03_soon, s12_checkbacklater, s00_closed,
# s05_postponed, s04_canceled, s14_other_channels, s16/s17.
_SALE_OPENED_STATUSES = {"s01_onsale", "s18_low_availability", "s02_soldout",
                         "s06_boxoffice", "s13_old_over"}


class TmEventsError(RuntimeError):
    pass


def _get_json(path):
    """GET an API path and return the SUCCESS payload's `data`. The server
    answers unknown paths with an HTML page (HTTP 200), which fails the JSON
    parse — so a route change surfaces as an error, never as empty data."""
    status, raw = ticketmaster._http_get(API_BASE + path)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TmEventsError(f"non-JSON from {path} — API route change?") from e
    if payload.get("status") != "SUCCESS":
        raise TmEventsError(f"API said {payload.get('status')} for {path}: {payload.get('errors')}")
    return payload.get("data")


def _date_text(ms):
    """Venue-local '26.01.2026 20:45' from an epoch-ms performance date."""
    if not ms:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(int(ms) / 1000, ZoneInfo("Asia/Jerusalem")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


def _norm(event_key, name, venue, city, first_ms, url, price_text=""):
    return {
        "source": SOURCE_NAME,
        "event_key": event_key,
        "name": (name or "").strip(),
        "venue": " · ".join(p for p in ((venue or "").strip(), (city or "").strip()) if p),
        "date_text": _date_text(first_ms),
        "first_date_ms": int(first_ms) if first_ms else None,
        "on_sale": None,  # unknown from the listing feeds — see check_on_sale
        "url": url,
        "price_text": price_text,
    }


def _group_url(group_id):
    return f"{ticketmaster.API_HOST}/event-group/{group_id}/ALL/iw"


def fetch_events():
    """Current homepage event list (top events + spotlight), normalized and
    deduped. Raises TmEventsError when the primary feed yields zero events —
    a feed/layout change must read as a fetch failure, never as 'all events
    removed'. Spotlight trouble alone doesn't fail the tick."""
    events = {}

    for it in _get_json("/getAllTopEvent/iw") or []:
        if not isinstance(it, dict):
            continue
        if it.get("eventType") == "EVENT" and it.get("btxEventId"):
            code = str(it["btxEventId"]).upper()
            events.setdefault("ev:" + code, _norm(
                "ev:" + code, it.get("eventName"), it.get("venueName"),
                it.get("venueCity"), it.get("firstPerformanceDate"),
                it.get("customUrl") or ticketmaster.event_url(code),
                price_text=(it.get("priceComment") or "").strip(),
            ))
        elif it.get("eventType") == "GROUP" and it.get("btxEventGroupId"):
            gid = str(it["btxEventGroupId"])
            events.setdefault("grp:" + gid, _norm(
                "grp:" + gid, it.get("eventGroupName"), None, None,
                it.get("firstPerformanceDate"),
                it.get("customUrl") or _group_url(gid),
            ))
    if not events:
        raise TmEventsError("getAllTopEvent parsed to 0 events — feed change?")

    try:
        spot = _get_json("/getSpotLightData/ALL/ALL/iw") or {}
        for el in spot.get("elements") or []:
            ev = (el or {}).get("event") or {}
            if ev.get("eventCode"):
                code = str(ev["eventCode"]).upper()
                events.setdefault("ev:" + code, _norm(
                    "ev:" + code, ev.get("eventName"), ev.get("venueName"),
                    ev.get("venueCity"), ev.get("firstPerformanceDate"),
                    ev.get("customUrl") or ticketmaster.event_url(code),
                ))
    except Exception as e:
        print(f"[tm_events] spotlight fetch failed (top list ok): {e}")

    return list(events.values())


def check_on_sale(ev):
    """Resolve whether one fetched event is currently buyable. Costs one
    request; the caller only asks for events not already known to be on sale.
    Raises on transport errors so the caller can fall back to its stored
    state instead of mis-recording a flip."""
    key = ev["event_key"]
    if key.startswith("grp:"):
        data = _get_json(f"/getGroupPageInfo/{key[4:]}/INTERNET/iw") or {}
        return (data.get("status") or "") in _SALE_OPENED_STATUSES
    code = key.split(":", 1)[1]
    try:
        perfs = ticketmaster.list_performances(code)
    except ticketmaster.TicketmasterError as e:
        if "no performances" in str(e):
            return False  # listed on the homepage but nothing scheduled yet
        raise
    return any((p.get("status") or "") in _SALE_OPENED_STATUSES for p in perfs)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    as_json = "--json" in argv
    resolve = "--onsale" in argv
    events = fetch_events()
    if resolve:
        for ev in events:
            try:
                ev["on_sale"] = check_on_sale(ev)
            except Exception as e:
                ev["on_sale"] = f"error: {e}"
            time.sleep(0.2)
    if as_json:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    print(f"{len(events)} events on ticketmaster.co.il homepage feeds\n")
    for ev in events:
        sale = {True: "ON SALE", False: "not selling", None: ""}.get(ev["on_sale"], str(ev["on_sale"]))
        print(f"  {ev['event_key']:<14} {ev['name']:<40.40} {ev['date_text']:<18} "
              f"{ev['venue']:<40.40} {sale}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
