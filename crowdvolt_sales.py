"""CrowdVolt sales tracking via CrowdVolt's own JSON API.

Replaces the /selling DOM scrape, which had broken twice on redesigns and
finally stopped returning anything at all: since the CrowdVolt Chrome split
(kartis-chrome-cv on :9223, residential proxy) the login lives in
user_data_cv, while scraper.py was still driving the main :9222 Chrome —
datacenter IP, no CV session, so the page sat on a Cloudflare interstitial
and the table "never rendered". Both halves of that are fixed here: the
fetch runs through crowdvolt_pricer's CvSession (which already knows how to
reach the CV Chrome and settle a challenge), and it reads JSON instead of
a table.

Endpoints (harvested from the /selling route's Next.js chunk, 2026-08-20 —
the same `buy_sell_history` family the pricer's sell_active call belongs to,
so they share auth, paging and sort params):

    GET api.crowdvolt.com/api/buy_sell_history/sell_delivered  -- "Completed" tab
    GET api.crowdvolt.com/api/buy_sell_history/sell_incomplete -- "Incomplete" tab
    GET api.crowdvolt.com/api/buy_sell_history/sell_active     -- unsold asks (pricer)
    GET api.crowdvolt.com/api/buy_sell_history/sell_all        -- all of the above
    ?limit=N&offset=M&sort_key=date&sort_desc=true

We page delivered + incomplete and deliberately NOT sell_all: "all" includes
the still-unsold active asks, which are listings, not sales, and would
inflate revenue. A sale that hasn't been handed over yet is still a sale —
that's the `incomplete` half, and it's why the old scrape (Completed tab
only) under-reported fresh sales.

Row shape, per the page's own row mapper: event_name, event_date,
venue_name, ticket_type_name (defaults "GA"), num_tickets, price_per,
earnings_amount ?? payout_amount, order_number, transaction_date. Field
names are read through alias tuples and every row is kept verbatim in
`raw_cells`, so if CrowdVolt renames one the damage is a NULL column plus a
visible raw row rather than a silently empty sync — run
`python crowdvolt_sales.py --raw` to see what the API actually returned.

Two transports, picked automatically (see the "transports" section below):
a configured bearer token goes over plain urllib with NO browser at all;
otherwise it falls back to the CrowdVolt Chrome. The token path exists
because api.crowdvolt.com is not IP-gated — only the interactive login on
www.crowdvolt.com is, behind Cloudflare Turnstile, which an Xvfb Chrome
with CDP attached tends never to clear.

Minting the token (once, on a real desktop browser where Turnstile passes):

  1. Log into crowdvolt.com in your normal Chrome.
  2. DevTools -> Application -> Cookies -> https://www.crowdvolt.com, copy
     the `stytch_session` value. Optionally also copy localStorage's
     `fingerprint_id` (what.crowdvolt.com wants it; the sales endpoints
     did not, as probed).
  3. On the box, drop them in .env.crowdvolt (gitignored, chmod 600):

         KARTIS_CV_TOKEN=<stytch_session value>
         KARTIS_CV_FINGERPRINT=<fingerprint_id value>

  4. `python crowdvolt_sales.py --check` should print the tab counts.

Refreshing the token: sessions do lapse, and an expired one surfaces as a
loud CvSalesError naming the problem (302 auth_required / 401), never as an
empty sync. Repeat the steps above when that happens. CrowdVolt does expose
`POST api/auth/refresh` (empty body, driven by a refresh cookie, no captcha
involved) which could automate this — but it answered 403 "missing required
header" to every header combination probed anonymously, so wiring it up
needs one look at a real logged-in request first. Not guessed at here.

CLI:

    .venv\\Scripts\\python crowdvolt_sales.py            # dry-run, print rows
    .venv\\Scripts\\python crowdvolt_sales.py --doctor   # full end-to-end check
    .venv\\Scripts\\python crowdvolt_sales.py --check    # verify the token only
    .venv\\Scripts\\python crowdvolt_sales.py --raw      # dump raw JSON per tab
    .venv\\Scripts\\python crowdvolt_sales.py --write    # persist like a real sync
    .venv\\Scripts\\python crowdvolt_sales.py --browser  # force the Chrome transport
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import db

API = "https://api.crowdvolt.com"
PAGE_LIMIT = 25
# Guard against an endpoint that ignores offset and pages forever.
MAX_PAGES = 40

# (status we store, endpoint). Order matters only for dedup: a row that
# somehow appears in both tabs keeps the first (delivered) status.
SALE_TABS = (
    ("delivered", "sell_delivered"),
    ("incomplete", "sell_incomplete"),
)

# Field aliases — first present (and non-empty) wins.
_F_ORDER = ("order_number", "order_uqid", "order_id")
_F_EVENT = ("event_name",)
_F_EVENT_DATE = ("event_date", "event_datetime", "pretty_event_date")
_F_VENUE = ("venue_name", "event_location", "venue")
_F_QTY = ("num_tickets", "quantity", "qty", "qty_left")
_F_TYPE = ("ticket_type_name", "ticket_type")
_F_PRICE_PER = ("price_per", "price")
_F_PAYOUT = ("earnings_amount", "payout_amount")
_F_SALE_DATE = ("transaction_date", "sale_date", "created_at", "date")

RAW_LIMIT = 4000


class CvSalesError(RuntimeError):
    pass


def _pick(row, names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", []):
            return v
    return None


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+(?:\.\d+)?", v.replace(",", ""))
        if m:
            return float(m.group(0))
    return None


def _int(v):
    n = _num(v)
    return int(n) if n is not None else None


def _iso_date(v):
    """ISO-8601 date (YYYY-MM-DD) out of whatever the API hands us: an ISO
    timestamp, an epoch in seconds or milliseconds, or a display string.
    Returns None rather than guessing — a wrong date is worse than a blank
    one, since sale_date_iso keys the dedupe against JeruJam rows."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        ts = float(v)
        if ts > 1e11:          # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.split("•")[0].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _normalize(row, status):
    """One API row -> one crowdvolt_sales row. Returns None for a row with no
    usable id (nothing to key the upsert on)."""
    order = _pick(row, _F_ORDER)
    order = str(order).strip() if order is not None else None
    if not order:
        return None

    qty = _int(_pick(row, _F_QTY))
    price_per = _num(_pick(row, _F_PRICE_PER))
    payout = _num(_pick(row, _F_PAYOUT))
    if payout is None and price_per is not None and qty:
        # Fall back to gross. Downstream (app.py) reads sale_price as the
        # payout, so prefer CrowdVolt's own earnings figure when it's there.
        payout = round(price_per * qty, 2)

    sale_raw = _pick(row, _F_SALE_DATE)
    event_raw = _pick(row, _F_EVENT_DATE)
    raw = json.dumps(row, separators=(",", ":"), default=str)

    return {
        "id": order,
        "order_id": order,
        "sale_date": str(sale_raw) if sale_raw is not None else None,
        "sale_date_iso": _iso_date(sale_raw),
        "event_name": _pick(row, _F_EVENT),
        "event_date": str(event_raw) if event_raw is not None else None,
        "event_date_iso": _iso_date(event_raw),
        "venue": _pick(row, _F_VENUE),
        "qty": qty,
        "ticket_type": _pick(row, _F_TYPE) or "GA",
        "price_per_ticket": price_per,
        "sale_price": payout,
        "status": status,
        "raw_cells": raw[:RAW_LIMIT],
    }


