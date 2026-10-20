#!/usr/bin/env python3
"""Frontgate stock watcher: paste a frontgatetickets.com event link, see how many
tickets are left in each section, keep a history, and get pinged when stock moves.

Frontgate event pages don't show counts, but the seating-chart code on every
event reads a public JSON endpoint, /section/<eventHubId>/info, that returns
totalSeats and availableSeats for each section. This server resolves an event
link to its eventHubId once, then polls that endpoint on a timer.

Runs as a small web app (standard library only) meant to sit behind the Caddy
basic-auth on kartis.homes at /frontgate. See README.md in this folder.

Environment variables (all optional):
  FRONTGATE_HOST        address to bind (default 127.0.0.1)
  FRONTGATE_PORT        port to bind (default 8787)
  FRONTGATE_DB          SQLite file (default frontgate.db next to this script)
  FRONTGATE_INTERVAL    seconds between checks of every watch (default 120)
  NTFY_TOPIC            ntfy.sh topic to push to
  NTFY_SERVER           ntfy server (default: https://ntfy.sh)
  DISCORD_WEBHOOK_URL   Discord channel webhook to post to
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)*frontgatetickets\.com$")

db_lock = threading.Lock()
db = None
wake = threading.Event()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── Frontgate ────────────────────────────────────────────────────────────────

def http(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_link(link):
    """Return (host, event_code) for a Frontgate event link, or raise ValueError."""
    link = link.strip()
    if "://" not in link:
        link = "https://" + link
    u = urllib.parse.urlparse(link)
    host = (u.hostname or "").lower()
    if not HOST_RE.match(host):
        raise ValueError("that isn't a frontgatetickets.com link")
    m = re.match(r"^/event/([A-Za-z0-9]+)", u.path)
    if not m:
        raise ValueError("expected a link like https://<name>.frontgatetickets.com/event/<id>")
    return host, m.group(1)


def attr(page, name):
    m = re.search(rf'data-{name}="([^"]*)"', page, re.I)
    return html.unescape(m.group(1)).strip() if m else None


def resolve_event(link):
    """Fetch the event page once and pull out the hub id plus display details."""
    host, code = parse_link(link)
    url = f"https://{host}/event/{code}"
    page = http(url)

    hub_id = attr(page, "eventHubId") or attr(page, "id")
    if not hub_id or not hub_id.isdigit():
        raise ValueError("couldn't find the event's id on the page (is it fully sold out or not on sale yet?)")

    og = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    title = re.search(r"<title>([^<]*)", page)
    image = re.search(r'<meta property="og:image" content="([^"]*)"', page)

    # section id -> ticket name, for sections whose info doesn't carry a name
    names = {}
    for m in re.finditer(r'class="ticket-price-section"(.*?)data-sectionId="(\d+)"', page, re.S):
        block, section_id = m.groups()
        name = re.search(r'<div class="xtix-title">([^<]*)', block)
        if name:
            names[section_id] = html.unescape(name.group(1)).strip()

    return {
        "url": url,
        "host": host,
        "code": code,
        "hub_id": hub_id,
        "title": html.unescape((title.group(1) if title else None) or (og.group(1) if og else code)).strip(),
        "subtitle": html.unescape(og.group(1)).strip() if og else None,
        "venue": attr(page, "variant"),
        "starts": attr(page, "eventStart"),
        "image": image.group(1).replace("http://", "https://", 1) if image else None,
        "names": names,
    }


def fetch_sections(host, hub_id):
    body = http(f"https://{host}/section/{hub_id}/info", headers={
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    data = json.loads(body)
    if data.get("error"):
        raise ValueError(f"Frontgate said: {data.get('msg') or data.get('errorType')}")
    info = (data.get("data") or {}).get("sectionInfo") or {}
    if isinstance(info, list):  # PHP sends [] when there are no sections
        info = {}
    sections = []
    for section_id, s in info.items():
        sections.append({
            "section_id": str(section_id),
            "name": (s.get("ticketTypes") or s.get("name") or "").strip(),
            "total": int(s.get("totalSeats") or 0),
            "available": int(s.get("availableSeats") or 0),
            "price": s.get("maxPrice"),
        })
    return sections


# ─── Storage ──────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY,
  url TEXT NOT NULL UNIQUE,
  host TEXT NOT NULL,
  hub_id TEXT NOT NULL,
  title TEXT, subtitle TEXT, venue TEXT, starts TEXT, image TEXT,
  alert_below INTEGER,
  alerted_below INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  added_at TEXT NOT NULL,
  last_checked TEXT,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS sections (
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  section_id TEXT NOT NULL,
  name TEXT, price REAL,
  total INTEGER NOT NULL, available INTEGER NOT NULL,
  first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (watch_id, section_id)
);
-- one row per section whenever its available count changes
CREATE TABLE IF NOT EXISTS history (
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  section_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  total INTEGER NOT NULL, available INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS history_watch ON history (watch_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
  ts TEXT NOT NULL,
  message TEXT NOT NULL
);
"""


