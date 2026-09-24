"""barby.co.il new-event monitor — pure-HTTP event-listing fetcher.

Companion to kupat_events.py / tm_events.py; the diff-and-ping loop lives in
app.py (`run_il_events`). Barby (Tel Aviv club) is a React SPA whose whole
upcoming-shows catalog comes from one anonymous endpoint:

  GET https://barby.co.il/api/shows/find
      → {"returnShow": {"show": [...]}} (~80 shows). Each show carries
        showId, showName, showDate ("DD/MM/YYYY"), showTime ("HH:MM"),
        showPrice (₪), showSold, showSeatType ("מופע עמידה" / seated), and
        image/description fields we ignore.

Cloudflare fronts the API and blocks curl's TLS fingerprint ("Attention
Required" page), but plain python urllib with browser-ish headers passes —
verified from both a residential IP and the Hetzner box. If this ever
starts failing with CF challenge HTML, the JSON parse raises and the tick
records an error rather than treating the catalog as empty.

There is no pre-sale listing state — presence in the list is the "selling"
signal — so events are normalized with on_sale=True and only the 'new'
ping kind ever fires (same model as kupat).

The public show page is https://barby.co.il/show/<showId>.

CLI probe:  python barby_events.py [--json]
"""
import gzip
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime

SOURCE_NAME = "barby"
SITE_BASE = "https://barby.co.il"
SHOWS_URL = SITE_BASE + "/api/shows/find"
REQUEST_TIMEOUT = 25

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he,en;q=0.8",
    "Accept-Encoding": "gzip",
    "Referer": SITE_BASE + "/",
}


class BarbyEventsError(RuntimeError):
    pass


def show_url(show_id):
    return f"{SITE_BASE}/show/{show_id}"


def _date_fields(date_str, time_str):
    """('DD.MM.YYYY HH:MM', epoch_ms) from the API's DD/MM/YYYY + HH:MM
    (venue-local). Returns ('', None) when unparseable."""
    d = (date_str or "").strip()
    t = (time_str or "").strip()
    try:
        dt = datetime.strptime(f"{d} {t}", "%d/%m/%Y %H:%M")
    except ValueError:
        return (d + (" " + t if t else "")).strip(), None
    try:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Jerusalem"))
    except Exception:
        pass
    return dt.strftime("%d.%m.%Y %H:%M"), int(dt.timestamp() * 1000)


def fetch_events():
    """Current barby.co.il show catalog, normalized. Raises BarbyEventsError
    on network trouble, on Cloudflare-challenge HTML (JSON parse failure),
    or when a 200 response parses to zero shows — an API change must read
    as a fetch failure, never as 'all events removed'."""
    req = urllib.request.Request(SHOWS_URL, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise BarbyEventsError(f"barby shows endpoint returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise BarbyEventsError(f"barby shows endpoint unreachable: {e.reason}") from e
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BarbyEventsError("non-JSON from /api/shows/find — Cloudflare challenge or API change?") from e
    shows = ((payload or {}).get("returnShow") or {}).get("show")
    if not isinstance(shows, list):
        raise BarbyEventsError("unexpected /api/shows/find shape — API change?")

    events = []
    for s in shows:
        if not isinstance(s, dict) or not s.get("showId"):
            continue
        sid = str(s["showId"])
        date_text, date_ms = _date_fields(s.get("showDate"), s.get("showTime"))
        price = (str(s.get("showPrice") or "")).strip()
        events.append({
            "source": SOURCE_NAME,
            "event_key": sid,
            "name": (s.get("showName") or "").strip(),
            # Barby is the venue; the useful venue-slot info is the format
            # (standing/seated show).
            "venue": (s.get("showSeatType") or "").strip(),
            "date_text": date_text,
            "first_date_ms": date_ms,
            "price_text": f"₪{price}" if price else "",
            # No pre-sale state on the listing — being listed = selling.
            "on_sale": True,
            "url": show_url(sid),
        })
    if not events:
        raise BarbyEventsError("shows endpoint parsed to 0 events — API change?")
    return events


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    events = fetch_events()
    if "--json" in argv:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    print(f"{len(events)} shows on {SHOWS_URL}\n")
    for ev in events:
        print(f"  {ev['event_key']:>6}  {ev['date_text']:<18} {ev['name']:<45.45} "
              f"{ev['price_text']:<7} {ev['venue']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