def _page_tab(session, endpoint):
    """Every row of one tab, following offset paging."""
    rows, offset = [], 0
    for _ in range(MAX_PAGES):
        data = session.api(
            f"{API}/api/buy_sell_history/{endpoint}"
            f"?limit={PAGE_LIMIT}&offset={offset}&sort_key=date&sort_desc=true")
        if not isinstance(data, list):
            raise CvSalesError(
                f"{endpoint} returned {type(data).__name__}, expected a list: "
                f"{json.dumps(data)[:200]}")
        rows.extend(data)
        if len(data) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    else:
        print(f"[kartis] crowdvolt {endpoint}: stopped at the {MAX_PAGES}-page "
              f"cap ({len(rows)} rows) — paging may be ignoring offset")
    return rows


def fetch_sales(session):
    """Normalized sales rows, newest tabs first, deduped on order number."""
    out, seen = [], set()
    for status, endpoint in SALE_TABS:
        for raw in _page_tab(session, endpoint):
            if not isinstance(raw, dict):
                continue
            row = _normalize(raw, status)
            if row and row["id"] not in seen:
                seen.add(row["id"])
                out.append(row)
    return out


def fetch_raw(session):
    """{tab: [raw rows]} — the probe path, for diagnosing field drift."""
    return {endpoint: _page_tab(session, endpoint) for _, endpoint in SALE_TABS}


