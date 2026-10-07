"""/trends - sales analytics for any event, or a whole tracked series.

Everything here is computed from the rows `app._build_combined_sales()` already
produces (the same rows /sales and /profit read), so the numbers tie out with
those pages: revenue is gross `sale_price`, profit is PAYOUT - cost, ROI is
profit / cost. This module owns no table and does no I/O beyond what the
caller hands it; app.py gathers the inputs.

The central axis is DAYS OUT = event date - sale date, in whole days. A sale on
the day of the show is 0. Rows missing either date land in an "unknown" bucket
and are left out of every days-out figure (but still count in the totals), so
a scraper that failed to parse a date can't drag the curve toward zero.

Series mode (`series=NEXT`): event NAMES are useless for NEXT (viagogo spells
the run five ways), so a sale belongs to the series when its event DATE is one
of the series dates and its venue agrees - the same identity series.py uses.
Its cost is then re-priced from the purchased block on /series (date, section,
row), which is the authoritative cost book for that run; the combined-sales
cost for a viagogo NEXT sale is otherwise just the listing's face value. A
sale that matches no block is priced at that date's average unit cost and
flagged `cost_est`.
"""

from datetime import date
from statistics import median

# Inclusive (lo, hi) day ranges. Fine at the short end, where the decisions
# (hold one more day or dump?) actually get made.
BUCKETS = [
    ("Day of", 0, 0),
    ("1 day", 1, 1),
    ("2-3 days", 2, 3),
    ("4-7 days", 4, 7),
    ("8-14 days", 8, 14),
    ("15-30 days", 15, 30),
    ("31-60 days", 31, 60),
    ("61+ days", 61, 10 ** 6),
]
UNKNOWN = "Unknown"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _d(iso):
    try:
        return date.fromisoformat((iso or "")[:10])
    except (TypeError, ValueError):
        return None


def days_out(sale):
    ev, sd = _d(sale.get("event_date_iso")), _d(sale.get("sale_date_iso"))
    if not ev or not sd:
        return None
    n = (ev - sd).days
    # A sale dated after the show is a scrape artefact (a payout date read as
    # the sale date, a rescheduled show); never let it pose as "day of".
    return n if n >= 0 else None


def bucket_of(n):
    if n is None:
        return UNKNOWN
    for label, lo, hi in BUCKETS:
        if lo <= n <= hi:
            return label
    return UNKNOWN


def _payout(s):
    p = s.get("payout")
    return (p if p is not None else s.get("sale_price")) or 0


def _blank():
    return {"orders": 0, "qty": 0, "revenue": 0.0, "payout": 0.0, "cost": 0.0,
            "costed_qty": 0, "costed_payout": 0.0, "costed_cost": 0.0}


def _add(b, s):
    qty = s.get("qty") or 0
    pay = _payout(s)
    cost = s.get("cost") or 0
    b["orders"] += 1
    b["qty"] += qty
    b["revenue"] += s.get("sale_price") or 0
    b["payout"] += pay
    b["cost"] += cost
    # ROI is only meaningful over rows whose cost we actually know: a $0 cost
    # is "never entered", not "free", and would read as infinite ROI.
    if cost > 0:
        b["costed_qty"] += qty
        b["costed_payout"] += pay
        b["costed_cost"] += cost


def _finish(b):
    qty = b["qty"]
    out = {
        "orders": b["orders"],
        "qty": qty,
        "revenue": round(b["revenue"], 2),
        "payout": round(b["payout"], 2),
        "cost": round(b["cost"], 2),
        "profit": round(b["payout"] - b["cost"], 2),
        "avg_payout": round(b["payout"] / qty, 2) if qty else None,
        "avg_cost": round(b["costed_cost"] / b["costed_qty"], 2) if b["costed_qty"] else None,
        "costed_qty": b["costed_qty"],
        "uncosted_qty": qty - b["costed_qty"],
    }
    cc = b["costed_cost"]
    out["costed_profit"] = round(b["costed_payout"] - cc, 2)
    out["roi"] = round((b["costed_payout"] - cc) / cc * 100, 1) if cc else None
    out["profit_per_ticket"] = (round(out["costed_profit"] / b["costed_qty"], 2)
                                if b["costed_qty"] else None)
    return out


