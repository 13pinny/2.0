"""One-shot importer: pulls JeruJam tickets/expenses from Firestore into kartis.db.

Safe to re-run. Wipes the three jerujam_* tables and repopulates from the source.
Run from CLI: python import_jerujam.py
Or hit POST /api/jerujam/import in the running app.
"""
import json
import re
import urllib.request
from datetime import datetime, timezone

import db

PROJECT = "jerujam"
API_KEY = "AIzaSyCWaAYQLHhc4e_h2G7Cvm6cd6S9oO6cO7g"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"


def _val(field):
    if field is None:
        return None
    if "stringValue" in field:
        return field["stringValue"]
    if "integerValue" in field:
        return int(field["integerValue"])
    if "doubleValue" in field:
        return float(field["doubleValue"])
    if "booleanValue" in field:
        return field["booleanValue"]
    if "timestampValue" in field:
        return field["timestampValue"]
    if "nullValue" in field:
        return None
    if "arrayValue" in field:
        return [_val(v) for v in field["arrayValue"].get("values", [])]
    if "mapValue" in field:
        return {k: _val(v) for k, v in field["mapValue"].get("fields", {}).items()}
    return None


def _doc_id(name):
    return name.rsplit("/", 1)[-1]


def _flatten(doc):
    return {k: _val(v) for k, v in doc.get("fields", {}).items()}


def _fetch_all(collection):
    docs, token = [], None
    while True:
        url = f"{BASE}/{collection}?key={API_KEY}&pageSize=300"
        if token:
            url += f"&pageToken={token}"
        with urllib.request.urlopen(url) as r:
            data = json.load(r)
        docs.extend(data.get("documents", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return docs


_DATE_FORMATS = (
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
)


def _to_iso(date_str):
    if not date_str:
        return None
    s = date_str.strip()
    s = re.sub(r"\s+", " ", s)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # last-ditch: pull the first YYYY-MM-DD or MM/DD/YYYY-ish substring
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _ticket_row(doc):
    f = _flatten(doc)
    event_date = f.get("eventDate") or ""
    return {
        "id": _doc_id(doc["name"]),
        "event_name": f.get("eventName") or "",
        "event_date": event_date,
        "event_date_iso": _to_iso(event_date),
        "venue": f.get("venue") or "",
        "city": f.get("city") or "",
        "section": f.get("section") or "",
        "row_label": f.get("row") or "",
        "seat_numbers": f.get("seatNumbers") or "",
        "quantity": f.get("quantity") or 0,
        "cost_per_ticket": f.get("costPerTicket") or 0,
        "total_purchase_cost": f.get("totalPurchaseCost") or 0,
        "purchase_platform": f.get("purchasePlatform") or "",
        "purchase_account": f.get("purchaseAccount") or "",
        "purchase_date": f.get("purchaseDate") or "",
        "listing_price": f.get("listingPrice") or 0,
        "listing_platform": f.get("listingPlatform") or "",
        "sold_price": f.get("soldPrice") or 0,
        "sold_count": f.get("soldCount") or 0,
        "sale_date": f.get("saleDate") or "",
        "seller_fees": f.get("sellerFees") or 0,
        "status": f.get("status") or "",
        "notes": f.get("notes") or "",
        "credit_card": f.get("creditCard") or "",
        "created_at": f.get("createdAt") or "",
        "updated_at": f.get("updatedAt") or "",
    }


def _sales_rows(doc):
    f = _flatten(doc)
    ticket_id = _doc_id(doc["name"])
    out = []
    for sale in f.get("sales") or []:
        if not isinstance(sale, dict):
            continue
        out.append({
            "id": sale.get("id") or f"{ticket_id}:{len(out)}",
            "ticket_id": ticket_id,
            "quantity": sale.get("quantity") or 0,
            "sale_price": sale.get("salePrice") or 0,
            "sale_date": sale.get("saleDate") or "",
            "platform": sale.get("platform") or "",
            "created_at": sale.get("createdAt") or "",
        })
    return out


def _expense_row(doc):
    f = _flatten(doc)
    return {
        "id": _doc_id(doc["name"]),
        "date": f.get("date") or "",
        "vendor": f.get("vendor") or "",
        "category": f.get("category") or "",
        "description": f.get("description") or "",
        "amount": f.get("amount") or 0,
        "notes": f.get("notes") or "",
        "created_at": f.get("createdAt") or "",
    }


def run():
    db.init()
    raw_tickets = _fetch_all("tickets")
    raw_expenses = _fetch_all("expenses")
    tickets = [_ticket_row(d) for d in raw_tickets]
    sales = []
    for d in raw_tickets:
        sales.extend(_sales_rows(d))
    expenses = [_expense_row(d) for d in raw_expenses]
    now_iso = datetime.now(timezone.utc).isoformat()
    db.replace_jerujam(tickets, sales, expenses, now_iso)
    return {"tickets": len(tickets), "sales": len(sales), "expenses": len(expenses)}


if __name__ == "__main__":
    counts = run()
    print(f"imported: {counts}")
