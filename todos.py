"""To-Do list logic: suggestions, recurrence, linked-entity enrichment.

Routes in app.py stay thin and delegate here. Suggestions are computed
on-demand (not persisted) — only "accepted" suggestions become real `todos`
rows, stamped with `auto_source` so the same suggestion doesn't reappear
while its derived task is open.
"""
import json
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

import db


URGENCY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
VALID_URGENCIES = ("low", "normal", "high", "critical")
VALID_RECURRENCES = ("daily", "weekly", "monthly")
VALID_SOURCES = (
    "inventory", "watcher", "lysted_purchase", "viagogo_listing",
    "expense", "maaser", "owed",
)


def new_id():
    return "todo-" + uuid.uuid4().hex[:12]


def new_subtask_id():
    return "sub-" + uuid.uuid4().hex[:12]


def _today_iso():
    return date.today().strftime("%Y-%m-%d")


def _today():
    return date.today()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def advance_recurrence(due_iso, recurrence):
    """Returns the next due-date ISO for a recurring task. Daily +1d,
    weekly +7d, monthly bumps the month (clamped to month length). If the
    base date is missing or recurrence unknown, returns None — the caller
    will skip regenerating."""
    base = _parse_iso(due_iso) or _today()
    if recurrence == "daily":
        return (base + timedelta(days=1)).strftime("%Y-%m-%d")
    if recurrence == "weekly":
        return (base + timedelta(days=7)).strftime("%Y-%m-%d")
    if recurrence == "monthly":
        y, m = base.year, base.month
        m += 1
        if m > 12:
            m = 1
            y += 1
        day = min(base.day, monthrange(y, m)[1])
        return date(y, m, day).strftime("%Y-%m-%d")
    return None


# ---------- Linked-entity summaries ----------------------------------------

def _by_id(rows, key="id"):
    return {str(r.get(key)): r for r in rows}


def _build_link_index():
    """Pre-fetch one row from each linkable table keyed by id. Cheap enough
    to do once per /api/todos call — avoids N+1 lookups when enriching the
    list. Each entry maps source-name → {id: snapshot-dict}."""
    return {
        "inventory": _by_id(db.all_inventory()),
        "watcher": _by_id(db.tm_all_watchers()),
        "lysted_purchase": _by_id(db.all_lysted_purchases()),
        "viagogo_listing": _by_id(db.all_viagogo()),
        "expense": _by_id(db.all_expenses()),
        "maaser": _by_id(db.all_maaser()),
        "owed": _by_id(db.all_owed_items()),
    }


def _summarize_link(source, source_id, idx):
    """Return {label, href} for a linked entity, or None if it's gone (the
    entity was deleted upstream). Each source surfaces its most identifying
    fields and an absolute URL the UI can hyperlink to.
    """
    if not source or not source_id:
        return None
    bucket = (idx or {}).get(source) or {}
    row = bucket.get(str(source_id))
    if not row:
        return {"label": f"{source}: {source_id} (missing)", "href": None, "missing": True}
    if source == "inventory":
        bits = [row.get("event_name") or "(unnamed event)"]
        if row.get("event_date_iso"):
            bits.append(str(row["event_date_iso"])[:10])
        return {"label": " · ".join(bits), "href": "/"}
    if source == "watcher":
        return {"label": row.get("label") or row.get("event_code") or source_id, "href": "/watchers"}
    if source == "lysted_purchase":
        bits = [row.get("event_name") or "Lysted order"]
        if row.get("event_date_iso"):
            bits.append(str(row["event_date_iso"])[:10])
        return {"label": " · ".join(bits), "href": "/"}
    if source == "viagogo_listing":
        bits = [row.get("event_name") or "Viagogo listing"]
        if row.get("section"):
            bits.append(f"sec {row['section']}")
        return {"label": " · ".join(bits), "href": "/"}
    if source == "expense":
        bits = [row.get("vendor") or row.get("category") or "expense"]
        if row.get("amount") is not None:
            bits.append(f"${row['amount']:.2f}")
        return {"label": " · ".join(bits), "href": "/expenses"}
    if source == "maaser":
        bits = [row.get("recipient") or "maaser"]
        if row.get("amount") is not None:
            bits.append(f"₪{row['amount']:.0f}")
        return {"label": " · ".join(bits), "href": "/maaser"}
    if source == "owed":
        bits = [row.get("description") or "owed item"]
        if row.get("amount") is not None:
            bits.append(f"${row['amount']:.2f}")
        return {"label": " · ".join(bits), "href": "/owed"}
    return {"label": f"{source}: {source_id}", "href": None}


