"""Laptop relay for B2B snipes: reads Automatiq + TickPick broker listings
out of a logged-in Chrome and posts them to the server (b2b_snipe.ingest).

Runs on the home relay laptop next to its dedicated B2B Chrome:

    chrome.exe --remote-debugging-port=9334 --user-data-dir=C:\\kartis\\user_data_b2b

The user logs into both sites in that Chrome by hand; this script never logs
in, never clicks, never buys. It only issues the same GETs the sites' own
pages make, from inside those pages (so the cookies, bot-check state and the
SPA's bearer token are the real ones):

  Automatiq  GET b2b.automatiq.com/api/ecomm/events/<id>/listings
             (prices in cents; pricing.wholesale.price_including_estimated_tax
             is the all-in broker price the page shows)
             GET b2b.automatiq.com/api/search/events?filter[event_name]=...
             (the ONLY filter that narrows by name; q/search/query are ignored)
  TickPick   GET api.tickpick.com/1.0/listings/internal/event-v2/<id>?paymentMethod=ACH
             (`p` = the Pro price at ACH; `ms` false = an unmapped listing)
             GET www.tickpick.com/search/?q=...  (server-rendered; event links
             look like /buy-<slug>-<m>-<d>-<yy>-<time>/<id>/)

The bearer token / client headers are captured from the page's own requests
(kept in memory only, never logged) and re-captured by reloading the tab when
a call comes back 401/403.

    python b2b_relay.py --loop            # forever, every --interval seconds
    python b2b_relay.py --once            # one pass
    python b2b_relay.py --probe automatiq <event url or id>
    python b2b_relay.py --probe tickpick  <event url or id>
    python b2b_relay.py --search tickpick "John Summit" 2026-11-13
"""
import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import cv_link_client

CDP = os.environ.get("KARTIS_B2B_CDP_URL") or "http://127.0.0.1:9334"
LOG = ROOT / "logs" / "b2b_relay.log"
NOT_FOUND_CACHE = ROOT / "logs" / "b2b_relay_notfound.json"
NOT_FOUND_HOURS = 6
PAUSE = float(os.environ.get("KARTIS_B2B_RELAY_PAUSE", "0.6"))   # between site calls

AQ_HOME = "https://app.sync.automatiq.com/sync/b2b/events"
AQ_API = "https://b2b.automatiq.com/api"
TP_HOME = "https://www.tickpick.com/broker-portal/#home"
TP_API = "https://api.tickpick.com/1.0"

# Request headers worth replaying; the browser adds the rest (and refuses
# to let a page set user-agent / referer / sec-* anyway).
_KEEP_HEADERS = {
    "automatiq": ("authorization", "accept"),
    "tickpick": ("authorization", "client-id", "client-platform", "session-id", "x-client-id",
                 "x-datadome-clientid", "forter-token-cookie", "utm-medium"),
}

_FETCH_JS = """async ([u, h, cred]) => {
  try {
    const r = await fetch(u, {headers: h, credentials: cred});
    return [r.status, await r.text()];
  } catch (e) { return [0, String(e)]; }
}"""


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


class RelayError(Exception):
    pass


# --- one logged-in site in the CDP Chrome --------------------------------

