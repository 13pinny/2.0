"""kupat.co.il new-event monitor — pure-HTTP event-listing fetcher.

Companion to pacha_events.py / tm_events.py; the diff-and-ping loop lives in
app.py (`run_il_events`). One endpoint does all the work:

  GET https://tickets.kupat.co.il/api/features
      → the ENTIRE current catalog (~70-80 features) as anonymous JSON.
        Unlike the seats/presentation endpoints (403 outside the in-page
        fetch chain — see kupat.py's _browse_capture), this listing endpoint
        answers plain urllib. Each feature has id, name, additionalName,
        urlName, code, categories (numeric ids), featureTypeId, and
        closestPresentationDateTime ("YYYY-MM-DD HH:MM", venue-local).

There is no per-feature sale-status flag — presence in the list is the
"selling" signal — so events are normalized with on_sale=True and only the
'new' ping kind ever fires for kupat.

CLI probe:  python kupat_events.py [--json]
"""
import gzip
import json
import sys
import urllib.error
import urllib.request

import kupat

SOURCE_NAME = "kupat"
FEATURES_URL = kupat.API_BASE + "/features"


class KupatEventsError(RuntimeError):
    pass


def fetch_events():
    """Current kupat catalog, normalized. Raises KupatEventsError on network
    trouble or when a 200 response parses to zero features (an API change
    must read as a fetch failure, never as 'all events removed')."""
    req = urllib.request.Request(FEATURES_URL, headers=kupat.REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=kupat.REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise KupatEventsError(f"kupat features endpoint returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise KupatEventsError(f"kupat features endpoint unreachable: {e.reason}") from e
    try:
        feats = json.loads(raw)
    except json.JSONDecodeError as e:
        raise KupatEventsError("non-JSON from /api/features — API change?") from e
    if not isinstance(feats, list):
        raise KupatEventsError("unexpected /api/features shape — API change?")

    events = []
    for f in feats:
        if not isinstance(f, dict) or f.get("id") is None:
            continue
        fid = str(f["id"])
        name = (f.get("name") or "").strip()
        extra = (f.get("additionalName") or "").strip()
        if extra:
            name = f"{name} — {extra}" if name else extra
        dt = (f.get("closestPresentationDateTime") or "").strip()
        events.append({
            "source": SOURCE_NAME,
            "event_key": fid,
            "name": name,
            "venue": "",  # not in the features payload
            "date_text": dt,
            "first_date_ms": None,
            # No sale-status flag on the listing — being listed = selling.
            "on_sale": True,
            "url": f"{kupat.SITE_BASE}/booking/features/{fid}",
        })
    if not events:
        raise KupatEventsError("features endpoint parsed to 0 events — API change?")
    return events


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    events = fetch_events()
    if "--json" in argv:
        print(json.dumps(events, indent=1, ensure_ascii=False))
        return 0
    print(f"{len(events)} features on {FEATURES_URL}\n")
    for ev in events:
        print(f"  {ev['event_key']:>6}  {ev['name']:<50.50} {ev['date_text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
