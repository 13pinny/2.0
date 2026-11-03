"""Hands-off check: which bot-protected sites load from THIS machine with
nobody clicking anything. Run it on a candidate relay box (the home relay
laptop) to see what it can take over from the VPS / the main PC.

For each source the server's relay targets list (eventim, tao/Marquee,
shotgun venue pages, tixr) it loads up to --per real pages in the relay's
own Chrome (same profile + CDP :9333 as eventim_relay.py, so a pass here
means the scheduled relay passes too) and reports PASS or HELD. DICE is a
plain-HTTP check of api.dice.fm. It NEVER clicks a challenge and NEVER posts
anything to the server -- read-only apart from loading the pages.

Leave the Chrome window alone while it runs: touching a challenge would make
the result meaningless.

Run (repo root):  .venv\\Scripts\\python scripts\\relay_check.py
                  [--per N]      pages per source (default 2)
                  [--rounds N]   repeat the whole check N times (default 1)
                  [--show]       keep the relay Chrome on-screen to watch
                  [--fresh]      use a brand-new throwaway Chrome profile (no
                                 saved Cloudflare clearance) instead of the
                                 relay's own -- the honest first-run test
      or with URLs (no server needed):
                  .venv\\Scripts\\python scripts\\relay_check.py --fresh "<tao or eventim or tixr or shotgun url>" ...
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import eventim_relay as relay  # loads .env, CDP/profile settings, renderers
import cv_link_client


def _arg(name, default):
    if name in sys.argv:
        return int(sys.argv[sys.argv.index(name) + 1])
    return default


def _url_targets(urls):
    """Pasted URLs -> relay-style targets, by host."""
    out = []
    for u in urls:
        host = u.lower()
        if "taogroup.com" in host:
            out.append({"source": "tao", "event_key": u.split("/e/")[-1].split("/")[0], "url": u})
        elif "eventim" in host:
            out.append({"source": "eventim", "event_key": u.split("?")[0].rstrip("/").split("/")[-1], "url": u})
        elif "tixr.com" in host:
            import tixr_events
            eid = tixr_events.parse_url(u)
            out.append({"source": "tixr", "event_key": eid, "url": tixr_events.event_page_url(eid)})
        elif "shotgun.live" in host:
            out.append({"source": "shotgun", "event_key": u.rstrip("/").split("/")[-1], "url": u})
        else:
            print(f"!! skipping {u[:80]} - not a tao / eventim / tixr / shotgun link")
    return out


def _fresh_chrome():
    """A throwaway Chrome on its own port + empty profile, so no Cloudflare
    clearance from earlier runs can make a page pass. Returns the process."""
    import subprocess
    import tempfile
    import urllib.request
    port = 9444
    profile = tempfile.mkdtemp(prefix="relay-check-")
    proc = subprocess.Popen([relay.CHROME, f"--remote-debugging-port={port}",
                             f"--user-data-dir={profile}", "--no-first-run",
                             "--no-default-browser-check",
                             f"--window-position={relay.OFFSCREEN[0]},{relay.OFFSCREEN[1]}",
                             f"--window-size={relay.WINDOW_SIZE[0]},{relay.WINDOW_SIZE[1]}",
                             "about:blank"])
    relay.CDP = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            urllib.request.urlopen(relay.CDP + "/json/version", timeout=2)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("fresh Chrome didn't start")


def check_browser_sources(per, show, targets=None):
    if targets is None:
        status, raw = cv_link_client.call("/api/edm/eventim-relay/targets", timeout=150)
        if status != 200:
            print(f"!! couldn't read relay targets from the server: HTTP {status} {raw[:200]}")
            return []
        targets = json.loads(raw)["targets"]
    by_source = {}
    for t in targets:
        by_source.setdefault(t.get("source") or "eventim", []).append(t)

    if "--fresh" not in sys.argv:
        relay.ensure_chrome()
    from patchright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        ctx = p.chromium.connect_over_cdp(relay.CDP).contexts[0]
        pg = relay.relay_tab(ctx)
        if not show:
            relay.hide_window(ctx, pg)
        for source in ("eventim", "tixr", "tao", "shotgun"):
            for t in by_source.get(source, [])[:per]:
                t0 = time.time()
                try:
                    if source == "tixr":
                        body, title = relay.render_tixr(pg, t["url"], t["event_key"])
                        ok = bool(body) and '"sales"' in body
                    else:
                        body, title = relay.render(pg, t["url"], source, t.get("expand"))
                        ok = body is not None
                    verdict = "PASS" if ok else ("HELD" if relay._is_challenge(title) else "FAIL")
                    note = "" if ok else f"page title {title!r}"
                except Exception as e:
                    verdict, note = "ERROR", f"{type(e).__name__}: {str(e)[:120]}"
                    if pg.is_closed():
                        pg = relay.relay_tab(ctx)
                results.append((source, t["event_key"], verdict, time.time() - t0, note))
                print(f"  {verdict:5} {source:8} {t['event_key'][:40]:40} {time.time() - t0:5.1f}s  {note}",
                      flush=True)
        try:
            pg.goto("about:blank")
        except Exception:
            pass
    return results


def check_dice(per):
    import dice
    status, raw = cv_link_client.call("/api/dice/relay/targets", timeout=150)
    if status != 200:
        print(f"!! couldn't read DICE targets: HTTP {status} {raw[:200]}")
        return []
    results = []
    for code in json.loads(raw)["targets"][:per]:
        t0 = time.time()
        try:
            body = json.loads(dice._http_get(f"{dice.API_BASE}/events/{code}/ticket_types"))
            ok = isinstance(body, (dict, list))
            verdict, note = ("PASS" if ok else "FAIL"), ""
        except Exception as e:
            verdict, note = "FAIL", f"{type(e).__name__}: {str(e)[:120]}"
        results.append(("dice", code, verdict, time.time() - t0, note))
        print(f"  {verdict:5} {'dice':8} {code[:40]:40} {time.time() - t0:5.1f}s  {note}", flush=True)
    return results


def main():
    per, rounds, show = _arg("--per", 2), _arg("--rounds", 1), "--show" in sys.argv
    flag_values = {sys.argv[i + 1] for i, a in enumerate(sys.argv[:-1]) if a in ("--per", "--rounds")}
    urls = [a for a in sys.argv[1:] if not a.startswith("--") and a not in flag_values]
    targets = _url_targets(urls) if urls else None
    proc = _fresh_chrome() if "--fresh" in sys.argv else None
    if proc:
        print("(fresh Chrome profile: nothing saved from earlier runs)")
    allres = []
    try:
        for r in range(1, rounds + 1):
            print(f"\n== round {r}/{rounds}  (hands off the Chrome window)")
            allres += check_browser_sources(len(urls) if urls else per, show, targets)
            if not urls:
                allres += check_dice(per)
            if r < rounds:
                time.sleep(30)
    finally:
        if proc:
            proc.kill()

    print("\n== summary (no clicks were made)")
    for source in ("eventim", "tixr", "tao", "shotgun", "dice"):
        rows = [x for x in allres if x[0] == source]
        if not rows:
            print(f"  {source:8} -  (nothing tracked)")
            continue
        passed = sum(1 for x in rows if x[2] == "PASS")
        verdict = "passes on its own" if passed == len(rows) else \
            "BLOCKED" if passed == 0 else "mixed"
        print(f"  {source:8} {passed}/{len(rows)} passed  -> {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