def _group(rows, keyfn):
    acc = {}
    for s in rows:
        k = keyfn(s)
        _add(acc.setdefault(k, _blank()), s)
    return {k: _finish(v) for k, v in acc.items()}


# ------------------------------------------------------------- selection ---

def parse_terms(q):
    """Keyword box -> (include, exclude) lists of word-lists.

    Commas separate alternatives (ANY may match); the words inside one
    alternative must ALL appear, in any order, so "next rita" finds
    "NEXT with ... and Rita". A leading "-" excludes: "fisher, -vip"."""
    inc, exc = [], []
    for raw in (q or "").split(","):
        t = raw.strip().lower()
        if not t:
            continue
        neg = t.startswith("-")
        words = t.lstrip("-").split()
        if words:
            (exc if neg else inc).append(words)
    return inc, exc


def _hay(s):
    return f"{s.get('event_name') or ''} | {s.get('venue') or ''}".lower()


def in_dates(s, date_from=None, date_to=None):
    """Show date inside [from, to] (inclusive ISO dates). A row with no
    show date never matches once a range is set."""
    if not date_from and not date_to:
        return True
    d = (s.get("event_date_iso") or "")[:10]
    if not d:
        return False
    return (not date_from or d >= date_from) and (not date_to or d <= date_to)


def select(sales, q="", groups=(), date_from=None, date_to=None):
    """The multi-event selection.

    Picked shows (event-group keys) and keyword alternatives are UNIONED -
    "these three shows plus anything matching 'fisher'"; with neither, every
    sale is a candidate. Exclusions and the show-date range then apply to the
    whole result."""
    inc, exc = parse_terms(q)
    groups = set(groups or ())
    out = []
    for s in sales:
        hay = _hay(s)
        if groups or inc:
            hit = (s.get("event_group") in groups
                   or any(all(w in hay for w in words) for words in inc))
            if not hit:
                continue
        if any(all(w in hay for w in words) for words in exc):
            continue
        if not in_dates(s, date_from, date_to):
            continue
        out.append(s)
    return out


def match_query(sales, q):
    return select(sales, q)


def _venue_agrees(a, b):
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return True
    return a in b or b in a or a.split()[0] == b.split()[0]


def recost_series(sales, series_data, sale_key):
    """Give every sale in `sales` that belongs to the series its /series
    block cost, in place, WITHOUT renaming it - so the "All sales" and name
    views price NEXT tickets the same way the series view does."""
    by_id = {(r.get("source"), r.get("sale_id")): r
             for r in apply_series(sales, series_data, sale_key)}
    for s in sales:
        hit = by_id.get((s.get("source"), s.get("sale_id")))
        if hit:
            s["cost"] = hit.get("cost")
            s["cost_est"] = hit.get("cost_est", False)


def apply_series(sales, series_data, sale_key):
    """Select + re-cost the sales belonging to a /series run.

    series_data is series.build(); sale_key(sale) -> (date, section_key, row)
    with the series section/row normalisation applied."""
    blocks = series_data.get("blocks") or []
    venue_by_date = {}
    unit_by_key = {}
    date_cost = {}
    for b in blocks:
        d = b["event_date_iso"]
        venue_by_date.setdefault(d, b.get("venue") or "")
        unit_by_key[(d, b["section_key"], b["row_key"])] = b["unit_cost"]
        acc = date_cost.setdefault(d, [0.0, 0])
        acc[0] += b["total_cost"] or 0
        acc[1] += b["qty"] or 0
    out = []
    for s in sales:
        d = (s.get("event_date_iso") or "")[:10]
        if d not in venue_by_date or not _venue_agrees(s.get("venue"), venue_by_date[d]):
            continue
        r = dict(s)
        # viagogo spells the run several ways; one date = one show here.
        r["event_name"] = series_data.get("series") or r.get("event_name")
        r["event_group"] = f"series:{r['event_name']}|{d}"
        qty = r.get("qty") or 0
        unit = unit_by_key.get(sale_key(r))
        if unit:
            r["cost"] = round(unit * qty, 2)
            r["cost_est"] = False
        else:
            tot, n = date_cost.get(d, (0, 0))
            if n and not (r.get("cost") or 0):
                r["cost"] = round(tot / n * qty, 2)
                r["cost_est"] = True
        out.append(r)
    return out


