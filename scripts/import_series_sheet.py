"""One-time import of the NEXT purchase sheet into `series_purchases`.

The Google Sheet is the historical record; after this runs the /series page owns
the data and the sheet is a dead archive. Re-running is safe: rows are matched
on (date, section, row, qty, account) and skipped if already present.

Usage:
    python scripts/import_series_sheet.py <csv-path> [--series NEXT] [--dry-run]

Export the sheet first:
    https://docs.google.com/spreadsheets/d/<id>/export?format=csv
"""

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

# The sheet writes dates as D/M with no year, and the venue is implied by the
# month: the October run is Tel Aviv, the December run is Jerusalem.
YEAR = 2026
VENUE_BY_MONTH = {
    10: "Ramat Gan Stadium, Tel Aviv",
    12: "Pais Arena Jerusalem, Jerusalem",
}

# Nickname variants the user confirmed are the same account. Written to
# series_account_alias so the page (and the 9-cap) can resolve them.
ALIASES = {
    "pbtign": "pbtsign",
    "adele white": "adelewhite",
    "ax@yellow dn": "ax@yellowdn",
    "ax green": "ax@yellowdn",
    "lau yehuda": "yehudalau",
    "lau yehida": "yehudalau",
}

# Section names the generic rules in series.norm_section cannot derive.
# "vip e" at Pais Arena is listed on viagogo as "Floor" (the user saved it that
# way), which is why those two blocks otherwise match nothing.
SECTION_OVERRIDES = [
    ("Pais Arena Jerusalem, Jerusalem", "vip e", "Floor"),
]


def _money(text):
    t = (text or "").replace("$", "").replace(",", "").strip()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _iso(dm):
    """'3/12' -> '2026-12-03'. The sheet is day/month."""
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$", dm or "")
    if not m:
        return None, None
    day, month = int(m.group(1)), int(m.group(2))
    return f"{YEAR:04d}-{month:02d}-{day:02d}", VENUE_BY_MONTH.get(month, "")


def parse_rows(csv_path):
    out = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for raw in csv.reader(fh):
            if len(raw) < 12:
                continue
            date_iso, venue = _iso(raw[1])
            if not date_iso:
                continue
            try:
                qty = int(float(raw[2])) if raw[2].strip() else 0
            except ValueError:
                continue
            if not qty:
                continue
            status = (raw[10] or "").strip()
            account = (raw[8] or "").strip().lower()
            out.append({
                "event_date_iso": date_iso,
                "venue": venue,
                "section": (raw[3] or "").strip(),
                "row_label": (raw[4] or "").strip(),
                "seats": (raw[5] or "").strip(),
                "qty": qty,
                "unit_cost": _money(raw[6]),
                "total_cost": _money(raw[7]),
                "account": ALIASES.get(account, account),
                "marketplace": (raw[9] or "viagogo").strip() or "viagogo",
                # "uploaded" in this column means the listing exists on viagogo,
                # not that the ticket PDFs are attached - that is `etickets`,
                # which the scraper fills in separately.
                "listed": 1 if status.lower().startswith("uploaded") else 0,
                "etickets": None,
                "source": "sheet",
                "note": "; ".join(x for x in [(raw[11] or "").strip(),
                                              status if not status.lower().startswith("uploaded") else ""]
                                  if x),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--series", default="NEXT")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init()
    now = datetime.now(timezone.utc).isoformat()
    rows = parse_rows(args.csv_path)

    if not args.dry_run:
        for alias, canonical in ALIASES.items():
            db.series_alias_set(alias, canonical, now)
        for venue, local, viagogo in SECTION_OVERRIDES:
            db.viagogo_section_map_set(venue, local, viagogo, now)

    added = skipped = 0
    for r in rows:
        r["series"] = args.series
        r["intake_id"] = None
        dupe = db.series_purchase_exists(args.series, r["event_date_iso"], r["section"],
                                         r["row_label"], r["qty"], r["account"])
        if dupe:
            skipped += 1
            continue
        if not args.dry_run:
            db.series_purchase_insert(r, now)
        added += 1

    tix = sum(r["qty"] for r in rows)
    spend = sum(r["total_cost"] or 0 for r in rows)
    print(f"parsed {len(rows)} rows | {tix} tickets | ${spend:,.2f}")
    print(f"{'would add' if args.dry_run else 'added'} {added}, skipped {skipped} already present")


if __name__ == "__main__":
    main()
