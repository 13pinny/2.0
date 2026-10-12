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
launched when it isn't already running. It is NOT the :9222 scraper Chrome, so
it can't disturb a Lysted/viagogo session. On the desktop it must never be
seen: it is launched parked off-screen (headless would get the Cloudflare
check, same as zappa_events), every pass re-parks the window in case it was
restored, and all events load in ONE reused tab -- opening a new tab per event
is what kept popping the window to the front.

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

Shotgun: shotgun.live puts a Vercel bot checkpoint in front of every
datacenter request, so the targets list also carries the watched Shotgun
VENUE pages (source "shotgun", see the venues block in shotgun_events). For
those the relay also clicks "See more" until the whole upcoming list is on
the page, then posts it back; the EDM tick pings each newly listed show.

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
# Comma list of sources to load here. Unset: the PC loads eventim only
# (Marquee and Shotgun run from the box's own relay, and loading 28 Marquee
# pages every 5 min on the PC kept popping Chrome windows); the box loads
# whatever KARTIS_RELAY_SOURCES names, or every source the server lists.
SOURCES = {x.strip() for x in (os.environ.get("KARTIS_RELAY_SOURCES") or "").split(",") if x.strip()} \
    or (set() if SERVER_MODE else {"eventim"})
# Desktop only: where the relay Chrome's window lives -- off every monitor.
OFFSCREEN = (-2400, -2400)    # same parking spot zappa_events uses on this PC
WINDOW_SIZE = (1100, 800)
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
                      "--no-default-browser-check",
                      f"--window-position={OFFSCREEN[0]},{OFFSCREEN[1]}",
                      f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}", "about:blank"])
    for _ in range(20):
        time.sleep(1)
        if up():
            return
    raise RuntimeError(f"Chrome didn't open CDP on :{CDP_PORT}")


# What a loaded page shows once its tickets are on screen, per source.
READY_SELECTOR = {
    "eventim": "ul.ticket-list .ticket-type",
    "tao": "#ticket-types-content .ticket-type-item",
    # shotgun venue pages: the section headings ("Upcoming events" / "Past
    # events"); Vercel's checkpoint page has none.
    "shotgun": "h2[data-slot='heading']",
}

# Clicks the "See more" button that sits ABOVE the "Past events" heading
# (the upcoming list's) until it's gone, waiting for new cards after each
# click. Returns the click count, or -1 if the button never went away.
_EXPAND_JS = """
async (label) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const want = label.trim().toLowerCase();
  const button = () => {
    const past = [...document.querySelectorAll('h2')]
      .find(h => h.textContent.trim().toLowerCase() === 'past events');
    return [...document.querySelectorAll('button')].find(b =>
      b.textContent.trim().toLowerCase() === want &&
      (!past || (b.compareDocumentPosition(past) & Node.DOCUMENT_POSITION_FOLLOWING)));
  };
  const cards = () => document.querySelectorAll('a[href*="/events/"]').length;
  for (let i = 0; i < 40; i++) {
    const b = button();
    if (!b) return i;
    const before = cards();
    b.scrollIntoView();
    b.click();
    for (let t = 0; t < 40 && cards() === before && button(); t++) await sleep(250);
    await sleep(500);
  }
  return button() ? -1 : 40;
}
"""


def hide_window(ctx, pg):
    """Desktop: park the relay Chrome's window off-screen (un-minimizing it
    first -- Chrome ignores bounds on a minimized window). Covers a Chrome
    that was started by an older version of this script, or restored by
    hand. Best effort: a failure here never stops the pass."""
    if SERVER_MODE:
        return  # the box's Chrome has to stay visible in noVNC
    try:
        cdp = ctx.new_cdp_session(pg)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {
            "left": OFFSCREEN[0], "top": OFFSCREEN[1],
            "width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1]}})
        cdp.detach()
    except Exception as e:
        log(f"couldn't park the relay window off-screen: {type(e).__name__}: {e}")


def relay_tab(ctx):
    """The one tab every pass loads its pages in (reused, never closed), so
    no new tab ever opens. Extra tabs left by older versions are closed."""
    pages = [p for p in ctx.pages if not p.is_closed()]
    pg = pages[0] if pages else ctx.new_page()
    for extra in pages[1:]:
        try:
            extra.close()
        except Exception:
            pass
    return pg


def render(pg, url, source="eventim", expand=None):
    pg.goto(url, timeout=60000, wait_until="domcontentloaded")
    # A Cloudflare check on a home IP usually clears itself in a few
    # seconds; give it that long before calling the page tierless.
    try:
        pg.wait_for_selector(READY_SELECTOR[source], timeout=25000)
    except Exception:
        return None, pg.title()
    if expand:
        clicks = pg.evaluate(_EXPAND_JS, expand)
        if clicks < 0:
            log(f"{url}: '{expand}' still showing after 40 clicks")
    return pg.content(), pg.title()


def _is_challenge(title):
    t = (title or "").lower()
    return ("just a moment" in t or "attention required" in t
            or "security checkpoint" in t)       # Vercel (shotgun.live)


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
        msg = (f"⚠️ **Relay paused** (Marquee prices / Shotgun new events) — a bot check is holding the server's "
               f"relay Chrome ({blocked} page(s)). Open https://vnc.kartis.homes/vnc.html "
               f"and click the checkbox in the relay Chrome window.")
        state = {"blocked": True, "pinged_at": now}
    elif loaded and state.get("blocked"):
        msg = "✅ **Relay flowing again** — the server relay loaded its pages (Marquee / Shotgun)."
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
        pg = relay_tab(ctx)
        hide_window(ctx, pg)
        for t in targets:
            source = t.get("source") or "eventim"
            try:
                html, title = render(pg, t["url"], source, t.get("expand"))
            except Exception as e:
                log(f"{t['event_key']}: load failed {type(e).__name__}: {e}")
                if pg.is_closed():  # tab crashed -- carry on in a fresh one
                    pg = relay_tab(ctx)
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
        # Park the tab on a blank page so nothing keeps running between passes.
        try:
            pg.goto("about:blank")
        except Exception:
            pass
    report_block(blocked, loaded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
