"""Self-repair for the VPS's noVNC login display (Xvfb :99 + Chrome + x11vnc).

A black noVNC screen means the bare X root is showing: no Chrome window is
mapped on the display. On the 4 GB VPS the usual cause is memory pressure —
the OOM killer takes Chrome (or, worse, Xvfb, which takes every window with
it) and noVNC keeps connecting to an empty display. Two quieter causes look
identical: a Chrome window someone minimized (openbox has no taskbar, so an
iconified window can never be brought back from inside noVNC), and a
display service that's down while the others still run.

`repair()` walks the stack bottom-up and fixes what it finds:

  1. any display unit (xvfb / wm / vnc / novnc) not active -> restart it
  2. a Chrome whose CDP port doesn't answer -> restart its unit
  3. Chrome windows that exist but are hidden -> un-minimize + raise them
  4. a live Chrome with no window at all -> open one through CDP

Restarts go through `sudo -n systemctl restart <unit>`; deploy/fix-vnc.sh
installs the NOPASSWD rule for exactly these units. Linux/VPS only — every
step degrades to a note elsewhere, nothing raises.
"""
import os
import shutil
import subprocess
import time
import urllib.request

DISPLAY = os.getenv("KARTIS_DISPLAY", ":99")

# Bottom-up order matters: x11vnc needs Xvfb, websockify needs x11vnc.
DISPLAY_UNITS = ["kartis-xvfb", "kartis-wm", "kartis-vnc", "kartis-novnc"]


def _chromes():
    """(unit, cdp_url) for each Chrome that renders on the display."""
    out = [(os.getenv("KARTIS_CHROME_SERVICE", "kartis-chrome"),
            os.getenv("KARTIS_CDP_URL", "http://localhost:9222"))]
    cv = os.getenv("KARTIS_CDP_URL_CROWDVOLT", "").strip()
    if cv:
        out.append((os.getenv("KARTIS_CHROME_CV_SERVICE", "kartis-chrome-cv"), cv))
    return out


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "DISPLAY": DISPLAY})
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _unit_state(unit):
    if not shutil.which("systemctl"):
        return "unknown"
    return _run(["systemctl", "is-active", unit], timeout=10)[1] or "unknown"


def _unit_enabled(unit):
    if not shutil.which("systemctl"):
        return False
    return _run(["systemctl", "is-enabled", unit], timeout=10)[1] == "enabled"


def _restart(unit):
    rc, out, err = _run(["sudo", "-n", "systemctl", "restart", unit], timeout=60)
    return rc == 0, (err or out or f"exit {rc}")


def _cdp_ok(cdp):
    try:
        urllib.request.urlopen(f"{cdp.rstrip('/')}/json/version", timeout=4).close()
        return True
    except Exception:
        return False


def _chrome_windows(visible_only):
    """X window ids of Chrome top-level windows on the display."""
    if not shutil.which("xdotool"):
        return None
    cmd = ["xdotool", "search"]
    if visible_only:
        cmd.append("--onlyvisible")
    # Chrome / Chromium / google-chrome all carry one of these WM classes.
    ids = set()
    for cls in ("chromium", "google-chrome", "Chromium", "Google-chrome"):
        rc, out, _ = _run(cmd + ["--class", cls], timeout=10)
        if rc == 0 and out:
            ids.update(out.split())
    return sorted(ids)


def mem_info():
    """MB totals from /proc/meminfo, or {} off Linux."""
    try:
        vals = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0]) // 1024
        return {"total_mb": vals.get("MemTotal"),
                "available_mb": vals.get("MemAvailable"),
                "swap_total_mb": vals.get("SwapTotal"),
                "swap_free_mb": vals.get("SwapFree")}
    except Exception:
        return {}


def status():
    units = {u: _unit_state(u) for u in DISPLAY_UNITS}
    chromes = []
    for unit, cdp in _chromes():
        chromes.append({"unit": unit, "cdp": cdp, "state": _unit_state(unit),
                        "cdp_ok": _cdp_ok(cdp)})
    return {"units": units, "chromes": chromes,
            "windows_visible": _chrome_windows(True),
            "windows_all": _chrome_windows(False),
            "memory": mem_info()}


def repair():
    actions, errors = [], []

    # 1. Display plumbing. Xvfb restarting kills every X client, so if it was
    #    down the Chromes come back via their own Restart=always — wait them out.
    xvfb_restarted = False
    for unit in DISPLAY_UNITS:
        if _unit_state(unit) in ("active", "activating", "unknown"):
            continue
        ok, msg = _restart(unit)
        (actions if ok else errors).append(
            f"restarted {unit}" if ok else f"{unit}: {msg}")
        xvfb_restarted |= ok and unit == "kartis-xvfb"
    if xvfb_restarted:
        time.sleep(4)

    # 2. Chrome processes.
    for unit, cdp in _chromes():
        if _cdp_ok(cdp) or not shutil.which("systemctl"):
            continue
        if unit != _chromes()[0][0] and not _unit_enabled(unit):
            continue  # CV Chrome configured in .env but not installed here
        ok, msg = _restart(unit)
        (actions if ok else errors).append(
            f"restarted {unit} (CDP wasn't answering)" if ok else f"{unit}: {msg}")
        for _ in range(20):
            if _cdp_ok(cdp):
                break
            time.sleep(0.5)

    # 3. Hidden windows -> map + raise.
    every = _chrome_windows(False)
    visible = set(_chrome_windows(True) or [])
    if every is None:
        errors.append("xdotool not installed — run deploy/fix-vnc.sh")
    else:
        hidden = [w for w in every if w not in visible]
        for wid in hidden:
            _run(["xdotool", "windowmap", wid], timeout=5)
            _run(["xdotool", "windowactivate", "--sync", wid], timeout=5)
        # Chrome keeps several unmapped helper windows per process; only
        # report the count that actually became visible.
        now_visible = set(_chrome_windows(True) or [])
        shown = len(now_visible - visible)
        if shown:
            actions.append(f"un-minimized {shown} Chrome window(s)")
        visible = now_visible

    # 4. Live Chrome, nothing on screen -> ask it for a window.
    if every is not None and not visible:
        for unit, cdp in _chromes():
            if not _cdp_ok(cdp):
                continue
            try:
                req = urllib.request.Request(
                    f"{cdp.rstrip('/')}/json/new?about:blank", method="PUT")
                urllib.request.urlopen(req, timeout=8).close()
                actions.append(f"opened a window in {unit}")
            except Exception as e:
                errors.append(f"{unit} new window: {type(e).__name__}")

    mem = mem_info()
    if mem.get("available_mb") is not None and mem["available_mb"] < 300:
        errors.append(f"only {mem['available_mb']} MB RAM free — "
                      "Chrome will keep getting killed")
    return {"ok": not errors, "actions": actions, "errors": errors,
            "status": status()}


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(repair() if "--repair" in sys.argv else status(), indent=2))