# ---------- List + stats ---------------------------------------------------

def list_todos(status=None):
    """Returns (rows, stats) — rows enriched with subtasks summary + linked-
    entity chip. Stats counts open/due-today/overdue/done-this-week."""
    rows = db.todo_all(status=status)
    sub_map = db.subtask_list_for_todos([r["id"] for r in rows])
    link_idx = _build_link_index()
    today = _today_iso()
    week_ago = (_today() - timedelta(days=7)).strftime("%Y-%m-%d")
    stats = {"open": 0, "due_today": 0, "overdue": 0, "done_week": 0}
    out = []
    for r in rows:
        subs = sub_map.get(r["id"], [])
        tags = []
        if r.get("tags"):
            try:
                tags = json.loads(r["tags"])
            except (TypeError, ValueError):
                tags = []
        link = _summarize_link(r.get("linked_source"), r.get("linked_source_id"), link_idx)
        r2 = {
            **r,
            "tags": tags,
            "subtasks": subs,
            "subtask_done": sum(1 for s in subs if s.get("done")),
            "subtask_total": len(subs),
            "link": link,
        }
        out.append(r2)
        if r["status"] == "open":
            stats["open"] += 1
            due = r.get("due_date_iso")
            if due:
                if due == today:
                    stats["due_today"] += 1
                elif due < today:
                    stats["overdue"] += 1
        if r["status"] == "done" and r.get("completed_at") and r["completed_at"] >= week_ago:
            stats["done_week"] += 1
    return out, stats


# ---------- Create / edit / toggle ----------------------------------------

def normalize_input(body, *, allow_partial=False):
    """Validate + normalize a request body into a dict suitable for the DB
    helpers. Returns (fields, error). If `allow_partial`, missing fields are
    omitted from the result (for edit/PATCH-style updates); otherwise the
    full set is returned with sensible defaults (for create).
    """
    fields = {}
    title = (body.get("title") or "").strip() if "title" in body else None
    if not allow_partial and not title:
        return None, "title is required"
    if title is not None:
        fields["title"] = title[:500]

    if "notes" in body:
        notes = body.get("notes")
        fields["notes"] = (notes or "").strip() or None

    if "due_date_iso" in body:
        raw = (body.get("due_date_iso") or "").strip() or None
        if raw and not _parse_iso(raw):
            return None, "due_date_iso must be YYYY-MM-DD"
        fields["due_date_iso"] = raw

    if "urgency" in body:
        u = (body.get("urgency") or "normal").strip().lower()
        if u not in VALID_URGENCIES:
            return None, f"urgency must be one of {VALID_URGENCIES}"
        fields["urgency"] = u
    elif not allow_partial:
        fields["urgency"] = "normal"

    if "status" in body:
        s = (body.get("status") or "open").strip().lower()
        if s not in ("open", "done"):
            return None, "status must be open or done"
        fields["status"] = s

    if "linked_source" in body or "linked_source_id" in body:
        src = (body.get("linked_source") or "").strip().lower() or None
        sid = (body.get("linked_source_id") or "").strip() or None
        if src and src not in VALID_SOURCES:
            return None, f"linked_source must be one of {VALID_SOURCES}"
        if src and not sid:
            return None, "linked_source_id required when linked_source is set"
        fields["linked_source"] = src if src else None
        fields["linked_source_id"] = sid if src else None

    if "tags" in body:
        raw = body.get("tags")
        tags = []
        if isinstance(raw, list):
            tags = [str(t).strip() for t in raw if str(t).strip()]
        elif isinstance(raw, str):
            tags = [t.strip() for t in raw.split(",") if t.strip()]
        fields["tags"] = json.dumps(tags) if tags else None

    if "amount" in body:
        v = body.get("amount")
        if v in (None, ""):
            fields["amount"] = None
        else:
            try:
                fields["amount"] = float(v)
            except (TypeError, ValueError):
                return None, "amount must be numeric"

    if "amount_currency" in body:
        cur = (body.get("amount_currency") or "").strip().upper() or None
        if cur and cur not in ("USD", "ILS"):
            return None, "amount_currency must be USD or ILS"
        fields["amount_currency"] = cur

    if "recurrence" in body:
        rec = (body.get("recurrence") or "").strip().lower() or None
        if rec and rec not in VALID_RECURRENCES:
            return None, f"recurrence must be one of {VALID_RECURRENCES}"
        fields["recurrence"] = rec

    if "auto_source" in body:
        fields["auto_source"] = (body.get("auto_source") or "").strip() or None

    return fields, None


