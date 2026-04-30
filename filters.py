"""Per-watcher filter rules: section blacklist, price range, min consecutive seats.

The filter dict (stored as JSON in tm_watchers.filters) has shape:

    {
      "min_group_size": 2,         # require at least N consecutive seats together
      "exclude_sections": ["BLT"],  # skip notifications for these block codes
      "min_price": 200,             # ILS lower bound (inclusive)
      "max_price": 500              # ILS upper bound (inclusive)
    }

`min_group_size` adjacency rule: two seats in the same block + same row
whose seat numbers differ by 1 OR 2 are considered adjacent. The "1 or 2"
covers both continuous numbering (5, 6, 7) and even/odd numbering
(1, 3, 5… or 2, 4, 6…) which Israeli venues commonly use.

Price filtering needs a `labels` payload from the source's get_labels()
to look up each block's ILS price. When labels are missing we skip the
price filter (don't drop seats just because we couldn't fetch metadata).

Filtering happens at notify-time, not track-time — the watcher still
records every available seat in tm_seat_state so changing the filter
mid-flight doesn't lose continuity. The drop history records both the
raw `added_count` and the `notify_count` that survived the filter.
"""
import json
import re


_NUM_RE = re.compile(r"\d+")


def _seat_number(seat):
    """Pull the numeric seat-position from a normalized seat dict.
    Falls back to the raw `l` (ticketmaster) when `seat` is missing.
    Returns None if no digits are present."""
    val = seat.get("seat")
    if val is None:
        val = seat.get("l")
    if val is None:
        return None
    m = _NUM_RE.search(str(val))
    return int(m.group()) if m else None


def _seat_block(seat):
    return str(seat.get("block") or seat.get("b") or "")


def _seat_row(seat):
    return str(seat.get("row") or seat.get("r") or "")


def parse_filters(raw):
    """Coerce the stored value (JSON string, dict, or None) into a dict.
    Returns None when there are no filters set so callers can fast-path."""
    if not raw:
        return None
    if isinstance(raw, dict):
        f = raw
    else:
        try:
            f = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(f, dict):
        return None
    # Empty / 1 / 0 / null all mean "no group-size restriction"
    mgs = f.get("min_group_size")
    excl = f.get("exclude_sections") or []
    minp = f.get("min_price")
    maxp = f.get("max_price")
    no_filter = (
        (mgs is None or mgs <= 1)
        and not excl
        and minp in (None, "")
        and maxp in (None, "")
    )
    if no_filter:
        return None
    return f


def _build_group_size_map(all_current):
    """For every seat in the current available set, compute the size of the
    consecutive run it belongs to. Returns {(block, row, seat_num): run_size}.
    Adjacency: same (block, row) and seat_num diff ≤ 2.

    Seats whose number can't be parsed are treated as singletons — safer
    to under-match than to falsely group them with unrelated seats.
    """
    by_loc = {}
    for s in all_current:
        n = _seat_number(s)
        if n is None:
            continue
        by_loc.setdefault((_seat_block(s), _seat_row(s)), set()).add(n)

    size_map = {}
    for (block, row), nums in by_loc.items():
        ordered = sorted(nums)
        if not ordered:
            continue
        run = [ordered[0]]
        for prev, cur in zip(ordered, ordered[1:]):
            if cur - prev <= 2:
                run.append(cur)
            else:
                for n in run:
                    size_map[(block, row, n)] = len(run)
                run = [cur]
        for n in run:
            size_map[(block, row, n)] = len(run)
    return size_map


def _passes_section(seat, excluded):
    if not excluded:
        return True
    return _seat_block(seat) not in {str(x) for x in excluded}


def _passes_price(seat, labels, min_price, max_price):
    if min_price in (None, "") and max_price in (None, ""):
        return True
    blocks = ((labels or {}).get("blocks")) or {}
    info = blocks.get(_seat_block(seat)) or blocks.get(str(_seat_block(seat))) or {}
    price = info.get("price")
    if price is None:
        # Missing price metadata — don't drop the seat over it. The user
        # would rather a spurious ping than a silent miss.
        return True
    try:
        price = float(price)
    except (TypeError, ValueError):
        return True
    if min_price not in (None, ""):
        try:
            if price < float(min_price):
                return False
        except (TypeError, ValueError):
            pass
    if max_price not in (None, ""):
        try:
            if price > float(max_price):
                return False
        except (TypeError, ValueError):
            pass
    return True


def apply(added_seats, all_current, filters_raw, labels=None):
    """Filter the just-added seats according to the watcher's stored filter
    config. `all_current` is the full set of currently-available seats —
    needed for the consecutive-seat check, since adjacency is computed
    against the live availability snapshot, not historical state.

    Returns a (possibly empty) list. Returns the input unchanged when
    there are no active filters.
    """
    f = parse_filters(filters_raw)
    if not f:
        return list(added_seats)

    excluded = f.get("exclude_sections") or []
    min_price = f.get("min_price")
    max_price = f.get("max_price")
    min_group = f.get("min_group_size") or 1

    out = []
    size_map = _build_group_size_map(all_current) if min_group > 1 else None
    for s in added_seats:
        if not _passes_section(s, excluded):
            continue
        if not _passes_price(s, labels, min_price, max_price):
            continue
        if min_group > 1:
            n = _seat_number(s)
            if n is None:
                # Unparseable seat number → can't be sure it's part of a
                # group. Treat as a singleton, exclude under min_group>=2.
                continue
            run = size_map.get((_seat_block(s), _seat_row(s), n), 1)
            if run < min_group:
                continue
        out.append(s)
    return out


def summarize(filters_raw):
    """Short label like "min 2 · skip BLT,BAL · ₪200-500" for the UI badge."""
    f = parse_filters(filters_raw)
    if not f:
        return ""
    bits = []
    mgs = f.get("min_group_size")
    if mgs and mgs > 1:
        bits.append(f"min {mgs}")
    excl = f.get("exclude_sections") or []
    if excl:
        bits.append("skip " + ",".join(str(x) for x in excl[:3]) + ("…" if len(excl) > 3 else ""))
    minp, maxp = f.get("min_price"), f.get("max_price")
    if minp not in (None, "") or maxp not in (None, ""):
        bits.append(f"₪{minp or '-'}-{maxp or '-'}")
    return " · ".join(bits)


if __name__ == "__main__":
    # Quick self-test
    seats = [
        {"block": "BALC", "row": "5", "seat": "10"},  # alone
        {"block": "BALC", "row": "5", "seat": "20"},  # part of pair
        {"block": "BALC", "row": "5", "seat": "22"},  # part of pair (diff 2)
        {"block": "BLT",  "row": "1", "seat": "1"},   # excluded section
        {"block": "BALC", "row": "5", "seat": "30"},  # alone
        {"block": "ORCC", "row": "8", "seat": "5"},   # part of trio
        {"block": "ORCC", "row": "8", "seat": "6"},
        {"block": "ORCC", "row": "8", "seat": "7"},
    ]
    f = {"min_group_size": 2, "exclude_sections": ["BLT"]}
    kept = apply(seats, seats, f)
    print(f"kept {len(kept)} of {len(seats)}:")
    for s in kept:
        print(f"  {s}")
    print("summary:", summarize(f))
