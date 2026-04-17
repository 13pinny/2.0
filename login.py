"""One-time interactive Lysted login.

Opens a real browser window so you can sign in manually (email, password,
SMS code if required, Cloudflare challenge if shown). All cookies and
browser state are persisted under user_data/ so the hourly scraper can
reuse the same browser profile and keep Cloudflare happy.

Run again if the session ever expires or Cloudflare starts challenging.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

load_dotenv()

USER_DATA = Path(__file__).parent / "user_data"


def main():
    USER_DATA.mkdir(exist_ok=True)
    login_url = os.environ.get("LYSTED_LOGIN_URL", "https://lysted.com/login")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            channel="chrome",
            headless=False,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(login_url)

        print()
        print("=" * 60)
        print(" A Chrome window opened. In that window:")
        print("   1. Enter your email and password.")
        print("   2. Enter the SMS code if prompted.")
        print("   3. Solve the Cloudflare 'Verify you are human' check")
        print("      if it appears.")
        print("   4. Wait until your tickets dashboard is fully loaded.")
        print(" Then come back to THIS terminal and press Enter.")
        print("=" * 60)
        print()
        input("Press Enter once you're logged in and on the tickets page... ")

        context.close()
        print(f"Browser profile saved to {USER_DATA.name}/")


if __name__ == "__main__":
    main()
