"""Quick helper: dump the HTML of whichever tab in the running Chrome
window matches a URL substring. Used to capture page state (e.g. an
expanded Viagogo event) that the background scraper can't reach yet.

Usage:
    python grab_current.py viagogo
    python grab_current.py lysted
"""
import sys
from datetime import datetime
from pathlib import Path

from patchright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"


def main():
    needle = (sys.argv[1] if len(sys.argv) > 1 else "viagogo").lower()
    out_dir = Path(__file__).parent / "debug"
    out_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        matching = [
            pg for ctx in browser.contexts for pg in ctx.pages
            if needle in (pg.url or "").lower()
        ]
        if not matching:
            print(f"No tab with '{needle}' in the URL. Is the Chrome window open?")
            return
        page = matching[0]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = out_dir / f"{needle}-manual-{stamp}.html"
        out.write_text(page.content(), encoding="utf-8")
        print(f"Saved {out} ({out.stat().st_size} bytes) from {page.url}")


if __name__ == "__main__":
    main()
