"""boletosexpress.com drop checker (Latin / NYC-area promoter ticketing —
United Palace concerts, club nights, Hudson cruises).

Pure HTTP, anonymous, no bot check (Apache + PHP, `Cache-Control:
no-store`); the event page is ~1.3MB raw / ~70KB gzipped. Everything is
server-rendered into that one page, so there is no API to call and nothing
lighter to poll. Two layouts:

  * SEATED (a venue map): every seat on the map is its own div,
        <div id='s1' … class='seat pl14 special0 custom_price open'
             data-id='4390660' data-section_id='97854' data-row='FF'
             data-section='Orchestra Left' data-seat='117'
             data-standardadmission='294.80' data-standardadmission-fees='44.80'>
    with `open` (buyable) or `unavailable` as the LAST class. That is a
    real per-seat state for the whole house — United Palace serves 3,344
    seats — so this source has exact released / left counts per section.
    `unavailable` covers sold, held and not-yet-released alike: a show
    whose sale hasn't opened (Lil Naay, 2026-11-26) serves every seat as
    `unavailable`, which is exactly the state a new-inventory watcher wants
    to sit on. Prices are ALL-IN (`data-standardadmission` = face + fees).
  * GA (a ticket-type table): one <tr> per type,
        <input type="hidden" name="sections[99373]" value="General Admission">
        <td class='vt_price_cell'>$81.62<div class='fees'>(75.00 + 6.62)</div>
        <select name="multi[99373][98454]"><option>0</option>…<option>8</option>
    No counts are published, but the highest quantity option is
    min(remaining, per-order limit `eventMaxTickets`), so it becomes a real
    count once a type drops under the limit — the eventim trick. A row with
    no <select> is treated as sold out.

Trap: the ld+json `offers.availability` reads InStock even when every seat
on the map is `unavailable`, so it is metadata only (name / date / venue).
A page with neither a seat map nor a ticket table raises — a layout change
must read as an error, never as "sold out".

Drop-checker seats: seated → one physical seat per `open` seat, keyed on
(section, row, seat) — the block|row|seat key the DB stores — so a returned
seat pings with its row and number;
GA → one pseudo-seat per buyable type (`ga: True`), keyed on the type name,
so a type coming back from sold out pings. perf_code is always "0".

The stock tracker (boletos_events.py, an EDM source) reads the same page
through `parse_event` and turns sections into tiers with real counts —
that's what the /events "ping me under N left" alert runs on.

CLI probe:  python boletos.py <boletosexpress.com event URL or id> [--json]
"""
import gzip
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SOURCE_NAME = "boletos"
SITE_BASE = "https://www.boletosexpress.com"
CACHE_DIR = Path(__file__).parent / "tm_cache"
CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT = 30
# A returned seat comes back one at a time, like TM returns — ping on singles.
DEFAULT_FILTERS = {"min_group_size": 1}
TZ = "America/New_York"

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}


class BoletosError(RuntimeError):
    pass


def parse_url(url):
    """(event_id, "0"). Accepts https://www.boletosexpress.com/<slug>/<id>/,
    …/event.php?event_id=<id>, or a bare numeric id."""
    s = (url or "").strip()
    if not s:
        raise BoletosError("URL is empty")
    m = re.search(r"event_id=(\d+)", s)
    if m:
        return m.group(1), "0"
    m = re.search(r"boletosexpress\.com/[^/?#]+/(\d+)", s, re.I)
    if m:
        return m.group(1), "0"
    m = re.fullmatch(r"(?:boletos\s*/\s*)?(\d{3,})", s, re.I)
    if m:
        return m.group(1), "0"
    raise BoletosError("URL must look like https://www.boletosexpress.com/<event-name>/<id>/")


def perf_url(event_code, perf_code="0"):
    return f"{SITE_BASE}/event.php?event_id={event_code}"


