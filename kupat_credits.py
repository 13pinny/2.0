"""Kupat refund-credit ledger.

User receives ILS-denominated refund credits from Kupat when a USD-purchased
ticket gets refunded. The shekel/USD rate drifts over time, so the real USD
value of an unspent balance moves with it. This module fetches a live FX rate
(cached for the day) and builds a view of credits + spends with USD figures
filled in.
"""

import json
import urllib.request
from datetime import date, datetime, timezone

import db

_FX_URL = "https://open.er-api.com/v6/latest/USD"
_FX_TIMEOUT = 8  # seconds

# In-process cache: {"date": "YYYY-MM-DD", "rate": float, "fetched_at": iso}
_fx_cache = {"date": None, "rate": None, "fetched_at": None}


def _fetch_fx_rate():
    req = urllib.request.Request(_FX_URL, headers={"User-Agent": "kartis-credits/1.0"})
    with urllib.request.urlopen(req, timeout=_FX_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rate = (payload.get("rates") or {}).get("ILS")
    if not rate:
        raise ValueError("FX API response missing ILS rate")
    return float(rate)


def current_fx_rate():
    """Return today's ILS-per-USD rate. Cached per process per day; falls
    back to the last-known rate on network failure."""
    today = date.today().isoformat()
    if _fx_cache["date"] == today and _fx_cache["rate"]:
        return _fx_cache["rate"], _fx_cache["fetched_at"], False
    try:
        rate = _fetch_fx_rate()
        _fx_cache["date"] = today
        _fx_cache["rate"] = rate
        _fx_cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return rate, _fx_cache["fetched_at"], False
    except Exception:
        if _fx_cache["rate"]:
            return _fx_cache["rate"], _fx_cache["fetched_at"], True
        return None, None, True


def build_credits_view():
    """Return {fx_rate, fx_fetched_at, fx_stale, rows: [...]}.

    Each row has the credit fields plus computed: ils_spent, ils_remaining,
    usd_value_today (today's USD value of remaining balance), delta_vs_original
    (today's USD of full ils_amount minus original_usd_cost — positive means
    shekel got stronger since issue).
    """
    rate, fetched_at, stale = current_fx_rate()
    credits = db.all_kupat_credits()
    spends = db.all_kupat_spends()
    by_credit = {}
    for s in spends:
        by_credit.setdefault(s["credit_id"], []).append(s)
    rows = []
    for c in credits:
        cspends = by_credit.get(c["id"], [])
        ils_spent = sum(float(s["ils_amount"] or 0) for s in cspends)
        ils_face = float(c["ils_amount"] or 0)
        ils_remaining = max(0.0, ils_face - ils_spent)
        usd_value_today = (ils_remaining / rate) if rate else None
        usd_face_today = (ils_face / rate) if rate else None
        original_usd = c.get("original_usd_cost")
        delta_vs_original = (
            (usd_face_today - float(original_usd))
            if (usd_face_today is not None and original_usd is not None)
            else None
        )
        # Decorate spends with USD-at-spend (frozen at the rate stored when logged)
        spend_rows = []
        for s in cspends:
            sr = float(s["fx_rate"] or 0)
            usd_at_spend = (float(s["ils_amount"] or 0) / sr) if sr else None
            spend_rows.append({**s, "usd_at_spend": usd_at_spend})
        spend_rows.sort(key=lambda x: (x.get("spend_date") or "", x.get("created_at") or ""))
        rows.append({
            **c,
            "spends": spend_rows,
            "ils_spent": ils_spent,
            "ils_remaining": ils_remaining,
            "usd_value_today": usd_value_today,
            "usd_face_today": usd_face_today,
            "delta_vs_original": delta_vs_original,
        })
    return {
        "fx_rate": rate,
        "fx_fetched_at": fetched_at,
        "fx_stale": stale,
        "rows": rows,
    }
