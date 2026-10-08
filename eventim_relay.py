"""Desktop relay for the Eventim tracker on /edm and the Marquee (tao) pages
on /marquee (DESKTOP only).

Cloudflare challenges the VPS on every route to eventim.us — plain HTTP with
the full Chrome header set, and a real Chrome on the box alike — while the same
pages load fine from the home connection. So this desktop side does the fetch:
ask kartis.homes which eventim events are tracked, load each URL in a dedicated
Chrome, and POST the rendered HTML to /api/edm/eventim-relay. The server's EDM
tick reads it from there (eventim_events.fetch_event), so diffing, Discord pings
and the /edm page work exactly as for every other source. Add / remove events
on /edm; nothing here needs editing.

Marquee: tickets.taogroup.com shows a Cloudflare check to every non-browser
request (the VPS, a residential proxy, even this PC's plain HTTP), so the
targets list also carries every tracked tao event (source "tao"), loaded the
same way and posted back with its source. A real browser load only -- nothing
here solves or skips a challenge; a page that doesn't show its tickets within
the wait is logged and skipped.

Chrome: its own profile (%LOCALAPPDATA%\\eventim-relay-chrome) on CDP :9333,
launched minimized when it isn't already running. It is NOT the :9222 scraper
Chrome, so it can't disturb a Lysted/viagogo session.

Auth is the same pair cv_link_client uses: Caddy basic auth
(KARTIS_WEB_USER / KARTIS_WEB_PASS) + KARTIS_CVAUTH_SECRET, all from .env.

SERVER MODE (Linux, the VPS): the same script runs on the box every 5 min
from kartis-relay.timer, driving its own headed Chrome (kartis-chrome-relay,
CDP :9333 on the Xvfb display, so it shows up in noVNC) and talking to Flask
on 127.0.0.1:8000 directly. That keeps Marquee prices flowing with the PC off.
KARTIS_RELAY_SOURCES=tao limits it to tao (Cloudflare blocks eventim from the
box even in a real Chrome). When the box's Chrome is held at the Cloudflare
check, one Discord status ping says so: open vnc.kartis.homes and click the
box in the "relay" Chrome window; a second ping says when pages load again.
It never solves or skips a challenge itself. See deploy/README.md.

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
# On the box systemd owns the Chrome (kartis-chrome-relay.service); never spawn one.
SERVER_MODE = sys.platform != "win32"
# Comma list of sources to load here; empty = every source the server lists.
SOURCES = {x.strip() for x in (os.environ.get("KARTIS_RELAY_SOURCES") or "").split(",") if x.strip()}
# Remembers whether we already pinged Discord about a Cloudflare hold.
BLOCK_STATE = ROOT / "logs" / "relay_block.json"
BLOCK_REPING_SECONDS = 6 * 3600


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
    if SERVER_MODE:
        raise RuntimeError(f"no Chrome on CDP :{CDP_PORT} - "
                           "sudo systemctl restart kartis-chrome-relay")
    subprocess.Popen([CHROME, f"--remote-debugging-port={CDP_PORT}",
                      f"--user-data-dir={PROFILE}", "--no-first-run",
                      "--no-default-browser-check", "--start-minimized",
                      "--window-size=1100,800", "about:blank"])
    for _ in range(20):
        time.sleep(1)
        if up():
            return
    raise RuntimeError(f"Chrome didn't open CDP on :{CDP_PORT}")


# What a loaded page shows once its tickets are on screen, per source.
READY_SELECTOR = {
    "eventim": "ul.ticket-list .ticket-type",
    "tao": "#ticket-types-content .ticket-type-item",
}


def render(ctx, url, source="eventim"):
    pg = ctx.new_page()
    try:
        pg.goto(url, timeout=60000, wait_until="domcontentloaded")
        # A Cloudflare check on a home IP usually clears itself in a few
        # seconds; give it that long before calling the page tierless.
        try:
            pg.wait_for_selector(READY_SELECTOR[source], timeout=25000)
        except Exception:
            return None, pg.title()
        return pg.content(), pg.title()
    finally:
        pg.close()


def _is_challenge(title):
    t = (title or "").lower()
    return "just a moment" in t or "attention required" in t


def report_block(blocked, loaded):
    """Server mode: one Discord status ping when the box's Chrome gets held
    at the Cloudflare check (repeated every BLOCK_REPING_SECONDS while it
    lasts) and one when pages load again. Nothing on the desktop -- a home
    connection clears the check by itself."""
    if not SERVER_MODE:
        return
    try:
        state = json.loads(BLOCK_STATE.read_text())
    except (OSError, ValueError):
        state = {}
    now = time.time()
    if blocked and not loaded:
        if now - state.get("pinged_at", 0) < BLOCK_REPING_SECONDS:
            return
        msg = (f"⚠️ **Marquee prices paused** — Cloudflare is holding the server's "
               f"relay Chrome ({blocked} page(s)). Open https://vnc.kartis.homes/vnc.html "
               f"and click the checkbox in the relay Chrome window.")
        state = {"blocked": True, "pinged_at": now}
    elif loaded and state.get("blocked"):
        msg = "✅ **Marquee prices flowing again** — the server relay loaded the ticket pages."
        state = {}
    else:
        return
    import notify
    hook = notify._discord_webhook("status")
    if hook:
        log(f"discord status: {notify._post_discord(hook, {'content': msg})}")
    BLOCK_STATE.parent.mkdir(exist_ok=True)
    BLOCK_STATE.write_text(json.dumps(state))


def main():
    if sys.stdout is None:  # pythonw: no console
        LOG.parent.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8")
    status, raw = cv_link_client.call("/api/edm/eventim-relay/targets")
    if status != 200:
        log(f"targets: HTTP {status} {raw[:200]}")
        return 1
    targets = [t for t in json.loads(raw)["targets"]
               if (t.get("source") or "eventim") in READY_SELECTOR
               and (not SOURCES or (t.get("source") or "eventim") in SOURCES)]
    if not targets:
        return 0
    ensure_chrome()
    from patchright.sync_api import sync_playwright
    blocked = loaded = 0
    with sync_playwright() as p:
        # Never browser.close() — on a CDP connection that closes the user's Chrome.
        ctx = p.chromium.connect_over_cdp(CDP).contexts[0]
        for t in targets:
            source = t.get("source") or "eventim"
            try:
                html, title = render(ctx, t["url"], source)
            except Exception as e:
                log(f"{t['event_key']}: load failed {type(e).__name__}: {e}")
                continue
            if html is None:
                log(f"{t['event_key']}: no ticket list (page title {title!r})")
                if _is_challenge(title):
                    blocked += 1
                    if blocked >= 2 and not loaded:
                        log("held at the Cloudflare check - skipping the rest of this pass")
                        break
                continue
            loaded += 1
            status, raw = cv_link_client.call("/api/edm/eventim-relay",
                                              {"source": source, "event_key": t["event_key"],
                                               "html": html})
            log(f"{t['event_key']}: HTTP {status} {raw[:160]}")
    report_block(blocked, loaded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
