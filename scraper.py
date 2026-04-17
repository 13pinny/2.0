import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from patchright.sync_api import sync_playwright

import db

load_dotenv()

DEBUG_DIR = Path(__file__).parent / "debug"
CDP_URL = "http://localhost:9222"


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
        if "login" in page.url or "automatiq.com" in page.url:
            raise RuntimeError(
                "Chrome session is logged out. Run `python login.py` and sign "
                "back into Lysted in that window."
            )
        raise
    page.wait_for_timeout(1500)


def _extract_row(r):
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
            row = _extract_row(r)
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


def _parse_viagogo_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%a %d %b %Y %H:%M").isoformat(timespec="minutes")
    except ValueError:
        try:
            return datetime.strptime(date_str.strip(), "%a %d %b %Y").date().isoformat()
        except ValueError:
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
        page.goto("https://inv.viagogo.com/Listings", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("tr.eventRow", timeout=15000)
        except Exception as e:
            print(f"[kartis] viagogo inventory never rendered: {e}")
            _save_debug(page, "viagogo")
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


def _parse_int(text):
    if text is None:
        return None
    m = re.search(r"-?\d+", text)
    return int(m.group(0)) if m else None


def scrape_all():
    result = {"lysted": [], "viagogo": []}
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            raise RuntimeError(
                f"Can't reach Chrome at {CDP_URL}. Run `python login.py` and "
                f"keep that Chrome window open. (Underlying: {e})"
            )
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        page = context.new_page()
        try:
            _ensure_logged_in(page)
            _save_debug(page, "tickets")
            result["lysted"] = _scrape_inventory(page)
        except Exception:
            _save_debug(page, "failure")
            raise
        finally:
            try:
                page.close()
            except Exception:
                pass

        try:
            result["viagogo"] = _scrape_viagogo(context)
        except Exception as e:
            print(f"[kartis] viagogo scrape failed: {type(e).__name__}: {e}")
    return result


def run_and_save():
    data = scrape_all()
    now_iso = datetime.now(timezone.utc).isoformat()
    db.upsert_inventory(data["lysted"], now_iso)
    db.upsert_viagogo(data["viagogo"], now_iso)
    return {"lysted": len(data["lysted"]), "viagogo": len(data["viagogo"])}


if __name__ == "__main__":
    db.init()
    counts = run_and_save()
    print(f"Saved {counts['lysted']} Lysted events, {counts['viagogo']} Viagogo listings.")
