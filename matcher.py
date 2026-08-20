"""Cross-source sale → inventory matcher.

When a sale lands on Lysted/Viagogo/CrowdVolt, the source platform already
removes it from its own inventory page. But the same physical ticket might
*also* exist as a JeruJam archive entry that the user never manually marked
sold. Without help, the unified inventory keeps showing the JeruJam row as
"unsold" forever.

This module pairs each cross-source sale with the most likely JeruJam
inventory row by event + section + row + qty, records the match in
`inventory_matches`, and adds the inventory row to `inventory_hidden` so it
disappears from the dashboard.

Notes:
- JeruJam-internal sales are skipped here — `_build_unified_inventory` already
  decrements remaining qty using the ticket's own `sales[]` array.
- A single JeruJam row only matches *once* per matcher pass. If the user has
  two distinct JeruJam entries for the same seats, both can match different
  sales.
- If a user manually undeletes (Ctrl+Z) an auto-matched row, the unhide
  endpoint adds the sale to `match_blocklist` so we don't keep re-matching it.
"""
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

import db


def _norm(s):
    return (s or "").strip().lower()


def _norm_event(s):
    """Lowercase, strip punctuation, collapse spaces — for fuzzy event matching.
    Handles minor variants like 'Wit' vs 'With' or 'SIDEPIECE' vs 'Sidepiece'."""
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _events_match(a, b, threshold=0.85):
    a, b = _norm_event(a), _norm_event(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _date_only(iso):
    if not iso:
        return ""
    return iso[:10]


def _candidate_jerujam_inventory(hidden_keys, sold_per_ticket, matched_qty=None):
    """Return JeruJam tickets that still have unsold qty and aren't hidden."""
    matched_qty = matched_qty or {}
    out = []
    for t in db.all_jerujam_tickets():
        status = (t.get("status") or "").strip().lower()
        if status == "sold":
            continue
        qty = t.get("quantity") or 0
        sold = sold_per_ticket.get(t.get("id"), 0)
        ext = matched_qty.get(("jerujam", str(t.get("id"))), 0)
        remaining = max(0, qty - sold - ext)
        if remaining <= 0:
            continue
        if ("jerujam", str(t.get("id"))) in hidden_keys:
            continue
        out.append({
            "source": "jerujam",
            "source_id": str(t.get("id")),
            "event_name": t.get("event_name") or "",
            "event_date_iso": _date_only(t.get("event_date_iso")),
            "section": t.get("section") or "",
            "row": t.get("row_label") or "",
            "seats": t.get("seat_numbers") or "",
            "qty": remaining,
        })
    return out


def _candidate_manual_inventory(hidden_keys, matched_qty):
    """Return manual_inventory rows still pending and unmatched.

    Skip rows already tied to a Lysted/Viagogo listing via run_pending_match
    (matched_source set) — the listing row owns the decrement. Skip rows hidden
    via inventory_hidden under ("manual", id).
    """
    out = []
    for m in db.all_manual_inventory():
        if m.get("matched_source"):
            continue
        mid = str(m.get("id"))
        if ("manual", mid) in hidden_keys:
            continue
        qty = m.get("qty") or 0
        consumed = matched_qty.get(("manual", mid), 0)
        remaining = max(0, qty - consumed)
        if remaining <= 0:
            continue
        out.append({
            "source": "manual",
            "source_id": mid,
            "event_name": m.get("event_name") or "",
            "event_date_iso": _date_only(m.get("event_date_iso")),
            "section": m.get("section") or "",
            "row": m.get("row_label") or "",
            "seats": m.get("seats") or "",
            "qty": remaining,
        })
    return out


def _collect_external_sales():
    """Sales from Lysted/Viagogo/CrowdVolt — the ones that might pair with a
    JeruJam archive row. We skip JeruJam's own sales (handled elsewhere)."""
    out = []
    for s in db.all_lysted_sales():
        out.append({
            "source": "lysted",
            "id": str(s.get("id")),
            "event_name": s.get("event_name") or "",
            "event_date_iso": _date_only(s.get("event_date_iso")),
            "section": s.get("section") or "",
            "row": s.get("row_label") or "",
            "seats": s.get("seats") or "",
            "qty": s.get("qty") or 0,
            "sale_date": s.get("sale_date_iso") or "",
        })
    for s in db.all_viagogo_sales():
        out.append({
            "source": "viagogo",
            "id": str(s.get("id")),
            "event_name": s.get("event_name") or "",
            "event_date_iso": _date_only(s.get("event_date_iso")),
            "section": s.get("section") or "",
            "row": s.get("row_label") or "",
            "seats": s.get("seats") or "",
            "qty": s.get("qty") or 0,
            "sale_date": s.get("sale_date_iso") or "",
        })
    for s in db.all_crowdvolt_sales():
        # CrowdVolt has no section field — its `ticket_type` plays that role
        # (e.g. "GA (Anytime Entry)"). Match against JeruJam section, which
        # for GA-style tickets typically also reads "GA"/"General Admission".
        out.append({
            "source": "crowdvolt",
            "id": str(s.get("id")),
            "event_name": s.get("event_name") or "",
            "event_date_iso": _date_only(s.get("event_date_iso")),
            "section": s.get("ticket_type") or "",
            "row": "",
            "seats": "",
            "qty": s.get("qty") or 0,
            "sale_date": s.get("sale_date_iso") or "",
        })
    return out


def _ga_like(text):
    """Fuzzy match for GA-style sections regardless of label variants.

    Matches "GA", "PIT", "GA PIT", "GA Lower Level", "General Admission", etc.
    The user explicitly confirmed GA == GA PIT == PIT for ticket-resale purposes.
    """
    t = _norm(text)
    if not t:
        return False
    if "general admission" in t:
        return True
    tokens = t.replace("/", " ").split()
    return "ga" in tokens or "pit" in tokens


def _score(sale, inv):
    """Return (score, reason). Score 0 means hard mismatch."""
    if not _events_match(sale["event_name"], inv["event_name"]):
        return 0, ""
    exact = _norm_event(sale["event_name"]) == _norm_event(inv["event_name"])
    score = 100 if exact else 90  # tiny penalty for fuzzy event match
    reasons = ["event" if exact else "event~"]
    if sale["event_date_iso"] and inv["event_date_iso"]:
        if sale["event_date_iso"] == inv["event_date_iso"]:
            score += 50
            reasons.append("date")
        else:
            return 0, ""  # different dates → not a match
    sec_s, sec_i = _norm(sale["section"]), _norm(inv["section"])
    if sec_s and sec_i:
        if sec_s == sec_i:
            score += 30
            reasons.append("section")
        elif _ga_like(sec_s) and _ga_like(sec_i):
            score += 20
            reasons.append("section~ga")
        else:
            return 0, ""  # different sections → not a match
    elif sec_s or sec_i:
        # one side knows section, the other doesn't — partial credit
        score += 5
    row_s, row_i = _norm(sale["row"]), _norm(inv["row"])
    if row_s and row_i:
        if row_s == row_i:
            score += 20
            reasons.append("row")
        else:
            return 0, ""  # different rows → not a match
    if sale["qty"] and inv["qty"] and sale["qty"] == inv["qty"]:
        score += 10
        reasons.append("qty")
    return score, "+".join(reasons)


MIN_SCORE = 130  # event (100) + section (30)


def run_match_pass():
    """One pass over external sales. Each unmatched sale is paired with the
    best-scoring inventory candidate (JeruJam first, then unlisted manual rows).
    Returns (n_matched, n_skipped).
    """
    db.init()
    all_match_rows = db.all_matches()
    already_matched = {(m["sale_source"], m["sale_id"]) for m in all_match_rows}
    # Pre-existing per-inventory consumption from prior matches (e.g. previous
    # passes, manual sales, manual pair-with-sale). Lets candidate builders
    # report the *remaining* qty so we don't double-consume.
    consumed_inv = {}
    for m in all_match_rows:
        key = (m.get("inv_source"), str(m.get("inv_source_id")))
        consumed_inv[key] = consumed_inv.get(key, 0) + (m.get("qty_matched") or 0)
    blocked = db.all_blocklist_keys()

    # Pre-compute JeruJam sold-per-ticket for the candidate pool
    sold_per = {}
    for s in db.all_jerujam_sales():
        sold_per[s["ticket_id"]] = sold_per.get(s["ticket_id"], 0) + (s.get("quantity") or 0)
    hidden = db.all_hidden_keys()
    candidates = _candidate_jerujam_inventory(hidden, sold_per, consumed_inv)
    candidates += _candidate_manual_inventory(hidden, consumed_inv)

    sales = _collect_external_sales()
    sales = [s for s in sales if (s["source"], s["id"]) not in already_matched
             and (s["source"], s["id"]) not in blocked]

    # Sort: most specific sales first (with row+seats), then by recency,
    # so they consume the more specific candidates before the GA-style ones.
    sales.sort(key=lambda s: (
        -(1 if s["row"] else 0),
        -(1 if s["seats"] else 0),
        s["sale_date"] or "",
    ), reverse=True)

    # Track running consumption keyed by (source, source_id) so a manual UUID
    # can't collide with a JeruJam id. Multiple sales can pair to the same
    # inventory row (e.g. one entry of qty 14 absorbing several smaller sales).
    consumed = {}
    matched_pairs = []
    for sale in sales:
        best = None
        sale_qty = sale.get("qty") or 0
        for inv in candidates:
            inv_key = (inv["source"], inv["source_id"])
            remaining = (inv["qty"] or 0) - consumed.get(inv_key, 0)
            if remaining <= 0:
                continue
            # Don't over-consume: a 4-qty sale can't absorb a 2-qty inventory row.
            if sale_qty > remaining:
                continue
            sc, reason = _score(sale, inv)
            if sc < MIN_SCORE:
                continue
            if best is None or sc > best[1]:
                best = (inv, sc, reason)
        if best is None:
            continue
        inv, sc, reason = best
        inv_key = (inv["source"], inv["source_id"])
        consumed[inv_key] = consumed.get(inv_key, 0) + sale_qty
        matched_pairs.append((sale, inv, reason))

    now_iso = datetime.now(timezone.utc).isoformat()
    for sale, inv, reason in matched_pairs:
        inv_key = (inv["source"], inv["source_id"])
        db.record_match(
            sale_source=sale["source"], sale_id=sale["id"],
            inv_source=inv["source"], inv_source_id=inv["source_id"],
            qty=sale["qty"], reason=reason, now_iso=now_iso,
        )
        # Only fully hide when remaining qty hits zero — partial sells leave
        # the inventory row visible with a reduced count (handled in
        # _build_unified_inventory).
        if consumed[inv_key] >= (inv["qty"] or 0):
            db.hide_inventory(inv["source"], inv["source_id"], now_iso)
    return len(matched_pairs), len(sales) - len(matched_pairs)


def _pending_section_compat(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return True  # one side missing — give the benefit of the doubt
    if a == b:
        return True
    # GA-equivalent helpers (mirror app._ga_like)
    def _ga(t):
        if "general admission" in t:
            return True
        toks = t.replace("/", " ").split()
        return "ga" in toks or "pit" in toks
    return _ga(a) and _ga(b)


def run_pending_match():
    """For each pending manual inventory entry, see if a Lysted purchase or
    Viagogo listing has appeared that matches. If so, mark the manual as
    listed so it falls off the pending list."""
    db.init()
    pending = [m for m in db.all_manual_inventory() if not m.get("matched_source")]
    if not pending:
        return 0
    lysted = [r for r in db.all_lysted_purchases()
              if (r.get("status") or "").strip().lower() != "sold"]
    viagogo = [r for r in db.all_viagogo() if (r.get("available") or 0) > 0]
    now_iso = datetime.now(timezone.utc).isoformat()
    n = 0
    for m in pending:
        ev = m.get("event_name") or ""
        sec = m.get("section") or ""
        row = m.get("row_label") or ""
        qty = m.get("qty") or 0
        # Try Lysted purchases (event + section + row)
        match = None
        for lp in lysted:
            if not _events_match(lp.get("event_name"), ev):
                continue
            if not _pending_section_compat(lp.get("section"), sec):
                continue
            if row and lp.get("row_label") and _norm(lp.get("row_label")) != _norm(row):
                continue
            match = ("lysted", str(lp.get("id")))
            break
        if not match:
            for vl in viagogo:
                if not _events_match(vl.get("event_name"), ev):
                    continue
                v_sec = (vl.get("section") or "").splitlines()[0] if vl.get("section") else ""
                if not _pending_section_compat(v_sec, sec):
                    continue
                match = ("viagogo", str(vl.get("id")))
                break
        if match:
            db.mark_manual_inventory_listed(m["id"], match[0], match[1], now_iso)
            n += 1
    return n


if __name__ == "__main__":
    # Pending-match runs first so manual rows already linked to a Lysted/Viagogo
    # listing get flagged before the auto-matcher considers them as candidates.
    moved = run_pending_match()
    print(f"pending → listed: {moved}")
    matched, skipped = run_match_pass()
    print(f"matched: {matched} · unmatched: {skipped}")