class Site:
    name = ""
    host = ""          # substring of the tab URL that is this site
    home = ""
    api_host = ""      # requests to this host carry the auth we replay
    cred = "include"

    def __init__(self, ctx):
        self.ctx = ctx
        self.page = None
        self.headers = {}

    def tab(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        for pg in self.ctx.pages:
            if self.host in pg.url:
                self.page = pg
                break
        else:
            self.page = self.ctx.new_page()
            self.page.goto(self.home, wait_until="domcontentloaded", timeout=60000)
        self.page.on("request", self._on_request)
        return self.page

    def _on_request(self, req):
        if self.api_host not in req.url:
            return
        h = req.headers
        if "authorization" not in h:
            return
        self.headers = {k: v for k, v in h.items() if k in _KEEP_HEADERS[self.name]}

    def capture(self):
        """Reload the tab so the SPA makes an authenticated call we can copy."""
        pg = self.tab()
        self.headers = {}
        if self.host not in pg.url:
            pg.goto(self.home, wait_until="domcontentloaded", timeout=60000)
        else:
            pg.reload(wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            if self.headers:
                return
            pg.wait_for_timeout(500)
        raise RelayError(f"{self.name}: no signed-in API call seen - is the tab logged in?")

    def get(self, url, *, auth=True, retry=True):
        pg = self.tab()
        if auth and not self.headers:
            self.capture()
        cred = self.cred if auth else "same-origin"
        status, body = pg.evaluate(_FETCH_JS, [url, self.headers if auth else {}, cred])
        if auth and status in (401, 403) and retry:
            self.capture()
            return self.get(url, auth=auth, retry=False)
        if status != 200:
            raise RelayError(f"{self.name}: HTTP {status} {str(body)[:160]}")
        return body


class Automatiq(Site):
    name, host, home, api_host = "automatiq", "automatiq.com", AQ_HOME, "b2b.automatiq.com/api"
    cred = "omit"      # bearer-token API; the SPA sends no cookies cross-origin

    ID_RE = re.compile(r"/events/([A-Z0-9]{5,})", re.I)

    @classmethod
    def event_id(cls, ref):
        ref = (ref or "").strip()
        m = cls.ID_RE.search(ref)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Z0-9]{5,}", ref, re.I):
            return ref
        raise RelayError(f"automatiq: can't read an event id from {ref!r}")

    @staticmethod
    def event_url(eid):
        return f"https://app.sync.automatiq.com/sync/b2b/events/{eid}"

    def listings(self, ref):
        eid = self.event_id(ref)
        url = (f"{AQ_API}/ecomm/events/{eid}/listings?page%5Bsize%5D=99999&order_by=price"
               f"&order_by_direction=asc&filter%5Bzone%5D=no&filter%5Bmy_listing%5D=all"
               f"&filter%5Bhas_venue_section_id%5D=all&filter%5Bmirror%5D=all")
        data = json.loads(self.get(url)).get("data") or []
        link = self.event_url(eid)
        out = []
        for row in data:
            a = row.get("attributes") or {}
            if a.get("my_listing"):
                continue
            whole = (a.get("pricing") or {}).get("wholesale") or {}
            cents = whole.get("price_including_estimated_tax") or whole.get("price") or a.get("price")
            if cents in (None, ""):
                continue
            out.append({
                "id": row.get("id"),
                "section": a.get("section") or "",
                "row": a.get("row"),
                "qty": a.get("quantity"),
                "price": round(float(cents) / 100, 2),
                "unmapped": a.get("venue_section_id") is None,
                "note": a.get("notes"),
                "url": link,
            })
        return out

    def search(self, name, day):
        for q in _queries(name):
            url = (f"{AQ_API}/search/events?page%5Bsize%5D=50&include_summaries=true"
                   f"&filter%5Bevent_name%5D={_q(q)}")
            data = json.loads(self.get(url)).get("data") or []
            cands = []
            for ev in data:
                a = ev.get("attributes") or {}
                d = _show_day(a.get("occurs_at"))
                if d and _day_ok(d, day):
                    cands.append((a.get("event_name") or "", self.event_url(ev["id"])))
            hit = _best(name, cands, dated=day is not None)
            if hit:
                return hit
            time.sleep(PAUSE)
        return None


class TickPick(Site):
    name, host, home, api_host = "tickpick", "tickpick.com", TP_HOME, "api.tickpick.com"
    cred = "omit"      # api.tickpick.com's CORS refuses credentialed calls; it's bearer auth

    ID_RE = re.compile(r"/(\d{5,})/?(?:[?#].*)?$")
    LINK_RE = re.compile(r'href="(/buy-([a-z0-9-]+?)-(\d{1,2})-(\d{1,2})-(\d{2})-[0-9apm]+/(\d+)/)"')

    @classmethod
    def event_id(cls, ref):
        ref = (ref or "").strip()
        if ref.isdigit():
            return ref
        m = cls.ID_RE.search(ref)
        if m:
            return m.group(1)
        raise RelayError(f"tickpick: can't read an event id from {ref!r}")

    def listings(self, ref):
        eid = self.event_id(ref)
        link = ref if ref.startswith("http") else f"https://www.tickpick.com/{eid}/"
        body = self.get(f"{TP_API}/listings/internal/event-v2/{eid}?trackView=false&paymentMethod=ACH")
        out = []
        for l in json.loads(body).get("listings") or []:
            if l.get("is_mine") or l.get("p") in (None, ""):
                continue
            out.append({
                "id": l.get("id"),
                "section": l.get("sid") or "",
                "row": l.get("r"),
                "qty": l.get("q"),
                "price": round(float(l["p"]), 2),
                "unmapped": not l.get("ms"),
                "note": l.get("n"),
                "url": link,
            })
        return out

    def search(self, name, day):
        for q in _queries(name):
            html = self.get(f"https://www.tickpick.com/search/?q={_q(q)}", auth=False)
            cands = []
            for path, slug, m, d, yy, _eid in self.LINK_RE.findall(html):
                try:
                    when = date(2000 + int(yy), int(m), int(d))
                except ValueError:
                    continue
                if _day_ok(when, day):
                    cands.append((slug.replace("-", " "), "https://www.tickpick.com" + path))
            hit = _best(name, cands, dated=day is not None)
            if hit:
                return hit
            time.sleep(PAUSE)
        return None


# --- name / date matching -------------------------------------------------

_NOISE = {"the", "and", "with", "presents", "present", "pres", "live", "at", "tickets", "ticket",
          "tour", "night", "show", "in", "of", "a", "an", "b2b", "featuring", "feat", "ft",
          "miami", "florida", "new", "york", "nyc", "brooklyn", "la", "los", "angeles",
          "california", "texas", "chicago", "illinois", "san", "diego", "francisco",
          "las", "vegas", "nevada", "day", "pass", "ga", "vip", "artist", "club", "center",
          "centre", "hall", "park", "studios", "studio", "theater", "theatre", "arena", "stage"}
_PASS_RE = re.compile(r"\bpass\b|\bmulti[- ]day\b|\b\d[- ]day\b|\bweekend\b", re.I)


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _NOISE and len(t) > 1]


