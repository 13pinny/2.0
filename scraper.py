import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

import db

load_dotenv()

DEBUG_DIR = Path(__file__).parent / "debug"
USER_DATA = Path(__file__).parent / "user_data"


def _parse_money(text):
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _text(el, selector):
    node = el.query_selector(selector)
    return node.inner_text().strip() if node else None


def _row_id(r):
    return (
        _text(r, '[data-field="id"]')
        or _text(r, '[data-testid="ticket-id"]')
        or r.get_attribute("data-ticket-id")
    )


def _save_debug(page, label):
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = DEBUG_DIR / f"{label}-{stamp}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        base.with_suffix(".url.txt").write_text(page.url, encoding="utf-8")
    except Exception:
        pass
    print(f"[kartis] saved debug artifacts to {base}.*")


def _ensure_logged_in(page):
    """Verify the saved session still works by hitting the inventory URL."""
    url = os.environ.get("LYSTED_INVENTORY_URL", "https://app.lysted.com/tickets")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    if "login" in page.url or "automatiq.com" in page.url:
        raise RuntimeError(
            "Saved Lysted session has expired. Run `python login.py` to "
            "sign in again (email + password + SMS code), then retry."
        )
    body_text = (page.inner_text("body") or "").lower()
    if "performing security verification" in body_text or "verify you are human" in body_text:
        raise RuntimeError(
            "Cloudflare is challenging the scraper. Run `python login.py`, "
            "complete the 'Verify you are human' check, wait for the tickets "
            "page to load, then press Enter in that terminal and retry."
        )


def _scrape_page(page, url):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    return page.query_selector_all(
        '[data-testid="inventory-row"], tr.inventory-row, tr[data-ticket-id]'
    )


def _scrape_tickets(page):
    url = os.environ.get("LYSTED_INVENTORY_URL", "https://app.lysted.com/tickets")
    out = {}
    for r in _scrape_page(page, url):
        tid = _row_id(r)
        if not tid:
            continue
        out[tid] = {
            "id": tid,
            "event_name": _text(r, '[data-field="event"]'),
            "event_date": _text(r, '[data-field="date"]'),
            "section": _text(r, '[data-field="section"]'),
            "row": _text(r, '[data-field="row"]'),
            "seat": _text(r, '[data-field="seat"]'),
            "status": _text(r, '[data-field="status"]'),
        }
    return out


def _scrape_purchases(page):
    url = os.environ.get("LYSTED_PURCHASES_URL", "https://app.lysted.com/purchases")
    out = {}
    for r in _scrape_page(page, url):
        tid = _row_id(r)
        if not tid:
            continue
        out[tid] = {
            "id": tid,
            "purchase_price": _parse_money(_text(r, '[data-field="purchase-price"]')),
        }
    return out


def _scrape_sales(page):
    url = os.environ.get("LYSTED_SALES_URL", "https://app.lysted.com/sales")
    out = {}
    for r in _scrape_page(page, url):
        tid = _row_id(r)
        if not tid:
            continue
        out[tid] = {
            "id": tid,
            "sale_price": _parse_money(_text(r, '[data-field="sale-price"]')),
        }
    return out


def _merge(*sources):
    merged = {}
    for src in sources:
        for tid, row in src.items():
            merged.setdefault(tid, {"id": tid})
            for k, v in row.items():
                if v is not None:
                    merged[tid][k] = v
    return list(merged.values())


def scrape_all():
    if not USER_DATA.exists():
        raise RuntimeError(
            "No saved browser profile found. Run `python login.py` once to "
            "sign in manually (including SMS + Cloudflare if prompted), then retry."
        )
    rows = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _ensure_logged_in(page)
            _save_debug(page, "tickets")
            inv = _scrape_tickets(page)
            purchases = _scrape_purchases(page)
            sales = _scrape_sales(page)
            rows = _merge(inv, purchases, sales)
        except Exception:
            _save_debug(page, "failure")
            raise
        finally:
            context.close()
    return rows


def run_and_save():
    rows = [t for t in scrape_all() if t.get("id")]
    defaults = {
        "event_name": None, "event_date": None, "section": None, "row": None,
        "seat": None, "purchase_price": None, "sale_price": None, "status": None,
    }
    rows = [{**defaults, **r} for r in rows]
    now_iso = datetime.now(timezone.utc).isoformat()
    db.upsert_tickets(rows, now_iso)
    return len(rows)


if __name__ == "__main__":
    db.init()
    count = run_and_save()
    print(f"Saved {count} tickets.")
