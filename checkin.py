"""/checkin — hand-run stock check-ins on events we hold or watch.

Some events publish nothing a fetcher can read (or sell through platforms we
don't scrape), so the only way to see how fast they move is to look and write
it down. Each event here carries its own free-text tiers ("GA", "Section 104",
"VIP table"); a check-in stores, per tier, how many are left and the price on
that New York day. Twice a week (Mon + Thu by default) a Discord reminder
lists every upcoming event with how long it has been since the last check-in.

State: checkin_events / checkin_tiers / checkin_entries (schema in db.py).
One entry per (tier, day) — re-saving the same day overwrites it, so a
check-in can be corrected without leaving a duplicate data point.
"""

import html as _html
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import db

# An event counts as "due" once this many days pass without a check-in.
DUE_AFTER_DAYS = 3
CURRENCIES = ("USD", "ILS", "EUR", "GBP")

_OG_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']', re.I),
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def today():
    """Check-ins are bucketed by New York calendar day (the VPS runs UTC)."""
    return datetime.now(db.ny_tz()).date().isoformat()


# --- artwork -----------------------------------------------------------------

def fetch_image(url):
    """og:image / twitter:image of an event page, or None. Never raises —
    a page behind a challenge just gets the gradient placeholder."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        from edm_common import fetch_text
        page = fetch_text(url, timeout=10)
    except Exception as e:
        print(f"[checkin] image fetch failed for {url}: {e}")
        return None
    for rx in _OG_RES:
        m = rx.search(page)
        if m:
            img = urljoin(url, _html.unescape(m.group(1)).strip())
            # DICE: keep the bare imgix attachment so the page picks its own
            # crop (the og:image query is a 1300x630 social crop), like /dice.
            if "imgix.net" in urlparse(img).netloc:
                img = img.split("?", 1)[0]
            return img
    return None


# --- reads -------------------------------------------------------------------

def _rows(sql, args=()):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_event(event_id):
    rows = _rows("SELECT * FROM checkin_events WHERE id = ?", (event_id,))
    return rows[0] if rows else None


def _days_between(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None


def all_events():
    """Every event with its tiers, per-tier history (oldest first) and the
    check-in summary the page and the reminder both need."""
    events = _rows("SELECT * FROM checkin_events ORDER BY archived, "
                   "(event_date_iso IS NULL OR event_date_iso = ''), event_date_iso, created_at")
    tiers = _rows("SELECT * FROM checkin_tiers WHERE archived = 0 ORDER BY sort, created_at")
    entries = _rows("SELECT tier_id, event_id, day, remaining, price, note "
                    "FROM checkin_entries ORDER BY day")
    by_tier = {}
    for e in entries:
        by_tier.setdefault(e["tier_id"], []).append(e)
    tiers_by_event = {}
    for t in tiers:
        t["history"] = [{"day": e["day"], "remaining": e["remaining"], "price": e["price"]}
                        for e in by_tier.get(t["id"], [])]
        tiers_by_event.setdefault(t["event_id"], []).append(t)
    days_by_event = {}
    for e in entries:
        days_by_event.setdefault(e["event_id"], set()).add(e["day"])

    now_day = today()
    for ev in events:
        try:
            ev["links"] = json.loads(ev.get("links_json") or "[]")
        except ValueError:
            ev["links"] = []
        ev["tiers"] = tiers_by_event.get(ev["id"], [])
        days = sorted(days_by_event.get(ev["id"], ()))
        ev["checkin_days"] = days
        ev["last_checkin"] = days[-1] if days else None
        ev["days_since"] = _days_between(days[-1], now_day) if days else None
        date_iso = (ev.get("event_date_iso") or "")[:10]
        ev["past"] = bool(date_iso) and date_iso < now_day
        ev["due"] = (not ev["archived"] and not ev["past"]
                     and (ev["days_since"] is None or ev["days_since"] >= DUE_AFTER_DAYS))
        ev["checked_today"] = ev["last_checkin"] == now_day
    return events


# --- writes ------------------------------------------------------------------

def _clean_links(raw):
    out = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"url": item}
        url = (item.get("url") or "").strip()
        if not url:
            continue
        out.append({"label": (item.get("label") or "").strip()[:60], "url": url})
    return out


def _event_fields(body):
    f = {}
    for k in ("name", "venue", "link", "notes"):
        if k in body:
            f[k] = (body.get(k) or "").strip() or None
    if "event_date_iso" in body:
        d = (body.get("event_date_iso") or "").strip()[:10]
        f["event_date_iso"] = d or None
    if "links" in body:
        f["links_json"] = json.dumps(_clean_links(body.get("links")))
    if "currency" in body:
        cur = (body.get("currency") or "USD").strip().upper()
        f["currency"] = cur if cur in CURRENCIES else "USD"
    if "image_url" in body:
        img = (body.get("image_url") or "").strip()
        f["image_url"] = img or None
        f["image_manual"] = 1 if img else 0
    if "archived" in body:
        f["archived"] = 1 if body.get("archived") else 0
    return f


def _sync_tiers(conn, event_id, tiers, now):
    """Upsert the event's tier list from the form. A tier dropped from the
    form is archived, not deleted, so its history survives."""
    keep = set()
    for i, t in enumerate(tiers or []):
        name = (t.get("name") or "").strip()
        if not name:
            continue
        tid = t.get("id")
        if tid and conn.execute("SELECT 1 FROM checkin_tiers WHERE id = ? AND event_id = ?",
                                (tid, event_id)).fetchone():
            conn.execute("UPDATE checkin_tiers SET name = ?, sort = ?, archived = 0 WHERE id = ?",
                         (name[:80], i, tid))
        else:
            tid = "cit-" + uuid.uuid4().hex[:12]
            conn.execute("INSERT INTO checkin_tiers (id, event_id, name, sort, created_at) "
                         "VALUES (?, ?, ?, ?, ?)", (tid, event_id, name[:80], i, now))
        t["id"] = tid
        keep.add(tid)
    for r in conn.execute("SELECT id FROM checkin_tiers WHERE event_id = ? AND archived = 0",
                          (event_id,)).fetchall():
        if r["id"] not in keep:
            conn.execute("UPDATE checkin_tiers SET archived = 1 WHERE id = ?", (r["id"],))


def _num(v, cast=float):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return cast(str(v).replace(",", "").replace("$", "").replace("₪", "").strip())
    except (TypeError, ValueError):
        return None


def _write_entries(conn, event_id, rows, day, now):
    """rows = [{tier_id, remaining, price}]; a tier with both blank is skipped
    (and clears any entry it had that day)."""
    n = 0
    for r in rows or []:
        tid = r.get("tier_id")
        if not tid:
            continue
        remaining = _num(r.get("remaining"), lambda s: int(float(s)))
        price = _num(r.get("price"))
        if remaining is None and price is None:
            conn.execute("DELETE FROM checkin_entries WHERE tier_id = ? AND day = ?", (tid, day))
            continue
        conn.execute(
            "INSERT INTO checkin_entries (tier_id, event_id, day, remaining, price, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(tier_id, day) DO UPDATE SET "
            "remaining = excluded.remaining, price = excluded.price, created_at = excluded.created_at",
            (tid, event_id, day, remaining, price, now))
        n += 1
    return n


def save_event(body):
    """Create (no id) or update an event, its tiers, and — on create — the
    starting counts typed into the tier rows as today's first check-in."""
    now = _now()
    event_id = body.get("id")
    existing = get_event(event_id) if event_id else None
    if event_id and not existing:
        raise ValueError("event not found")
    fields = _event_fields(body)
    if not existing and not fields.get("name"):
        raise ValueError("name required")
    if existing and "name" in fields and not fields["name"]:
        raise ValueError("name required")

    # Artwork: a pasted image always wins; otherwise re-read the event page
    # whenever the link changes (or on create).
    link = fields.get("link", existing.get("link") if existing else None)
    manual = fields.get("image_manual", existing.get("image_manual") if existing else 0)
    link_changed = not existing or ("link" in fields and fields["link"] != existing.get("link"))
    cleared_manual = bool(existing and existing.get("image_manual") and not manual)
    if existing and not manual and not cleared_manual and not link_changed:
        # blank box on an auto image = "keep what was found", not "remove it"
        fields.pop("image_url", None)
    if not manual and (link_changed or cleared_manual):
        fields["image_url"] = fetch_image(link)
        fields["image_checked_at"] = now

    with db.connect() as conn:
        if existing:
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                conn.execute(f"UPDATE checkin_events SET {sets}, updated_at = ? WHERE id = ?",
                             (*fields.values(), now, event_id))
        else:
            event_id = "cie-" + uuid.uuid4().hex[:12]
            fields.setdefault("currency", "USD")
            cols = ["id", "created_at", *fields]
            conn.execute(f"INSERT INTO checkin_events ({', '.join(cols)}) "
                         f"VALUES ({', '.join('?' for _ in cols)})",
                         (event_id, now, *fields.values()))
        if "tiers" in body:
            tiers = body.get("tiers") or []
            _sync_tiers(conn, event_id, tiers, now)
            if not existing:
                _write_entries(conn, event_id,
                               [{"tier_id": t.get("id"), "remaining": t.get("remaining"),
                                 "price": t.get("price")} for t in tiers],
                               today(), now)
        conn.commit()
    return event_id