def create(body):
    fields, err = normalize_input(body, allow_partial=False)
    if err:
        return None, err
    row = {"id": new_id(), **fields}
    db.todo_insert(row, datetime.now(timezone.utc).isoformat())
    return row["id"], None


def edit(id_, body):
    if not db.todo_get(id_):
        return False, "not found"
    fields, err = normalize_input(body, allow_partial=True)
    if err:
        return False, err
    db.todo_update(id_, fields, datetime.now(timezone.utc).isoformat())
    return True, None


def toggle(id_):
    """Flip status open<->done. When a recurring task is marked done, also
    spawn a fresh copy with the next due date so the recurrence continues
    even without a separate cron job. Subtask state is reset on the new copy
    (each instance is its own checklist)."""
    cur = db.todo_get(id_)
    if not cur:
        return False, "not found"
    now_iso = datetime.now(timezone.utc).isoformat()
    if cur["status"] == "open":
        db.todo_update(id_, {"status": "done", "completed_at": now_iso}, now_iso)
        if cur.get("recurrence"):
            next_due = advance_recurrence(cur.get("due_date_iso"), cur["recurrence"])
            if next_due:
                clone = {
                    "id": new_id(),
                    "title": cur["title"],
                    "notes": cur.get("notes"),
                    "due_date_iso": next_due,
                    "urgency": cur.get("urgency") or "normal",
                    "status": "open",
                    "linked_source": cur.get("linked_source"),
                    "linked_source_id": cur.get("linked_source_id"),
                    "tags": cur.get("tags"),
                    "amount": cur.get("amount"),
                    "amount_currency": cur.get("amount_currency"),
                    "recurrence": cur.get("recurrence"),
                    "auto_source": cur.get("auto_source"),
                }
                db.todo_insert(clone, now_iso)
                return True, {"new_id": clone["id"], "due_date_iso": next_due}
    else:
        db.todo_update(id_, {"status": "open", "completed_at": None, "notified_at": None}, now_iso)
    return True, None


# ---------- Suggestions ---------------------------------------------------

def _days_between(iso_a, iso_b):
    a, b = _parse_iso(iso_a), _parse_iso(iso_b)
    if not a or not b:
        return None
    return (a - b).days


