"""/series - multi-date concert-series tracking (first use: the NEXT run).

One row per purchased block in `series_purchases`; everything else is derived
by joining to the viagogo tables that the scraper already refreshes.

Matching contract (set by the user, and validated against the NEXT data):
  key = (event_date_iso, normalised section, row_label)
Seats are NOT part of the key - the user does not put seat numbers on viagogo,
so `viagogo_sales.seats` is empty on every row. Quantity is not part of the key
either, because one purchased block routinely sells as several partial sales
(11/10 upper 3 row 61: 8 bought, sold 2 + 4 + 2).

Event *name* is never matched on: viagogo stores the NEXT run under five
different spellings ("Next", "NEXT with ... and Rita", "NEXT - ... , Rita",
plus variants with stray spaces). Date + venue is the stable identity.
"""

import re
from collections import defaultdict

import db

CAP_PER_ACCOUNT = 9


# --------------------------------------------------------------- sections ---

def _tierless_venue(venue):
    """Venues where viagogo drops the tier word from the section label.

    Ramat Gan Stadium lists the user's "upper 3" as plain "3" - and, notably,
    "lower 1" as plain "1" too, so the row number is the only thing separating
    an upper and a lower block in the same numbered section."""
    return "ramat gan" in (venue or "").lower()


def norm_section(section, venue, overrides=None):
    """Canonical section key for matching a purchase to a viagogo listing.

    `overrides` is the (venue, section) -> viagogo_section map, so a mapping the
    generic rules cannot express (NEXT's "vip e" is listed as "Floor" at Pais
    Arena) is data, editable in the UI, rather than code.
    """
    s = (section or "").strip().lower()
    if not s:
        return ""
    if overrides:
        hit = overrides.get(((venue or "").strip().lower(), s))
        if hit:
            s = hit.strip().lower()
    s = s.replace("uppper", "upper")                    # sheet typo
    s = re.sub(r"^(upper|lower)(\d)", r"\1 \2", s)      # "UPPER3" -> "upper 3"
    s = re.sub(r"\s*/\s*\d+\s*continue\s*$", "", s)     # "upper 4/3 continue" -> "upper 4"
    s = re.sub(r"^vip\s+", "", s)                       # "vip a4" -> "a4"
    s = re.sub(r"\s+", " ", s).strip()
    if _tierless_venue(venue):
        s = re.sub(r"^(upper|lower)\s+", "", s)
    return s


def _split_listing_section(blob):
    """viagogo_listings.section is a two-line blob: "Upper 9\\nRow 7"."""
    parts = [p.strip() for p in (blob or "").splitlines() if p.strip()]
    sec = parts[0] if parts else ""
    row = parts[1] if len(parts) > 1 else ""
    return sec, _norm_row(row)


def _norm_row(row):
    r = (row or "").strip()
    r = re.sub(r"^row\s*", "", r, flags=re.I)
    r = r.replace("+ Notes", "").replace("+ notes", "")
    return r.strip()


def _overrides():
    out = {}
    for r in db.viagogo_section_map_all() or []:
        venue = (r.get("venue") or "").strip().lower()
        out[(venue, (r.get("kupat_section") or "").strip().lower())] = r.get("viagogo_section") or ""
    return out


# ---------------------------------------------------------------- the join ---

