"""Pacha "final sold" guesstimate — how many tickets a show will have sold
online by show night, with a range, from how past Pacha shows sold.

Two pieces, both built only from `pacha_release_log` (one row per release,
first sighting → sell-out/removal, with that release's own `used` count):

1. sold_so_far(releases) — Σ each release's `used_last`. This is the
   trustworthy total. `pacha_seen_events.sold_cum` (availability drops
   between ticks) is NOT: a release pulled with stock left, or a tier that
   flaps out of the feed for a tick, reads as sales — Vintage Culture
   finished at 5,556 "sold" against 3,128 across its releases. One
   correction: a numbered "... Release" rung that vanished while the monitor
   was blind (last_seen → sold_out gap > OUTAGE_GAP_HOURS; the Aug 20-23
   2026 outage) is counted as sold through — the next rung replaced it. A
   non-rung tier that vanished in a gap (an Early Entry pulled) is not.

2. estimate(...) — reference curves. For every FINISHED show, rebuild
   "sold by N days out" by spreading each release's sales evenly over the
   time it was listed, and take frac(d) = sold(d days out) / final. An
   upcoming show d days out with S sold projects to S / frac(d) for each
   reference; the median is the estimate. The RANGE comes from a
   leave-one-out backtest over the finished shows: how far the same method
   missed at that many days out (10th-90th percentile of its misses,
   interpolated between horizons, so ~80% of past shows landed inside), and
   never below what's already sold. As of 2026-11-02 it was unbiased at
   every horizon and missed by a median ±1% a day out, ±10% a week out,
   ±17% at two weeks, ±35-40% at 30-60 days. References are whatever
   past shows we have (all outdoor Pacha New York so far, so for the indoor
   Brooklyn Pacha room it's an assumption that demand curves look alike).

Caveats worth carrying to the page: counts are ONLINE FourVenues sales only
(no door, comps or tables); the even-spread within a release is an
approximation (a long "parking" release sells faster near the show); shows
first seen before release logging began (2026-07-13) are only used as a
reference for days-out after logging started.

CLI:  python pacha_estimate.py [--db kartis.db] [--backtest] [--json]
"""
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

OUTAGE_GAP_HOURS = 1.0
MIN_REFERENCES = 4          # fewer valid reference shows → no estimate
LOW_Q, HIGH_Q = 0.1, 0.9    # range = these quantiles of the backtest's misses
BACKTEST_DAYS = (1, 3, 7, 14, 21, 30, 45, 60)
MIN_BACKTEST = 8            # a horizon needs this many leave-one-out trials
_RUNG_RE = re.compile(r"\brelease\b", re.I)


def _t(s):
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _release_sold(r):
    """(sold, start, end) for one release-log row; sales assumed evenly
    spread over [start, end]."""
    used = r.get("used_last") or 0
    start = _t(r.get("first_seen_at"))
    last = _t(r.get("last_seen_at")) or start
    gone = _t(r.get("sold_out_at"))
    left = r.get("available_last") or 0
    end = gone or last
    if (gone and left > 0 and _RUNG_RE.search(r.get("tier_name") or "")
            and (gone - last).total_seconds() > OUTAGE_GAP_HOURS * 3600):
        used += left   # sold through while the monitor was down
    if end < start:
        end = start
    return used, start, end


def sold_so_far(releases):
    return sum(_release_sold(r)[0] for r in releases)


def sold_by(releases, t):
    total = 0.0
    for r in releases:
        n, a, b = _release_sold(r)
        if t >= b:
            total += n
        elif t > a:
            total += n * (t - a).total_seconds() / max((b - a).total_seconds(), 1)
    return total


def _quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def build_references(events, release_log, now):
    """Finished shows usable as references: [{event_id, name, show, final,
    track_start, releases}]."""
    logging_began = min((_t(r["first_seen_at"]) for rel in release_log.values()
                         for r in rel), default=None)
    refs = []
    for e in events:
        show = _t(e.get("start_date"))
        rel = release_log.get(e["event_id"]) or []
        if not show or show >= now or not rel:
            continue
        # Only shows announced after release logging began. On older ones an
        # earlier release re-appearing (e.g. Alok's "GA - 2nd Release" on
        # 07-21, 919 used) carries all its PAST sales in used_last, which the
        # curve would book on the day it re-appeared.
        if logging_began and _t(e.get("first_seen_at")) < logging_began + timedelta(hours=1):
            continue
        final = sold_so_far(rel)
        if final < 100:
            continue  # barely-sold / data-less event, says nothing about curves
        refs.append({
            "event_id": e["event_id"], "name": e.get("name"), "show": show,
            "final": final, "releases": rel,
            "track_start": min(_t(r["first_seen_at"]) for r in rel),
        })
    return refs


def _fracs(refs, days_out, exclude=None):
    out = []
    for r in refs:
        if r["event_id"] == exclude:
            continue
        at = r["show"] - timedelta(days=days_out)
        if at < r["track_start"] + timedelta(hours=6):
            continue  # before we were watching this show
        f = sold_by(r["releases"], at) / r["final"]
        if f > 0.02:
            out.append((r, f))
    return out


def _miss_band(calib, days_out):
    """(low, high, typical) relative misses at days_out, interpolated
    between backtested horizons; beyond the last one, the last one."""
    pts = sorted(calib.items())
    if not pts:
        return None
    if days_out <= pts[0][0]:
        return pts[0][1]
    for (d0, b0), (d1, b1) in zip(pts, pts[1:]):
        if days_out <= d1:
            w = (days_out - d0) / (d1 - d0)
            return tuple(x0 + (x1 - x0) * w for x0, x1 in zip(b0, b1))
    return pts[-1][1]


