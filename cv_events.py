"""CrowdVolt event catalog (sitemap) + DICE → CrowdVolt event matching.

CrowdVolt event pages live at www.crowdvolt.com/event/<slug>, and the slugs
are hand-written by their team — "kettama-knockdown-center-fri-oct-9-new-york",
"francis-mercier-oct-11-pacha-new-york",
"genesi-and-guests-knockdown-center-new-york-queens-october-12-2026" — so the
link can't be derived from a DICE event name, and the site has no search URL.
What it does publish is www.crowdvolt.com/sitemap.xml (~550KB, verified
2026-10-11): one <loc> per event page, past and upcoming. The slug carries
the artist, usually the venue/city and usually the date, which is enough to
match on. /dice uses this to fill its CV buttons on its own; a hand-pasted
link always wins (see app._dice_cv_resolve).

Matching is deliberately conservative — a wrong link is worse than the
"CV?" search fallback:
  * name coverage: share of the DICE name's meaningful tokens found in the
    slug (stopwords, weekdays, months and bare numbers dropped). Promoter
    prefixes are common on DICE ("RUSH presents: Kettama") and absent on CV,
    so the part after "presents"/":" is scored too and the better one wins.
  * date: when the slug carries a month+day it must equal the DICE date or
    sit one day either side (afters / past-midnight starts); a slug year must
    match. A dateless slug needs higher coverage.
  * a near-tie between two different slugs is ambiguous → no match.

Cloudflare fronts www.crowdvolt.com; from the VPS plain urllib with the full
Chrome header set gets through (cv_auth already POSTs auth/refresh there).
A challenge page or any error raises, the caller treats that as "no
catalog this time", and a stale cached copy is served when there is one.

CLI: python cv_events.py "<dice event name>" [YYYY-MM-DD] [venue]
"""
import gzip
import json
import os
import re
import threading
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

SITEMAP_URL = "https://www.crowdvolt.com/sitemap.xml"
EVENT_URL = "https://www.crowdvolt.com/event/{slug}"
CACHE_FILE = Path(__file__).parent / "tm_cache" / "cv_sitemap.json"
TTL_SECONDS = float(os.environ.get("KARTIS_CV_SITEMAP_TTL_HOURS", "6")) * 3600
RETRY_SECONDS = 30 * 60      # after a failed fetch, don't hammer CF every page load
REQUEST_TIMEOUT = 20

_LOC_RE = re.compile(r"<loc>\s*https?://(?:www\.)?crowdvolt\.com/event/([^<\s]+?)/?\s*</loc>", re.I)
_lock = threading.Lock()
_mem = None   # {"at": epoch, "slugs": [...], "failed_at": epoch|None}


def _headers():
    try:
        from edm_common import BROWSER_HEADERS
        h = dict(BROWSER_HEADERS)
    except Exception:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"}
    h["Accept"] = "application/xml,text/xml;q=0.9,*/*;q=0.8"
    return h


def fetch_sitemap():
    """Every /event/<slug> in the sitemap. Raises on a block or bad payload
    — a Cloudflare challenge must never read as 'CrowdVolt has no events'."""
    req = urllib.request.Request(SITEMAP_URL, headers=_headers())
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    slugs = list(dict.fromkeys(m.group(1) for m in _LOC_RE.finditer(text)))
    if not slugs:
        raise RuntimeError(f"no event URLs in sitemap ({len(text)} bytes — challenge page?)")
    return slugs


def _load_cache():
    global _mem
    if _mem is None:
        try:
            _mem = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _mem = {"at": 0, "slugs": [], "failed_at": None}
    return _mem


def event_slugs():
    """Cached slug list (6h TTL). On a failed refresh serve the stale copy
    and back off RETRY_SECONDS; with no copy at all, return []."""
    with _lock:
        c = _load_cache()
        now = time.time()
        fresh = c.get("slugs") and now - (c.get("at") or 0) < TTL_SECONDS
        backing_off = c.get("failed_at") and now - c["failed_at"] < RETRY_SECONDS
        if fresh or backing_off:
            return c.get("slugs") or []
        try:
            slugs = fetch_sitemap()
            c.update({"at": now, "slugs": slugs, "failed_at": None})
        except Exception as e:
            c["failed_at"] = now
            c["error"] = f"{type(e).__name__}: {e}"
        try:
            CACHE_FILE.parent.mkdir(exist_ok=True)
            CACHE_FILE.write_text(json.dumps(c), encoding="utf-8")
        except OSError:
            pass
        return c.get("slugs") or []


# ---- matching ---------------------------------------------------------------

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {"mon", "monday", "tue", "tues", "tuesday", "wed", "wednesday", "thu",
             "thur", "thurs", "thursday", "fri", "friday", "sat", "saturday",
             "sun", "sunday"}
