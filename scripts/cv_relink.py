r"""One-shot CrowdVolt relink: harvest the refresh cookie here, push it up.

Usage (from the repo root, on the DESKTOP with the CV Chrome signed in):
    .venv\Scripts\python scripts\cv_relink.py
    .venv\Scripts\python scripts\cv_relink.py --dry-run     # show, send nothing
    .venv\Scripts\python scripts\cv_relink.py --local       # cache here instead
    .venv\Scripts\python scripts\cv_relink.py --with-cf-clearance
    .venv\Scripts\python scripts\cv_relink.py --cookie      # paste, no CDP

This is the manual counterpart to cv_agent.py, which does the same thing on
demand when you press "Re-link CrowdVolt" on /inventory. Use this one when
the agent is not running, or to debug the handoff. Both share
cv_link_client.py, so they cannot drift.

Two ways to supply the cookie:

  * --cdp (default): harvest from the DEDICATED Chrome login.py launches on
    :9222 with the user_data/ profile. Sign in there once; it persists.
  * --cookie: paste the value out of ANY Chrome's DevTools. Use this to reuse
    a session you already have in your everyday browser. Chrome >= 136
    refuses --remote-debugging-port on the default profile (a deliberate
    anti-cookie-theft change), so an everyday Chrome cannot be attached to
    over CDP at all - but DevTools still displays the value, HttpOnly and all:
    Application > Cookies > https://www.crowdvolt.com > cv_refresh_token.

Config in .env: KARTIS_CVAUTH_SECRET (must match the server), KARTIS_BASE_URL,
KARTIS_WEB_USER, KARTIS_WEB_PASS (the Caddy basic-auth pair).
"""
import argparse
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import cv_auth
import cv_link_client as client


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default=client.DEFAULT_CDP)
    ap.add_argument("--base-url", default=client.DEFAULT_BASE_URL)
    ap.add_argument("--dry-run", action="store_true",
                    help="harvest and report, send nothing")
    ap.add_argument("--local", action="store_true",
                    help="write this machine's cv_session.json instead of pushing")
    ap.add_argument("--with-cf-clearance", action="store_true",
                    help="also ship cf_clearance (IP-bound; see cv_link_client)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-import live auth/refresh check")
    ap.add_argument("--cookie", nargs="?", const="", default=None,
                    metavar="VALUE",
                    help="paste cv_refresh_token instead of harvesting over "
                         "CDP - use this to reuse the session in your EVERYDAY "
                         "Chrome. Omit the value to be prompted (keeps the "
                         "credential out of shell history).")
    args = ap.parse_args()

    if args.cookie is not None:
        # Chrome >=136 refuses --remote-debugging-port on the default profile,
        # so an everyday Chrome cannot be harvested over CDP at all. Its
        # DevTools still SHOWS the value though (Application > Cookies >
        # https://www.crowdvolt.com > cv_refresh_token), so let it be pasted.
        value = args.cookie
        if not value:
            import getpass
            print("Paste cv_refresh_token (DevTools > Application > Cookies > "
                  "https://www.crowdvolt.com):")
            value = getpass.getpass("  value (hidden): ").strip()
        value = value.strip().strip('"').strip("'")
        if not value:
            raise SystemExit("no cookie value given")
        if value.startswith("cv_refresh_token="):
            value = value.split("=", 1)[1]
        cookies = {"cv_refresh_token": value}
        print(f"using pasted cookie: {client.mask(value)}")
    else:
        print(f"harvesting from Chrome at {args.cdp} ...")
        try:
            cookies = client.harvest(args.cdp, args.with_cf_clearance)
        except (client.RelinkError, cv_auth.CvAuthError) as e:
            raise SystemExit(str(e))
    for name, value in sorted(cookies.items()):
        print(f"  {name}: {client.mask(value)}")

    if args.dry_run:
        print("\n--dry-run: nothing sent.")
        return 0

    if args.local:
        status = cv_auth.import_cookies(cookies)
        print(f"\nwrote {cv_auth.STATE_PATH}")
        print(json.dumps(status, indent=2))
        return 0

    print(f"\npushing to {args.base_url} ...")
    try:
        imported = client.push(cookies, base_url=args.base_url,
                               verify=not args.no_verify)
    except client.RelinkError as e:
        raise SystemExit(str(e))
    print("imported: " + json.dumps(imported, indent=2))
    if not args.no_verify:
        print("verified: the server minted an access token with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
