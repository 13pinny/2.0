"""Desktop relay for the Eventim tracker on /edm (DESKTOP only).

Cloudflare challenges the VPS on every route to eventim.us — plain HTTP with
the full Chrome header set, and a real Chrome on the box alike — while the same
pages load fine from the home connection. So this desktop side does the fetch:
ask kartis.homes which eventim events are tracked, load each URL in a dedicated
Chrome, and POST the rendered HTML to /api/edm/eventim-relay. The server's EDM
tick reads it from there (eventim_events.fetch_event), so diffing, Discord pings
and the /edm page work exactly as for every other source. Add / remove events
on /edm; nothing here needs editing.

Chrome: its own profile (%LOCALAPPDATA%\\eventim-relay-chrome) on CDP :9333,
launched minimized when it isn't already running. It is NOT the :9222 scraper
Chrome, so it can't disturb a Lysted/viagogo session.

Auth is the same pair cv_link_client uses: Caddy basic auth
(KARTIS_WEB_USER / KARTIS_WEB_PASS) + KARTIS_CVAUTH_SECRET, all from .env.

Run:  .venv\\Scripts\\python eventim_relay.py           one pass, prints results
      .venv\\Scripts\\pythonw eventim_relay.py          what the scheduled task runs
                                                       (logs to logs\\eventim_relay.log)
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import cv_link_client

CDP_PORT = int(os.environ.get("KARTIS_EVENTIM_RELAY_CDP_PORT") or 9333)
CDP = f"http://127.0.0.1:{CDP_PORT}"
CHROME = os.environ.get("KARTIS_CHROME_EXE") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", str(ROOT)), "eventim-relay-chrome")
LOG = ROOT / "logs" / "eventim_relay.log"


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def ensure_chrome():
    def up():
        try:
            urllib.request.urlopen(CDP + "/json/version", timeout=3)
            return True
        except Exception:
            return False
    if up():
        return
    subprocess.Popen([CHROME, f"--remote-debugging-port={CDP_PORT}",
                      f"--user-data-dir={PROFILE}", "--no-first-run",
                      "--no-default-browser-check", "--start-minimized",
                      "--window-size=1100,800", "about:blank"])
    for _ in range(20):
        time.sleep(1)
        if up():
            return
    raise RuntimeError(f"Chrome didn't open CDP on :{CDP_PORT}")


def render(ctx, url):
    pg = ctx.new_page()
    try:
        pg.goto(url, timeout=60000, wait_until="domcontentloaded")
        # A Cloudflare check on a home IP usually clears itself in a few
        # seconds; give it that long before calling the page tierless.
        try:
            pg.wait_for_selector("ul.ticket-list .ticket-type", timeout=25000)
        except Exception:
            return None, pg.title()
        return pg.content(), pg.title()
    finally:
        pg.close()


def main():
    if sys.stdout is None:  # pythonw: no console
        LOG.parent.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8")
    status, raw = cv_link_client.call("/api/edm/eventim-relay/targets")
    if status != 200:
        log(f"targets: HTTP {status} {raw[:200]}")
        return 1
    targets = json.loads(raw)["targets"]
    if not targets:
        return 0
    ensure_chrome()
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        # Never browser.close() — on a CDP connection that closes the user's Chrome.
        ctx = p.chromium.connect_over_cdp(CDP).contexts[0]
        for t in targets:
            try:
                html, title = render(ctx, t["url"])
            except Exception as e:
                log(f"{t['event_key']}: load failed {type(e).__name__}: {e}")
                continue
            if html is None:
                log(f"{t['event_key']}: no ticket list (page title {title!r})")
                continue
            status, raw = cv_link_client.call("/api/edm/eventim-relay",
                                              {"event_key": t["event_key"], "html": html})
            log(f"{t['event_key']}: HTTP {status} {raw[:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
