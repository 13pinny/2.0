#!/usr/bin/env python3
"""Watch a Shotgun venue page and send a ping when a new upcoming event appears.

shotgun.live sits behind a Vercel bot checkpoint that blocks plain HTTP clients
(and headless browsers from datacenter IPs), so the page is fetched through the
Jina reader proxy (r.jina.ai), which renders it in a real browser.

The page only renders the first 24 upcoming events; the rest sit behind a
"See more" button. If JINA_API_KEY is set, a small script is injected to click
it until every upcoming event is loaded. Without a key, only the first 24
(roughly the next month) are checked.

Environment variables (all optional):
  SHOTGUN_URL          venue page to watch (default: Nu Androids)
  JINA_API_KEY         Jina reader key, enables loading the full upcoming list
  NTFY_TOPIC           ntfy.sh topic to push to (install the ntfy app and subscribe)
  NTFY_SERVER          ntfy server (default: https://ntfy.sh)
  DISCORD_WEBHOOK_URL  Discord channel webhook to post to
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://shotgun.live/en/venues/nu-androids"
STATE_FILE = Path(__file__).with_name("seen_events.json")
LOCAL_TZ = ZoneInfo("America/New_York")  # Nu Androids is in Washington, DC
USER_AGENT = "shotgun-watch/1.0"

# Clicks the "See more" button that sits before the "Past events" heading
# until it disappears, then flags the body so the reader knows it can snapshot.
EXPAND_SCRIPT = """
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const upcomingMore = () => {
    const past = [...document.querySelectorAll('h2')].find(h => h.textContent.trim().toLowerCase() === 'past events');
    return [...document.querySelectorAll('button')].find(b =>
      b.textContent.trim().toLowerCase() === 'see more' &&
      (!past || (b.compareDocumentPosition(past) & Node.DOCUMENT_POSITION_FOLLOWING)));
  };
  for (let i = 0; i < 30; i++) {
    const btn = upcomingMore();
    if (!btn) break;
    btn.click();
    await sleep(2000);
  }
  document.body.setAttribute('data-shotgun-watch-done', '1');
})();
"""


def http(url, data=None, headers=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_page(url, api_key=None):
    headers = {"X-Return-Format": "html", "X-Timeout": "60"}
    if api_key:
        headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Wait-For-Selector": "body[data-shotgun-watch-done]",
        })
        body = json.dumps({"url": url, "injectPageScript": EXPAND_SCRIPT}).encode()
        return http("https://r.jina.ai/", data=body, headers=headers)
    return http(f"https://r.jina.ai/{url}", headers=headers)


def text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse_upcoming(page):
    """Return the event cards listed under the "Upcoming events" heading."""
    start = page.find(">Upcoming events<")
    if start == -1:
        if "upcoming_events_empty1" in page or "No upcoming events" in page:
            return []
        raise ValueError("couldn't find the 'Upcoming events' section (page blocked or layout changed)")
    end = page.find(">Past events<", start)
    section = page[start:end if end != -1 else len(page)]

    events = {}
    for m in re.finditer(r'<a[^>]*href="(?:https://shotgun\.live)?/(?:[a-z-]+/)?events/([^"/?#]+)"[^>]*>(.*?)</a>', section, re.S):
        slug, card = m.group(1), m.group(2)
        title = re.search(r"<p[^>]*font-bold[^>]*>(.*?)</p>", card, re.S) or re.search(r'<img[^>]*alt="([^"]*)"', card)
        when = re.search(r'<time datetime="([^"]+)"', card)
        place = re.search(r'<div class="text-muted-foreground[^"]*">(.*?)</div>', card, re.S)
        status = re.search(r'<span class="[^"]*bg-rainbow[^"]*">(.*?)</span>', card, re.S)
        tags = re.findall(r'<div class="[^"]*rounded-full[^"]*uppercase[^"]*">(.*?)</div>', card, re.S)
        events[slug] = {
            "title": text(title.group(1)) if title else slug,
            "start": when.group(1) if when else None,
            "place": text(place.group(1)) if place else None,
            "status": text(status.group(1)) if status else None,
            "tags": [text(t) for t in tags if not text(t).startswith("+")],
            "url": f"https://shotgun.live/en/events/{slug}",
        }
    return events


def format_when(iso):
    if not iso:
        return "date TBA"
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
    return dt.strftime("%a %b %-d, %-I:%M %p")


def notify(event):
    lines = [format_when(event["start"])]
    if event.get("place"):
        lines.append(event["place"])
    if event.get("tags"):
        lines.append(" · ".join(event["tags"]))
    if event.get("status"):
        lines.append(event["status"])
    message = "\n".join(lines)
    sent = False

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        payload = {
            "topic": topic,
            "title": f"New event: {event['title']}",
            "message": message,
            "click": event["url"],
            "tags": ["tada"],
            "actions": [{"action": "view", "label": "Tickets", "url": event["url"]}],
        }
        http(server, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, timeout=30)
        sent = True

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook:
        content = f"**New event: [{event['title']}]({event['url']})**\n{message}"
        http(webhook, data=json.dumps({"content": content}).encode(), headers={"Content-Type": "application/json"}, timeout=30)
        sent = True

    if not sent:
        print("  (no NTFY_TOPIC or DISCORD_WEBHOOK_URL set, not pinging)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("SHOTGUN_URL", DEFAULT_URL))
    ap.add_argument("--state", type=Path, default=STATE_FILE)
    ap.add_argument("--dry-run", action="store_true", help="print new events without pinging or saving")
    ap.add_argument("--notify-existing", action="store_true", help="ping for every event on the first run instead of seeding silently")
    ap.add_argument("--test-ping", action="store_true", help="send a ping for the next upcoming event and exit")
    args = ap.parse_args()

    api_key = os.environ.get("JINA_API_KEY") or None
    for attempt in range(3):
        try:
            events = parse_upcoming(fetch_page(args.url, api_key))
            break
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"fetch attempt {attempt + 1} failed: {e}", file=sys.stderr)
            if attempt == 2:
                sys.exit(1)
            time.sleep(10 * (attempt + 1))

    print(f"found {len(events)} upcoming events" + ("" if api_key else " (first page only, set JINA_API_KEY for all)"))

    if args.test_ping:
        if not events:
            sys.exit("no events to test with")
        notify(next(iter(events.values())))
        return

    first_run = not args.state.exists()
    seen = {} if first_run else json.loads(args.state.read_text())
    new = [(slug, ev) for slug, ev in events.items() if slug not in seen]

    if first_run and not args.notify_existing:
        print(f"first run: recording {len(new)} existing events without pinging")
    else:
        for slug, ev in new:
            print(f"NEW: {ev['title']} | {format_when(ev['start'])} | {ev['url']}")
            if not args.dry_run:
                notify(ev)
        if not new:
            print("no new events")

    if args.dry_run:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for slug, ev in new:
        seen[slug] = {"title": ev["title"], "start": ev["start"], "first_seen": now}
    args.state.write_text(json.dumps(seen, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
