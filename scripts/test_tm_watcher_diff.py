"""Regression tests for the Ticketmaster event-level watcher diff.

Run: .venv\\Scripts\\python scripts\\test_tm_watcher_diff.py

Covers the "460 new seats dropped when one seat moved" class of bug. A
sold-out performance's raw seat feed carries its whole standing unsold-seat
set; the moment the perf flips to on sale (or a failed fetch recovers) every
one of those seats changes key or reappears at once, and a naive set diff
reports the lot as a drop. The tick guards that with the per-perf tracking
sentinels in ticketmaster.py — these tests drive the real db helpers through
the same sequence both tick implementations use.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ticketmaster as tm  # noqa: E402

WID = "tmw-testtesttest"
FAILS = []


def _tick(seats, perf_errors=None):
    """The state half of _check_one_watcher, for an event-level TM watcher.
    Returns (added_after_baselining, removed_keys)."""
    perf_errors = perf_errors or {}
    prev_keys = db.tm_get_seat_keys(WID)
    by_key = {}
    for s in seats:
        by_key.setdefault(tm.event_seat_key(s), s)
    added = [by_key[k] for k in set(by_key) - prev_keys]
    removed = list(prev_keys - set(by_key))
    stale = tuple(tm.stale_perf_prefixes(perf_errors))
    if stale:
        removed = [k for k in removed if not str(k).startswith(stale)]
    db.tm_replace_seat_state(WID, seats, keep_prefixes=stale)
    return tm.filter_baselined(added, prev_keys), removed


def seat(perf, block, row, num, unconfirmed=False):
    s = {"_perf": perf, "block": block, "row": str(row), "seat": str(num)}
    if unconfirmed:
        s["unconfirmed"] = True
    return s


def status_seat(perf, state):
    return tm._perf_status_seat({"performanceCode": perf, "status": ""}, state)


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n        got {got!r}\n       want {want!r}"))
    if not ok:
        FAILS.append(name)


def standing(perf, n, unconfirmed):
    return [seat(perf, "A", 1, i, unconfirmed=unconfirmed) for i in range(n)]


def soldout_snapshot(perf, n):
    return ([status_seat(perf, "soldout"), tm.tracking_seat(perf, "unconfirmed")]
            + standing(perf, n, unconfirmed=True))


def onsale_snapshot(perf, n):
    return ([status_seat(perf, "available"), tm.tracking_seat(perf, "confirmed")]
            + standing(perf, n, unconfirmed=False))


def reset():
    db.tm_replace_seat_state(WID, [])


def run():
    # --- 1. A sold-out perf starting to be tracked is a baseline, not a drop.
    reset()
    added, _ = _tick(soldout_snapshot("001", 460))
    check("sold-out perf first seen: no seat pings",
          [s for s in added if not s.get("festival")], [])
    check("sold-out perf first seen: status seat still pings",
          [s["status"] for s in added if s.get("festival")], ["soldout"])

    # --- 2. Once tracked, a genuinely new seat in the sold-out feed pings.
    added, _ = _tick(soldout_snapshot("001", 460) + [seat("001", "B", 3, 7, unconfirmed=True)])
    check("tracked sold-out perf: one new seat pings as one",
          [tm.seat_key(s) for s in added], ["B|3|7"])

    # --- 3. The sold-out -> on-sale flip re-keys all 461 seats. Only the
    #        status flip should ping; the standing set is inventory.
    added, _ = _tick(onsale_snapshot("001", 460))
    check("flip to on sale: no seat pings", [s for s in added if not s.get("festival")], [])
    check("flip to on sale: status seat pings",
          [s["status"] for s in added if s.get("festival")], ["available"])

    # --- 4. After the flip the perf is tracked as confirmed; a real drop pings.
    added, _ = _tick(onsale_snapshot("001", 460) + [seat("001", "C", 9, 4)])
    check("on-sale perf: one new seat pings as one",
          [tm.seat_key(s) for s in added], ["C|9|4"])

    # --- 5. A failed seat fetch must not empty the perf's state or report
    #        every seat as removed.
    reset()
    _tick(onsale_snapshot("001", 460) + onsale_snapshot("002", 12))
    added, removed = _tick(onsale_snapshot("001", 460) + [status_seat("002", "available")],
                           perf_errors={"002": "seats: TicketmasterError: HTTP 502"})
    check("failed perf fetch: nothing reported added", added, [])
    check("failed perf fetch: nothing reported removed", removed, [])
    # 12 standing seats + the status pseudo-seat + the tracking sentinel.
    check("failed perf fetch: perf 002 keys carried forward",
          sum(1 for k in db.tm_get_seat_keys(WID) if k.startswith("002|")), 14)

    # --- 6. ...and recovery doesn't re-ping the carried set.
    added, _ = _tick(onsale_snapshot("001", 460) + onsale_snapshot("002", 12))
    check("recovery after failed fetch: no re-ping", added, [])

    # --- 7. A brand-new perf appearing on the event baselines its own seats
    #        without muting the other perfs' real drops.
    added, _ = _tick(onsale_snapshot("001", 460) + [seat("001", "D", 2, 2)]
                     + onsale_snapshot("002", 12) + onsale_snapshot("003", 300))
    check("new perf added: its standing seats don't ping",
          [tm.seat_key(s) for s in added if not s.get("festival")], ["D|2|2"])
    check("new perf added: its status seat pings",
          [s["_perf"] for s in added if s.get("festival")], ["003"])


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmpd:
        db.DB_PATH = Path(tmpd) / "test.db"
        db.init()
        run()
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