# --- transports -----------------------------------------------------------
# Two ways to reach the API, same one-method contract (.api(url) -> parsed
# JSON, raise on anything else), so fetch_sales() doesn't care which it got:
#
#   HttpSession — plain urllib + a bearer token. NO browser: no Chrome, no
#     Xvfb, no noVNC, no residential proxy. Viable because api.crowdvolt.com
#     is not IP-gated — probed 2026-08-20 from a datacenter ASN, every
#     endpoint answered with an application-level JSON error (302
#     auth_required / 401 missing bearer token) rather than a Cloudflare
#     interstitial. Only www.crowdvolt.com carries Turnstile, and only the
#     interactive LOGIN needs to clear it — which is why the token is minted
#     on a real desktop browser and merely carried here.
#
#   CvSession — the pricer's in-page fetch() through the CrowdVolt Chrome.
#     The original path; kept as the fallback for whenever the token lapses.

ENV_FILE = Path(__file__).resolve().parent / ".env.crowdvolt"

# XHR header set (not the document-navigation one in edm_common — Sec-Fetch
# values that say "navigate" on an API call are a fingerprint mismatch).
_API_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.crowdvolt.com",
    "Referer": "https://www.crowdvolt.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# x-pokedex is the SPA's build constant; crowdvolt_pricer pins the same value
# and documents how to sniff a new one if CrowdVolt rotates it.
POKEDEX = "0376"


def _read_env_file():
    """Parsed .env.crowdvolt, or {} when there isn't one.

    Deliberately re-read on EVERY call rather than cached into os.environ:
    the file is two lines, and the whole point of it is that a token can be
    rotated while the app runs. Seeding os.environ once would pin the first
    token for the life of the gunicorn worker, so pasting a fresh cookie
    would appear to do nothing until a restart — the exact failure this
    module exists to avoid.
    """
    out = {}
    try:
        text = ENV_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def credentials():
    """(token, fingerprint) or (None, None) when no token is configured.

    The file wins over the environment when it exists — it's the rotatable
    surface, and `setup_crowdvolt_token.sh` writes it. With no file we fall
    back to the process env so systemd/.env still works.
    """
    env = _read_env_file()
    token = (env.get("KARTIS_CV_TOKEN")
             or os.environ.get("KARTIS_CV_TOKEN") or "").strip()
    fingerprint = (env.get("KARTIS_CV_FINGERPRINT")
                   or os.environ.get("KARTIS_CV_FINGERPRINT") or "").strip()
    return (token or None), (fingerprint or None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An expired session answers 302 -> /signup with the reason in the JSON
    body. Following it would turn a clean 'you are logged out' into an
    unrelated HTML page, so redirects are surfaced, not chased."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpSession:
    """Browser-free transport. Mirrors CvSession.api()."""

    def __init__(self, token, fingerprint=None, timeout=30):
        self.token = token
        self.fingerprint = fingerprint
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)
        # Present so the scrape can close() either transport uniformly.
        self.page = None

    def api(self, url, method="GET", body=None):
        headers = dict(_API_HEADERS)
        headers["Authorization"] = f"Bearer {self.token}"
        headers["x-pokedex"] = POKEDEX
        if self.fingerprint:
            headers["x-fingerprint-id"] = self.fingerprint
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = (e.read() or b"").decode("utf-8", "replace")
            if e.code in (301, 302, 303, 307, 308, 401, 403):
                raise CvSalesError(
                    f"{method} {url.split('?')[0]} -> {e.code}: {raw[:200]} "
                    "— the CrowdVolt token is missing or expired; mint a new "
                    "one (see 'refreshing the token' in the module docstring)")
            raise CvSalesError(f"{method} {url.split('?')[0]} -> {e.code}: {raw[:200]}")
        except urllib.error.URLError as e:
            raise CvSalesError(f"{method} {url.split('?')[0]} failed: {e.reason}")
        try:
            return json.loads(raw)
        except ValueError as e:
            raise CvSalesError(f"bad JSON from {url.split('?')[0]}: {e}; got {raw[:160]}")

    def close(self):
        pass


