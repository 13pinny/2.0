"""Average turnaround time (purchase -> sale) report.

Kartis doesn't compute this anywhere today, and no source cleanly tracks
purchase-date -> sale-date on every row: JeruJam does (one row per ticket,
purchase_date and sale_date both present), but Lysted/Viagogo/CrowdVolt
sales are scraped aggregates with no first-class link back to a purchase
row unless a manual match exists in inventory_matches.

This script reports:
  1. JeruJam turnaround (authoritative -- direct purchase_date/sale_date
     pair per ticket row), overall + by event.
  2. Lysted turnaround for the subset of sales matched to a purchase via
     inventory_matches or an exact (event, section, row) fallback -- best
     effort, will under-cover since most Lysted purchases/sales aren't
     explicitly linked.

Viagogo and CrowdVolt are skipped: those tables carry no purchase date at
all (viagogo_listings/crowdvolt_sales have no linked purchase-cost date),
so there is nothing to diff.

Run: .venv\\Scripts\\python scripts\\turnaround_report.py
"""
import statistics
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def _parse_date(s):
    if not s:
        return None
    s = str(s)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _summarize(days_list, label):
    if not days_list:
        print(f"{label}: no matched purchase->sale pairs found")
        return
    days_list = sorted(days_list)
    print(f"{label}: n={len(days_list)}")
    print(f"  mean   = {statistics.mean(days_list):.1f} days")
    print(f"  median = {statistics.median(days_list):.1f} days")
    print(f"  min    = {days_list[0]:.0f} days")
    print(f"  max    = {days_list[-1]:.0f} days")
    if len(days_list) >= 4:
        q1 = statistics.median(days_list[: len(days_list) // 2])
        q3 = statistics.median(days_list[(len(days_list) + 1) // 2 :])
        print(f"  IQR    = {q1:.0f} - {q3:.0f} days")


def jerujam_turnaround():
    tickets = {t["id"]: t for t in db.all_jerujam_tickets()}
    sales = db.all_jerujam_sales()

    days_list = []
    by_event = {}
    skipped_no_purchase_date = 0
    skipped_no_sale_date = 0
    skipped_negative = 0

    for s in sales:
        t = tickets.get(s.get("ticket_id"))
        if not t:
            continue
        p = _parse_date(t.get("purchase_date"))
        sd = _parse_date(s.get("sale_date"))
        if p is None:
            skipped_no_purchase_date += 1
            continue
        if sd is None:
            skipped_no_sale_date += 1
            continue
        delta = (sd - p).days
        if delta < 0:
            skipped_negative += 1
            continue
        days_list.append(delta)
        ev = t.get("event_name") or "(unknown event)"
        by_event.setdefault(ev, []).append(delta)

    _summarize(days_list, "JeruJam turnaround (purchase -> sale)")
    if skipped_no_purchase_date or skipped_no_sale_date or skipped_negative:
        print(
            f"  (skipped: {skipped_no_purchase_date} missing purchase_date, "
            f"{skipped_no_sale_date} missing sale_date, "
            f"{skipped_negative} negative/bad dates)"
        )

    if by_event:
        print("\n  Slowest-turning events (mean days, n>=2):")
        ranked = sorted(
            ((ev, statistics.mean(ds), len(ds)) for ev, ds in by_event.items() if len(ds) >= 2),
            key=lambda x: -x[1],
        )
        for ev, mean_days, n in ranked[:10]:
            print(f"    {mean_days:6.1f}d  (n={n:3d})  {ev}")

    return days_list


def lysted_turnaround():
    purchases = db.all_lysted_purchases()
    sales = db.all_lysted_sales()
    matches = {(m["sale_source"], m["sale_id"]): m for m in db.all_matches()}

    purchases_by_id = {p.get("id"): p for p in purchases}
    purchases_by_key = {}
    for p in purchases:
        key = (p.get("event_name") or "", p.get("section") or "", p.get("row_label") or "")
        purchases_by_key.setdefault(key, []).append(p)

    days_list = []
    matched_via = {"exact_id": 0, "match_table": 0, "event_section_row": 0}

    for s in sales:
        sale_id = str(s.get("id"))
        sd = _parse_date(s.get("sale_date_iso") or s.get("sale_date"))
        if sd is None:
            continue

        purchase_row = None
        # 1. Same id (scraper sometimes carries the purchase id through)
        if s.get("id") in purchases_by_id:
            purchase_row = purchases_by_id[s.get("id")]
            src = "exact_id"
        else:
            m = matches.get(("lysted", sale_id))
            if m and m.get("inv_source") == "lysted":
                purchase_row = purchases_by_id.get(m.get("inv_source_id"))
                src = "match_table"
            if purchase_row is None:
                key = (s.get("event_name") or "", s.get("section") or "", s.get("row_label") or "")
                candidates = purchases_by_key.get(key)
                if candidates:
                    purchase_row = candidates[0]
                    src = "event_section_row"

        if purchase_row is None:
            continue
        p = _parse_date(purchase_row.get("order_date"))
        if p is None:
            continue
        delta = (sd - p).days
        if delta < 0:
            continue
        days_list.append(delta)
        matched_via[src] += 1

    _summarize(days_list, "Lysted turnaround (order -> sale, best-effort match)")
    if days_list:
        print(f"  (matched via: {matched_via})")
    print(
        "  NOTE: this only covers sales matchable back to a purchase row; "
        "most Lysted rows have no explicit link, so this undercounts and "
        "should be read as a sample, not the true average."
    )
    return days_list


if __name__ == "__main__":
    print("=" * 60)
    j = jerujam_turnaround()
    print()
    print("=" * 60)
    l = lysted_turnaround()

    print()
    print("=" * 60)
    all_days = j + l
    if all_days:
        _summarize(all_days, "COMBINED (JeruJam + matched Lysted)")
    else:
        print("No turnaround data available at all.")