def _queries(name):
    toks = _tokens(name)
    out = []
    for q in (" ".join(toks[:3]), " ".join(toks[:2]), toks[0] if toks else ""):
        if q and q not in out:
            out.append(q)
    return out


def _q(text):
    from urllib.parse import quote
    return quote(text)


def _show_day(text):
    try:
        return datetime.fromisoformat(str(text)[:19]).date()
    except (TypeError, ValueError):
        return None


def _day_ok(found, wanted):
    """Same night. A sitemap/CV date can be the calendar day after a late show
    started, so allow found = wanted - 1."""
    if not wanted:
        return True
    return found == wanted or found == wanted - timedelta(days=1)


def _best(name, cands, dated=True):
    """Pick the candidate sharing the most name tokens, with no tie. A match
    must share the name's FIRST word (the headliner) and two words in all when
    the name has two, or "James Hype" finds a "James Murray" comedy night and
    "Guy Gerber" a "Guy Torry". A multi-day / festival pass only matches a
    show that is itself a pass. `dated` is kept for callers; the rules are the
    same either way now that they are strict."""
    order = _tokens(name)[:4]
    if not order:
        return None
    want, head = set(order), order[0]
    need = min(2, len(want))
    wants_pass = bool(_PASS_RE.search(name or ""))
    scored = []
    for n, url in cands:
        if not wants_pass and _PASS_RE.search(n or ""):
            continue
        theirs = set(_tokens(n))
        got = want & theirs
        # Automatiq names a show by its artist alone ("Madeon"), so a one-word
        # candidate only has to be the headliner.
        if head in got and len(got) >= min(need, len(theirs)):
            scored.append((len(got), url))
    scored.sort(reverse=True)
    if not scored:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0] and scored[0][1] != scored[1][1]:
        return None
    return scored[0][1]


