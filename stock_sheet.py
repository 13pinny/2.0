"""/stock — a hand-kept stock sheet: shows by date, sections, tickets left.

The page is a spreadsheet replacement. Each show (name, city, venue, date,
main link + secondary links) has free-text sections; per section we keep how
many WE hold and what we paid, and per check DAY how many are LEFT on sale
and the price. One entry per (section, day), so re-typing a cell on the same
day overwrites it.

Helping with the typing:
  * Link auto-fill (`autofill`): a pasted link on a site Kartis already reads
    (posh / leap / eventim / shotgun / tixr / tao / frontgate / universe /
    LIV) comes back through that site's fetcher; anything else is read for
    its ld+json Event / og:title. AXS answers every non-browser request with
    a Cloudflare block (and its shop bot-checks even a real Chrome), so AXS
    links fall back to the name in the URL slug and the rest is typed.
  * Auto-read stock (`auto_read`): for a show whose main link is one of the
    sites above, today's Left/Price come from the fetcher, one row per tier
    (a missing section is created). Only cells that are blank or were
    auto-written before are touched; a value you typed is never overwritten.
    `run_auto` does every show; app.py schedules it.
  * Google Sheet import (`import_sheet`): reads the old free-form sheet's CSV
    export (event lines + "date, section, left, price" lines in any order).

Tables live here, not in db.py (created on first use): stock_events,
stock_sections, stock_entries.
"""

