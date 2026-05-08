"""Launch the dedicated logged-in Chrome the scraper attaches to.

Launches a Google Chrome instance with remote debugging on port 9222 and
a sticky user_data/ profile so your Lysted/Viagogo/CrowdVolt sessions
persist across runs. You log in manually in that window — no automation
is controlling it during login, so Auth0 and Cloudflare don't block you.
The hourly scraper attaches to that same Chrome via CDP.

Two modes:
  * Default (interactive): prints instructions, waits for Enter. Used by
    start.bat when you want to verify everything before launching Flask.
  * --no-prompt (silent): opens Chrome and exits immediately. Used by
    start_chrome.bat / Windows Startup so the browser comes up logged-in
    automatically on boot. Existing sessions stay live; just double-click
    the icon (or let it autostart) and Chrome's there.

Keep the Chrome window open (minimized is fine). If you close it, run
this again before the next scrape.
"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

USER_DATA = Path(__file__).parent / "user_data"
CDP_URL = "http://localhost:9222"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# All three logins open as tabs on launch. Order matters — Lysted first
# because Cloudflare's challenge there is the most fiddly; you want it
# foregrounded.
DEFAULT_URLS = [
    "https://lysted.com/login",
    "https://inv.viagogo.com/",
    "https://crowdvolt.com/login",
]


def is_chrome_running():
    try:
        urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=2)
        return True
    except Exception:
        return False


def find_chrome():
    for path in CHROME_PATHS:
        if Path(path).exists():
            return path
    return None


def launch_chrome(urls=None):
    """Spawn Chrome as a detached background process with the right flags
    plus a tab per URL. Idempotent at the user level — if Chrome is already
    listening on 9222 we don't try a second time (we'd just collide on the
    port). Returns True if Chrome is reachable on CDP afterwards."""
    if is_chrome_running():
        return True
    chrome = find_chrome()
    if not chrome:
        print("ERROR: Google Chrome not found. Install it from https://www.google.com/chrome/")
        return False
    USER_DATA.mkdir(exist_ok=True)
    args = [
        chrome,
        "--remote-debugging-port=9222",
        # Chrome 111+ silently drops external WebSocket CDP connections
        # unless the allowed origin is whitelisted. Without this flag,
        # curl http://localhost:9222/json/version returns fine (HTTP path)
        # but playwright's connect_over_cdp hangs on the WebSocket
        # upgrade. * is safe here because Chrome only listens on
        # localhost — same risk profile as before the mitigation.
        "--remote-allow-origins=*",
        f"--user-data-dir={USER_DATA.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    args.extend(urls or DEFAULT_URLS)
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)
    for _ in range(20):
        if is_chrome_running():
            return True
        time.sleep(1)
    return False


def main():
    no_prompt = "--no-prompt" in sys.argv[1:] or "-q" in sys.argv[1:]

    if is_chrome_running():
        if not no_prompt:
            print("Chrome is already running with remote debugging — good.")
    else:
        if not no_prompt:
            print("Launching a dedicated Chrome window...")
        ok = launch_chrome()
        if not ok:
            if not no_prompt:
                print("ERROR: Chrome did not start in time. Try running this script again.")
            sys.exit(1)
        if not no_prompt:
            print("Chrome is ready.")

    if no_prompt:
        # Silent autostart mode — exit immediately so the .bat can finish
        # and the cmd window can close. Sessions in user_data/ persist
        # across reboots so most of the time there's nothing for the user
        # to do; if a session expired they'll see the login prompt next
        # time they click on a tab.
        return

    print()
    print("=" * 60)
    print(" Chrome opened with three tabs:")
    print("   1. Lysted — log in (email, password, SMS code if asked).")
    print("      Solve Cloudflare's 'Verify you are human' if shown.")
    print("   2. Viagogo (inv.viagogo.com) — sign in.")
    print("   3. CrowdVolt — phone + SMS OTP.")
    print()
    print(" Once you've landed on each dashboard the sessions are cached")
    print(" in user_data/ for the scraper.")
    print()
    print(" IMPORTANT: keep that Chrome window open. Minimize it — don't")
    print(" close it. The hourly scraper attaches to it silently.")
    print("=" * 60)
    print()
    input("Press Enter once you're logged in and on the dashboards... ")
    print("Session ready. Now run: python app.py")


if __name__ == "__main__":
    main()
