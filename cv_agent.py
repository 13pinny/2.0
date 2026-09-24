"""Desktop agent: answers the CrowdVolt buttons on kartis.homes.

Run this on the machine that has the CV Chrome. It polls the server for a
parked job and does it here, where a signed-in browser exists:

    relink - harvest cv_refresh_token out of the CDP Chrome and push it up
    sales  - read CrowdVolt's sell_delivered feed and push the rows up

    .venv/Scripts/python cv_agent.py            # loop (start_cv_agent.bat)
    .venv/Scripts/python cv_agent.py --once     # one poll, then exit
    .venv/Scripts/python cv_agent.py --now      # relink immediately, no button
    .venv/Scripts/python cv_agent.py --sales    # pull sales now, no button

Why a poller rather than the server calling us: kartis.homes cannot open a
connection into this LAN, so the desktop has to ask. The poll doubles as a
heartbeat - the button greys out and says so when no agent has checked in
recently, instead of spinning on a request nothing will ever answer.

This process holds no CrowdVolt credential of its own. It reads a cookie out
of Chrome and forwards it; nothing is written to disk here.

Config in .env: KARTIS_CVAUTH_SECRET, KARTIS_BASE_URL, KARTIS_WEB_USER,
KARTIS_WEB_PASS, and optionally KARTIS_CVAUTH_AGENT_INTERVAL (seconds, 30).
"""
import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import os

import cv_link_client as client

INTERVAL = int(os.environ.get("KARTIS_CVAUTH_AGENT_INTERVAL") or 30)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def report(state, error=None, result=None):
    """Tell the server how it went. Best-effort: a job that worked must not
    be reported as failed just because this call did."""
    try:
        client.call("/api/cvauth/job/result",
                    {"state": state, "error": error, "result": result})
    except Exception as e:
        log(f"  (could not report {state}: {type(e).__name__}: {e})")


def do_relink(with_cf_clearance=False):
    """Harvest and push. Returns True on success."""
    report("running")
    try:
        cookies = client.harvest(with_cf_clearance=with_cf_clearance)
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"  harvest failed: {msg}")
        report("error", msg)
        return False
    log("  harvested: " + ", ".join(
        f"{k}={client.mask(v)}" for k, v in sorted(cookies.items())))
    try:
        imported = client.push(cookies)
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"  push failed: {msg}")
        report("error", msg)
        return False
    log(f"  relinked OK - {imported.get('days_remaining')}d estimated remaining")
    return True     # /api/cvauth/import marks the job done itself


def do_sales():
    """Pull delivered sales in this machine's browser and push them up.

    Exists because the VPS records nothing: its cv_refresh_token dies and
    scraper._fetch_crowdvolt_sales_api is cache-only, so it cannot
    re-harvest; the UI fallback finds nothing because CrowdVolt retired the
    Completed table, and the tick logs crowdvolt_sales: 0 with no error. A
    signed-in browser reads the same feed perfectly, so it reads it here.
    """
    report("running")
    try:
        rows = client.fetch_sales()
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"  fetch failed: {msg}")
        report("error", msg)
        return False
    log(f"  fetched {len(rows)} delivered sale(s) from CrowdVolt")
    try:
        summary = client.push_sales(rows)
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"  push failed: {msg}")
        report("error", msg)
        return False
    log(f"  imported {summary['mapped']} row(s) - {summary['inserted']} new, "
        f"{summary['updated']} updated, {summary['unmappable']} unmappable "
        f"({summary['crowdvolt_sales_total']} on the box now)")
    # /api/cvsales/import closes the job on its own, but a chunked push closes
    # it on the FIRST chunk and would leave the button reporting that chunk's
    # count as the whole pull. Report the merged total, which lands last.
    report("done", result={k: summary[k] for k in
                           ("mapped", "inserted", "updated",
                            "crowdvolt_sales_total")})
    return True


JOBS = {"relink": lambda cf: do_relink(cf), "sales": lambda cf: do_sales()}


def poll_once(with_cf_clearance=False):
    status, raw = client.call("/api/cvauth/job")
    if status == 403:
        raise client.RelinkError(
            "server rejected the secret - KARTIS_CVAUTH_SECRET must match on "
            "both machines.")
    if status == 503:
        raise client.RelinkError(
            "server has no KARTIS_CVAUTH_SECRET set - the cvauth routes are "
            "disabled there.")
    if status != 200:
        log(f"poll -> {status}: {raw[:160]}")
        return False
    job = json.loads(raw)
    if job.get("state") != "pending":
        return False
    kind = job.get("kind") or "relink"
    handler = JOBS.get(kind)
    if handler is None:
        # A server newer than this agent. Say so on the job rather than spin
        # on a pending request forever.
        msg = f"this agent does not know the job kind {kind!r} - update it"
        log(f"!! {msg}")
        report("error", msg)
        return False
    log(f"{kind} requested at {job.get('requested_at')} - starting")
    return handler(with_cf_clearance)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    ap.add_argument("--now", action="store_true",
                    help="relink immediately, ignoring the queue")
    ap.add_argument("--sales", action="store_true",
                    help="pull CrowdVolt sales immediately, ignoring the queue")
    ap.add_argument("--interval", type=int, default=INTERVAL)
    ap.add_argument("--with-cf-clearance", action="store_true",
                    help="also ship cf_clearance (IP-bound; usually useless)")
    args = ap.parse_args()

    if args.sales:
        return 0 if do_sales() else 1
    if args.now:
        return 0 if do_relink(args.with_cf_clearance) else 1
    if args.once:
        try:
            poll_once(args.with_cf_clearance)
        except client.RelinkError as e:
            log(f"!! {e}")
            return 1
        return 0

    log(f"cv_agent polling {client.DEFAULT_BASE_URL} every {args.interval}s")
    log(f"CDP Chrome: {client.DEFAULT_CDP}")
    fails = 0
    while True:
        try:
            poll_once(args.with_cf_clearance)
            fails = 0
        except client.RelinkError as e:
            # Config problems repeat every tick; say it once, keep running so
            # fixing .env on the server does not need a restart here.
            if fails == 0:
                log(f"!! {e}")
            fails += 1
        except Exception as e:
            if fails == 0:
                log(f"!! {type(e).__name__}: {e}")
            fails += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