# --- server side -----------------------------------------------------------

def _notfound_load():
    try:
        return json.loads(NOT_FOUND_CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _notfound_save(d):
    try:
        NOT_FOUND_CACHE.parent.mkdir(exist_ok=True)
        NOT_FOUND_CACHE.write_text(json.dumps(d))
    except OSError:
        pass


def post(payload):
    status, raw = cv_link_client.call("/api/snipe/relay", payload)
    if status != 200:
        log(f"post {payload.get('event_id')}/{payload.get('platform')}: HTTP {status} {raw[:160]}")
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def run_pass(sites):
    status, raw = cv_link_client.call("/api/snipe/relay/targets")
    if status != 200:
        log(f"targets: HTTP {status} {raw[:200]}")
        return 0
    targets = json.loads(raw).get("targets") or []
    nf = _notfound_load()
    now = time.time()
    nf = {k: v for k, v in nf.items() if now - v < NOT_FOUND_HOURS * 3600}
    done = 0
    for t in targets:
        site = sites.get(t["platform"])
        if site is None:
            continue
        key = f"{t['event_id']}/{t['platform']}"
        ref = t.get("ref")
        payload = {"event_id": t["event_id"], "platform": t["platform"]}
        try:
            if not ref:
                if key in nf:
                    continue
                day = _show_day(t.get("event_date"))
                ref = site.search(t.get("name") or "", day)
                if not ref:
                    nf[key] = now
                    log(f"{site.name}: no match for {t.get('name')!r} {t.get('event_date')}")
                    continue
                payload["ref"] = ref
                log(f"{site.name}: matched {t.get('name')!r} -> {ref}")
            payload["listings"] = site.listings(ref)
        except Exception as e:
            payload["error"] = str(e)[:300]
            log(f"{key}: {e}")
        res = post(payload)
        done += 1
        if res and res.get("pinged"):
            log(f"{key}: pinged {res['pinged']}")
        time.sleep(PAUSE)
    _notfound_save(nf)
    return done


def connect():
    from patchright.sync_api import sync_playwright
    pw = sync_playwright().start()
    # Never browser.close(): on a CDP connection that closes the user's Chrome.
    browser = pw.chromium.connect_over_cdp(CDP)
    ctx = browser.contexts[0]
    return pw, {"automatiq": Automatiq(ctx), "tickpick": TickPick(ctx)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--probe", nargs=2, metavar=("SITE", "REF"))
    ap.add_argument("--search", nargs=3, metavar=("SITE", "NAME", "DATE"))
    a = ap.parse_args()
    if sys.stdout is None or a.loop:
        if sys.stdout is None:          # pythonw: no console
            LOG.parent.mkdir(exist_ok=True)
            sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8")
    pw, sites = connect()
    if a.probe:
        rows = sites[a.probe[0]].listings(a.probe[1])
        print(len(rows), "listings")
        for r in sorted(rows, key=lambda r: r["price"])[:25]:
            print(f"  ${r['price']:>8.2f}  x{r['qty']}  {r['section']!r} row {r['row']}"
                  f"{'  [unmapped]' if r['unmapped'] else ''}")
        return 0
    if a.search:
        print(sites[a.search[0]].search(a.search[1], _show_day(a.search[2])))
        return 0
    if not a.loop:
        print(run_pass(sites), "targets read")
        return 0
    log("b2b relay started")
    while True:
        t0 = time.time()
        try:
            run_pass(sites)
        except Exception:
            log("pass failed:\n" + traceback.format_exc())
            try:            # a dead CDP connection: start over
                pw.stop()
            except Exception:
                pass
            time.sleep(10)
            try:
                pw, sites = connect()
            except Exception as e:
                log(f"reconnect failed: {e}")
        time.sleep(max(1.0, a.interval - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
