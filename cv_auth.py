"""CrowdVolt auth + HTTP transport, no browser required.

Replaces the in-page fetch() rig crowdvolt_pricer.py used to need. That rig
existed because Cloudflare 503'd out-of-browser requests (probed
2026-07-22); as of 2026-08-23 it does not - plain urllib from the VPS gets
200 from api.crowdvolt.com and what.crowdvolt.com. CrowdVolt shipping
iPhone/Android apps is the likely reason the API opened to non-browser
clients.

The auth flow CrowdVolt uses now:

  * `cv_refresh_token` - HttpOnly, host-only on www.crowdvolt.com, ~30 day
    expiry. The long-lived credential. document.cookie cannot read it, so
    it is harvested once over CDP from the logged-in Chrome and cached.
  * POST www.crowdvolt.com/api/auth/refresh with Content-Type json,
    X-Requested-With: XMLHttpRequest and body `{}` exchanges that cookie
    for {access_token: <JWT, ~369 chars>, user}. Omitting X-Requested-With
    returns 403 {"error": "missing required header"}.
  * That JWT goes to api.crowdvolt.com / what.crowdvolt.com as a Bearer.

There is no `stytch_session` cookie any more (Stytch is still the backend -
user ids are `user-live-*` - but the browser session is wrapped in
CrowdVolt's own cookies). Anything still reading `document.cookie` for it
is dead code.

Sign-in therefore becomes a ~monthly chore: log in once in the CV Chrome,
and everything here runs on the harvested cookie until it expires.

The cached cookie IS a live credential - cv_session.json is written 0600
and is gitignored. Nothing in this module logs cookie or token values.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

REFRESH_URL = "https://www.crowdvolt.com/api/auth/refresh"
STATE_PATH = Path(os.environ.get("KARTIS_CV_SESSION_FILE",
                                 Path(__file__).parent / "cv_session.json"))

# Sent on every call. An okhttp/* UA gets 403 from what.crowdvolt.com; a
# browser-shaped one passes, so keep this browser-shaped.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
POKEDEX = os.environ.get("KARTIS_CV_POKEDEX", "0376")

# Cookies worth carrying. cv_refresh_token is the credential; cf_clearance
# costs nothing to send and helps if Cloudflare tightens up again.
COOKIE_NAMES = ("cv_refresh_token", "cf_clearance")

TOKEN_SKEW_SECONDS = 60   # re-mint this long before the JWT's own exp


class CvAuthError(Exception):
    """Auth could not be established - the message says what a human must do."""


def _jwt_exp(token):
    """Unix exp out of a JWT payload, or None. Not verified - we only need
    to know when to re-mint, and the server is the real authority."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def _read_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass          # windows
    os.replace(tmp, STATE_PATH)


def harvest_cookies(playwright, cdp_url):
    """Read the auth cookies out of the logged-in Chrome over CDP. Only
    needed when the cache is empty or the refresh cookie has expired -
    every other call in this module is plain HTTP.

    Deliberately a plain connect rather than scraper._connect_over_cdp: that
    one sweeps stray ticketing tabs, and a human-cleared CrowdVolt tab is
    load-bearing for the browser-based pricer path. We only read the cookie
    jar - no page is opened or navigated - so the sweep's crash protection
    buys nothing here and the risk of closing that tab is real.
    """
    browser = playwright.chromium.connect_over_cdp(cdp_url)
    jar = browser.contexts[0].cookies()
    found = {c["name"]: c["value"] for c in jar
             if c["name"] in COOKIE_NAMES and "crowdvolt" in c.get("domain", "")}
    if "cv_refresh_token" not in found:
        raise CvAuthError(
            "no cv_refresh_token in the CrowdVolt Chrome - sign in at "
            "crowdvolt.com in that browser (noVNC), then retry")
    return found


class CvAuth:
    """Mints and caches access tokens, and makes authenticated calls.

    `playwright` is optional and used only to re-harvest the refresh cookie
    when the cached one is gone or expired; without it an expired cookie
    raises with instructions instead of silently failing.
    """

    def __init__(self, playwright=None, cdp_url=None):
        self._pw = playwright
        self._cdp = cdp_url
        self._cookies = None
        self._token = None
        self._token_exp = 0

    # -- cookies --

    def _load_cookies(self, force_harvest=False):
        if self._cookies and not force_harvest:
            return self._cookies
        if not force_harvest:
            cached = _read_state().get("cookies") or {}
            if cached.get("cv_refresh_token"):
                self._cookies = cached
                return self._cookies
        if self._pw is None:
            raise CvAuthError(
                "no cached CrowdVolt session and no browser to harvest one "
                f"from - sign in at crowdvolt.com in the CV Chrome ({self._cdp}) "
                "and run a tick with the browser reachable")
        self._cookies = harvest_cookies(self._pw, self._cdp)
        state = _read_state()
        state["cookies"] = self._cookies
        state["harvested_at"] = int(time.time())
        _write_state(state)
        return self._cookies

    def _cookie_header(self):
        return "; ".join(f"{k}={v}" for k, v in self._load_cookies().items())

    # -- token --

    def token(self, force=False):
        now = time.time()
        if self._token and not force and now < self._token_exp - TOKEN_SKEW_SECONDS:
            return self._token
        status, body = self._raw(REFRESH_URL, method="POST", data="{}",
                                 cookie=self._cookie_header(), token=None)
        if status in (401, 403):
            # Cached refresh cookie is dead - one re-harvest, then give up.
            self._load_cookies(force_harvest=True)
            status, body = self._raw(REFRESH_URL, method="POST", data="{}",
                                     cookie=self._cookie_header(), token=None)
        if status != 200:
            raise CvAuthError(
                f"auth/refresh -> {status}: {body[:200]} - sign in at "
                "crowdvolt.com in the CV Chrome (noVNC) and retry")
        try:
            token = json.loads(body)["access_token"]
        except (ValueError, KeyError) as e:
            raise CvAuthError(f"auth/refresh returned no access_token: {e}")
        self._token = token
        exp = _jwt_exp(token)
        # No exp claim: assume a short life so we re-mint often rather than
        # ride a dead token through a whole tick.
        self._token_exp = exp if exp else now + 300
        return token

    # -- transport --

    def _raw(self, url, method="GET", data=None, cookie=None, token=None):
        req = urllib.request.Request(url, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Origin", "https://www.crowdvolt.com")
        req.add_header("Referer", "https://www.crowdvolt.com/")
        req.add_header("x-pokedex", POKEDEX)
        if cookie:
            req.add_header("Cookie", cookie)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Requested-With", "XMLHttpRequest")
            req.data = data.encode()
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def call(self, url, method="GET", body=None):
        """Authenticated call returning (status, raw_body). Re-mints once on
        a 401/403 so a token expiring mid-tick costs a retry, not the tick."""
        payload = None if body is None else json.dumps(body)
        status, raw = self._raw(url, method=method, data=payload,
                                cookie=self._cookie_header(),
                                token=self.token())
        if status in (401, 403):
            status, raw = self._raw(url, method=method, data=payload,
                                    cookie=self._cookie_header(),
                                    token=self.token(force=True))
        return status, raw