import csv
import html as _html
import io
import json
import re
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from urllib.parse import unquote, urljoin, urlparse

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_events (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT,
    venue TEXT,
    event_date_iso TEXT,
    link TEXT,
    links_json TEXT,
    image_url TEXT,
    notes TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    auto_at TEXT,
    auto_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_sections (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    name TEXT NOT NULL,
    held INTEGER,
    cost REAL,
    sort INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_sections_event ON stock_sections(event_id, sort);
CREATE TABLE IF NOT EXISTS stock_entries (
    section_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    day TEXT NOT NULL,
    remaining INTEGER,
    price REAL,
    auto INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (section_id, day)
);
CREATE INDEX IF NOT EXISTS idx_stock_entries_event ON stock_entries(event_id, day);
"""

_ready = False


@contextmanager
def _conn():
    global _ready
    with db.connect() as conn:
        if not _ready:
            conn.executescript(SCHEMA)
            conn.commit()
            _ready = True
        yield conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def today():
    """Checks are bucketed by New York calendar day (the VPS runs UTC)."""
    return datetime.now(db.ny_tz()).date().isoformat()


def _rows(sql, args=()):
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _num(v, cast=float):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, (int, float)):
        return cast(v)
    s = str(v).replace(",", "").replace("$", "").replace("₪", "").strip()
    if s.upper() in ("OOS", "SO", "SOLD OUT", "SOLDOUT"):
        return cast(0)
    try:
        return cast(s)
    except (TypeError, ValueError):
        return None


def _int(v):
    return _num(v, lambda s: int(float(s)))


# --- reads -------------------------------------------------------------------

def all_events():
    events = _rows("SELECT * FROM stock_events ORDER BY archived, "
                   "(event_date_iso IS NULL OR event_date_iso = ''), event_date_iso, created_at")
    sections = _rows("SELECT * FROM stock_sections ORDER BY sort, created_at")
    entries = _rows("SELECT section_id, event_id, day, remaining, price, auto "
                    "FROM stock_entries ORDER BY day")
    by_sec = {}
    for e in entries:
        by_sec.setdefault(e["section_id"], {})[e["day"]] = {
            "remaining": e["remaining"], "price": e["price"], "auto": bool(e["auto"])}
    secs_by_event, days_by_event = {}, {}
    for s in sections:
        s["entries"] = by_sec.get(s["id"], {})
        secs_by_event.setdefault(s["event_id"], []).append(s)
    for e in entries:
        days_by_event.setdefault(e["event_id"], set()).add(e["day"])
    now_day = today()
    for ev in events:
        try:
            ev["links"] = json.loads(ev.get("links_json") or "[]")
        except ValueError:
            ev["links"] = []
        ev.pop("links_json", None)
        ev["sections"] = secs_by_event.get(ev["id"], [])
        ev["days"] = sorted(days_by_event.get(ev["id"], ()))
        d = (ev.get("event_date_iso") or "")[:10]
        ev["past"] = bool(d) and d < now_day
        ev["auto_source"] = source_of(ev.get("link"))
    return events


def get_event(event_id):
    rows = _rows("SELECT * FROM stock_events WHERE id = ?", (event_id,))
    return rows[0] if rows else None


# --- writes ------------------------------------------------------------------

def _clean_links(raw):
    out = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"url": item}
        url = (item.get("url") or "").strip()
        if url:
            out.append({"label": (item.get("label") or "").strip()[:60], "url": url})
    return out


_EVENT_TEXT = ("name", "city", "venue", "link", "notes", "image_url")


def save_event(body):
    """Create (no id) or patch an event. Only keys present in `body` change."""
    now = _now()
    event_id = body.get("id")
    existing = get_event(event_id) if event_id else None
    if event_id and not existing:
        raise ValueError("event not found")
    f = {}
    for k in _EVENT_TEXT:
        if k in body:
            f[k] = (str(body.get(k) or "")).strip()[:2000] or None
    if "event_date_iso" in body:
        f["event_date_iso"] = parse_date(body.get("event_date_iso")) or None
    if "links" in body:
        f["links_json"] = json.dumps(_clean_links(body.get("links")))
    if "archived" in body:
        f["archived"] = 1 if body.get("archived") else 0
    if (not existing and not f.get("name")) or ("name" in f and not f["name"]):
        raise ValueError("name required")
    with _conn() as conn:
        if existing:
            if f:
                sets = ", ".join(f"{k} = ?" for k in f)
                conn.execute(f"UPDATE stock_events SET {sets}, updated_at = ? WHERE id = ?",
                             (*f.values(), now, event_id))
        else:
            event_id = "sse-" + uuid.uuid4().hex[:12]
            cols = ["id", "created_at", *f]
            conn.execute(f"INSERT INTO stock_events ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' for _ in cols)})",
                         (event_id, now, *f.values()))
        conn.commit()
    return event_id


def delete_event(event_id):
    with _conn() as conn:
        for t, col in (("stock_entries", "event_id"), ("stock_sections", "event_id"),
                       ("stock_events", "id")):
            conn.execute(f"DELETE FROM {t} WHERE {col} = ?", (event_id,))
        conn.commit()


def _add_section(conn, event_id, name, now, held=None, cost=None):
    n = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM stock_sections WHERE event_id = ?",
                     (event_id,)).fetchone()[0]
    sid = "sss-" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO stock_sections (id, event_id, name, held, cost, sort, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, event_id, name[:80], held, cost, n, now))
    return sid


def save_section(body):
    """Create (no id; needs event_id + name) or patch one section's name /
    held / cost."""
    now = _now()
    sid = body.get("id")
    with _conn() as conn:
        if not sid:
            ev = body.get("event_id")
            if not conn.execute("SELECT 1 FROM stock_events WHERE id = ?", (ev,)).fetchone():
                raise ValueError("event not found")
            name = (body.get("name") or "").strip() or "GA"
            sid = _add_section(conn, ev, name, now, _int(body.get("held")), _num(body.get("cost")))
        else:
            if not conn.execute("SELECT 1 FROM stock_sections WHERE id = ?", (sid,)).fetchone():
                raise ValueError("section not found")
            f = {}
            if "name" in body:
                name = (body.get("name") or "").strip()
                if not name:
                    raise ValueError("section name required")
                f["name"] = name[:80]
            if "held" in body:
                f["held"] = _int(body.get("held"))
            if "cost" in body:
                f["cost"] = _num(body.get("cost"))
            if f:
                conn.execute(f"UPDATE stock_sections SET {', '.join(k + ' = ?' for k in f)} "
                             "WHERE id = ?", (*f.values(), sid))
        conn.commit()
    return sid


def delete_section(section_id):
    with _conn() as conn:
        conn.execute("DELETE FROM stock_entries WHERE section_id = ?", (section_id,))
        conn.execute("DELETE FROM stock_sections WHERE id = ?", (section_id,))
        conn.commit()


def _write_entry(conn, section_id, event_id, day, remaining, price, auto, now):
    if remaining is None and price is None:
        conn.execute("DELETE FROM stock_entries WHERE section_id = ? AND day = ?", (section_id, day))
        return
    conn.execute(
        "INSERT INTO stock_entries (section_id, event_id, day, remaining, price, auto, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(section_id, day) DO UPDATE SET "
        "remaining = excluded.remaining, price = excluded.price, auto = excluded.auto, "
        "created_at = excluded.created_at",
        (section_id, event_id, day, remaining, price, 1 if auto else 0, now))


def save_entry(body):
    """One cell pair: {section_id, day?, remaining?, price?}. A key that is
    absent keeps the stored value; both blank deletes the entry."""
    sid = body.get("section_id")
    day = parse_date(body.get("day")) or today()
    with _conn() as conn:
        row = conn.execute("SELECT event_id FROM stock_sections WHERE id = ?", (sid,)).fetchone()
        if not row:
            raise ValueError("section not found")
        old = conn.execute("SELECT remaining, price FROM stock_entries WHERE section_id = ? AND day = ?",
                           (sid, day)).fetchone()
        rem = _int(body["remaining"]) if "remaining" in body else (old["remaining"] if old else None)
        price = _num(body["price"]) if "price" in body else (old["price"] if old else None)
        _write_entry(conn, sid, row["event_id"], day, rem, price, False, _now())
        conn.execute("UPDATE stock_events SET updated_at = ? WHERE id = ?", (_now(), row["event_id"]))
        conn.commit()
    return day


def delete_day(event_id, day):
    with _conn() as conn:
        conn.execute("DELETE FROM stock_entries WHERE event_id = ? AND day = ?", (event_id, day))
        conn.commit()


# --- dates -------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(v, past=False):
    """ISO / 'M/D/YY' / 'M/D/YYYY' / 'M/D' (year inferred) / 'Mar 10, 2027'
    → 'YYYY-MM-DD', else None. `past`: a yearless date is the most recent
    one on or before today (check days); otherwise the next one from today
    (show dates)."""
    s = str(v or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?$", s)
    mon = day = yr = None
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        yr = m.group(3)
    else:
        m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?", s)
        if not m or m.group(1).lower() not in _MONTHS:
            return None
        mon, day, yr = _MONTHS[m.group(1).lower()], int(m.group(2)), m.group(3)
    t = datetime.fromisoformat(today()).date()
    try:
        if yr:
            y = int(yr)
            return date(y + 2000 if y < 100 else y, mon, day).isoformat()
        d = date(t.year, mon, day)
        if past and d > t:
            d = date(t.year - 1, mon, day)
        elif not past and d < t:
            d = date(t.year + 1, mon, day)
        return d.isoformat()
    except ValueError:
        return None


# --- link auto-fill + auto-read ----------------------------------------------

def source_of(url):
    """The EDM fetcher that can read this link, or None."""
    if not url or "//" not in str(url):
        return None
    try:
        import edm_events
        return edm_events.detect_source(url)
    except Exception:
        return None


def _slug_name(url):
    """'…/events/1683710/boy-throbs-real-tour-tickets' → 'Boy Throbs Real Tour'."""
    path = [p for p in urlparse(url).path.split("/") if p]
    words = ""
    for p in reversed(path):
        p = unquote(p)
        if re.search(r"[a-z]{3}", p, re.I) and not re.fullmatch(r"[0-9a-f]{12,}", p, re.I):
            words = p
            break
    words = re.sub(r"\.(html?|php|aspx?)$", "", words)
    words = re.sub(r"[-_]+", " ", words)
    words = re.sub(r"\b(tickets?|event|events|e)\b", " ", words, flags=re.I)
    words = re.sub(r"\b\d{5,}\b", " ", words)
    return " ".join(w.capitalize() if w.islower() else w for w in words.split()) or None


def _ld_event(page):
    for raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
                t = d.get("@type")
                t = t if isinstance(t, list) else [t]
                if any(isinstance(x, str) and x.endswith("Event") for x in t):
                    return d
    return None


def _meta(page, prop):
    for rx in (rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]*content=["\']([^"\']*)["\']',
               rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{prop}["\']'):
        m = re.search(rx, page, re.I)
        if m:
            return _html.unescape(m.group(1)).strip()
    return None


def autofill(url):
    """Best-effort {name, city, venue, event_date_iso, image_url, source,
    note} for a pasted link. Never raises."""
    url = (url or "").strip()
    out = {"link": url}
    if not url.lower().startswith(("http://", "https://")):
        out["name"] = url or None
        return out
    src = source_of(url)
    if src:
        try:
            import edm_events
            ev = edm_events.fetch_url(url)
            out.update(name=ev.get("name"), venue=ev.get("venue"), source=src,
                       event_date_iso=parse_date((ev.get("start_date") or "")[:10])
                       or parse_date(ev.get("date_text")))
            if out.get("name"):
                return out
        except Exception as e:
            out["note"] = f"{src}: {e}"[:200]
    try:
        from edm_common import fetch_text
        page = fetch_text(url, timeout=12)
        ld = _ld_event(page) or {}
        loc = ld.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
        img = ld.get("image")
        if isinstance(img, list):
            img = img[0] if img else None
        if isinstance(img, dict):
            img = img.get("url")
        title = _meta(page, "og:title")
        out.setdefault("name", None)
        out["name"] = out["name"] or ld.get("name") or (title.split(" - ")[0].split(" | ")[0] if title else None)
        out["venue"] = out.get("venue") or (loc.get("name") if isinstance(loc, dict) else None)
        out["city"] = addr.get("addressLocality") if isinstance(addr, dict) else None
        out["event_date_iso"] = out.get("event_date_iso") or parse_date(str(ld.get("startDate") or "")[:10])
        image = img or _meta(page, "og:image")
        out["image_url"] = urljoin(url, image) if image else None
        if out["name"]:
            out["name"] = _html.unescape(out["name"])
            return out
    except Exception as e:
        out["note"] = (out.get("note") or "") + f" page: {e}"[:200]
    out["name"] = out.get("name") or _slug_name(url)
    host = urlparse(url).netloc.lower()
    if "axs.com" in host:
        out["note"] = "AXS blocks automatic reads; name taken from the link, type the rest."
    return out


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def auto_read(event_id):
    """Fill today's Left/Price from the show's link when Kartis can read that
    site. Returns {"source", "tiers", "written"}; raises ValueError when the
    link isn't readable, EdmEventsError etc. when the fetch fails."""
    ev = get_event(event_id)
    if not ev:
        raise ValueError("event not found")
    src = source_of(ev.get("link"))
    if not src:
        raise ValueError("Kartis can't read stock from this link's site")
    import edm_events
    now, day = _now(), today()
    try:
        data = edm_events.fetch_url(ev["link"])
    except Exception as e:
        with _conn() as conn:
            conn.execute("UPDATE stock_events SET auto_error = ?, auto_at = ? WHERE id = ?",
                         (str(e)[:300], now, event_id))
            conn.commit()
        raise
    written = 0
    tiers = data.get("tiers") or []
    with _conn() as conn:
        secs = {_norm(r["name"]): r["id"] for r in conn.execute(
            "SELECT id, name FROM stock_sections WHERE event_id = ?", (event_id,)).fetchall()}
        for t in tiers:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            sid = secs.get(_norm(name))
            if not sid:
                sid = secs[_norm(name)] = _add_section(conn, event_id, name, now)
            old = conn.execute("SELECT remaining, price, auto FROM stock_entries "
                               "WHERE section_id = ? AND day = ?", (sid, day)).fetchone()
            if old and not old["auto"] and (old["remaining"] is not None or old["price"] is not None):
                continue  # typed by hand today — leave it
            rem = t.get("available")
            if rem is None and t.get("sold_out"):
                rem = 0
            price = t.get("price")
            if rem is None and price is None:
                continue
            _write_entry(conn, sid, event_id, day, rem, price, True, now)
            written += 1
        conn.execute("UPDATE stock_events SET auto_at = ?, auto_error = NULL WHERE id = ?",
                     (now, event_id))
        conn.commit()
    return {"source": src, "tiers": len(tiers), "written": written}


def run_auto():
    """auto_read every upcoming, non-archived show with a readable link."""
    out = {"ok": 0, "failed": 0, "skipped": 0, "errors": []}
    for ev in all_events():
        if ev["archived"] or ev["past"] or not ev["auto_source"]:
            out["skipped"] += 1
            continue
        try:
            auto_read(ev["id"])
            out["ok"] += 1
        except Exception as e:
            out["failed"] += 1
            out["errors"].append(f"{ev['name']}: {e}"[:200])
    return out


# --- Google Sheet import ------------------------------------------------------

_SHORT_DATE = re.compile(r"^\d{1,2}/\d{1,2}$")
_FULL_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")


def sheet_csv_url(url):
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url or "")
    if not m:
        raise ValueError("not a Google Sheets link")
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
            + (f"&gid={gid.group(1)}" if gid else ""))