def build(series="NEXT"):
    """Everything the /series page renders: blocks, per-date rollups, per-account
    cap state, and the series P&L. Costs are USD throughout; proceeds from
    viagogo are already net of their ~10% seller commission."""
    purchases = db.series_purchases_all(series)
    ov = _overrides()

    dates = {p["event_date_iso"] for p in purchases}
    venue_by_date = {}
    for p in purchases:
        if p.get("venue"):
            venue_by_date.setdefault(p["event_date_iso"], p["venue"])

    # --- index viagogo listings + sales by the match key -------------------
    listings = defaultdict(list)
    for L in db.all_viagogo() or []:
        d = (L.get("event_date_iso") or "")[:10]
        if d not in dates:
            continue
        sec, row = _split_listing_section(L.get("section"))
        listings[(d, norm_section(sec, L.get("venue"), ov), row)].append(L)

    sales = defaultdict(list)
    for s in db.all_viagogo_sales() or []:
        d = (s.get("event_date_iso") or "")[:10]
        if d not in dates:
            continue
        sec = (s.get("section") or "").strip()
        row = _norm_row(s.get("row_label"))
        sales[(d, norm_section(sec, s.get("venue"), ov), row)].append(s)

    # --- group purchases by key (two buys of the same block merge) ---------
    groups = defaultdict(list)
    for p in purchases:
        venue = p.get("venue") or venue_by_date.get(p["event_date_iso"], "")
        key = (p["event_date_iso"], norm_section(p.get("section"), venue, ov),
               _norm_row(p.get("row_label")))
        groups[key].append(p)

    blocks = []
    for key, rows in sorted(groups.items()):
        d, sec, row = key
        qty = sum(r.get("qty") or 0 for r in rows)
        cost = sum(r.get("total_cost") or 0 for r in rows)
        unit = (cost / qty) if qty else 0.0
        srows = sales.get(key, [])
        sold_qty = sum(s.get("qty") or 0 for s in srows)
        proceeds = sum(s.get("sale_price") or 0 for s in srows)
        lrows = listings.get(key, [])
        cost_of_sold = round(unit * sold_qty, 2)
        blocks.append({
            "event_date_iso": d,
            "venue": rows[0].get("venue") or venue_by_date.get(d, ""),
            "section": rows[0].get("section"),
            "section_key": sec,
            "row_label": rows[0].get("row_label"),
            "seats": ", ".join(r.get("seats") or "" for r in rows).strip(", "),
            "accounts": sorted({(r.get("account") or "").lower() for r in rows}),
            "qty": qty,
            "unit_cost": round(unit, 2),
            "total_cost": round(cost, 2),
            "sold_qty": sold_qty,
            "proceeds": round(proceeds, 2),
            "cost_of_sold": cost_of_sold,
            "profit": round(proceeds - cost_of_sold, 2),
            "roi": round((proceeds - cost_of_sold) / cost_of_sold * 100, 1) if cost_of_sold else None,
            "unsold_qty": max(qty - sold_qty, 0),
            "listed": bool(lrows),
            "listing_count": len(lrows),
            "listed_available": sum(L.get("available") or 0 for L in lrows),
            "list_states": sorted({(L.get("list_state") or "") for L in lrows if L.get("list_state")}),
            "etickets": rows[0].get("etickets"),
            "purchase_ids": [r["id"] for r in rows],
            # Oversupply, not listing count, is the real signal: two listings
            # can be legitimate when two buys of the same block were merged.
            # More units listed than owned means we are undercutting ourselves.
            "duplicate_listing": (sum(L.get("available") or 0 for L in lrows)
                                  + sum(L.get("sold") or 0 for L in lrows)) > qty,
            "note": "; ".join(r.get("note") or "" for r in rows).strip("; "),
        })

    # --- viagogo inventory with no purchase behind it ----------------------
    known = set(groups)
    orphans = []
    for key, lrows in sorted(listings.items()):
        if key in known:
            continue
        for L in lrows:
            orphans.append({
                "event_date_iso": key[0],
                "section": _split_listing_section(L.get("section"))[0],
                "row_label": key[2], "available": L.get("available") or 0,
                "sold": L.get("sold") or 0, "list_state": L.get("list_state") or "",
            })

    return {
        "series": series,
        "blocks": blocks,
        "orphan_listings": orphans,
        "by_date": _by_date(blocks),
        "caps": _caps(purchases),
        "totals": _totals(blocks, purchases),
        "cap_per_account": CAP_PER_ACCOUNT,
    }


def match_series(event_date_iso, venue=None):
    """Which tracked series (if any) a freshly-parsed purchase belongs to.

    Identity is date + venue, never event name — viagogo and the ticketing
    sites spell the NEXT run five different ways between them. A date already
    present in series_purchases with the same venue is the same run; if the
    date matches and we have no venue to compare, we accept on the date alone,
    since a tracked series date is specific enough.
    """
    d = (event_date_iso or "")[:10]
    if not d:
        return None
    want = (venue or "").strip().lower()
    for row in db.series_purchases_all_any_series():
        if row["event_date_iso"][:10] != d:
            continue
        have = (row.get("venue") or "").strip().lower()
        if not want or not have or want in have or have in want:
            return row["series"]
    return None