def open_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def q(sql, args=()):
    with db_lock:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def watch_summary(w):
    sections = q("SELECT section_id, name, price, total, available, updated_at FROM sections "
                 "WHERE watch_id = ? ORDER BY price IS NULL, price, section_id", (w["id"],))
    total = sum(s["total"] for s in sections)
    available = sum(s["available"] for s in sections)
    return {**w, "sections": sections, "total": total, "available": available,
            "sold": total - available,
            "recent_alerts": q("SELECT ts, message FROM alerts WHERE watch_id = ? ORDER BY id DESC LIMIT 5", (w["id"],))}


def list_watches():
    return [watch_summary(w) for w in q("SELECT * FROM watches ORDER BY active DESC, id DESC")]


def get_watch(watch_id):
    rows = q("SELECT * FROM watches WHERE id = ?", (watch_id,))
    return watch_summary(rows[0]) if rows else None


# ─── Checking + alerts ────────────────────────────────────────────────────────

def section_label(name, section_id):
    return name or f"Section {section_id}"


def check_watch(watch_id, quiet=False):
    """Poll one watch, store changes, and send alerts. Returns the fresh summary."""
    w = q("SELECT * FROM watches WHERE id = ?", (watch_id,))
    if not w:
        return None
    w = w[0]
    ts = now()
    try:
        fresh = fetch_sections(w["host"], w["hub_id"])
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        with db_lock, db:
            db.execute("UPDATE watches SET last_checked = ?, last_error = ? WHERE id = ?", (ts, str(e), watch_id))
        print(f"[{ts}] watch {watch_id}: check failed: {e}", file=sys.stderr)
        return get_watch(watch_id)

    old = {s["section_id"]: s for s in q("SELECT * FROM sections WHERE watch_id = ?", (watch_id,))}
    first_check = not old
    messages = []

    with db_lock, db:
        for s in fresh:
            prev = old.get(s["section_id"])
            name = s["name"] or (prev or {}).get("name") or ""
            price = s["price"] if s["price"] is not None else (prev or {}).get("price")
            label = section_label(name, s["section_id"])
            if prev is None:
                db.execute("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (watch_id, s["section_id"], name, price, s["total"], s["available"], ts, ts))
                db.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?)",
                           (watch_id, s["section_id"], ts, s["total"], s["available"]))
                if not first_check:
                    messages.append(f"New tier released: {label}, {s['available']:,} of {s['total']:,} available")
                continue

            db.execute("UPDATE sections SET name = ?, price = ?, total = ?, available = ?, updated_at = ? "
                       "WHERE watch_id = ? AND section_id = ?",
                       (name, price, s["total"], s["available"], ts, watch_id, s["section_id"]))
            if (prev["available"], prev["total"]) != (s["available"], s["total"]):
                db.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?)",
                           (watch_id, s["section_id"], ts, s["total"], s["available"]))
            if s["total"] > prev["total"]:
                messages.append(f"{label}: capacity raised {prev['total']:,} → {s['total']:,} "
                                f"({s['available']:,} available)")
            if prev["available"] > 0 and s["available"] == 0:
                messages.append(f"{label} is SOLD OUT (or every remaining ticket is in a cart)")
            elif prev["available"] == 0 and s["available"] > 0:
                messages.append(f"{label} is back: {s['available']:,} available")

        available = sum(s["available"] for s in fresh)
        threshold = w["alert_below"]
        alerted = w["alerted_below"]
        if threshold is not None and available < threshold and not alerted:
            if not first_check or not quiet:
                messages.append(f"Only {available:,} tickets left (below your alert of {threshold:,})")
            alerted = 1
        elif threshold is None or available >= threshold:
            alerted = 0  # re-arm once stock is back above the line

        db.execute("UPDATE watches SET last_checked = ?, last_error = NULL, alerted_below = ? WHERE id = ?",
                   (ts, alerted, watch_id))
        for m in messages:
            db.execute("INSERT INTO alerts (watch_id, ts, message) VALUES (?, ?, ?)", (watch_id, ts, m))

    if messages:
        notify(w, messages, available)
    return get_watch(watch_id)


