"""One-time interactive Lysted login.

Launches a dedicated Google Chrome window with remote debugging enabled
and leaves it running. You log in manually in that window — no
automation is controlling it during login, so Auth0 and Cloudflare don't
block you. The hourly scraper attaches to that same Chrome via CDP.

Keep the Chrome window open (minimized is fine). If you close it, run
this script again before the next scrape.
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


def launch_chrome():
    chrome = find_chrome()
    if not chrome:
        print("ERROR: Google Chrome not found. Install it from https://www.google.com/chrome/")
        sys.exit(1)
    USER_DATA.mkdir(exist_ok=True)
    args = [
        chrome,
        "--remote-debugging-port=9222",
        f"--user-data-dir={USER_DATA.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://lysted.com/login",
    ]
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)
    for _ in range(20):
        if is_chrome_running():
            return
        time.sleep(1)


def main():
    if is_chrome_running():
        print("Chrome is already running with remote debugging — good.")
    else:
        print("Launching a dedicated Chrome window...")
        launch_chrome()
        if not is_chrome_running():
            print("ERROR: Chrome did not start in time. Try running this script again.")
            sys.exit(1)
        print("Chrome is ready.")

    print()
    print("=" * 60)
    print(" In the Chrome window that opened:")
    print("   1. Log in to Lysted (email, password, SMS code if asked).")
    print("   2. Solve the Cloudflare 'Verify you are human' check if shown.")
    print("   3. Open a 2nd tab → https://inv.viagogo.com/  → sign in.")
    print("   4. Open a 3rd tab → https://crowdvolt.com/login → sign in")
    print("      with your phone + SMS OTP. Once you land on the dashboard")
    print("      the session is cached in user_data/ for the scraper.")
    print()
    print(" Then come back here and press Enter.")
    print()
    print(" IMPORTANT: keep that Chrome window open. Minimize it — don't")
    print(" close it. The hourly scraper attaches to it silently.")
    print("=" * 60)
    print()
    input("Press Enter once you're logged in and on the dashboards... ")
    print("Session ready. Now run: python app.py")


if __name__ == "__main__":
    main()
