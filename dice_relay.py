"""Desktop relay for DICE prices (DESKTOP only).

api.dice.fm answers 403 to the VPS's (Hetzner) IP whatever the headers,
while a home connection gets through. So this side does the fetch: ask
kartis.homes which DICE events are tracked (drop watchers + the /dice
tracker), read each one's ticket_types from api.dice.fm, and POST the raw
JSON to /api/dice/relay. On the server, dice._fetch_ticket_types serves
those copies (up to KARTIS_DICE_RELAY_MAX_AGE_SECONDS old) whenever the API
refuses it, so watchers, the tier history and /dice work as before.
Plain HTTP -- no Chrome.

Auth is the same pair cv_link_client uses: Caddy basic auth
(KARTIS_WEB_USER / KARTIS_WEB_PASS) + KARTIS_CVAUTH_SECRET, all from .env.

Run:  .venv\\Scripts\\python dice_relay.py            one pass, prints results
      .venv\\Scripts\\python dice_relay.py --loop     forever, every
                                                    KARTIS_DICE_RELAY_SECONDS (60)
      start_dice_relay.bat                          the loop, windowless
                                                    (logs to logs\\dice_relay.log)
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import cv_link_client
import dice

INTERVAL = int(os.environ.get("KARTIS_DICE_RELAY_SECONDS") or 60)
LOG = ROOT / "logs" / "dice_relay.log"


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def one_pass():
    status, raw = cv_link_client.call("/api/dice/relay/targets")
    if status != 200:
        log(f"targets: HTTP {status} {raw[:200]}")
        return False
    targets = json.loads(raw)["targets"]
    payloads, failed = {}, 0
    for code in targets:
        try:
            payloads[code] = json.loads(dice._http_get(f"{dice.API_BASE}/events/{code}/ticket_types"))
        except Exception as e:
            failed += 1
            log(f"{code}: {type(e).__name__}: {e}")
        time.sleep(0.3)
    if payloads:
        status, raw = cv_link_client.call("/api/dice/relay", {"payloads": payloads})
        log(f"sent {len(payloads)}/{len(targets)} ({failed} failed): HTTP {status} {raw[:200]}")
    return True


def main():
    if sys.stdout is None:  # pythonw: no console
        LOG.parent.mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8")
    if "--loop" not in sys.argv:
        return 0 if one_pass() else 1
    while True:
        try:
            one_pass()
        except Exception as e:
            log(f"pass failed: {type(e).__name__}: {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