def notify(w, messages, available=None):
    title = w["title"] or w["url"]
    body = "\n".join(messages)
    if available is not None:
        body += f"\n{available:,} left in total"
    print(f"[{now()}] ALERT {title}: {body}")
    try:
        topic = os.environ.get("NTFY_TOPIC")
        if topic:
            server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
            payload = {"topic": topic, "title": f"Frontgate: {title}", "message": body, "click": w["url"],
                       "tags": ["ticket"], "actions": [{"action": "view", "label": "Tickets", "url": w["url"]}]}
            http(server, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if webhook:
            content = f"**Frontgate: [{title}]({w['url']})**\n{body}"
            http(webhook, data=json.dumps({"content": content}).encode(), headers={"Content-Type": "application/json"})
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"notification failed: {e}", file=sys.stderr)


def poller(interval):
    while True:
        for w in q("SELECT id FROM watches WHERE active = 1"):
            check_watch(w["id"])
            time.sleep(2)  # spread requests out a little
        wake.wait(interval)
        wake.clear()


def add_watch(link, alert_below=None):
    ev = resolve_event(link)
    existing = q("SELECT id FROM watches WHERE url = ?", (ev["url"],))
    if existing:
        return check_watch(existing[0]["id"])
    with db_lock, db:
        cur = db.execute(
            "INSERT INTO watches (url, host, hub_id, title, subtitle, venue, starts, image, alert_below, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ev["url"], ev["host"], ev["hub_id"], ev["title"], ev["subtitle"], ev["venue"], ev["starts"],
             ev["image"], alert_below, now()))
        watch_id = cur.lastrowid
    summary = check_watch(watch_id, quiet=True)
    if ev["names"]:  # fill in names the info endpoint left blank
        with db_lock, db:
            for section_id, name in ev["names"].items():
                db.execute("UPDATE sections SET name = ? WHERE watch_id = ? AND section_id = ? AND (name IS NULL OR name = '')",
                           (name, watch_id, section_id))
        summary = get_watch(watch_id)
    return summary


# ─── Web ──────────────────────────────────────────────────────────────────────

def parse_threshold(value):
    if value in (None, ""):
        return None
    n = int(value)
    if n < 0:
        raise ValueError("alert threshold can't be negative")
    return n