def open_http_session():
    """HttpSession from the configured token, or None when none is set."""
    token, fingerprint = credentials()
    if not token:
        return None
    return HttpSession(token, fingerprint)


def check_token(session=None):
    """Cheapest authenticated call there is — the /selling tab counts. Returns
    whatever the API says so a human can eyeball that it's the right account."""
    session = session or open_http_session()
    if session is None:
        raise CvSalesError(
            f"no CrowdVolt token configured — set KARTIS_CV_TOKEN in {ENV_FILE} "
            "or the environment")
    return session.api(f"{API}/api/buy_sell_history/seller_counts")


# --- doctor ---------------------------------------------------------------

_ALL_ALIASES = set(
    _F_ORDER + _F_EVENT + _F_EVENT_DATE + _F_VENUE + _F_QTY + _F_TYPE
    + _F_PRICE_PER + _F_PAYOUT + _F_SALE_DATE)

# Columns that must populate for the sync to be worth anything. A column here
# that is empty on EVERY row means an alias missed — almost certainly a
# CrowdVolt rename, which the report names explicitly rather than leaving as a
# quietly blank column.
_CRITICAL = ("order_id", "event_name", "qty", "sale_price", "sale_date_iso")


def _refresh_probe(token):
    """Can the session be renewed without a browser? POST auth/refresh takes
    an empty body and no captcha, but rejects anonymous callers with 403
    'missing required header' — this finds which credential shape it wants,
    using the real session, so renewal can be automated instead of guessed.
    Read-only: refresh mints a new access token, it never invalidates one."""
    attempts = (
        ("bearer", {"Authorization": f"Bearer {token}"}),
        ("cookie", {"Cookie": f"stytch_session={token}"}),
        ("bearer+cookie", {"Authorization": f"Bearer {token}",
                           "Cookie": f"stytch_session={token}"}),
    )
    out = []
    for label, extra in attempts:
        headers = dict(_API_HEADERS)
        headers["x-pokedex"] = POKEDEX
        headers["Content-Type"] = "application/json"
        headers.update(extra)
        req = urllib.request.Request(
            f"{API}/api/auth/refresh", data=b"{}", headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
            has_token = '"access_token"' in body or '"accessToken"' in body
            out.append((label, resp.status, "access_token returned" if has_token
                        else body[:80]))
        except urllib.error.HTTPError as e:
            body = (e.read() or b"").decode("utf-8", "replace")
            out.append((label, e.code, body[:80]))
        except urllib.error.URLError as e:
            out.append((label, "-", str(e.reason)[:80]))
    return out


def doctor():
    """End-to-end check of the token transport. Returns True when sales
    tracking is actually working. Prints a report either way — the point is
    that every failure mode names itself instead of showing up as zero rows."""
    ok = True
    print("CrowdVolt sales — doctor\n" + "=" * 58)

    # 1. credentials
    token, fingerprint = credentials()
    if not token:
        print(f"[FAIL] no token. Set KARTIS_CV_TOKEN in {ENV_FILE} or the "
              "environment.\n       Get it by pasting scripts/cv_token_grab.js "
              "into the DevTools\n       console of a browser logged into "
              "crowdvolt.com.")
        return False
    print(f"[ ok ] token configured ({len(token)} chars), "
          f"fingerprint {'set' if fingerprint else 'absent (fine)'}")

    session = HttpSession(token, fingerprint)

    # 2. transport
    try:
        counts = session.api(f"{API}/api/buy_sell_history/seller_counts")
        print(f"[ ok ] api.crowdvolt.com accepted the token — no browser "
              f"involved\n       seller_counts: {json.dumps(counts)[:120]}")
    except CvSalesError as e:
        print(f"[FAIL] {str(e)[:240]}")
        return False

    # 3. sales + field-mapping coverage
    try:
        raw_by_tab = fetch_raw(session)
    except CvSalesError as e:
        print(f"[FAIL] sales endpoints: {str(e)[:200]}")
        return False
    raw_rows = [r for rows in raw_by_tab.values() for r in rows
                if isinstance(r, dict)]
    rows = fetch_sales(session)
    print(f"[ ok ] fetched {len(raw_rows)} raw rows -> {len(rows)} sales "
          + ", ".join(f"{t}={len(v)}" for t, v in raw_by_tab.items()))

    if not rows:
        print("[note] no sales on the account right now, so the field mapping "
              "could not be\n       checked. Re-run the doctor after the next "
              "sale.")
        return ok

    filled = {k: sum(1 for r in rows if r.get(k) not in (None, ""))
              for k in rows[0]}
    blank = [k for k in _CRITICAL if filled.get(k, 0) == 0]
    for k in _CRITICAL:
        n = filled.get(k, 0)
        print(f"       {k:<16} {n}/{len(rows)} rows populated"
              + ("   <-- EMPTY" if n == 0 else ""))
    if blank:
        ok = False
        seen_keys = sorted({k for r in raw_rows for k in r})
        unmapped = [k for k in seen_keys if k not in _ALL_ALIASES]
        print(f"[FAIL] these columns never populated: {', '.join(blank)}\n"
              f"       CrowdVolt most likely renamed a field. Keys present in "
              f"the API rows\n       that no alias claims: "
              f"{', '.join(unmapped) or '(none)'}\n"
              f"       Add the right name to the _F_* tuples at the top of "
              f"this module.")
    else:
        print("[ ok ] every critical column populated — field mapping is good")

    # 4. renewal
    print("\n--- session renewal (auth/refresh) ---")
    for label, status, detail in _refresh_probe(token):
        mark = "ok  " if str(status) == "200" else "    "
        print(f"[{mark}] {label:<14} -> {status}  {detail}")
    print("       A 200 with access_token means renewal can be automated;\n"
          "       otherwise re-paste the cookie when it lapses.")
    return ok


def _open_session(p):
    """Session on the CrowdVolt Chrome. Lives in crowdvolt_pricer so the
    Cloudflare-challenge handling and dead-tab reopen have exactly one
    implementation."""
    import crowdvolt_pricer
    return crowdvolt_pricer.open_session(p)


def fetch_via_cdp():
    """Standalone fetch — opens its own playwright + CV Chrome session. The
    hourly scrape does NOT use this (it reuses the context it already has);
    this is for the CLI and any caller without a live context."""
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        session = _open_session(p)
        try:
            return fetch_sales(session)
        finally:
            try:
                session.page.close()
            except Exception:
                pass


def _main():
    import sys
    args = sys.argv[1:]

    session = None if "--browser" in args else open_http_session()
    if session is not None:
        print(f"[kartis] transport: HTTP (token from {ENV_FILE.name}/env, no browser)")
        try:
            return _run(session, args)
        finally:
            session.close()

    if ("--check" in args or "--doctor" in args) and "--browser" not in args:
        raise SystemExit(
            f"no CrowdVolt token configured — set KARTIS_CV_TOKEN in {ENV_FILE}\n"
            "(see 'Minting the token' in this module's docstring)")

    print("[kartis] transport: CrowdVolt Chrome over CDP")
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        session = _open_session(p)
        try:
            return _run(session, args)
        finally:
            try:
                session.page.close()
            except Exception:
                pass


def _run(session, args):
    if "--doctor" in args:
        raise SystemExit(0 if doctor() else 1)
    if "--check" in args:
        print(json.dumps(check_token(session), indent=2, default=str))
        return
    if "--raw" in args:
        print(json.dumps(fetch_raw(session), indent=2, default=str))
        return
    rows = fetch_sales(session)
    if "--json" in args:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['sale_date_iso'] or '????-??-??'}  {r['status']:<10} "
                  f"#{r['order_id']:<12} {(r['event_name'] or '?')[:38]:<38} "
                  f"{r['qty'] or '?'}x {(r['ticket_type'] or '')[:14]:<14} "
                  f"${r['sale_price'] if r['sale_price'] is not None else '?'}")
        print(f"\n{len(rows)} sales")
    if "--write" in args:
        db.init()
        db.upsert_crowdvolt_sales(rows, datetime.now(timezone.utc).isoformat())
        print(f"wrote {len(rows)} rows to crowdvolt_sales")


if __name__ == "__main__":
    _main()