def parse_sheet(text):
    """The free-form sheet → [{name, city, venue, event_date_iso, link,
    links, rows: [{day, section, remaining, price}]}].

    A line whose cells hold a short 'M/D' date is a stock line (that date is
    the check day; a whole number is Left, '$x' or a decimal is Price, 'OOS'
    is 0 left, other text is the section). Any other line with text in its
    first cell starts a new show: column 2 is the city, more text is the
    venue, a 'M/D/YY' date is the show date, URLs are links."""
    events, cur = [], None
    for cells in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in cells]
        if not any(cells):
            continue
        if any(_SHORT_DATE.match(c) for c in cells):
            if cur is None:
                continue
            row = {"day": None, "section": None, "remaining": None, "price": None}
            for c in cells:
                if not c:
                    continue
                if _SHORT_DATE.match(c) and not row["day"]:
                    row["day"] = parse_date(c, past=True)
                elif c.upper() in ("OOS", "SOLD OUT", "SO"):
                    row["remaining"] = 0
                elif c.startswith("$") or re.fullmatch(r"\d+\.\d+", c):
                    row["price"] = _num(c)
                elif re.fullmatch(r"\d[\d,]*", c) and row["remaining"] is None:
                    row["remaining"] = _int(c)
                elif not row["section"]:
                    row["section"] = c
            cur["rows"].append(row)
            continue
        cur = {"name": cells[0] or None, "city": None, "venue": None, "event_date_iso": None,
               "link": None, "links": [], "rows": []}
        for i, c in enumerate(cells[1:], 1):
            if not c:
                continue
            if c.lower().startswith("http"):
                if cur["link"]:
                    cur["links"].append({"label": "", "url": c})
                else:
                    cur["link"] = c
            elif _FULL_DATE.match(c) or re.match(r"^\d{4}-\d{2}-\d{2}$", c):
                cur["event_date_iso"] = parse_date(c)
            elif i == 1:
                cur["city"] = c
            elif not cur["venue"]:
                cur["venue"] = c
        if cur["name"]:
            events.append(cur)
        else:
            cur = None
    return events