def record_from_intake(fields, buyer_email=None, intake_id=None, now_iso=None):
    """Append an emailed purchase to the series it belongs to.

    Returns the new series_purchases id, or None when the email is not for a
    tracked series (the overwhelmingly common case — this runs on every
    ingested purchase mail).

    The buying account is resolved through series_account_alias, which doubles
    as an email -> nickname map. An unmapped address is stored verbatim rather
    than dropped: it surfaces on /series as a new account, and one alias call
    re-points every row it owns.
    """
    from datetime import datetime, timezone

    date_iso = (fields.get("event_date_iso") or "")[:10]
    venue = fields.get("venue") or ""
    name = match_series(date_iso, venue)
    if not name:
        return None

    qty = fields.get("qty") or 0
    if not qty:
        return None
    cost = fields.get("cost")
    unit = fields.get("cost_per_unit")
    if cost in (None, "") and unit not in (None, ""):
        cost = float(unit) * qty
    elif unit in (None, "") and cost not in (None, ""):
        unit = float(cost) / qty if qty else None

    account = db.series_canonical_account(buyer_email or "")
    section = fields.get("section") or ""
    row_label = fields.get("row_label") or ""
    if db.series_purchase_exists(name, date_iso, section, row_label, qty, account):
        return None

    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    return db.series_purchase_insert({
        "series": name,
        "event_date_iso": date_iso,
        "venue": venue,
        "section": section,
        "row_label": row_label,
        "seats": fields.get("seats") or "",
        "qty": qty,
        "unit_cost": float(unit) if unit not in (None, "") else None,
        "total_cost": float(cost) if cost not in (None, "") else None,
        "account": account,
        "marketplace": "viagogo",
        "listed": 0,          # the viagogo push sets this once a listing exists
        "etickets": None,
        "source": "email",
        "intake_id": intake_id,
        "note": "",
    }, now_iso)


def cap_state(series_name, event_date_iso, account):
    """Where an account stands against the per-event cap. Used to warn on a
    new emailed purchase that pushes a date over the limit."""
    acct = db.series_canonical_account(account or "")
    owned = sum(p.get("qty") or 0 for p in db.series_purchases_all(series_name)
                if p["event_date_iso"][:10] == (event_date_iso or "")[:10]
                and (p.get("account") or "").lower() == acct)
    return {"account": acct, "qty": owned, "cap": CAP_PER_ACCOUNT,
            "headroom": CAP_PER_ACCOUNT - owned, "over": owned > CAP_PER_ACCOUNT}


def _by_date(blocks):
    out = {}
    for b in blocks:
        d = out.setdefault(b["event_date_iso"], {
            "event_date_iso": b["event_date_iso"], "venue": b["venue"], "qty": 0,
            "total_cost": 0.0, "sold_qty": 0, "proceeds": 0.0, "cost_of_sold": 0.0})
        d["qty"] += b["qty"]
        d["total_cost"] += b["total_cost"]
        d["sold_qty"] += b["sold_qty"]
        d["proceeds"] += b["proceeds"]
        d["cost_of_sold"] += b["cost_of_sold"]
    for d in out.values():
        d["profit"] = round(d["proceeds"] - d["cost_of_sold"], 2)
        d["roi"] = round(d["profit"] / d["cost_of_sold"] * 100, 1) if d["cost_of_sold"] else None
        d["unsold_qty"] = d["qty"] - d["sold_qty"]
        for k in ("total_cost", "proceeds", "cost_of_sold"):
            d[k] = round(d[k], 2)
    return [out[k] for k in sorted(out)]


def _caps(purchases):
    """Per (date, account) ticket count against the 9 cap. This is the number
    the user buys against, so it counts tickets *owned*, sold or not."""
    counts = defaultdict(int)
    for p in purchases:
        counts[(p["event_date_iso"], (p.get("account") or "").lower())] += p.get("qty") or 0
    out = []
    for (d, acct), n in sorted(counts.items()):
        out.append({"event_date_iso": d, "account": acct, "qty": n,
                    "headroom": CAP_PER_ACCOUNT - n,
                    "state": "over" if n > CAP_PER_ACCOUNT else
                             ("full" if n == CAP_PER_ACCOUNT else "ok")})
    return out


def _totals(blocks, purchases):
    qty = sum(b["qty"] for b in blocks)
    cost = sum(b["total_cost"] for b in blocks)
    sold = sum(b["sold_qty"] for b in blocks)
    proceeds = sum(b["proceeds"] for b in blocks)
    cost_of_sold = sum(b["cost_of_sold"] for b in blocks)
    return {
        "tickets_bought": qty,
        "total_spend": round(cost, 2),
        "tickets_sold": sold,
        "tickets_unsold": qty - sold,
        "proceeds": round(proceeds, 2),
        "cost_of_sold": round(cost_of_sold, 2),
        "cost_unsold": round(cost - cost_of_sold, 2),
        "profit": round(proceeds - cost_of_sold, 2),
        "roi": round((proceeds - cost_of_sold) / cost_of_sold * 100, 1) if cost_of_sold else None,
        "net_position": round(proceeds - cost, 2),
        "sell_through": round(sold / qty * 100, 1) if qty else 0.0,
        "accounts": len({(p.get("account") or "").lower() for p in purchases}),
    }
