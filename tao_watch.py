#!/usr/bin/env python3
"""Watch Tao Group event ticket prices and alert when they go up.

Designed to run on a schedule in GitHub Actions (see README.md), using its
own headless Chromium - nothing runs in your browser or on your computer.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "prices.json"
PROFILE_DIR = HERE / "browser-profile"
DEBUG_DIR = HERE / "debug"

EVENTS_API = "https://taogroup.com/wp-json/wp/v2/events"
TICKET_HOST = "tickets.taogroup.com"
UA = "Mozilla/5.0 (tao-price-watch)"

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
# Lines that contain a $ amount but aren't a ticket tier.
NOT_A_TIER = re.compile(r"\b(sub)?total\b|\bfees?\b|\btax(es)?\b|\bcart\b|\bservice charge\b", re.I)
SOLD_OUT_RE = re.compile(r"sold\s*out", re.I)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- discovery

def discover_events(cfg):
    """Return {ticket_url: event_name} from the taogroup.com events feed."""
    found = {}
    horizon = datetime.now() + timedelta(days=cfg.get("days_ahead", 30))
    for term in cfg.get("search", []):
        page = 1
        while True:
            qs = urllib.parse.urlencode({"search": term, "per_page": 100, "page": page, "_fields": "link,acf"})
            req = urllib.request.Request(f"{EVENTS_API}?{qs}", headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    events = json.load(r)
                    total_pages = int(r.headers.get("X-WP-TotalPages", 1))
            except Exception as e:  # noqa: BLE001
                log(f"Could not search events for '{term}': {e}")
                break
            for ev in events:
                acf = ev.get("acf") or {}
                try:
                    start = datetime.strptime(acf.get("event_start_date", ""), "%m/%d/%Y %I:%M %p")
                except ValueError:
                    continue
                if not (datetime.now() - timedelta(hours=12) <= start <= horizon):
                    continue
                title = (acf.get("event_title") or {}).get("display_title") or ev.get("link", "")
                venue = (acf.get("event_venue") or [{}])[0].get("post_title", "")
                for item in acf.get("links") or []:
                    url = ((item or {}).get("link") or {}).get("url", "")
                    if TICKET_HOST in url:
                        found[url.split("?")[0]] = f"{title} @ {venue} ({start:%a %b %d})".replace(" @  ", " ")
            if page >= total_pages:
                break
            page += 1
    return found


# ---------------------------------------------------------------- scraping

def parse_tiers(text):
    """Pull {tier name: price} pairs out of the rendered page text."""
    tiers, last_label = {}, None
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        m = PRICE_RE.search(line)
        if not m:
            if SOLD_OUT_RE.search(line) and tiers:
                tiers[next(reversed(tiers))]["sold_out"] = True
            elif len(line) >= 2 and not line.isdigit():
                last_label = line
            continue
        if NOT_A_TIER.search(line):
            continue
        price = float(m.group(1).replace(",", ""))
        if price <= 0:
            continue
        label = PRICE_RE.sub("", line).strip(" -–:|")
        name = label if re.search(r"[A-Za-z]{3}", label) else (last_label or "Ticket")
        key, n = name, 2
        while key in tiers:
            key, n = f"{name} #{n}", n + 1
        tiers[key] = {"price": price, "sold_out": bool(SOLD_OUT_RE.search(line))}
    return tiers


def scrape(page, url, debug=False):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    # Wait out the Cloudflare "Just a moment..." check if it shows up.
    for _ in range(30):
        if "just a moment" not in page.title().lower():
            break
        page.wait_for_timeout(1000)
    else:
        raise RuntimeError("stuck on the Cloudflare check - run once with --show and click the checkbox")
    try:
        page.wait_for_function("() => /\\$\\s?\\d/.test(document.body.innerText)", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    text = page.inner_text("body")
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9-]+", "_", url.lower())[-80:]
        (DEBUG_DIR / f"{slug}.txt").write_text(text)
        (DEBUG_DIR / f"{slug}.html").write_text(page.content())
    return parse_tiers(text)


# ---------------------------------------------------------------- alerts

def notify(cfg, title, body):
    log(f"ALERT: {title}\n{body}")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook_url")
    if webhook:
        payload = json.dumps({"username": "Tao Price Watch", "content": f"**{title}**\n{body}"[:2000]})
        req = urllib.request.Request(
            webhook, data=payload.encode(), headers={"Content-Type": "application/json", "User-Agent": UA}
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:  # noqa: BLE001
            log(f"Discord alert failed: {e}")
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if topic:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode(),
            headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": "chart_with_upwards_trend"},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:  # noqa: BLE001
            log(f"ntfy push failed: {e}")


def compare(cfg, name, url, old, new):
    changes = []
    for tier, now in new.items():
        before = old.get(tier)
        if before is None:
            if old and cfg.get("alert_new_tiers", False):
                changes.append(f"NEW  {tier}: ${now['price']:.2f}")
        elif now["price"] > before["price"]:
            changes.append(f"UP   {tier}: ${before['price']:.2f} -> ${now['price']:.2f}")
        elif now["price"] < before["price"] and cfg.get("alert_price_drops", False):
            changes.append(f"DOWN {tier}: ${before['price']:.2f} -> ${now['price']:.2f}")
        if now["sold_out"] and not (before or {}).get("sold_out") and cfg.get("alert_sold_out", True) and old:
            changes.append(f"SOLD OUT {tier}")
    if changes:
        notify(cfg, f"Tao price change: {name}", "\n".join(changes) + f"\n{url}")


# ---------------------------------------------------------------- main loop

def run_once(cfg, show=False, debug=False):
    targets = {u.split("?")[0]: u for u in cfg.get("ticket_urls", [])}
    targets.update(discover_events(cfg))
    limit = cfg.get("max_events", 40)
    if len(targets) > limit:
        log(f"{len(targets)} events matched; only checking the first {limit} (raise max_events or narrow search)")
        targets = dict(list(targets.items())[:limit])
    if not targets:
        log("Nothing to watch - add ticket_urls or search terms to config.json")
        return

    state = load_json(STATE_FILE, {})
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=not show, viewport={"width": 1280, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        blocked = 0
        for url, name in targets.items():
            try:
                tiers = scrape(page, url, debug)
                blocked = 0
            except Exception as e:  # noqa: BLE001
                log(f"FAILED {name}: {e}")
                blocked += "Cloudflare" in str(e)
                if blocked >= 2:
                    log("Blocked by Cloudflare twice in a row - stopping this run")
                    break
                continue
            if not tiers:
                log(f"No prices found on {url} (sales closed? run with --debug to inspect)")
                continue
            old = state.get(url, {}).get("tiers", {})
            compare(cfg, name, url, old, tiers)
            state[url] = {"name": name, "tiers": tiers, "checked": datetime.now().isoformat(timespec="seconds")}
            summary = ", ".join(f"{k} ${v['price']:.0f}{' (sold out)' if v['sold_out'] else ''}" for k, v in tiers.items())
            log(f"{name}: {summary}")
            STATE_FILE.write_text(json.dumps(state, indent=2))
            time.sleep(cfg.get("delay_between_pages_seconds", 5))
        ctx.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="check one time and exit (used by the GitHub Action)")
    ap.add_argument("--show", action="store_true", help="show the browser window (use if Cloudflare asks you to click)")
    ap.add_argument("--debug", action="store_true", help="save page text/HTML to ./debug for troubleshooting")
    ap.add_argument("--test-alert", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("config.json missing or invalid")
    if args.test_alert:
        notify(cfg, "Tao price watch test", "Notifications are working.")
        return

    while True:
        run_once(cfg, show=args.show, debug=args.debug)
        if args.once:
            break
        mins = cfg.get("check_every_minutes", 30)
        log(f"Next check in {mins} min (Ctrl+C to stop)")
        time.sleep(mins * 60)


if __name__ == "__main__":
    main()