class Handler(BaseHTTPRequestHandler):
    server_version = "frontgate-watch"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, msg, status=HTTPStatus.BAD_REQUEST):
        self.send_json({"error": msg}, status)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def route(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        m = re.fullmatch(r"/api/watches/(\d+)(/history|/check)?", path)
        return path, (int(m.group(1)) if m else None), (m.group(2) if m else None)

    def do_GET(self):
        path, watch_id, sub = self.route()
        if path == "/":
            body = INDEX_HTML.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self.send_json({"ok": True})
        elif path == "/api/watches":
            self.send_json({"watches": list_watches(), "interval": INTERVAL})
        elif watch_id and sub == "/history":
            self.send_json({"history": q("SELECT section_id, ts, total, available FROM history "
                                         "WHERE watch_id = ? ORDER BY ts", (watch_id,))})
        elif watch_id and not sub:
            w = get_watch(watch_id)
            self.send_json(w) if w else self.error("not found", HTTPStatus.NOT_FOUND)
        else:
            self.error("not found", HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path, watch_id, sub = self.route()
        try:
            if path == "/api/watches":
                data = self.read_json()
                self.send_json(add_watch(data.get("url", ""), parse_threshold(data.get("alert_below"))),
                               HTTPStatus.CREATED)
            elif watch_id and sub == "/check":
                w = check_watch(watch_id)
                self.send_json(w) if w else self.error("not found", HTTPStatus.NOT_FOUND)
            elif path == "/api/test-notify":
                notify({"title": "Test ping", "url": "https://kartis.homes/frontgate/"},
                       ["Frontgate watcher notifications are working."])
                self.send_json({"ok": True})
            else:
                self.error("not found", HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as e:
            self.error(str(e))
        except (urllib.error.URLError, TimeoutError) as e:
            self.error(f"couldn't reach Frontgate: {e}", HTTPStatus.BAD_GATEWAY)

    def do_PATCH(self):
        _, watch_id, sub = self.route()
        if not watch_id or sub:
            return self.error("not found", HTTPStatus.NOT_FOUND)
        try:
            data = self.read_json()
            with db_lock, db:
                if "alert_below" in data:
                    db.execute("UPDATE watches SET alert_below = ?, alerted_below = 0 WHERE id = ?",
                               (parse_threshold(data["alert_below"]), watch_id))
                if "active" in data:
                    db.execute("UPDATE watches SET active = ? WHERE id = ?", (1 if data["active"] else 0, watch_id))
        except (ValueError, json.JSONDecodeError) as e:
            return self.error(str(e))
        w = get_watch(watch_id)
        self.send_json(w) if w else self.error("not found", HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        _, watch_id, sub = self.route()
        if not watch_id or sub:
            return self.error("not found", HTTPStatus.NOT_FOUND)
        with db_lock, db:
            db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        self.send_json({"ok": True})


INTERVAL = 120


def main():
    global db, INTERVAL
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("FRONTGATE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FRONTGATE_PORT", 8787)))
    ap.add_argument("--db", default=os.environ.get("FRONTGATE_DB", str(HERE / "frontgate.db")))
    ap.add_argument("--interval", type=int, default=int(os.environ.get("FRONTGATE_INTERVAL", 120)),
                    help="seconds between checks (default 120)")
    ap.add_argument("--check", metavar="LINK", help="print the stock for one event link and exit")
    args = ap.parse_args()

    if args.check:
        ev = resolve_event(args.check)
        print(f"{ev['title']} — {ev['venue'] or ''} {ev['starts'] or ''}".strip())
        rows = fetch_sections(ev["host"], ev["hub_id"])
        for s in rows:
            print(f"  {section_label(s['name'] or ev['names'].get(s['section_id'], ''), s['section_id'])}: "
                  f"{s['available']:,} of {s['total']:,} left")
        print(f"  total: {sum(s['available'] for s in rows):,} of {sum(s['total'] for s in rows):,} left")
        return

    INTERVAL = max(30, args.interval)
    db = open_db(args.db)
    threading.Thread(target=poller, args=(INTERVAL,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"frontgate watcher on http://{args.host}:{args.port} (checking every {INTERVAL}s, db {args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
