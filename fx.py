"""ILS -> USD conversion for the viagogo auto-listing pipeline.

Uses a free, no-API-key endpoint (open.er-api.com) and caches the rate in
app_settings for FX_CACHE_TTL_SECONDS so we don't hit the API on every
intake email. Falls back to a stale cached rate (rather than failing) if the
API is unreachable, since a slightly-stale FX rate is far less harmful than
blocking the whole pipeline.
"""
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import db

FX_API_URL = "https://open.er-api.com/v6/latest/ILS"
FX_CACHE_TTL_SECONDS = 24 * 3600
FX_SETTING_KEY = "fx_ils_usd_rate"
REQUEST_TIMEOUT = 15


class FxError(RuntimeError):
    pass


def _fetch_rate():
    req = urllib.request.Request(
        FX_API_URL,
        headers={"User-Agent": "kartis-fx/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rate = (payload.get("rates") or {}).get("USD")
    if not rate:
        raise FxError(f"no USD rate in FX API response: {payload}")
    return float(rate)


def ils_to_usd_rate(now_iso=None, force=False):
    """Returns ILS->USD rate (1 ILS = X USD), using a 24h cache.

    On API failure, falls back to the last cached rate (however stale) rather
    than raising, so a transient FX outage doesn't block ticket intake.
    """
    cached_raw = db.setting_get(FX_SETTING_KEY)
    cached = json.loads(cached_raw) if cached_raw else None
    fresh_needed = force or cached is None or (time.time() - (cached.get("fetched_at") or 0) > FX_CACHE_TTL_SECONDS)
    if not fresh_needed:
        return cached["rate"]
    try:
        rate = _fetch_rate()
    except Exception:
        if cached is not None:
            return cached["rate"]
        raise
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    db.setting_set(FX_SETTING_KEY, json.dumps({"rate": rate, "fetched_at": time.time()}), now_iso)
    return rate


def ils_to_usd(amount_ils, now_iso=None):
    if amount_ils is None:
        return None
    rate = ils_to_usd_rate(now_iso=now_iso)
    return round(amount_ils * rate, 2)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    r = ils_to_usd_rate(force=True)
    print(f"1 ILS = {r:.5f} USD")
    print(f"₪100 = ${ils_to_usd(100):.2f}")