def estimate(event, releases, refs, now, calib=None, exclude=None):
    """Estimate dict for one upcoming event, or a dict with 'reason' when
    there's nothing honest to say."""
    show = _t(event.get("start_date"))
    sold = sold_so_far(releases)
    base = {"sold": sold}
    if not show:
        return {**base, "reason": "no show date"}
    days_out = (show - now).total_seconds() / 86400
    base["days_out"] = round(days_out, 1)
    if days_out <= 0:
        return {**base, "final": sold, "low": sold, "high": sold,
                "reason": "show has started"}
    tiers = event.get("tiers") or []
    if tiers and not any((t.get("available") or 0) > 0 for t in tiers):
        return {**base, "final": sold, "low": sold, "high": sold,
                "reason": "sold out — nothing left to sell unless Pacha adds a release"}
    fr = _fracs(refs, days_out, exclude=exclude)
    if len(fr) < MIN_REFERENCES:
        return {**base, "reason": f"too early — only {len(fr)} past shows were tracked this far out",
                "n_refs": len(fr)}
    proj = [sold / f for _, f in fr]
    final = statistics.median(proj)
    band = _miss_band(calib or {}, days_out)
    if band is None:
        return {**base, "reason": "not enough finished shows to measure the error yet",
                "n_refs": len(fr)}
    lo_err, hi_err, typical = band
    fr_sorted = sorted(fr, key=lambda x: x[1])
    return {
        **base,
        "final": round(max(final, sold)),
        "low": round(max(final * (1 + lo_err), sold)),
        "high": round(max(final * (1 + hi_err), sold)),
        "typical_miss": round(typical, 3),
        # further out than any horizon we could back-test: the range is the
        # last measured one carried forward, so it's likely too narrow
        "untested": days_out > max(calib or {0: None}),
        "tested_to_days": max(calib or {0: None}),
        "frac_median": round(statistics.median(f for _, f in fr), 3),
        "n_refs": len(fr),
        # a few comparables: what share of their final those shows had by now
        "comparables": [
            {"name": r["name"], "date": r["show"].date().isoformat(),
             "final": r["final"], "share_by_now": round(f, 3)}
            for r, f in (fr_sorted[0], fr_sorted[len(fr_sorted) // 2], fr_sorted[-1])
        ],
    }


def estimate_all(events, release_log, now=None):
    now = now or datetime.now(timezone.utc)
    refs = build_references(events, release_log, now)
    calib = calibrate(refs)
    out = {}
    for e in events:
        show = _t(e.get("start_date"))
        if show and show > now:
            out[e["event_id"]] = estimate(e, release_log.get(e["event_id"]) or [],
                                          refs, now, calib=calib)
    return out


def _loo_errors(refs, days=BACKTEST_DAYS):
    """Leave-one-out: re-estimate each finished show at d days out from the
    others; {d: [relative miss est/final - 1, ...]}."""
    out = {}
    for d in days:
        errs = []
        for r in refs:
            at = r["show"] - timedelta(days=d)
            if at < r["track_start"] + timedelta(hours=6):
                continue
            s = sold_by(r["releases"], at)
            if s < 20:
                continue
            fr = _fracs(refs, d, exclude=r["event_id"])
            if len(fr) < MIN_REFERENCES:
                continue
            errs.append(statistics.median(s / f for _, f in fr) / r["final"] - 1)
        out[d] = errs
    return out


def calibrate(refs):
    """{d: (low_miss, high_miss, typical_abs_miss)} for horizons with
    enough trials — what the range is built from."""
    return {d: (_quantile(e, LOW_Q), _quantile(e, HIGH_Q),
                statistics.median(abs(x) for x in e))
            for d, e in _loo_errors(refs).items() if len(e) >= MIN_BACKTEST}


def backtest(events, release_log, now):
    """Per-horizon summary of the leave-one-out misses (CLI --backtest)."""
    refs = build_references(events, release_log, now)
    calib = calibrate(refs)
    res = {}
    for d, errs in _loo_errors(refs).items():
        if not errs:
            continue
        row = {"n": len(errs),
               "median_abs_err": round(statistics.median(abs(x) for x in errs), 3),
               "median_err": round(statistics.median(errs), 3)}
        if d in calib:
            lo, hi, _ = calib[d]
            row["band"] = [round(lo, 3), round(hi, 3)]
        res[d] = row
    return res


def _load(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    events = [dict(r) for r in c.execute("SELECT * FROM pacha_seen_events")]
    for e in events:
        try:
            e["tiers"] = json.loads(e.get("tiers_json") or "[]")
        except ValueError:
            e["tiers"] = []
    log = {}
    for r in c.execute("SELECT * FROM pacha_release_log ORDER BY first_seen_at, id"):
        log.setdefault(r["event_id"], []).append(dict(r))
    return events, log


def main(argv):
    path = argv[argv.index("--db") + 1] if "--db" in argv else "kartis.db"
    events, log = _load(path)
    now = datetime.now(timezone.utc)
    if "--backtest" in argv:
        print(json.dumps(backtest(events, log, now), indent=1))
        return
    est = estimate_all(events, log, now)
    if "--json" in argv:
        print(json.dumps(est, indent=1, default=str))
        return
    names = {e["event_id"]: e for e in events}
    for eid, x in sorted(est.items(), key=lambda kv: names[kv[0]].get("start_date") or ""):
        e = names[eid]
        rng = f"{x['final']:>5} ({x['low']}–{x['high']})" if "final" in x else "    —"
        print(f"{(e.get('start_date') or '')[:10]}  {e.get('name', '')[:34]:34}  sold {x['sold']:>5}  "
              f"final {rng}  {x.get('reason', '')}  refs={x.get('n_refs', '')}")


if __name__ == "__main__":
    main(sys.argv[1:])
