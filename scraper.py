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


def scrape_inventory():
    """Log in to Lysted and return a list of ticket dicts.

    The selectors below are starting guesses. After the first real run
    we'll inspect the live HTML and refine them.
    """
    email = os.environ["LYSTED_EMAIL"]
    password = os.environ["LYSTED_PASSWORD"]
    login_url = os.environ.get("LYSTED_LOGIN_URL", "https://lysted.com/login")
    inventory_url = os.environ.get(
        "LYSTED_INVENTORY_URL", "https://app.lysted.com/tickets"
    )

    tickets = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        page.goto(login_url)
        page.fill('input[type="email"], input[name="email"]', email)
        page.fill('input[type="password"], input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(inventory_url)
        page.wait_for_load_state("networkidle")

        rows = page.query_selector_all(
            '[data-testid="inventory-row"], tr.inventory-row, tr[data-ticket-id]'
        )
        for r in rows:
            tid = (
                _text(r, '[data-field="id"]')
                or _text(r, '[data-testid="ticket-id"]')
                or r.get_attribute("data-ticket-id")
            )
            tickets.append({
                "id": tid,
                "event_name": _text(r, '[data-field="event"]'),
                "event_date": _text(r, '[data-field="date"]'),
                "section": _text(r, '[data-field="section"]'),
                "row": _text(r, '[data-field="row"]'),
                "seat": _text(r, '[data-field="seat"]'),
                "purchase_price": _parse_money(_text(r, '[data-field="purchase-price"]')),
                "sale_price": _parse_money(_text(r, '[data-field="sale-price"]')),
                "status": _text(r, '[data-field="status"]'),
            })

        browser.close()

    return tickets


def run_and_save():
    tickets = [t for t in scrape_inventory() if t.get("id")]
    now_iso = datetime.now(timezone.utc).isoformat()
    db.upsert_tickets(tickets, now_iso)
    return len(tickets)


if __name__ == "__main__":
    db.init()
    count = run_and_save()
    print(f"Saved {count} tickets.")