# ---------------------------------------------------------------- the build ---

def build(sales, today=None, holdings=None, unsold=None, title=""):
    """The /trends payload.

    sales     combined-sale rows already filtered to the selection
    holdings  [{event_date_iso, venue, qty, cost}] still owned and unsold
              (series mode: /series blocks; else empty)
    unsold    [{event_name, event_date_iso, qty, cost}] the "didn't sell"
              archive rows for the selection - a realised loss
    """
    today = today or date.today()
    holdings = holdings or []
    unsold = unsold or []

    for s in sales:
        s["_days_out"] = days_out(s)
        s["_bucket"] = bucket_of(s["_days_out"])

    totals_b = _blank()
    for s in sales:
        _add(totals_b, s)
    totals = _finish(totals_b)
    hold_qty = sum(h.get("qty") or 0 for h in holdings)
    hold_cost = sum(h.get("cost") or 0 for h in holdings)
    unsold_qty = sum(u.get("qty") or 0 for u in unsold)
    unsold_cost = sum(u.get("cost") or 0 for u in unsold)
    totals.update({
        "events": len({_event_id(s) for s in sales}),
        "holding_qty": hold_qty,
        "holding_cost": round(hold_cost, 2),
        "unsold_qty": unsold_qty,
        "unsold_cost": round(unsold_cost, 2),
        # Realised P&L: the sold tickets' profit minus what was eaten on
        # tickets that never sold. Open holdings are not a loss yet.
        "net_profit": round(totals["profit"] - unsold_cost, 2),
        "sell_through": (round(totals["qty"] / (totals["qty"] + hold_qty + unsold_qty) * 100, 1)
                         if (totals["qty"] + hold_qty + unsold_qty) else None),
        "cost_est_qty": sum(s.get("qty") or 0 for s in sales if s.get("cost_est")),
    })

    known = [s for s in sales if s["_days_out"] is not None]
    weights = []
    for s in known:
        weights.extend([s["_days_out"]] * max(int(s.get("qty") or 0), 1))
    totals["median_days_out"] = median(weights) if weights else None
    totals["avg_days_out"] = round(sum(weights) / len(weights), 1) if weights else None
    totals["unknown_days_qty"] = sum(s.get("qty") or 0 for s in sales if s["_days_out"] is None)

    # --- days-out buckets ---------------------------------------------------
    by_b = _group(sales, lambda s: s["_bucket"])
    buckets = []
    for label, lo, hi in BUCKETS + [(UNKNOWN, None, None)]:
        row = by_b.get(label)
        if not row and label == UNKNOWN:
            continue
        row = row or _finish(_blank())
        row.update({"label": label, "lo": lo, "hi": hi if hi != 10 ** 6 else None})
        row["share"] = round(row["qty"] / totals["qty"] * 100, 1) if totals["qty"] else 0
        buckets.append(row)

    # --- cumulative curve: % of tickets sold by N days out --------------------
    # Walk from far out toward the show; y = share of (known-date) tickets that
    # had sold by the time we were N days out.
    curve = []
    known_qty = sum(s.get("qty") or 0 for s in known)
    if known:
        horizon = max(s["_days_out"] for s in known)
        per_day = {}
        for s in known:
            per_day[s["_days_out"]] = per_day.get(s["_days_out"], 0) + (s.get("qty") or 0)
        cum = 0
        for n in range(horizon, -1, -1):
            cum += per_day.get(n, 0)
            curve.append({"days_out": n, "sold": cum,
                          "pct": round(cum / known_qty * 100, 1) if known_qty else 0})
        curve.reverse()

    # --- one point per sale for the price-vs-days-out scatter -----------------
    points = []
    for s in known:
        qty = s.get("qty") or 0
        if not qty:
            continue
        cost = s.get("cost") or 0
        points.append({
            "x": s["_days_out"],
            "y": round(_payout(s) / qty, 2),
            "cost": round(cost / qty, 2) if cost else None,
            "qty": qty,
            "event": s.get("event_name") or "",
            "event_date_iso": (s.get("event_date_iso") or "")[:10],
            "section": s.get("section") or "",
            "platform": s.get("platform") or s.get("source") or "",
            "sale_date_iso": s.get("sale_date_iso") or "",
            "series_key": _event_id(s),
        })

    # --- per event (one show / one date) --------------------------------------
    ev_rows = {}
    for s in sales:
        k = _event_id(s)
        e = ev_rows.setdefault(k, {"key": k, "event_name": s.get("event_name") or "",
                                   "event_date_iso": (s.get("event_date_iso") or "")[:10],
                                   "venue": s.get("venue") or "", "_b": _blank(), "_days": []})
        _add(e["_b"], s)
        if s["_days_out"] is not None:
            e["_days"].extend([s["_days_out"]] * max(int(s.get("qty") or 0), 1))
    hold_by_date = {}
    for h in holdings:
        d = (h.get("event_date_iso") or "")[:10]
        x = hold_by_date.setdefault(d, [0, 0.0])
        x[0] += h.get("qty") or 0
        x[1] += h.get("cost") or 0
    events = []
    for e in ev_rows.values():
        row = _finish(e.pop("_b"))
        days = e.pop("_days")
        row.update(e)
        row["median_days_out"] = median(days) if days else None
        row["first_days_out"] = max(days) if days else None
        row["last_days_out"] = min(days) if days else None
        evd = _d(row["event_date_iso"])
        row["days_until"] = (evd - today).days if evd else None
        hq, hc = hold_by_date.pop(row["event_date_iso"], (0, 0.0)) if holdings else (0, 0.0)
        row["holding_qty"] = hq
        row["holding_cost"] = round(hc, 2)
        events.append(row)
    # Series dates with holdings but no sale yet still deserve a row.
    for d, (hq, hc) in hold_by_date.items():
        evd = _d(d)
        row = _finish(_blank())
        row.update({"key": f"|{d}|", "event_name": title or "", "event_date_iso": d,
                    "venue": next((h.get("venue") for h in holdings
                                   if (h.get("event_date_iso") or "")[:10] == d), ""),
                    "median_days_out": None, "first_days_out": None, "last_days_out": None,
                    "days_until": (evd - today).days if evd else None,
                    "holding_qty": hq, "holding_cost": round(hc, 2)})
        events.append(row)
    events.sort(key=lambda r: (r["event_date_iso"] or "9999", r["event_name"]))

    # --- simple breakdowns ----------------------------------------------------
    def ranked(groups, label_key):
        rows = []
        for k, v in groups.items():
            v[label_key] = k or "(blank)"
            rows.append(v)
        return sorted(rows, key=lambda r: -r["qty"])

    by_platform = ranked(_group(sales, lambda s: (s.get("platform") or s.get("source") or "").lower()), "platform")
    by_section = ranked(_group(sales, lambda s: _section_label(s)), "section")
    by_weekday = []
    wk = _group([s for s in sales if _d(s.get("sale_date_iso"))],
                lambda s: _d(s.get("sale_date_iso")).weekday())
    for i, name in enumerate(WEEKDAYS):
        row = wk.get(i) or _finish(_blank())
        row["weekday"] = name
        by_weekday.append(row)
    ev_wk = _group([s for s in sales if _d(s.get("event_date_iso"))],
                   lambda s: _d(s.get("event_date_iso")).weekday())
    by_event_weekday = []
    for i, name in enumerate(WEEKDAYS):
        row = ev_wk.get(i) or _finish(_blank())
        row["weekday"] = name
        by_event_weekday.append(row)

    # --- timeline: tickets sold per calendar day ------------------------------
    tl = _group([s for s in sales if _d(s.get("sale_date_iso"))],
                lambda s: (s.get("sale_date_iso") or "")[:10])
    timeline = [dict(v, date=k) for k, v in sorted(tl.items())]

    # --- open holdings, by days until the show --------------------------------
    holding_rows = []
    for h in holdings:
        evd = _d(h.get("event_date_iso"))
        holding_rows.append(dict(h, days_until=(evd - today).days if evd else None))
    holding_rows.sort(key=lambda h: (h["days_until"] is None, h["days_until"] or 0))

    payload = {
        "title": title,
        "today": today.isoformat(),
        "totals": totals,
        "buckets": buckets,
        "curve": curve,
        "points": points,
        "events": events,
        "by_platform": by_platform,
        "by_section": by_section[:25],
        "by_weekday": by_weekday,
        "by_event_weekday": by_event_weekday,
        "timeline": timeline,
        "holdings": holding_rows,
        "unsold": unsold,
        "sales": [_sale_out(s) for s in sorted(
            sales, key=lambda s: (s.get("sale_date_iso") or ""), reverse=True)],
    }
    payload["insights"] = insights(payload)
    return payload


