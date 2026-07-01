import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

import db

load_dotenv()

DEBUG_DIR = Path(__file__).parent / "debug"
# KARTIS_CDP_URL points at the main Chrome the scraper attaches to. The
# default matches the local-dev setup (login.py launches Chrome on 9222).
# KARTIS_CDP_URL_VIAGOGO is an optional Phase-2 override that routes the
# Viagogo scrape through a separate Chrome (different profile + residential
# proxy) to dodge Cloudflare 403s. When unset, Viagogo uses the same Chrome
# as everything else — i.e. behavior is identical to before this change.
CDP_URL = os.getenv("KARTIS_CDP_URL", "http://localhost:9222")
CDP_URL_VIAGOGO = os.getenv("KARTIS_CDP_URL_VIAGOGO") or CDP_URL


def _parse_money(text):
    if not text:
        return None
    match = re.search(r"\$?([\d,]+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _text(el, selector):
    node = el.query_selector(selector)
    return node.inner_text().strip() if node else None


def _parse_event_datetime(date_str, time_str):
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%a %b %d, %Y").date()
    except ValueError:
        return None
    if time_str:
        try:
            t = datetime.strptime(time_str.strip(), "%I:%M%p").time()
            return datetime.combine(d, t).isoformat(timespec="minutes")
        except ValueError:
            pass
    return d.isoformat()


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


# Cleared at the start of each scrape_all(). Sources whose pages got bounced to
# a login URL during scrape (typically session expired) end up here so the
# dashboard can show a "VIAGOGO LOGIN EXPIRED" badge instead of failing silently.
_auth_errors = set()


def _is_login_redirect(url):
    if not url:
        return False
    u = url.lower()
    return (
        "account.viagogo.com" in u
        or "automatiq.com" in u  # Lysted's auth provider
        or "/login" in u
        or "/signin" in u
    )


def _flag_auth_if_login(page, source):
    """Call after a scrape failure where the page might have redirected to
    login. Records the source in _auth_errors so the dashboard can surface it."""
    try:
        if _is_login_redirect(page.url):
            _auth_errors.add(source)
    except Exception:
        pass


def _ensure_logged_in(page):
    url = os.environ.get("LYSTED_INVENTORY_URL", "https://app.lysted.com/tickets")
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("table.b-table tbody tr", timeout=20000)
    except Exception:
        body_text = (page.inner_text("body") or "").lower()
        if "performing security verification" in body_text or "verify you are human" in body_text:
            raise RuntimeError(
                "Cloudflare is challenging the browser. Switch to the Chrome "
                "window, complete the 'Verify you are human' check, then retry."
            )
        # Logged-out signal: redirected to a login page, the auth provider,
        # or the marketing root (anything other than app.lysted.com — Lysted
        # bounces logged-out users to lysted.com/ rather than a /login URL).
        cur = (page.url or "").lower()
        if ("login" in cur
                or "automatiq.com" in cur
                or "app.lysted.com" not in cur):
            _auth_errors.add("lysted")
            raise RuntimeError(
                "Chrome session is logged out. Run `python login.py` and sign "
                "back into Lysted in that window."
            )
        raise
    page.wait_for_timeout(1500)


def _stubhub_href_in(scope):
    """Return a StubHub event URL found anywhere under `scope` (a row element
    or the page), or None. Lysted's ⋮ menu exposes external-marketplace links
    as plain anchors; we just read the href."""
    try:
        a = scope.query_selector('a[href*="stubhub.com"]')
    except Exception:
        return None
    if not a:
        return None
    href = (a.get_attribute("href") or "").strip()
    return href or None


# Selectors for the per-row ⋮ ("External links") dropdown toggle. Lysted uses
# BootstrapVue, whose dropdown menus are lazy-rendered — the StubHub anchor
# isn't in the DOM until the menu opens. These are best-effort; the discovery
# probe (lysted_probe/probe_stubhub_link.py) is the source of truth if Lysted's
# markup shifts. Everything is wrapped so a miss just yields no link (the event
# then shows up in the dashboard's "skipped" list rather than breaking a scrape).
_DROPDOWN_TOGGLE_SELECTORS = (
    "td:last-child button.dropdown-toggle",
    "button.dropdown-toggle",
    'button[aria-haspopup="menu"]',
)


def _row_stubhub_url(r, page=None):
    # Cheapest path: anchor already present in the row's DOM (no extra work).
    url = _stubhub_href_in(r)
    if url or page is None:
        return url
    # Fallback: open the row's ⋮ menu, read the revealed anchor, close it.
    toggle = None
    for sel in _DROPDOWN_TOGGLE_SELECTORS:
        toggle = r.query_selector(sel)
        if toggle:
            break
    if not toggle:
        return None
    try:
        toggle.click()
        page.wait_for_timeout(200)
        # Menu may render inside the row or be appended to <body>.
        url = _stubhub_href_in(r) or _stubhub_href_in(page)
    except Exception:
        url = None
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return url


def _extract_row(r, page=None):
    event_name = _text(r, "span.smart-title")
    if not event_name:
        return None

    secondary = r.query_selector("td[aria-colindex='1'] div.secondary")
    event_date = None
    event_time = None
    venue = None
    if secondary:
        date_el = secondary.query_selector("b")
        event_date = date_el.inner_text().strip() if date_el else None
        full = secondary.inner_text().strip()
        if event_date:
            full = full.replace(event_date, "", 1)
        # full is now "8:00PM, Moody Center ATX" (whitespace/newlines collapse)
        cleaned = " ".join(full.split())
        if "," in cleaned:
            time_part, venue_part = cleaned.split(",", 1)
            event_time = time_part.strip() or None
            venue = venue_part.strip() or None
        else:
            venue = cleaned or None

    listings_count = None
    tickets_count = None
    primary = r.query_selector("td[aria-colindex='2'] div.primary")
    if primary:
        numbers = re.findall(r"\d+", primary.inner_text())
        if len(numbers) >= 2:
            listings_count = int(numbers[0])
            tickets_count = int(numbers[1])

    total_cost = None
    total_list = None
    price_sec = r.query_selector("td[aria-colindex='2'] div.secondary")
    if price_sec:
        amounts = re.findall(r"\$[\d,]+(?:\.\d+)?", price_sec.inner_text())
        if len(amounts) >= 1:
            total_cost = _parse_money(amounts[0])
        if len(amounts) >= 2:
            total_list = _parse_money(amounts[1])

    return {
        "id": f"{event_name}|{event_date or ''}|{venue or ''}",
        "event_name": event_name,
        "event_date": event_date,
        "event_time": event_time,
        "event_date_iso": _parse_event_datetime(event_date, event_time),
        "venue": venue,
        "listings_count": listings_count,
        "tickets_count": tickets_count,
        "total_cost": total_cost,
        "total_list": total_list,
        "stubhub_url": _row_stubhub_url(r, page),
    }


NEXT_BTN_SELECTORS = (
    'ul.pagination li:not(.disabled) button[aria-label*="next" i]',
    'ul.pagination li:not(.disabled) a[aria-label*="next" i]',
    'button[aria-label="Go to next page"]:not([disabled])',
    'a[aria-label="Go to next page"]:not([aria-disabled="true"])',
)


def _find_next_button(page):
    for sel in NEXT_BTN_SELECTORS:
        btn = page.query_selector(sel)
        if btn:
            return btn
    return None


def _scrape_inventory(page):
    out = []
    seen = set()
    for _ in range(30):  # safety cap
        page.wait_for_selector("table.b-table tbody tr", timeout=10000)
        page.wait_for_timeout(800)
        for r in page.query_selector_all("table.b-table tbody tr"):
            row = _extract_row(r, page)
            if row and row["id"] not in seen:
                seen.add(row["id"])
                out.append(row)
        next_btn = _find_next_button(page)
        if not next_btn:
            break
        try:
            next_btn.click()
        except Exception:
            break
        page.wait_for_timeout(1200)
    return out


PURCHASES_EXTRACT_JS = r"""
() => Array.from(document.querySelectorAll("table.b-table tbody tr[role='row']")).map(tr => ({
  cells: Array.from(tr.querySelectorAll("td")).map(td => (td.innerText || "").trim())
}))
"""


def _parse_lysted_event_date(text):
    """Parse a purchases-page event date like '05/08/26' or 'May 08, 2026'."""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_purchases_row(cells):
    """Best-effort parse of the Purchases page cells.

    Structure from the screenshot (5 columns):
      0: Order  — '14386374\n04/16/26'
      1: Event  — 'Blake Shelton\nThe Colosseum at Caesars Palace · Las Vegas, NV\nMay 08, 2026 · 08:00PM'
      2: Seating — '202\nRow K · Qty 4\nSeats 213-216'
      3: Type/Notes — 'Mobile Transfer\n13pinny@gmail.com\n30000710743076238'
      4: Cost/Unit — '$364.00\n$91.00/ea\nActive'
    """
    if len(cells) < 5:
        return None

    order_lines = [l.strip() for l in cells[0].splitlines() if l.strip()]
    event_lines = [l.strip() for l in cells[1].splitlines() if l.strip()]
    seat_lines = [l.strip() for l in cells[2].splitlines() if l.strip()]
    type_lines = [l.strip() for l in cells[3].splitlines() if l.strip()]
    cost_lines = [l.strip() for l in cells[4].splitlines() if l.strip()]

    order_id = order_lines[0] if order_lines else None
    order_date = order_lines[1] if len(order_lines) > 1 else None

    event_name = event_lines[0] if event_lines else None
    venue = event_lines[1] if len(event_lines) > 1 else None
    event_datetime = event_lines[2] if len(event_lines) > 2 else None
    event_date_iso = None
    if event_datetime:
        date_part = event_datetime.split("·")[0].strip()
        event_date_iso = _parse_lysted_event_date(date_part)

    section = seat_lines[0] if seat_lines else None
    row_label = None
    qty = None
    seats = None
    if len(seat_lines) > 1:
        m_row = re.search(r"Row\s+(\S+)", seat_lines[1])
        m_qty = re.search(r"Qty\s+(\d+)", seat_lines[1])
        row_label = m_row.group(1) if m_row else None
        qty = int(m_qty.group(1)) if m_qty else None
    if len(seat_lines) > 2:
        seats = seat_lines[2].replace("Seats", "").strip()

    delivery_type = type_lines[0] if type_lines else None
    account_email = type_lines[1] if len(type_lines) > 1 else None
    transaction_id = type_lines[2] if len(type_lines) > 2 else None

    total_cost = _parse_money(cost_lines[0]) if cost_lines else None
    cost_per_unit = _parse_money(cost_lines[1]) if len(cost_lines) > 1 else None
    status = cost_lines[-1] if cost_lines else None

    composite_id = "|".join(filter(None, [order_id, section, row_label, seats or ""]))
    if not composite_id:
        return None

    return {
        "id": composite_id,
        "order_id": order_id,
        "order_date": order_date,
        "event_name": event_name,
        "event_date": event_datetime,
        "event_date_iso": event_date_iso,
        "venue": venue,
        "section": section,
        "row_label": row_label,
        "qty": qty,
        "seats": seats,
        "delivery_type": delivery_type,
        "account_email": account_email,
        "transaction_id": transaction_id,
        "total_cost": total_cost,
        "cost_per_unit": cost_per_unit,
        "status": status,
    }


def _lysted_sale_from_api(rec):
    """Convert one record from api.lysted.com/api/sales to our DB shape."""
    invoice_id = rec.get("invoiceId")
    if invoice_id is None:
        return None
    invoice_date = rec.get("invoiceDate") or ""
    event_date = rec.get("eventDate") or ""
    low = rec.get("lowSeat")
    high = rec.get("highSeat")
    seats = None
    if low is not None and high is not None:
        seats = f"{low}-{high}" if str(low) != str(high) else str(low)
    venue_parts = [rec.get("venueName"), rec.get("venueCity")]
    venue = ", ".join(p for p in venue_parts if p) or None
    sale_price = None
    payout = None
    cost = None
    try:
        sale_price = float(rec["total"]) if rec.get("total") not in (None, "") else None
    except (TypeError, ValueError):
        pass
    try:
        payout = float(rec["payout"]) if rec.get("payout") not in (None, "") else None
    except (TypeError, ValueError):
        pass
    try:
        cost = float(rec["cost"]) if rec.get("cost") not in (None, "") else None
    except (TypeError, ValueError):
        pass
    fees = round(sale_price - payout, 2) if sale_price is not None and payout is not None else None
    import json as _json
    return {
        "id": f"lysted-{invoice_id}",
        "order_id": str(invoice_id),
        "sale_date": invoice_date,
        "sale_date_iso": invoice_date[:10] if invoice_date else None,
        "event_name": rec.get("eventName"),
        "event_date": event_date,
        "event_date_iso": event_date[:10] if event_date else None,
        "venue": venue,
        "section": rec.get("section"),
        "row_label": rec.get("row"),
        "qty": rec.get("quantity"),
        "seats": seats,
        "delivery_type": rec.get("stockType"),
        "sale_price": sale_price,
        "payout": payout,
        "fees": fees,
        "cost": cost,
        "status": rec.get("status") or rec.get("fulfillmentStatus"),
        "raw_cells": _json.dumps(rec, default=str)[:8000],
    }


LYSTED_SALES_LOOKBACK_DAYS = int(os.environ.get("LYSTED_SALES_LOOKBACK_DAYS", "7"))


def _scrape_lysted_sales(context):
    """Pull Lysted sales via api.lysted.com directly (much cleaner than HTML scrape).

    The /sales UI calls /api/sales with a JWT pulled from localStorage. We hit
    that same API with a `saledate.min:YYYY-MM-DD` filter to grab a sliding
    lookback window (default 7 days), so a missed hourly tick doesn't lose data.
    """
    import json as _json
    from datetime import timedelta
    # Find an already-logged-in app.lysted.com tab that *currently* has the
    # JWT in localStorage (otherwise we'd grab a tab that already redirected
    # to the marketing site and lose the auth).
    existing = None
    for pg in context.pages:
        try:
            if "app.lysted.com" not in (pg.url or ""):
                continue
            keys = pg.evaluate("Object.keys(localStorage)")
            if "ninja-auth" in keys:
                existing = pg
                break
        except Exception:
            continue
    page = existing or context.new_page()
    created_page = existing is None
    try:
        if created_page:
            page.goto("https://app.lysted.com/sales", wait_until="domcontentloaded", timeout=15000)
            try:
                page.wait_for_function(
                    'window.localStorage && window.localStorage.getItem("ninja-auth") != null',
                    timeout=15000,
                )
            except Exception:
                print("[kartis] timed out waiting for ninja-auth in localStorage; Lysted session may be expired.")
                _save_debug(page, "sales")
                _auth_errors.add("lysted")
                page.close()
                return []
        raw_auth = page.evaluate('localStorage.getItem("ninja-auth")')
        # Only proceed if the cached JWT hasn't expired. Reloading the tab to
        # force a refresh tends to redirect to the marketing site if cookies
        # are also expired, which logs the user out — so we'd rather report
        # cleanly than wreck the session.
        try:
            auth_check = _json.loads(raw_auth or "{}")
            exp_ms = (auth_check.get("expiresAt") or 0) * 1000
            now_ms = int(datetime.now().timestamp() * 1000)
            if exp_ms and exp_ms < now_ms:
                print("[kartis] Lysted JWT in localStorage is expired. "
                      "Open the Lysted tab in Chrome and click around (or re-log in) to refresh it.")
                _auth_errors.add("lysted")
                if created_page:
                    page.close()
                return []
        except Exception:
            pass
        if not raw_auth:
            print("[kartis] no ninja-auth in localStorage — Lysted login may be expired.")
            _save_debug(page, "sales")
            _auth_errors.add("lysted")
            return []
        auth = _json.loads(raw_auth)
        token = auth.get("token")
        api_base = auth.get("api") or "https://api.lysted.com"
        if not token:
            print("[kartis] ninja-auth has no token — login expired.")
            _save_debug(page, "sales")
            _auth_errors.add("lysted")
            return []
        since = (datetime.now() - timedelta(days=LYSTED_SALES_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        url = f"{api_base}/api/sales?search=saledate.min:{since}&dateOffset=240&perPage=500&force=true"
        resp = page.request.get(url, headers={
            "Authorization": f"Bearer {token}",
            "accept": "application/json, text/plain, */*",
        })
        if resp.status != 200:
            print(f"[kartis] lysted sales API returned {resp.status}: {resp.text()[:200]}")
            return []
        data = _json.loads(resp.text())
        records = data.get("rows") or data.get("data") or []
        out = []
        seen = set()
        for rec in records:
            parsed = _lysted_sale_from_api(rec)
            if parsed and parsed["id"] not in seen:
                seen.add(parsed["id"])
                out.append(parsed)
        return out
    finally:
        if created_page:
            try:
                page.close()
            except Exception:
                pass


def _scrape_lysted_purchases(context):
    url = os.environ.get("LYSTED_PURCHASES_URL", "https://app.lysted.com/purchases")
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("table.b-table tbody tr", timeout=20000)
        except Exception as e:
            print(f"[kartis] purchases table never rendered: {e}")
            _save_debug(page, "purchases")
            _flag_auth_if_login(page, "lysted")
            return []
        page.wait_for_timeout(1500)

        out = []
        seen = set()
        for _ in range(30):
            rows = page.evaluate(PURCHASES_EXTRACT_JS)
            for r in rows:
                parsed = _parse_purchases_row(r.get("cells") or [])
                if parsed and parsed["id"] not in seen:
                    seen.add(parsed["id"])
                    out.append(parsed)
            next_btn = _find_next_button(page)
            if not next_btn:
                break
            try:
                next_btn.click()
            except Exception:
                break
            page.wait_for_timeout(1200)

        _save_debug(page, "purchases")
        return out
    finally:
        try:
            page.close()
        except Exception:
            pass


VIAGOGO_SALES_TABS = ("Upload E-Tickets", "Upload Transfer Receipts", "Get Paid")


def _viagogo_select_tab(page, label):
    return page.evaluate(
        """(want) => {
          for (const li of document.querySelectorAll('li.state')) {
            const lbl = li.querySelector('.label');
            if (lbl && lbl.innerText.trim().toLowerCase() === want.toLowerCase()) {
              if (!li.classList.contains('sel')) li.click();
              return true;
            }
          }
          return false;
        }""",
        label,
    )


def _parse_viagogo_sale_row(cells, tab):
    """Parse one /Transactions row. Column count varies per tab — find fields by pattern.

    Looking for: a money cell ($X\\nUSD), an order cell starting with '#',
    a seating cell containing '× <qty>', an event cell with a Mon DD MMM YYYY
    date, an optional 'UPLOAD BY LATEST' deadline cell, and a row-status badge
    like 'Upload' or 'Postponed' (only present on the upload tabs).
    """
    if not cells:
        return None
    raw = "\t".join(cells)

    def _lines(s):
        return [l.strip() for l in (s or "").splitlines() if l.strip()]

    # Identify cells by content.
    sale_price = None
    sale_price_text = None
    order_cell = None
    seating_cell = None
    event_cell = None
    type_cell = None
    deadline_cell = None
    row_status = None
    for c in cells:
        text = (c or "").strip()
        if not text:
            continue
        lines = _lines(text)
        if "$" in text and "USD" in text and sale_price is None:
            m = re.search(r"\$([\d,]+(?:\.\d+)?)", text)
            sale_price = _parse_money(m.group(1)) if m else None
            sale_price_text = text
            continue
        if text.startswith("#") and order_cell is None:
            order_cell = lines
            continue
        if "×" in text and seating_cell is None:
            seating_cell = lines
            continue
        if "UPLOAD BY LATEST" in text.upper() and deadline_cell is None:
            deadline_cell = lines
            continue
        if text in ("E-Tickets", "Paper", "Mobile Transfer", "Transfer", "Hard Tickets") and type_cell is None:
            type_cell = text
            continue
        if event_cell is None and len(lines) >= 2 and re.search(r"\d{4}", text):
            event_cell = lines
            continue
        if row_status is None and len(text) <= 24 and not any(ch.isdigit() for ch in text):
            row_status = text

    if not order_cell or not event_cell:
        return None

    order_id = order_cell[0].lstrip("#").strip() if order_cell else None
    sale_date = order_cell[1] if len(order_cell) > 1 else None
    sale_date_iso = _parse_viagogo_date(sale_date)
    if sale_date_iso and "T" in sale_date_iso:
        sale_date_iso = sale_date_iso.split("T", 1)[0]

    event_name = event_cell[0]
    event_date = event_cell[1] if len(event_cell) > 1 else None
    venue = event_cell[2] if len(event_cell) > 2 else None
    event_date_iso = _parse_viagogo_date(event_date)
    if event_date_iso and "T" in event_date_iso:
        event_date_iso = event_date_iso.split("T", 1)[0]

    section = None
    row_label = None
    seats = None
    qty = None
    if seating_cell:
        section = seating_cell[0] if seating_cell else None
        if len(seating_cell) > 1:
            mid = seating_cell[1]
            mr = re.search(r"Row\s+([^,]+)", mid)
            ms = re.search(r"Seat\s+([\d\-\s]+)", mid)
            row_label = mr.group(1).strip() if mr else None
            seats = ms.group(1).strip() if ms else None
        for ln in seating_cell:
            mq = re.search(r"×\s*(\d+)", ln)
            if mq:
                qty = int(mq.group(1))
                break

    upload_deadline = None
    if deadline_cell and len(deadline_cell) > 1:
        upload_deadline = deadline_cell[1]

    if not order_id:
        return None
    return {
        "id": order_id,
        "order_id": order_id,
        "sale_date": sale_date,
        "sale_date_iso": sale_date_iso,
        "event_name": event_name,
        "event_date": event_date,
        "event_date_iso": event_date_iso,
        "venue": venue,
        "section": section,
        "row_label": row_label,
        "seats": seats,
        "qty": qty,
        "ticket_type": type_cell,
        "sale_price": sale_price,
        "upload_deadline": upload_deadline,
        "tab": tab,
        "status": row_status or tab,
        "raw_cells": raw,
    }


def _scrape_viagogo_sales(context):
    page = context.new_page()
    try:
        # 60s goto + 30s selector wait: Viagogo's Transactions page ships a
        # heavy JS bundle that's slow to render from a fresh datacenter IP.
        # The defaults (15s / 15s) tripped consistently on the VPS even
        # though local laptop runs were fine.
        page.goto("https://inv.viagogo.com/Transactions", wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("li.state", timeout=30000)
        except Exception as e:
            print(f"[kartis] viagogo sales tabs never rendered: {e}")
            _save_debug(page, "viagogo-sales")
            _flag_auth_if_login(page, "viagogo")
            return []
        page.wait_for_timeout(1500)

        seen = set()
        out = []
        for tab in VIAGOGO_SALES_TABS:
            ok = _viagogo_select_tab(page, tab)
            if not ok:
                continue
            page.wait_for_timeout(2000)
            try:
                page.wait_for_function(
                    "() => document.querySelector('table tbody tr') != null",
                    timeout=8000,
                )
            except Exception:
                pass
            rows_data = page.evaluate(
                """() => Array.from(document.querySelectorAll('table tbody tr')).map(tr => ({
                  cells: Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim())
                }))"""
            )
            for r in rows_data:
                parsed = _parse_viagogo_sale_row(r.get("cells") or [], tab)
                if parsed and parsed["id"] not in seen:
                    seen.add(parsed["id"])
                    out.append(parsed)
        _save_debug(page, "viagogo-sales")
        return out
    finally:
        try:
            page.close()
        except Exception:
            pass


def _parse_viagogo_date(date_str):
    if not date_str:
        return None
    s = date_str.strip()
    # Viagogo emits month-before-day, e.g. "Wed Apr 08 2026 14:17". The
    # day-before-month variants are kept as fallbacks in case the locale flips.
    for fmt in ("%a %b %d %Y %H:%M", "%a %d %b %Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).isoformat(timespec="minutes")
        except ValueError:
            pass
    for fmt in ("%a %b %d %Y", "%a %d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


VIAGOGO_EXTRACT_JS = r"""
(el) => {
  const q = (sel) => el.querySelector(sel);
  const t = (sel) => q(sel)?.innerText?.trim() ?? null;
  const headerH = q('td.w25 div.h');
  const headerText = headerH?.innerText?.trim() ?? '';
  const name = headerText.replace(/\[.*?\]/, '').trim();
  const listings = [];
  const next = el.nextElementSibling;
  if (next && next.classList.contains('js-listing-container')) {
    for (const tr of next.querySelectorAll('tr[data-id]')) {
      const tds = tr.querySelectorAll('td');
      const txt = (i) => tds[i]?.innerText?.trim() ?? null;
      listings.push({
        listing_id: tr.getAttribute('data-id'),
        quantity: tr.getAttribute('data-quantity'),
        section: txt(1),
        ticket_type: txt(2),
        visibility: txt(3),
        face_value: txt(4),
        price: txt(5),
        proceeds: txt(6),
        available: txt(7),
        sold: txt(8),
      });
    }
  }
  return {
    event_id: el.getAttribute('data-eventid'),
    event_name: name,
    event_date: t('td.w25 div.t.xs.nowrap'),
    venue: t('td.w25 div.t.xxs.cGry4'),
    listings,
  };
}
"""


def _scrape_viagogo(context):
    page = context.new_page()
    try:
        # 60s goto + 30s selector wait — same reason as _scrape_viagogo_sales:
        # the Listings page is JS-heavy and slow to render from a datacenter IP.
        page.goto("https://inv.viagogo.com/Listings", wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("tr.eventRow", timeout=30000)
        except Exception as e:
            print(f"[kartis] viagogo inventory never rendered: {e}")
            _save_debug(page, "viagogo")
            _flag_auth_if_login(page, "viagogo")
            return []
        page.wait_for_timeout(1500)

        # Expand every collapsed event so its listings render into the DOM.
        for _ in range(20):
            collapsed = page.query_selector_all("tr.eventRow:not(.expanded) td.js-expand")
            if not collapsed:
                break
            for arrow in collapsed:
                try:
                    arrow.click()
                except Exception:
                    pass
            page.wait_for_timeout(600)

        _save_debug(page, "viagogo")

        out = []
        for evt in page.query_selector_all("tr.eventRow"):
            try:
                data = evt.evaluate(VIAGOGO_EXTRACT_JS)
            except Exception as e:
                print(f"[kartis] viagogo row extract failed: {e}")
                continue
            event_name = data.get("event_name")
            event_date = data.get("event_date")
            venue = data.get("venue")
            event_id = data.get("event_id")
            event_date_iso = _parse_viagogo_date(event_date)
            for L in data.get("listings") or []:
                listing_id = L.get("listing_id")
                if not listing_id:
                    continue
                out.append({
                    "id": listing_id,
                    "event_id": event_id,
                    "event_name": event_name,
                    "event_date": event_date,
                    "event_date_iso": event_date_iso,
                    "venue": venue,
                    "section": L.get("section"),
                    "ticket_type": L.get("ticket_type"),
                    "visibility": L.get("visibility"),
                    "face_value": _parse_money(L.get("face_value")),
                    "price": _parse_money(L.get("price")),
                    "proceeds": _parse_money(L.get("proceeds")),
                    "available": _parse_int(L.get("available")),
                    "sold": _parse_int(L.get("sold")),
                })
        return out
    finally:
        try:
            page.close()
        except Exception:
            pass


def _parse_crowdvolt_sale_date(text):
    """Parse e.g. '04/25/26 • 2PM' or '04/25/26'."""
    if not text:
        return None
    head = text.split("•")[0].strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(head, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_crowdvolt_event_date(text, sale_iso=None):
    """Parse 'Sat, April 25 • 9PM'. Year is missing — anchor it to sale year
    (sales happen before the event), then bump +1 year if the parsed event
    falls before the sale.
    """
    if not text:
        return None
    head = text.split("•")[0].strip()
    head = re.sub(r"^[A-Za-z]{3,9},\s*", "", head)
    anchor_year = datetime.now().year
    sale_d = None
    if sale_iso:
        try:
            sale_d = datetime.strptime(sale_iso, "%Y-%m-%d").date()
            anchor_year = sale_d.year
        except ValueError:
            pass
    for fmt in ("%B %d %Y", "%B %d", "%b %d %Y", "%b %d"):
        try:
            d = datetime.strptime(head, fmt).date()
            if "%Y" not in fmt:
                d = d.replace(year=anchor_year)
                if sale_d and d < sale_d:
                    d = d.replace(year=anchor_year + 1)
            return d.isoformat()
        except ValueError:
            continue
    return None


def _parse_crowdvolt_row(cells):
    if len(cells) < 6:
        return None
    raw = "\t".join(cells)
    def _lines(s):
        return [l.strip() for l in (s or "").splitlines() if l.strip()]

    ev = _lines(cells[0])
    event_name = ev[0] if ev else None

    # CrowdVolt has rearranged the line order at least once. Old: event,
    # date, venue, qty/type. New: event, qty/type, date, venue. Recognise
    # each line by content rather than position.
    qty = None
    ticket_type = None
    event_date = None
    venue = None
    qty_pat = re.compile(r"^\s*(\d+)\s*(?:x|Tickets?\s*•?)\s*(.*)$", re.IGNORECASE)
    for line in ev[1:]:
        m = qty_pat.match(line)
        if m and qty is None:
            qty = int(m.group(1))
            ticket_type = (m.group(2) or "").strip() or None
            continue
        if event_date is None and ("•" in line or re.search(r"\b(?:AM|PM|am|pm)\b", line)):
            event_date = line
            continue
        if venue is None:
            venue = line
            continue
    if not ticket_type and venue and event_date is None:
        # Fallback for malformed rows
        ticket_type = venue
        venue = None

    order_id = (cells[1] or "").strip() or None
    sale_date = (cells[2] or "").strip() or None
    sale_date_iso = _parse_crowdvolt_sale_date(sale_date)
    event_date_iso = _parse_crowdvolt_event_date(event_date, sale_date_iso)

    price_per_ticket = _parse_money((cells[3] or "").strip())
    sale_price = _parse_money((cells[4] or "").strip())
    status = (cells[5] or "").strip() or None

    if not order_id:
        return None
    return {
        "id": order_id,
        "order_id": order_id,
        "sale_date": sale_date,
        "sale_date_iso": sale_date_iso,
        "event_name": event_name,
        "event_date": event_date,
        "event_date_iso": event_date_iso,
        "venue": venue,
        "qty": qty,
        "ticket_type": ticket_type,
        "price_per_ticket": price_per_ticket,
        "sale_price": sale_price,
        "status": status,
        "raw_cells": raw,
    }


def _scrape_crowdvolt_sales(context):
    # CrowdVolt redesigned the page in May 2026: /dashboard/sales redirects
    # to /selling, the layout swapped from a single table to tabs (Incomplete
    # / Completed), and the ?orderType=completed query param is no longer
    # honored — the page always defaults to "Incomplete" (which is usually
    # empty), so a naïve goto found no table rows and returned 0 sales.
    # Fix: navigate, wait for the SPA to render, click the Completed tab,
    # then wait for the table to populate.
    url = os.environ.get("CROWDVOLT_SALES_URL", "https://www.crowdvolt.com/selling")
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)  # let Next.js hydrate
        # Click the Completed tab (text-match — selector classes are utility
        # hashes that change on every deploy, so don't rely on them).
        try:
            page.get_by_text("Completed", exact=True).first.click(timeout=5000)
        except Exception as e:
            print(f"[kartis] couldn't click Completed tab on crowdvolt: {e}")
        page.wait_for_timeout(2000)
        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
        except Exception as e:
            print(f"[kartis] crowdvolt sales table never rendered: {e}")
            _save_debug(page, "crowdvolt-sales")
            _flag_auth_if_login(page, "crowdvolt")
            return []
        page.wait_for_timeout(1500)
        rows_data = page.evaluate(
            """() => Array.from(document.querySelectorAll('table tbody tr')).map(tr => ({
              cells: Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').trim())
            }))"""
        )
        out = []
        seen = set()
        for r in rows_data:
            parsed = _parse_crowdvolt_row(r.get("cells") or [])
            if parsed and parsed["id"] not in seen:
                seen.add(parsed["id"])
                out.append(parsed)
        _save_debug(page, "crowdvolt-sales")
        return out
    finally:
        try:
            page.close()
        except Exception:
            pass


def _parse_int(text):
    if text is None:
        return None
    m = re.search(r"-?\d+", text)
    return int(m.group(0)) if m else None


# Hosts whose leftover login tabs carry auth/silent-renewal or reCAPTCHA
# iframes that periodically re-navigate on their own. patchright's
# connect_over_cdp attaches to EVERY open tab and reads its frame tree; if such
# a frame detaches mid-attach the Node driver dies ("Frame was detached" ->
# "Connection closed while reading from the driver"). Standalone that's a quick
# error, but inside the long-lived gunicorn worker it manifests as an
# INDEFINITE hang that holds the scrape lock and wedges every source (the 2026-06
# outage). The scrape never needs these tabs — the login lives in the on-disk
# user_data profile and each source opens its own page — so we close them
# before connecting. The noVNC "open-logins" button reopens them on demand.
_CDP_STRAY_HOSTS = ("lysted.com", "automatiq.com", "crowdvolt.com", "viagogo.com")


def _close_stray_cdp_pages(cdp_url):
    """Best-effort: close leftover ticketing login tabs via the CDP HTTP API so
    their self-navigating iframes can't crash connect_over_cdp. Never raises."""
    import json as _json
    import urllib.request
    base = cdp_url.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
            targets = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"[kartis] CDP /json/list unavailable for pre-connect cleanup: "
              f"{type(e).__name__}: {e}")
        return
    pages = [t for t in targets if t.get("type") == "page"]
    strays = [t for t in pages if t.get("id")
              and any(h in (t.get("url") or "").lower() for h in _CDP_STRAY_HOSTS)]
    if not strays:
        return
    # Closing the last remaining page makes Chrome quit (no windows left) ->
    # systemd restarts it and reopens the very login tabs we're clearing. If the
    # strays are all that's open, leave a frame-free about:blank keeper behind.
    # (A prior run's blank keeper is a non-stray page, so keepers don't pile up.)
    if len(strays) == len(pages):
        try:
            req = urllib.request.Request(f"{base}/json/new?about:blank", method="PUT")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:
            print(f"[kartis] couldn't open keeper tab ({type(e).__name__}); "
                  f"skipping cleanup so Chrome isn't killed")
            return
    closed = 0
    for t in strays:
        try:
            urllib.request.urlopen(f"{base}/json/close/{t['id']}", timeout=5).close()
            closed += 1
        except Exception:
            pass
    if closed:
        print(f"[kartis] closed {closed} stray login tab(s) before CDP connect")


def _connect_over_cdp(p, cdp_url, attempts=3):
    """connect_over_cdp hardened against the 'Frame was detached' driver crash.

    Closes stray login tabs up front (the usual culprit), passes a real timeout
    so a wedged attach can't hang the worker forever, and retries — the
    reCAPTCHA-iframe variant is racy, so a second attempt against a quiet frame
    tree usually succeeds. Raises the last error if every attempt fails.
    """
    import time
    last = None
    for i in range(1, attempts + 1):
        _close_stray_cdp_pages(cdp_url)
        try:
            return p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        except Exception as e:
            last = e
            print(f"[kartis] connect_over_cdp attempt {i}/{attempts} failed: "
                  f"{type(e).__name__}: {str(e)[:140]}")
            time.sleep(2)
    raise last


def scrape_all():
    _auth_errors.clear()
    result = {
        "lysted": [],
        "lysted_purchases": [],
        "lysted_sales": [],
        "viagogo": [],
        "viagogo_sales": [],
        "crowdvolt_sales": [],
    }
    with sync_playwright() as p:
        try:
            browser = _connect_over_cdp(p, CDP_URL)
        except Exception as e:
            # Three failure modes we try to self-heal:
            #  1. Chrome is down (auto-update, accidental close, reboot) —
            #     launch_chrome() spawns it fresh and we retry.
            #  2. Chrome is up but the CDP WebSocket is broken — usually an
            #     older Chrome bound to :9222 without --remote-allow-origins=*.
            #     launch_chrome() short-circuits because is_chrome_running()
            #     sees the HTTP endpoint and returns True, so the connect
            #     still fails. Second pass: restart_chrome() kills the stale
            #     Chrome (matched by --user-data-dir, so your personal
            #     Chrome is untouched) and relaunches with the right flags.
            #  3. Both attempts still fail — surface a clear error.
            import login
            e1 = e2 = None
            try:
                if login.launch_chrome():
                    browser = _connect_over_cdp(p, CDP_URL)
                else:
                    raise RuntimeError("login.launch_chrome() returned False")
            except Exception as ex1:
                e1 = ex1
                try:
                    print(f"[kartis] first relaunch attempt failed ({ex1}); forcing kill+restart")
                    if login.restart_chrome():
                        browser = _connect_over_cdp(p, CDP_URL)
                    else:
                        raise RuntimeError("login.restart_chrome() returned False")
                except Exception as ex2:
                    e2 = ex2
                    raise RuntimeError(
                        f"Can't reach Chrome at {CDP_URL} and auto-launch failed. "
                        f"Double-click restart_chrome.bat (or run `python login.py`) "
                        f"and keep that Chrome window open. "
                        f"(Connect: {e}. Auto-launch: {e1}. Force-restart: {e2})"
                    )
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # Phase 2 hook: if a dedicated Viagogo Chrome is configured (different
        # CDP URL), connect to it and route Viagogo through that context only.
        # If the connect fails we log and fall back to None so the Viagogo
        # scrapes raise cleanly (caught by the per-source try/except below) —
        # we don't want to abort the whole run just because Viagogo Chrome is
        # down. When no override is set, Viagogo uses the same context as
        # everything else (current behavior).
        viagogo_context = context
        if CDP_URL_VIAGOGO != CDP_URL:
            try:
                viagogo_browser = _connect_over_cdp(p, CDP_URL_VIAGOGO)
                viagogo_context = (
                    viagogo_browser.contexts[0]
                    if viagogo_browser.contexts
                    else viagogo_browser.new_context()
                )
            except Exception as e:
                print(f"[kartis] viagogo CDP connect failed at {CDP_URL_VIAGOGO}: {type(e).__name__}: {e}")
                viagogo_context = None

        page = context.new_page()
        try:
            _ensure_logged_in(page)
            _save_debug(page, "tickets")
            result["lysted"] = _scrape_inventory(page)
        except Exception as e:
            _save_debug(page, "failure")
            # Don't re-raise — a Lysted logout/timeout used to abort the whole
            # batch, leaving Viagogo + CrowdVolt unscraped. Now we log it,
            # let the auth_errors signal surface to the dashboard, and let the
            # other sources still run.
            print(f"[kartis] lysted inventory scrape failed: {type(e).__name__}: {e}")
        finally:
            try:
                page.close()
            except Exception:
                pass

        try:
            result["lysted_purchases"] = _scrape_lysted_purchases(context)
        except Exception as e:
            print(f"[kartis] lysted purchases scrape failed: {type(e).__name__}: {e}")

        try:
            result["lysted_sales"] = _scrape_lysted_sales(context)
        except Exception as e:
            print(f"[kartis] lysted sales scrape failed: {type(e).__name__}: {e}")

        try:
            if viagogo_context is None:
                raise RuntimeError(f"viagogo CDP unavailable at {CDP_URL_VIAGOGO}")
            result["viagogo"] = _scrape_viagogo(viagogo_context)
        except Exception as e:
            print(f"[kartis] viagogo scrape failed: {type(e).__name__}: {e}")

        try:
            if viagogo_context is None:
                raise RuntimeError(f"viagogo CDP unavailable at {CDP_URL_VIAGOGO}")
            result["viagogo_sales"] = _scrape_viagogo_sales(viagogo_context)
        except Exception as e:
            print(f"[kartis] viagogo sales scrape failed: {type(e).__name__}: {e}")

        try:
            result["crowdvolt_sales"] = _scrape_crowdvolt_sales(context)
        except Exception as e:
            print(f"[kartis] crowdvolt sales scrape failed: {type(e).__name__}: {e}")
    return result


def run_and_save():
    data = scrape_all()
    now_iso = datetime.now(timezone.utc).isoformat()
    db.upsert_inventory(data["lysted"], now_iso)
    db.upsert_lysted_purchases(data["lysted_purchases"], now_iso)
    db.upsert_lysted_sales(data["lysted_sales"], now_iso)
    db.upsert_viagogo(data["viagogo"], now_iso)
    db.upsert_viagogo_sales(data["viagogo_sales"], now_iso)
    db.upsert_crowdvolt_sales(data["crowdvolt_sales"], now_iso)
    matched = 0
    pending_listed = 0
    try:
        import matcher
        # Pending-match first: flag manual rows already listed on Lysted/Viagogo
        # so the auto-matcher skips them as candidates (the listing row owns
        # decrement).
        pending_listed = matcher.run_pending_match()
        matched, _ = matcher.run_match_pass()
    except Exception as e:
        print(f"[kartis] matcher pass failed: {type(e).__name__}: {e}")
    return {
        "lysted": len(data["lysted"]),
        "lysted_purchases": len(data["lysted_purchases"]),
        "lysted_sales": len(data["lysted_sales"]),
        "viagogo": len(data["viagogo"]),
        "viagogo_sales": len(data["viagogo_sales"]),
        "crowdvolt_sales": len(data["crowdvolt_sales"]),
        "auto_matched": matched,
        "pending_listed": pending_listed,
        "auth_errors": sorted(_auth_errors),
    }


if __name__ == "__main__":
    db.init()
    counts = run_and_save()
    print(f"Saved {counts['lysted']} Lysted events, {counts['viagogo']} Viagogo listings.")