def get_page(event_code):
    req = urllib.request.Request(perf_url(event_code), headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        raise BoletosError(f"boletos event {event_code} returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise BoletosError(f"boletosexpress.com unreachable: {e.reason}") from e
    except OSError as e:
        raise BoletosError(f"boletos event {event_code} failed: {e}") from e
    # The page declares windows-1252 in a meta tag but the server sends UTF-8.
    return raw.decode("utf-8", "replace")


# --- parsing -----------------------------------------------------------------

# Attribute values are single-quoted and may hold raw markup (a VIP seat's
# data-details carries "<span class=special39>…"), so a tag ends at the
# first '>' OUTSIDE quotes, not the first '>'.
_SEAT_RE = re.compile(r"<div id='s\d+'(?:[^>']|'[^']*')*?class='seat ([^']*)'((?:[^>']|'[^']*')*)>")
_ATTR_RE = re.compile(r"data-([a-z_-]+)='([^']*)'")
_GA_ROW_RE = re.compile(
    r'name="sections\[(\d+)\]" value="([^"]*)"(.*?)</tr>', re.S)


def _money(s):
    try:
        return float(str(s).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _jsonld(page):
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        txt = m.group(1)
        try:
            return json.loads(txt, strict=False)
        except ValueError:
            # The description field carries raw HTML/quotes that sometimes
            # break the JSON; pull the fields we need by regex instead.
            out = {}
            for k in ("name", "startDate"):
                mm = re.search(rf'"{k}":\s*"([^"]*)"', txt)
                if mm:
                    out[k] = mm.group(1)
            mm = re.search(r'"location":\s*\{.*?"name":\s*"([^"]*)"', txt, re.S)
            if mm:
                out["location"] = {"name": mm.group(1)}
            mm = re.search(r'"price":\s*"([^"]*)"', txt)
            if mm:
                out["offers"] = {"price": mm.group(1)}
            return out
    return {}


def _clean(s):
    return re.sub(r"\s+", " ", html.unescape(s or "").replace("\xa0", " ")).strip()


def parse_event(page):
    """The whole page → {name, venue, start, image, layout, seats, sections}.

    seats    (seated only) every seat on the map: {id, section_id, section,
             row, seat, open, price}
    sections [{id, name, price, available, quantity, sold_out, max_qty}]
             available/quantity exact on a seated map; on GA, available is
             the select's top option when under the per-order limit, else None.
    Raises BoletosError when the page has neither layout."""
    ld = _jsonld(page)
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    # The <title> carries the bare headliner ("ISHAY RIBO FEATURING ZUSHA
    # Tickets - BoletosExpress"); ld+json `name` appends the promoter's
    # subtitle ("…: Family Seating Concert - VIP Tickets - proceeds to …"),
    # which makes a 90-char Discord channel slug. Title first.
    short = re.sub(r"\s*Tickets\s*-\s*BoletosExpress\s*$", "",
                   _clean(title.group(1)) if title else "", flags=re.I)
    name = short or _clean(ld.get("name"))
    loc = ld.get("location") or {}
    img = re.search(r'<meta property="og:image" content="([^"]+)"', page)
    out = {
        "name": name,
        "venue": _clean(loc.get("name")),
        "start": (ld.get("startDate") or "").strip() or None,
        "image": img.group(1) if img else None,
        "ld_price": _money((ld.get("offers") or {}).get("price")),
    }

    seats = []
    for m in _SEAT_RE.finditer(page):
        classes = m.group(1).split()
        attrs = dict(_ATTR_RE.findall(m.group(2)))
        is_open = "open" in classes
        seats.append({
            "id": attrs.get("id"),
            "section_id": attrs.get("section_id"),
            "section": _clean(attrs.get("section")),
            "row": _clean(attrs.get("row")),
            "seat": _clean(attrs.get("seat")),
            "open": is_open,
            "price": _money(attrs.get("standardadmission")) if is_open else None,
        })
    if seats:
        secs = {}
        for s in seats:
            sec = secs.setdefault(s["section_id"] or s["section"], {
                "id": s["section_id"], "name": s["section"], "price": None,
                "available": 0, "quantity": 0, "max_qty": None})
            sec["quantity"] += 1
            if s["open"]:
                sec["available"] += 1
                if s["price"] is not None and (sec["price"] is None or s["price"] < sec["price"]):
                    sec["price"] = s["price"]
        for sec in secs.values():
            sec["sold_out"] = sec["available"] == 0
        out.update(layout="seated", seats=seats, sections=list(secs.values()))
        return out

    m = re.search(r"var eventMaxTickets=(\d+)", page)
    limit = int(m.group(1)) if m else None
    sections = []
    for sid, sname, body in _GA_ROW_RE.findall(page):
        pm = re.search(r"vt_price_cell'>\s*\$([\d,.]+)", body)
        sel = re.search(r"<select name=\"multi\[\d+\]\[\d+\]\"[^>]*>(.*?)</select>", body, re.S)
        opts = [int(x) for x in re.findall(r"<option[^>]*>\s*(\d+)\s*</option>", sel.group(1))] if sel else []
        top = max(opts) if opts else 0
        sections.append({
            "id": sid, "name": _clean(sname), "price": _money(pm.group(1)) if pm else None,
            "available": (top if (limit is None or top < limit) else None),
            "quantity": None, "max_qty": top or None, "sold_out": top == 0,
        })
    if sections:
        out.update(layout="ga", seats=[], sections=sections)
        return out
    raise BoletosError("no seat map and no ticket table on the page — layout change?")


def row_runs(seats):
    """{section: {row: longest run of open seats side by side}} for a seated
    map. United Palace numbers its side AND center orchestra sections odd or
    even only (seats 101, 103, 105 sit next to each other), so a section
    whose every seat number shares one parity steps by 2; otherwise by 1."""
    by_sec = {}
    for s in seats:
        if str(s.get("seat") or "").isdigit():
            by_sec.setdefault(s["section"], []).append(s)
    out = {}
    for sec, ss in by_sec.items():
        step = 2 if len({int(s["seat"]) % 2 for s in ss}) == 1 else 1
        rows = {}
        for s in ss:
            if s["open"]:
                rows.setdefault(s["row"], []).append(int(s["seat"]))
        best_by_row = {}
        for row, nums in rows.items():
            nums.sort()
            best = cur = 1
            for a, b in zip(nums, nums[1:]):
                cur = cur + 1 if b - a == step else 1
                best = max(best, cur)
            best_by_row[row] = best
        out[sec] = best_by_row
    return out


# --- drop-checker interface ---------------------------------------------------

def fetch_selectable_seats(event_code, perf_code="0"):
    page = get_page(event_code)
    ev = parse_event(page)
    _write_labels(event_code, ev)
    if ev["layout"] == "seated":
        return [{
            "block": s["section"], "row": s["row"], "seat": s["seat"],
            "section_id": s["section_id"], "seat_id": s["id"], "price": s["price"],
        } for s in ev["seats"] if s["open"]]
    return [{
        "block": sec["name"], "row": "GA", "seat": "1", "ga": True,
        "status": "available", "qty_available": sec["available"], "price": sec["price"],
    } for sec in ev["sections"] if not sec["sold_out"]]


def seat_key(seat):
    # Must equal the key db.tm_replace_seat_state stores (block|row|seat) or
    # every seat re-reports as added on every tick. Section names are unique
    # per map, so (section, row, seat) is exactly one seat; a GA type is
    # "<type>|GA|1".
    return f"{seat.get('block', '')}|{seat.get('row', '')}|{seat.get('seat', '')}"


def format_seat(seat):
    if seat.get("ga"):
        return str(seat.get("block", "?"))
    return f"{seat.get('block')} row {seat.get('row')} seat {seat.get('seat')}"


# --- labels (event meta), cached on disk ------------------------------------

def _cache_path(event_code, lang):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"boletos_{event_code}_{lang}.json"


def _perf_ms(start):
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(start)
        return int(dt.replace(tzinfo=ZoneInfo(TZ)).timestamp() * 1000)
    except Exception:
        return None


def _write_labels(event_code, ev, lang="iw"):
    secs = ev.get("sections") or []
    avail = sum(s["available"] or 0 for s in secs)
    total = sum(s["quantity"] or 0 for s in secs) if ev["layout"] == "seated" else None
    start = ev.get("start") or ""
    payload = {
        "_fetched_at": time.time(),
        "source": SOURCE_NAME,
        "event_code": str(event_code),
        "perf_code": "0",
        "lang": lang,
        "meta": {
            "eventName": ev.get("name") or f"Boletos event {event_code}",
            "venueName": ev.get("venue") or "",
            "firstPerfMs": _perf_ms(start),
            "firstPerfText": start.replace("T", " ") if start else "",
            "status": "selling" if any(not s["sold_out"] for s in secs) else "soldout",
            "availSeats": avail if ev["layout"] == "seated" else None,
            "totalSeats": total,
            "ga": ev["layout"] == "ga",
            "currency": "USD",
            "image": ev.get("image"),
        },
        # Keyed by the seat's `block` (the section name), all-in USD.
        # A seated section is priced per seat (Orchestra Left runs $179-$295),
        # so its block carries no price and the ping shows each seat's own.
        "blocks": {s["name"]: {"name": s["name"],
                               "price": s["price"] if ev["layout"] == "ga" else None}
                   for s in secs},
        "min_price": min((s["price"] for s in secs if not s["sold_out"] and s["price"] is not None),
                         default=None),
    }
    _cache_path(event_code, lang).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def get_labels(event_code, perf_code="0", lang="iw", force=False, missing_block=None):
    path = _cache_path(event_code, lang)
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cached = None
    if not force and cached and time.time() - (cached.get("_fetched_at") or 0) <= CACHE_TTL_SECONDS:
        if not missing_block or missing_block in (cached.get("blocks") or {}):
            return cached
    try:
        return _write_labels(event_code, parse_event(get_page(event_code)), lang)
    except Exception:
        return cached or {"source": SOURCE_NAME, "event_code": str(event_code),
                          "perf_code": "0", "lang": lang, "meta": {}, "blocks": {}}


def event_summary(labels):
    meta = (labels or {}).get("meta") or {}
    parts = [p for p in (meta.get("eventName"), meta.get("venueName"), meta.get("firstPerfText")) if p]
    return " · ".join(parts)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: python boletos.py <boletosexpress.com event URL or id> [--json]")
        sys.exit(2)
    ev_code, perf = parse_url(args[0])
    ev = parse_event(get_page(ev_code))
    _write_labels(ev_code, ev)
    if "--json" in sys.argv:
        print(json.dumps({k: v for k, v in ev.items() if k != "seats"}, indent=2, ensure_ascii=False))
        sys.exit(0)
    print(f"{ev['name']} · {ev['venue']} · {ev['start']}  [{ev['layout']}]  {perf_url(ev_code)}")
    for s in ev["sections"]:
        q = f"/{s['quantity']}" if s["quantity"] is not None else ""
        a = "?" if s["available"] is None else s["available"]
        p = f"${s['price']:,.2f}" if s["price"] is not None else "—"
        print(f"  {s['name']:<32} {a}{q} left  {p}")
    if ev["layout"] == "seated":
        print(f"  total: {sum(1 for s in ev['seats'] if s['open'])} open of {len(ev['seats'])}")
