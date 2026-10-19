"""Universe (universe.com, Ticketmaster's self-serve platform) single-event
tracker — pure-HTTP per-tier stock.

LIV Miami sells its GA tickets here (the club's own site books tables
through UrVenue, see urvenue_events.py). The event page is an empty React
shell, but the listing API behind it is public, anonymous, and answers the
VPS directly:

    GET https://www.universe.com/api/v2/listings/<slug>

    {"listing":  {title, venue_name, address, tz, cover_photo_id, ...},
     "events":   [{start_time, rate_ids:[…], count_attending, full}],
     "rates":    [{id, name, price (ALL-IN), capacity, max_tickets,
                   preemptive_rate_id, sort_index, state}],
     "capacities": [{rate_id, count_available, count_attending}],
     "images":   [{id, url_500, …}]}

So, like Frontgate, this source has REAL counts per tier: one tier per rate,
quantity = rate.capacity (released), available = capacity.count_available
(left), used = capacity.count_attending (sold). released - left - sold is
not always 0 (LIV's Early Arrival Female read 125 / 109 / 0 on 2026-10-19):
those are holds or comps, so sold comes from count_attending, never from
released - left.

Notes / traps
- `rates[].state` reads "active" and `availability` is null on EVERY rate,
  sold-out or not. Sold out = count_available == 0.
- Tiers are a ladder: `preemptive_rate_id` names the rate that has to sell
  out before this one opens. Which rates are released right now is
  `events[0].rate_ids` (sold-out ones included); a rate outside it is
  'closed' (not released yet), so a $183 Tier 5 with 75 left can't win the
  cheapest-buyable slot or read as on sale. Without that list we fall back
  to the chain: open when it has no predecessor or the predecessor is gone.
- Prices are all-in (`all_in_pricing: true`); `raw_src_price` is face.
- The page URL slug (`art-basel-thursday-tickets-R3CY8M`) is cosmetic; the
  6-char code at the end is the listing id.

    python universe_events.py "<universe.com/events/… URL | code>"  [--json]
"""
import json
import re
import sys
from datetime import datetime

import edm_common
from edm_common import EdmEventsError, make_tier, rollup

SOURCE_NAME = "universe"
SITE_BASE = "https://www.universe.com"

_URL_RE = re.compile(r"universe\.com/events/(?:[A-Za-z0-9-]*-)?([A-Za-z0-9]{5,8})(?:[/?#]|$)",
                     re.I)
_API_RE = re.compile(r"universe\.com/api/v2/listings/([A-Za-z0-9]{5,8})", re.I)
_CODE_RE = re.compile(r"^[A-Za-z0-9]{5,8}$")
_GROUP_RE = re.compile(r"\s+-\s+(Female|Male|Ladies|Men|Women)\s*$", re.I)


def parse_url(url):
    """universe.com event URL (…/events/<slug>-<CODE>) or bare code → CODE."""
    s = (url or "").strip()
    m = _API_RE.search(s) or _URL_RE.search(s)
    if m:
        return m.group(1).upper()
    if _CODE_RE.match(s) and any(c.isdigit() for c in s):
        return s.upper()
    raise ValueError(f"not a universe.com event link: {url!r}")


def listing_url(code):
    return f"{SITE_BASE}/api/v2/listings/{code}"


def page_url(code, slug_param=None):
    return f"{SITE_BASE}/events/{slug_param or code}"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_listing(data):
    """listing JSON → (meta dict, tiers). Raises EdmEventsError on anything
    that doesn't look like a listing — never an empty tier list."""
    if not isinstance(data, dict) or not isinstance(data.get("rates"), list):
        raise EdmEventsError(f"universe: not a listing payload ({str(data)[:160]!r})")
    listing = data.get("listing") or (data.get("listings") or [{}])[0] or {}
    events = data.get("events") or []
    event = events[0] if events else {}
    caps = {c.get("rate_id"): c for c in data.get("capacities") or [] if isinstance(c, dict)}
    rates = [r for r in data["rates"] if isinstance(r, dict) and r.get("id")]
    if not rates:
        raise EdmEventsError("universe: listing has no rates")
    by_id = {r["id"]: r for r in rates}
    released = set(event.get("rate_ids") or [])

    def left_of(rid):
        return _int((caps.get(rid) or {}).get("count_available"))

    tiers = []
    for r in sorted(rates, key=lambda r: (r.get("sort_index") or 0)):
        cap = caps.get(r["id"]) or {}
        total = _int(r.get("capacity"))
        left = _int(cap.get("count_available"))
        sold = _int(cap.get("count_attending"))
        if released:
            is_open = r["id"] in released
        else:
            prev = r.get("preemptive_rate_id")
            is_open = not prev or prev not in by_id or left_of(prev) == 0
        name = (r.get("name") or "").strip()
        gm = _GROUP_RE.search(name)
        t = make_tier(name, r.get("price"),
                      group=gm.group(1).title() if gm else None,
                      face_price=r.get("raw_src_price"),
                      available=left, quantity=total, used=sold,
                      sold_out=(left == 0),
                      closed=(not is_open and left != 0),
                      max_qty=_int(r.get("max_tickets")))
        t["section_id"] = r["id"]
        t["sort"] = r.get("sort_index")
        t["after"] = (by_id.get(r.get("preemptive_rate_id")) or {}).get("name")
        t["note"] = (r.get("description") or "").strip() or None
        tiers.append(t)

    start = event.get("start_time") or listing.get("start_time")
    start_date, date_text = None, None
    if start:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            start_date = dt.date().isoformat()
            date_text = dt.strftime("%a %b %-d, %Y · %-I:%M%p")
        except ValueError:
            pass
    image = None
    cover = listing.get("cover_photo_id")
    for im in data.get("images") or []:
        if isinstance(im, dict) and (im.get("id") == cover or image is None):
            image = im.get("url_500") or im.get("url")
            if im.get("id") == cover:
                break
    meta = {
        "name": (listing.get("title") or "").strip() or None,
        "venue": listing.get("venue_name") or listing.get("address"),
        "start_date": start_date, "date_text": date_text,
        "slug_param": listing.get("slug_param"),
        "image": image or listing.get("image_url_500"),
        "sold_total": _int(event.get("count_attending")),
        "full": bool(event.get("full")),
    }
    return meta, tiers


def fetch_event(event_key):
    """One Universe listing, normalized (see edm_common's docstring)."""
    code = parse_url(event_key)
    headers = dict(edm_common.BROWSER_HEADERS, Accept="application/json")
    meta, tiers = parse_listing(edm_common.fetch_json(listing_url(code), headers=headers))
    ev = {
        "source": SOURCE_NAME,
        "event_key": code,
        "name": meta["name"] or f"Universe {code}",
        "venue": meta["venue"],
        "date_text": meta["date_text"],
        "start_date": meta["start_date"],
        "page_url": page_url(code, meta["slug_param"]),
        "image": meta["image"],
        "currency": "USD",
        "tiers": tiers,
        "total_sold": None,
    }
    ev = rollup(ev)
    # rollup's totals only count buyable tiers; released/sold here are
    # whole-event figures, like frontgate's.
    ev["total_released"] = sum(t["quantity"] or 0 for t in tiers)
    ev["total_sold"] = (meta["sold_total"] if meta["sold_total"] is not None
                        else sum(t["used"] or 0 for t in tiers))
    return ev


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    try:
        ev = fetch_event(args[0])
    except (EdmEventsError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(ev, indent=1, ensure_ascii=False))
        return 0
    edm_common.print_event(ev)
    print(f"  released {ev['total_released']:,} · sold {ev['total_sold']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