def refresh_image(event_id):
    ev = get_event(event_id)
    if not ev:
        raise ValueError("event not found")
    img = fetch_image(ev.get("link"))
    with db.connect() as conn:
        conn.execute("UPDATE checkin_events SET image_url = ?, image_manual = 0, "
                     "image_checked_at = ? WHERE id = ?", (img, _now(), event_id))
        conn.commit()
    return img


def save_checkin(event_id, rows, day=None):
    if not get_event(event_id):
        raise ValueError("event not found")
    day = (day or today())[:10]
    datetime.fromisoformat(day)  # ValueError on junk
    with db.connect() as conn:
        n = _write_entries(conn, event_id, rows, day, _now())
        conn.execute("UPDATE checkin_events SET updated_at = ? WHERE id = ?", (_now(), event_id))
        conn.commit()
    return n


def delete_checkin_day(event_id, day):
    with db.connect() as conn:
        conn.execute("DELETE FROM checkin_entries WHERE event_id = ? AND day = ?", (event_id, day))
        conn.commit()


def delete_event(event_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM checkin_entries WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM checkin_tiers WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM checkin_events WHERE id = ?", (event_id,))
        conn.commit()


# --- reminder ----------------------------------------------------------------

def reminder_events():
    """Upcoming, non-archived events — the ones a reminder should list."""
    return [e for e in all_events() if not e["archived"] and not e["past"]]


if __name__ == "__main__":
    import sys
    db.init()
    if len(sys.argv) > 1:
        print(fetch_image(sys.argv[1]))
    else:
        for e in reminder_events():
            print(e["event_date_iso"], e["name"], "last:", e["last_checkin"], "due" if e["due"] else "")