_STOP = {"the", "a", "an", "and", "of", "at", "in", "on", "presents", "present",
         "pres", "tickets", "ticket", "w", "with", "b2b", "x", "vs", "plus",
         "feat", "ft", "featuring", "tour", "show"} | _WEEKDAYS | set(_MONTHS)


def _tokens(s):
    s = (s or "").lower().replace("&", " and ")
    return [t for t in re.split(r"[^a-z0-9]+", s) if t]


def _meaningful(tokens):
    return [t for t in tokens if t not in _STOP and not t.isdigit() and len(t) >= 2]


def _name_variants(name):
    """The full name plus the headliner part after a promoter prefix
    ("RUSH presents: Kettama" → also "Kettama")."""
    out = [name or ""]
    m = re.split(r"\bpresents?\b|\bpres\.?\b|:", name or "", maxsplit=1, flags=re.I)
    if len(m) == 2 and m[1].strip():
        out.append(m[1])
    return out


def _slug_date(tokens):
    """(month, day, year|None) from a slug's tokens, or None."""
    for i, t in enumerate(tokens):
        mo = _MONTHS.get(t)
        if not mo or i + 1 >= len(tokens):
            continue
        d = tokens[i + 1]
        if d.isdigit() and 1 <= int(d) <= 31:
            yr = None
            if i + 2 < len(tokens) and re.fullmatch(r"20\d\d", tokens[i + 2]):
                yr = int(tokens[i + 2])
            return mo, int(d), yr
    return None


def _date_score(slug_date, event_date):
    """+0.3 exact day, +0.15 one day off, None = incompatible, 0 = unknown."""
    if not slug_date or not event_date:
        return 0.0
    mo, day, yr = slug_date
    if yr and yr != event_date.year:
        return None
    for delta, pts in ((0, 0.3), (1, 0.15), (-1, 0.15)):
        d = event_date + timedelta(days=delta)
        if (d.month, d.day) == (mo, day):
            return pts
    return None


def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def score(slug, name, venue=None, event_date=None):
    """Match score for one slug, or None when it can't be this event."""
    st = _tokens(slug)
    sset = set(st)
    cov = 0.0
    for variant in _name_variants(name):
        want = _meaningful(_tokens(variant))
        if not want:
            continue
        hit = [t for t in want if t in sset]
        # One long token must carry it — "dj"/"nyc"-style hits alone don't.
        if not any(len(t) >= 4 for t in hit):
            continue
        cov = max(cov, len(hit) / len(want))
    if cov < 0.6:
        return None
    ds = _date_score(_slug_date(st), event_date)
    if ds is None:
        return None
    vt = [t for t in _meaningful(_tokens(venue)) if len(t) >= 3]
    venue_bonus = 0.1 if vt and any(t in sset for t in vt) else 0.0
    if ds == 0:
        # Undated slug (recurring series pages like "...-afters-new-york-pacha")
        # against a dated event: could be any night, so the whole name AND the
        # venue must line up. Undated event: demand a strong name hit.
        if event_date and not (cov >= 1.0 and venue_bonus):
            return None
        if cov < 0.8:
            return None
    return round(cov + ds + venue_bonus, 3)


def match(name, venue=None, event_date=None, slugs=None):
    """Best CrowdVolt event URL for a DICE event, or None. event_date is a
    date or 'YYYY-MM-DD…' string. Ambiguous near-ties return None."""
    if slugs is None:
        slugs = event_slugs()
    d = event_date if isinstance(event_date, date) else _parse_date(event_date)
    scored = sorted(((s, slug) for slug in slugs
                     if (s := score(slug, name, venue, d)) is not None), reverse=True)
    if not scored:
        return None
    best, slug = scored[0]
    if best < 0.9:
        return None
    if len(scored) > 1 and best - scored[1][0] < 0.05:
        return None
    return EVENT_URL.format(slug=slug)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print('usage: python cv_events.py "<dice event name>" [YYYY-MM-DD] [venue]')
        sys.exit(2)
    nm = sys.argv[1]
    dt = sys.argv[2] if len(sys.argv) > 2 else None
    vn = sys.argv[3] if len(sys.argv) > 3 else None
    sl = event_slugs()
    print(f"{len(sl)} CrowdVolt event slugs in sitemap")
    d0 = _parse_date(dt)
    top = sorted(((s, x) for x in sl if (s := score(x, nm, vn, d0)) is not None), reverse=True)[:5]
    for s, x in top:
        print(f"  {s:.3f}  {EVENT_URL.format(slug=x)}")
    print("match:", match(nm, vn, dt, sl))
