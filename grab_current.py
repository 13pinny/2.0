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
    print(f"[grab] looking for tab matching '{needle}'", flush=True)
    out_dir = Path(__file__).parent / "debug"
    out_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        print("[grab] connecting to Chrome CDP on localhost:9222 ...", flush=True)
        browser = p.chromium.connect_over_cdp(CDP_URL)
        print(f"[grab] connected. contexts: {len(browser.contexts)}", flush=True)

        all_pages = [pg for ctx in browser.contexts for pg in ctx.pages]
        print(f"[grab] {len(all_pages)} open pages:", flush=True)
        for pg in all_pages:
            print(f"  - {pg.url}", flush=True)

        matching = [pg for pg in all_pages if needle in (pg.url or "").lower()]
        if not matching:
            print(f"[grab] no tab with '{needle}' in the URL.", flush=True)
            return
        page = matching[0]
        print(f"[grab] grabbing HTML from: {page.url}", flush=True)

        try:
            html = page.content()
        except Exception as e:
            print(f"[grab] page.content() failed: {type(e).__name__}: {e}", flush=True)
            return
        print(f"[grab] got {len(html)} chars of HTML", flush=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = out_dir / f"{needle}-manual-{stamp}.html"
        out.write_text(html, encoding="utf-8")
        print(f"[grab] saved {out} ({out.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