def compute_suggestions():
    """Surface auto-derived tasks without persisting them. Each returns a
    stable `key` so the user can snooze it (persisted) or accept it (turns
    into a real todo whose `auto_source` matches the key)."""
    today = _today_iso()
    today_d = _today()
    dismissed = db.suggestion_active_dismissals(today)
    suggestions = []

    def emit(key, **payload):
        if key in dismissed:
            return
        # Don't re-suggest if a corresponding open todo already exists.
        if db.todo_open_by_auto_source(key):
            return
        suggestions.append({"key": key, **payload})

    # 1. Inventory delivery prep — events in next 7 days.
    for inv in db.all_inventory():
        iso = (inv.get("event_date_iso") or "")[:10]
        delta = _days_between(iso, today)
        if delta is None or delta < 0 or delta > 7:
            continue
        urgency = "critical" if delta <= 1 else ("high" if delta <= 3 else "normal")
        emit(
            f"inventory_delivery:{inv['id']}",
            title=f"Deliver tickets — {inv.get('event_name') or 'event'} ({iso})",
            reason=f"Event in {delta} day{'s' if delta != 1 else ''}; {inv.get('tickets_count') or 0} ticket(s) on hand",
            due_date_iso=iso,
            urgency=urgency,
            linked_source="inventory",
            linked_source_id=inv["id"],
        )

    # 2. Stale watcher — active watcher with no successful check in 24h.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    for w in db.tm_active_watchers():
        last = w.get("last_check_at")
        if last and last >= cutoff:
            continue
        emit(
            f"watcher_stale:{w['id']}",
            title=f"Watcher hasn't ticked in 24h — {w.get('label') or w['id']}",
            reason="No successful check in the last 24 hours; verify the watcher manually",
            due_date_iso=today,
            urgency="high",
            linked_source="watcher",
            linked_source_id=w["id"],
        )

    # 3. Maaser nudge — no payment in the last 30 days.
    payments = db.all_maaser()
    last_pmt_iso = payments[0]["date_iso"] if payments else None
    if not last_pmt_iso or _parse_iso(last_pmt_iso) < (today_d - timedelta(days=30)):
        last_str = last_pmt_iso or "never"
        emit(
            "maaser_overdue:monthly",
            title="Pay maaser",
            reason=f"Last payment: {last_str}",
            due_date_iso=today,
            urgency="normal",
            linked_source=None,
            linked_source_id=None,
        )

    # 4. Subscription due — active subscriptions whose next charge is in 7d.
    for sub in db.all_expense_subscriptions():
        if sub.get("ended_at_iso"):
            continue
        start = _parse_iso(sub.get("started_at_iso"))
        if not start:
            continue
        # Next charge anchors on the start day-of-month, this month or next.
        if today_d.day <= start.day:
            charge_y, charge_m = today_d.year, today_d.month
        else:
            charge_m = today_d.month + 1
            charge_y = today_d.year + (1 if charge_m > 12 else 0)
            if charge_m > 12:
                charge_m = 1
        day = min(start.day, monthrange(charge_y, charge_m)[1])
        charge_d = date(charge_y, charge_m, day)
        delta = (charge_d - today_d).days
        if delta < 0 or delta > 7:
            continue
        amt = sub.get("amount") or 0
        emit(
            f"subscription_due:{sub['id']}:{charge_d.strftime('%Y-%m')}",
            title=f"Subscription renews — {sub.get('name') or 'subscription'}",
            reason=f"${amt:.2f} due {charge_d.strftime('%Y-%m-%d')}",
            due_date_iso=charge_d.strftime("%Y-%m-%d"),
            urgency="low",
            linked_source=None,
            linked_source_id=None,
        )

    suggestions.sort(key=lambda s: (s.get("due_date_iso") or "9", URGENCY_RANK.get(s.get("urgency", "normal"), 2)))
    return suggestions


def accept_suggestion(key):
    """Find the suggestion matching `key`, persist it as a real todo, and
    return the new id. Returns (id, None) on success or (None, error)."""
    for s in compute_suggestions():
        if s["key"] != key:
            continue
        body = {
            "title": s["title"],
            "notes": s.get("reason"),
            "due_date_iso": s.get("due_date_iso"),
            "urgency": s.get("urgency") or "normal",
            "linked_source": s.get("linked_source"),
            "linked_source_id": s.get("linked_source_id"),
            "auto_source": key,
        }
        return create(body)
    return None, "suggestion not found"


def dismiss_suggestion(key, days):
    """Hide a suggestion until `days` from today. Stored persistently so the
    dismissal survives restarts."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 365))
    until = (_today() + timedelta(days=days)).strftime("%Y-%m-%d")
    db.suggestion_dismiss(key, until, datetime.now(timezone.utc).isoformat())
    return until
