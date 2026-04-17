"""One-time interactive Lysted login.

Opens a real browser window so you can log in manually (including any
SMS 2FA code). Saves the resulting session cookies to auth.json so the
hourly scraper can reuse them without re-authenticating.

Run again if the saved session ever expires.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

AUTH_PATH = Path(__file__).parent / "auth.json"


def main():
    login_url = os.environ.get("LYSTED_LOGIN_URL", "https://lysted.com/login")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(login_url)

        print()
        print("=" * 60)
        print(" A Chrome window opened. Log in to Lysted there:")
        print("   1. Enter your email and password.")
        print("   2. Enter the SMS code if prompted.")
        print("   3. Wait until you see your tickets dashboard.")
        print(" Then come back to THIS terminal and press Enter.")
        print("=" * 60)
        print()
        input("Press Enter once you're logged in... ")

        context.storage_state(path=str(AUTH_PATH))
        print(f"Saved session to {AUTH_PATH.name}")
        browser.close()


if __name__ == "__main__":
    main()
