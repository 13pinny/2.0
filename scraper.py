import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import db

load_dotenv()


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


def _login(page):
    email = os.environ["LYSTED_EMAIL"]
    password = os.environ["LYSTED_PASSWORD"]
    login_url = os.environ.get("LYSTED_LOGIN_URL", "https://lysted.com/login")
    page.goto(login_url)
    page.fill('input[type="email"], input[name="email"]', email)
    page.fill('input[type="password"], input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def _scrape_page(page, url):
    page.goto(url)
    page.wait_for_load_state("networkidle")
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
    tickets = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        _login(page)
        inv = _scrape_tickets(page)
        purchases = _scrape_purchases(page)
        sales = _scrape_sales(page)

        browser.close()

    return _merge(inv, purchases, sales)


def run_and_save():
    rows = [t for t in scrape_all() if t.get("id")]
    # Ensure every column exists so DB insert doesn't KeyError.
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