def _event_id(s):
    return s.get("event_group") or f"{(s.get('event_name') or '').lower()}|{(s.get('event_date_iso') or '')[:10]}"


def _section_label(s):
    sec = (s.get("section") or "").strip()
    return sec.splitlines()[0].strip() if sec else ""


def _sale_out(s):
    qty = s.get("qty") or 0
    pay = _payout(s)
    cost = s.get("cost") or 0
    return {
        "source": s.get("source"), "sale_id": s.get("sale_id"),
        "sale_date_iso": s.get("sale_date_iso") or "",
        "event_name": s.get("event_name") or "",
        "event_date_iso": (s.get("event_date_iso") or "")[:10],
        "venue": s.get("venue") or "",
        "section": _section_label(s), "row": s.get("row") or "",
        "platform": s.get("platform") or s.get("source") or "",
        "qty": qty, "payout": round(pay, 2), "cost": round(cost, 2),
        "per_ticket": round(pay / qty, 2) if qty else None,
        "profit": round(pay - cost, 2),
        "roi": round((pay - cost) / cost * 100, 1) if cost > 0 else None,
        "days_out": s.get("_days_out"),
        "cost_est": bool(s.get("cost_est")),
    }


# -------------------------------------------------------------- insights ---

def _money(x):
    return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"


