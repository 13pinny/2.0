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

# Separate logins, one state file each. "main" is the selling account (sales
# tracking). "watch" is a throwaway account that only READS order books for
# b2b_snipe.py - the user does not want their selling account polling
# CrowdVolt every minute, so the two must never share a cookie.
PROFILES = {
    "main": STATE_PATH,
    "watch": Path(os.environ.get("KARTIS_CV_WATCH_SESSION_FILE",
                                 Path(__file__).parent / "cv_session_watch.json")),
}


def _path(profile):
    try:
        return PROFILES[profile or "main"]
    except KeyError:
        raise CvAuthError(f"unknown CrowdVolt profile {profile!r} "
                          f"(expected one of {sorted(PROFILES)})")

# Sent on every call. An okhttp/* UA gets 403 from what.crowdvolt.com; a
# browser-shaped one passes, so keep this browser-shaped.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")
POKEDEX = os.environ.get("KARTIS_CV_POKEDEX", "0376")

# Cookies worth carrying. cv_refresh_token is the credential; cf_clearance
# costs nothing to send and helps if Cloudflare tightens up again.
COOKIE_NAMES = ("cv_refresh_token", "cf_clearance")

TOKEN_SKEW_SECONDS = 60   # re-mint this long before the JWT's own exp

# cv_refresh_token's observed lifetime. Only used to tell the UI how long
# is left before the next relink - the server is the real authority.
REFRESH_COOKIE_DAYS = 30


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


def _read_state(profile="main"):
    try:
        with open(_path(profile), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_state(state, profile="main"):
    path = _path(profile)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass          # windows
    os.replace(tmp, path)



def import_cookies(cookies, profile="main"):
    """Install cookies harvested on another machine into this box's cache.

    The VPS runs no logged-in Chrome and cv_refresh_token is HttpOnly, so a
    fresh session cannot be obtained server-side at all - it has to be pushed
    in from a desktop that does have one. scripts/cv_relink.py is the sender;
    this is the receiver.

    Only COOKIE_NAMES survive, and cv_refresh_token is mandatory: a state file
    carrying, say, cf_clearance alone would make _load_cookies believe a
    session exists and turn a clear "sign in" error into a puzzling 401 on
    every call.
    """
    clean = {k: v for k, v in (cookies or {}).items() if k in COOKIE_NAMES and v}
    if not clean.get("cv_refresh_token"):
        raise CvAuthError(
            "payload carries no cv_refresh_token - harvest it from a Chrome "
            "signed in at crowdvolt.com")
    # A new cookie may be a different account - drop the old identity so the
    # same-account guard re-checks against whoever this cookie belongs to.
    state = {"cookies": clean, "harvested_at": int(time.time())}
    _write_state(state, profile)
    return session_status(profile)


def session_status(profile="main"):
    """Whether a CrowdVolt session is cached and how much life it has left.

    Feeds a web response, so it returns cookie NAMES and ages only - never a
    value. cv_session.json is a live credential.
    """
    state = _read_state(profile)
    cookies = state.get("cookies") or {}
    harvested_at = state.get("harvested_at")
    age_days = None
    if harvested_at:
        age_days = round((time.time() - harvested_at) / 86400.0, 1)
    return {
        "linked": bool(cookies.get("cv_refresh_token")),
        "harvested_at": harvested_at,
        "age_days": age_days,
        # Surface the remainder rather than make the reader subtract. This is
        # an estimate off REFRESH_COOKIE_DAYS, not something CrowdVolt told us,
        # so <= 0 means "certainly relink", not "expired at this instant".
        "days_remaining": (None if age_days is None
                           else round(REFRESH_COOKIE_DAYS - age_days, 1)),
        "cookie_names": sorted(cookies),
        "profile": profile,
        # Which CrowdVolt user the cookie belongs to (recorded at the first
        # token mint). An id, not an email - safe for a web response.
        "user_id": state.get("user_id"),
    }

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
            "crowdvolt.com in THAT window (the :9222 profile, not your "
            "everyday Chrome), then retry")
    return found


class CvAuth:
    """Mints and caches access tokens, and makes authenticated calls.

    `playwright` is optional and used only to re-harvest the refresh cookie
    when the cached one is gone or expired; without it an expired cookie
    raises with instructions instead of silently failing.
    """

    def __init__(self, playwright=None, cdp_url=None, profile="main"):
        self._profile = profile or "main"
        _path(self._profile)                 # unknown profile fails here
        self._pw = playwright if self._profile == "main" else None
        self._cdp = cdp_url
        self._cookies = None
        self._token = None
        self._token_exp = 0

    # -- cookies --

    def _load_cookies(self, force_harvest=False):
        if self._cookies and not force_harvest:
            return self._cookies
        if not force_harvest:
            cached = _read_state(self._profile).get("cookies") or {}
            if cached.get("cv_refresh_token"):
                self._cookies = cached
                return self._cookies
        if self._profile != "main":
            raise CvAuthError(
                f"no CrowdVolt session for the '{self._profile}' account - "
                f"link it with scripts/cv_relink.py --profile {self._profile} "
                "--cookie")
        if self._pw is None:
            raise CvAuthError(
                "no cached CrowdVolt session and no browser to harvest one "
                "from - this box has no Chrome. Press Re-link CrowdVolt on "
                "/inventory, or run scripts/cv_relink.py on the desktop")
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
                f"auth/refresh -> {status}: {body[:200]} - the cached session "
                "is dead. Press Re-link CrowdVolt on /inventory, or run "
                "scripts/cv_relink.py on the desktop with the CV Chrome "
                "signed in")
        try:
            parsed = json.loads(body)
            token = parsed["access_token"]
        except (ValueError, KeyError) as e:
            raise CvAuthError(f"auth/refresh returned no access_token: {e}")
        self._remember_user(parsed.get("user"))
        self._token = token
        exp = _jwt_exp(token)
        # No exp claim: assume a short life so we re-mint often rather than
        # ride a dead token through a whole tick.
        self._token_exp = exp if exp else now + 300
        return token

    def _remember_user(self, user):
        """Record which CrowdVolt user this profile's cookie belongs to, and
        refuse to let the watch profile run on the selling account."""
        uid = None
        if isinstance(user, dict):
            for k in ("uqid", "user_uqid", "id", "user_id"):
                if user.get(k):
                    uid = str(user[k])
                    break
        if not uid:
            return
        state = _read_state(self._profile)
        if state.get("user_id") != uid:
            state["user_id"] = uid
            _write_state(state, self._profile)
        if self._profile != "main":
            main_uid = _read_state("main").get("user_id")
            if main_uid and main_uid == uid:
                self._token = None
                raise CvAuthError(
                    f"the '{self._profile}' CrowdVolt cookie belongs to your "
                    "MAIN selling account - sign in with the second account "
                    "and relink it")

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
