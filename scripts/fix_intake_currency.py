"""Repair purchase costs that were stored in shekels as if they were dollars.

Before the currency fix, confirming a kupat / TM-IL / tickchak email on
/pending copied the receipt's ILS cost straight into
manual_inventory.cost_per_unit (which everything downstream reads as USD),
and the /series hook did the same into series_purchases. This finds those
rows and converts them.

A row is only touched when it provably still holds the raw receipt number:
  - manual_inventory: created by Confirm (id 'pending-...'), not yet
    converted (orig_currency NULL), and paired with exactly one confirmed
    ILS intake row with the same date + qty + cost_per_unit.
  - series_purchases: source 'email', its intake_id points at an ILS intake
    row, and unit_cost still equals that row's cost_per_unit.
Rows from an ILS provider that DON'T match that way (you typed a cost on
Confirm, edited it later, ...) are listed under "check by hand" and never
changed.

Usage (dry run by default):
    .venv\\Scripts\\python scripts\\fix_intake_currency.py
    .venv\\Scripts\\python scripts\\fix_intake_currency.py --apply
    .venv\\Scripts\\python scripts\\fix_intake_currency.py --apply --rate 0.27

--rate overrides the ILS->USD rate (default: today's cached rate, which is
close enough for purchases from the last few months; pass the rate from the
purchase date if you want it exact).
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import fx  # noqa: E402
import mail_intake  # noqa: E402

# Stamped into series_purchases.note on conversion — that table has no
# currency columns, and without a marker a converted row would no longer
# match its receipt and get listed under "check by hand" on every re-run.
SERIES_MARK = "[ILS->USD"


def _same(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) < 0.005


def _intakes():
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM pending_intake WHERE status = 'confirmed'").fetchall()
    out = []
    for r in rows:
        r = dict(r)
        cur = (r.get("currency") or mail_intake.PROVIDER_CURRENCY.get(r.get("provider") or "") or "").upper()
        if cur == "ILS":
            out.append(r)
    return out


def plan():
    intakes = _intakes()
    by_id = {r["id"]: r for r in intakes}
    used = set()
    fix_mi, review_mi = [], []
    for m in db.all_manual_inventory():
        if m.get("orig_currency") or not str(m.get("id") or "").startswith("pending-"):
            continue
        if m.get("intake_id"):
            continue  # confirmed after the fix — already converted
        date = (m.get("event_date_iso") or "")[:10]
        exact = [i for i in intakes if i["id"] not in used
                 and (i.get("event_date_iso") or "")[:10] == date
                 and (i.get("qty") or 0) == (m.get("qty") or 0)
                 and _same(i.get("cost_per_unit"), m.get("cost_per_unit"))]
        if len(exact) == 1:
            used.add(exact[0]["id"])
            fix_mi.append((m, exact[0]))
            continue
        loose = [i for i in intakes
                 if (i.get("event_date_iso") or "")[:10] == date
                 and (i.get("qty") or 0) == (m.get("qty") or 0)]
        if loose or len(exact) > 1:
            review_mi.append((m, (exact or loose)[0]))

    fix_sp, review_sp = [], []
    with db.connect() as conn:
        sps = [dict(r) for r in conn.execute(
            "SELECT * FROM series_purchases WHERE source = 'email' AND intake_id IS NOT NULL").fetchall()]
    for p in sps:
        i = by_id.get(p["intake_id"])
        if not i or SERIES_MARK in (p.get("note") or ""):
            continue
        if _same(p.get("unit_cost"), i.get("cost_per_unit")) or (
                p.get("unit_cost") is None and _same(p.get("total_cost"), i.get("cost"))):
            fix_sp.append((p, i))
        elif p.get("unit_cost") is not None:
            review_sp.append((p, i))
    return fix_mi, review_mi, fix_sp, review_sp


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the conversions (default: dry run)")
    ap.add_argument("--rate", type=float, help="ILS->USD rate to use (1 ILS = RATE USD)")
    args = ap.parse_args()

    db.init()
    rate = args.rate or fx.ils_to_usd_rate()
    fix_mi, review_mi, fix_sp, review_sp = plan()
    print(f"ILS->USD rate: {rate:.4f}{' (override)' if args.rate else ''}\n")

    print(f"/inventory pending tickets to convert: {len(fix_mi)}")
    for m, i in fix_mi:
        cpu = m["cost_per_unit"]
        print(f"  {m['id']}  {m.get('event_date_iso') or '?':10}  {m.get('event_name')!s:40.40}  "
              f"x{m.get('qty')}  ₪{cpu:.2f} -> ${cpu * rate:.2f}/ea  ({i['provider']})")
    print(f"\n/series email purchases to convert: {len(fix_sp)}")
    for p, i in fix_sp:
        print(f"  #{p['id']}  {p['series']} {p['event_date_iso']}  {p.get('section')!s:12.12}  x{p['qty']}  "
              f"unit ₪{p.get('unit_cost') or 0:.2f} -> ${(p.get('unit_cost') or 0) * rate:.2f}  "
              f"total ₪{p.get('total_cost') or 0:.2f} -> ${(p.get('total_cost') or 0) * rate:.2f}")
    if review_mi or review_sp:
        print("\nCheck by hand (ILS purchase, but the stored cost no longer matches the receipt):")
        for m, i in review_mi:
            print(f"  inventory {m['id']}  {m.get('event_name')!s:40.40}  stored {m.get('cost_per_unit')}/ea, "
                  f"receipt ₪{i.get('cost_per_unit')}/ea")
        for p, i in review_sp:
            print(f"  series #{p['id']}  {p['event_date_iso']}  stored {p.get('unit_cost')}/ea, "
                  f"receipt ₪{i.get('cost_per_unit')}/ea")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to convert.")
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    for m, i in fix_mi:
        cpu = m["cost_per_unit"]
        db.update_manual_inventory(m["id"], {
            "cost_per_unit": round(cpu * rate, 4),
            "orig_currency": "ILS",
            "orig_cost_per_unit": cpu,
            "fx_rate": rate,
            "intake_id": i["id"],
            "provider": i.get("provider"),
            "buyer_email": m.get("buyer_email") or i.get("buyer_email"),
            "ticket_url": m.get("ticket_url") or i.get("ticket_url"),
        })
    for p, i in fix_sp:
        fields = {}
        if p.get("unit_cost") is not None:
            fields["unit_cost"] = round(p["unit_cost"] * rate, 4)
        if p.get("total_cost") is not None:
            fields["total_cost"] = round(p["total_cost"] * rate, 2)
        if fields:
            mark = f"{SERIES_MARK} @{rate:.4f}: was ₪{p.get('unit_cost')}/ea]"
            fields["note"] = ((p.get("note") or "") + " " + mark).strip()
            db.series_purchase_update(p["id"], fields, now_iso)
    print(f"\nConverted {len(fix_mi)} inventory row(s) and {len(fix_sp)} series row(s).")


if __name__ == "__main__":
    main()