def insights(p):
    """A handful of plain-English takeaways. Each one only fires when there
    is enough data behind it to mean something - a "best bucket" built on two
    sales is noise, so buckets need MIN_QTY costed tickets to compete."""
    MIN_QTY = 4
    out = []
    t = p["totals"]
    if not t["qty"]:
        return out

    if t.get("median_days_out") is not None:
        out.append(f"Half of your tickets sold within <b>{_fmt_days(t['median_days_out'])}</b> "
                   f"of the show (average {t['avg_days_out']} days out).")

    late = sum(b["qty"] for b in p["buckets"] if b["label"] in ("Day of", "1 day", "2-3 days"))
    known = sum(b["qty"] for b in p["buckets"] if b["label"] != UNKNOWN)
    if known:
        out.append(f"<b>{late / known * 100:.0f}%</b> of tickets sold in the final 3 days "
                   f"({late} of {known}).")

    ranked = [b for b in p["buckets"]
              if b["label"] != UNKNOWN and b["roi"] is not None and b["costed_qty"] >= MIN_QTY]
    if len(ranked) >= 2:
        best = max(ranked, key=lambda b: b["roi"])
        worst = min(ranked, key=lambda b: b["roi"])
        out.append(f"Best ROI window: <b>{best['label']}</b> out at <b>{best['roi']:+.0f}%</b> "
                   f"({best['costed_qty']} tickets, {_money(best['profit_per_ticket'])}/ticket). "
                   f"Worst: {worst['label']} at {worst['roi']:+.0f}%.")

    # Late vs early price: did holding pay?
    early = [x for x in p["points"] if x["x"] >= 8]
    lastw = [x for x in p["points"] if x["x"] <= 3]
    if sum(x["qty"] for x in early) >= MIN_QTY and sum(x["qty"] for x in lastw) >= MIN_QTY:
        ea = sum(x["y"] * x["qty"] for x in early) / sum(x["qty"] for x in early)
        la = sum(x["y"] * x["qty"] for x in lastw) / sum(x["qty"] for x in lastw)
        diff = (la - ea) / ea * 100 if ea else 0
        word = "higher" if diff >= 0 else "lower"
        out.append(f"Tickets sold in the last 3 days paid <b>{_money(la)}</b> on average vs "
                   f"<b>{_money(ea)}</b> for sales 8+ days out - <b>{abs(diff):.0f}% {word}</b>.")

    evs = [e for e in p["events"] if e["roi"] is not None and e["costed_qty"] >= MIN_QTY]
    if len(evs) >= 2:
        best = max(evs, key=lambda e: e["roi"])
        worst = min(evs, key=lambda e: e["roi"])
        out.append(f"Strongest show: <b>{_ev_label(best)}</b> at {best['roi']:+.0f}% ROI; "
                   f"weakest: {_ev_label(worst)} at {worst['roi']:+.0f}%.")

    secs = [s for s in p["by_section"] if s["roi"] is not None and s["costed_qty"] >= MIN_QTY]
    if len(secs) >= 2:
        best = max(secs, key=lambda s: s["roi"])
        out.append(f"Best section: <b>{best['section']}</b> at {best['roi']:+.0f}% ROI "
                   f"over {best['costed_qty']} tickets.")

    plats = [s for s in p["by_platform"] if s["avg_payout"] is not None and s["qty"] >= MIN_QTY]
    if len(plats) >= 2:
        best = max(plats, key=lambda s: s["profit_per_ticket"] or -1e9)
        if best["profit_per_ticket"] is not None:
            out.append(f"<b>{best['platform']}</b> made the most per ticket "
                       f"({_money(best['profit_per_ticket'])} profit/ticket).")

    if t["holding_qty"]:
        soon = [h for h in p["holdings"] if h.get("days_until") is not None
                and 0 <= h["days_until"] <= 7 and h.get("qty")]
        msg = (f"Still holding <b>{t['holding_qty']}</b> tickets "
               f"({_money(t['holding_cost'])} cost).")
        if soon:
            msg += (f" {sum(h['qty'] for h in soon)} of them are for shows in the next 7 days.")
        out.append(msg)

    if t["uncosted_qty"]:
        out.append(f"{t['uncosted_qty']} sold tickets have no cost recorded - they're left out "
                   f"of ROI (enter costs on /sales to include them).")
    if t.get("cost_est_qty"):
        out.append(f"{t['cost_est_qty']} tickets are costed at their date's average "
                   f"(no exact /series block match).")
    return out


def _fmt_days(n):
    if n == 0:
        return "the day of"
    return f"{n:g} day" + ("" if n == 1 else "s")


def _ev_label(e):
    d = e.get("event_date_iso") or ""
    name = e.get("event_name") or ""
    return f"{name} {d}".strip()
