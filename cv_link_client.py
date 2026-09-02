"""Desktop-side transport for the CrowdVolt relink.

Shared by the two senders so they cannot drift:
  * scripts/cv_relink.py - one-shot, run by hand
  * cv_agent.py          - long-running, answers the /inventory button

Why a desktop sender exists at all: cv_auth reaches CrowdVolt over plain HTTP
from anywhere, but cv_refresh_token is HttpOnly and only readable out of a
signed-in Chrome's cookie jar over CDP. The VPS has no such Chrome, cannot
reach into this machine's LAN, and a tab on kartis.homes cannot touch
localhost:9222 either (https->http is mixed content, and CDP rejects any
request whose Host header is not localhost). So every relink starts here.

The Chrome that matters is the DEDICATED one login.py launches on :9222 with
the sticky user_data/ profile - not your everyday Chrome. Cookies live per
profile, so signing in anywhere else leaves the harvest empty.
"""
import base64
import json
import os
import urllib.error
import urllib.request

import cv_auth

DEFAULT_CDP = os.environ.get("KARTIS_CV_CDP_URL") or "http://localhost:9222"
DEFAULT_BASE_URL = os.environ.get("KARTIS_BASE_URL") or "https://kartis.homes"


class RelinkError(Exception):
    """Something a human has to fix; the message says what."""


def mask(value):
    """Never print a credential. Length plus head/tail is enough to eyeball
    that two machines hold the same cookie."""
    if not value:
        return "(empty)"
    if len(value) <= 12:
        return f"<{len(value)} chars>"
    return f"{value[:4]}...{value[-4:]} <{len(value)} chars>"


def secret():
    s = (os.environ.get("KARTIS_CVAUTH_SECRET") or "").strip()
    if not s:
        raise RelinkError(
            "KARTIS_CVAUTH_SECRET is not set in .env - it must match the "
            "server's value. Generate one with:\n"
            '  python -c "import secrets;print(secrets.token_urlsafe(32))"')
    return s


def harvest(cdp_url=None, with_cf_clearance=False):
    """Read the CrowdVolt cookies out of the CDP Chrome.

    cf_clearance is dropped by default: it is bound to the IP and User-Agent
    that solved the Cloudflare challenge, so this desktop's copy is worthless
    from the VPS's address. cv_auth only treats cv_refresh_token as the
    credential in any case.
    """
    from patchright.sync_api import sync_playwright
    cdp_url = cdp_url or DEFAULT_CDP
    with sync_playwright() as pw:
        try:
            cookies = cv_auth.harvest_cookies(pw, cdp_url)
        except cv_auth.CvAuthError:
            raise
        except Exception as e:
            if "ECONNREFUSED" in str(e) or "connect" in str(e).lower():
                raise RelinkError(
                    f"could not reach Chrome over CDP at {cdp_url}.\n"
                    "Start it with start_chrome.bat (or python login.py), sign "
                    "in at crowdvolt.com in THAT window, then retry.")
            raise
    if not with_cf_clearance:
        cookies = {k: v for k, v in cookies.items() if k != "cf_clearance"}
    return cookies


def call(path, payload=None, method=None, base_url=None, timeout=45):
    """Authenticated call to the kartis server.

    Two gates stack: Caddy basic auth on the host, and KARTIS_CVAUTH_SECRET
    on the cvauth routes themselves. The secret rides in a header so it stays
    out of query strings and access logs.
    """
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    method = method or ("POST" if payload is not None else "GET")
    req = urllib.request.Request(base_url + path, method=method)
    req.add_header("X-Kartis-Secret", secret())
    user = os.environ.get("KARTIS_WEB_USER") or ""
    if user:
        pw = os.environ.get("KARTIS_WEB_PASS") or ""
        blob = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req.add_header("Authorization", "Basic " + blob)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise RelinkError(f"could not reach {base_url}: {e.reason}")


def push(cookies, base_url=None, verify=True):
    """Send harvested cookies to the server, then prove CrowdVolt still
    honours them. Returns the import status dict.

    The verify step matters: /import can only check the payload's SHAPE, so
    without it a stale cookie is stored happily and only fails at the next
    scheduled tick, far from anyone watching.
    """
    status, raw = call("/api/cvauth/import",
                       {"secret": secret(), "cookies": cookies},
                       base_url=base_url)
    if status == 401:
        raise RelinkError("401 from the edge - check KARTIS_WEB_USER / "
                          "KARTIS_WEB_PASS against the Caddy basicauth block.")
    if status != 200:
        raise RelinkError(f"import failed ({status}): {raw[:300]}")
    imported = json.loads(raw)
    if not verify:
        return imported
    status, raw = call("/api/cvauth/verify", {}, base_url=base_url)
    if status != 200:
        raise RelinkError(
            f"cookie stored but CrowdVolt rejected it ({status}): {raw[:200]}\n"
            "Sign in again at crowdvolt.com in the CV Chrome and retry.")
    return imported