def import_sheet(url_or_text):
    """Import a sheet (Google Sheets link or raw CSV). A show already here
    (same name + city + venue + date) is merged into, not duplicated."""
    text = url_or_text or ""
    if "docs.google.com" in text and "\n" not in text.strip():
        import urllib.request
        req = urllib.request.Request(sheet_csv_url(text.strip()),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8-sig", "replace")
            if "<html" in text[:500].lower():
                raise ValueError("the sheet isn't shared as 'anyone with the link'")
    parsed = parse_sheet(text)
    now = _now()
    out = {"events": 0, "merged": 0, "sections": 0, "entries": 0}
    with _conn() as conn:
        for ev in parsed:
            key = tuple(_norm(ev.get(k)) for k in ("name", "city", "venue")) + (ev["event_date_iso"] or "",)
            eid = None
            for r in conn.execute("SELECT id, name, city, venue, event_date_iso FROM stock_events").fetchall():
                if tuple(_norm(r[k]) for k in ("name", "city", "venue")) + (r["event_date_iso"] or "",) == key:
                    eid = r["id"]
                    break
            if eid:
                out["merged"] += 1
            else:
                eid = "sse-" + uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT INTO stock_events (id, name, city, venue, event_date_iso, link, links_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (eid, ev["name"], ev["city"], ev["venue"], ev["event_date_iso"], ev["link"],
                     json.dumps(ev["links"]), now))
                out["events"] += 1
            secs = {_norm(r["name"]): r["id"] for r in conn.execute(
                "SELECT id, name FROM stock_sections WHERE event_id = ?", (eid,)).fetchall()}
            for row in ev["rows"]:
                name = row["section"] or "GA"
                sid = secs.get(_norm(name))
                if not sid:
                    sid = secs[_norm(name)] = _add_section(conn, eid, name, now)
                    out["sections"] += 1
                if row["day"] and (row["remaining"] is not None or row["price"] is not None):
                    _write_entry(conn, sid, eid, row["day"], row["remaining"], row["price"], False, now)
                    out["entries"] += 1
        conn.commit()
    return out


if __name__ == "__main__":
    import sys
    db.init()
    if len(sys.argv) > 2 and sys.argv[1] == "--parse":
        print(json.dumps(parse_sheet(open(sys.argv[2], encoding="utf-8").read()), indent=1))
    elif len(sys.argv) > 2 and sys.argv[1] == "--import":
        print(import_sheet(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--auto":
        print(run_auto())
    elif len(sys.argv) > 1:
        print(json.dumps(autofill(sys.argv[1]), indent=1))
    else:
        for e in all_events():
            print(e["event_date_iso"], e["name"], e["city"] or "", e["venue"] or "",
                  len(e["sections"]), "sections", len(e["days"]), "days")
