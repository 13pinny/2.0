import io
import json
import os
import re
import subprocess
import tempfile
import threading
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, send_file
from openpyxl import Workbook

import attachments as attachments_mod
import db
import filters as watcher_filters
import import_jerujam
import kupat
import kupat_credits
import kupat_pdf
import mail_intake
import matcher
import notify
import pacha_tickets
import scraper
import tickchak
import tickchak_pdf
import ticketmaster
import todos as todos_mod
import viagogo_listing

# Drop-checker sources keyed by the value stored in tm_watchers.source.
# Each module exposes parse_url, perf_url, fetch_selectable_seats,
# seat_key, get_labels, event_summary. Adding a new ticketing site is a
# matter of writing one module and adding a row here.
WATCHER_SOURCES = {
    "ticketmaster": ticketmaster,
    "kupat": kupat,
    "tickchak": tickchak,
}


def _detect_source(url):
    """Pick the right source module from a URL. Returns (source_name, module).

    Falls back to ticketmaster for shorthand 'ABC123/001' with letters,
    kupat for shorthand '1358/51596' (digits only — both kupat IDs are
    numeric), so existing callers keep working.
    """
    s = (url or "").strip().lower()
    if "tickchak.co.il" in s:
        return "tickchak", tickchak
    if "kupat.co.il" in s:
        return "kupat", kupat
    if "ticketmaster.co.il" in s:
        return "ticketmaster", ticketmaster
    if re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", s or ""):
        return "kupat", kupat
    # Bare slug shorthand with no slash, scheme, or query string — most
    # likely a tickchak event slug (e.g. "mada26", "103350").
    if re.fullmatch(r"\s*[a-z0-9_\-]{2,}\s*", s or "") and "/" not in s and "?" not in s:
        return "tickchak", tickchak
    return "ticketmaster", ticketmaster

load_dotenv()

app = Flask(__name__)
# Cap multipart uploads (file + form overhead) — slightly above the per-file
# MAX_BYTES in attachments.py so legitimate uploads aren't rejected by Flask
# before our route handler can return a friendly error.
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024
db.init()

_last_run = {"at": None, "count": 0, "error": None, "running": False}
_run_lock = threading.Lock()

BACKUP_DIR = Path(os.getenv("KARTIS_BACKUP_DIR") or (Path(__file__).parent / "backups"))
BACKUP_KEEP_DAYS = int(os.getenv("KARTIS_BACKUP_KEEP_DAYS") or 30)
_last_backup = {"at": None, "path": None, "error": None, "running": False}
_backup_lock = threading.Lock()

_last_jerujam = {"at": None, "count": None, "error": None, "running": False}
_jerujam_lock = threading.Lock()

# Pacha ticket fetch is on-demand from the Tools page. Synchronous like the
# kupat/tickchak PDF tools, but guarded by a lock so two clicks can't hit Gmail
# at once.
_pacha_lock = threading.Lock()

_last_tm = {"at": None, "checked": 0, "drops": 0, "errors": 0, "running": False}
_tm_lock = threading.Lock()
TM_CHECK_INTERVAL_SECONDS = int(os.getenv("TM_CHECK_INTERVAL_SECONDS") or 60)
# Drop-checking can be disabled here when the watcher runs on another machine
# (e.g. the VPS), so the dashboard still serves inventory/sales without
# double-pinging Discord. Manual "check now" from the UI still works.
TM_CHECK_ENABLED = (os.getenv("TM_CHECK_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")

_last_intake = {"at": None, "fetched": 0, "saved": 0, "skipped_dupe": 0, "errors": 0, "error": None, "running": False}
_intake_lock = threading.Lock()
INTAKE_INTERVAL_MINUTES = int(os.getenv("KARTIS_INTAKE_INTERVAL_MINUTES") or 10)

_last_todo_remind = {"at": None, "sent_count": 0, "due_today": 0, "overdue": 0,
                     "error": None, "running": False, "paused": False,
                     "muted": False, "result": None}
_todo_remind_lock = threading.Lock()


def run_mail_intake():
    if not _intake_lock.acquire(blocking=False):
        return
    _last_intake["running"] = True
    try:
        summary = mail_intake.run_intake()
        _last_intake.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=None,
            **summary,
        )
    except Exception as e:
        _last_intake.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
        traceback.print_exc()
    finally:
        _last_intake["running"] = False
        _intake_lock.release()


def run_todo_remind():
    """Daily digest of open to-dos that are due today or overdue. Mirrors the
    drop-checker gating: master_paused skips entirely, master_muted logs but
    doesn't send. Each task gets `notified_at` stamped so the same item
    isn't re-pinged later the same day."""
    if not _todo_remind_lock.acquire(blocking=False):
        return
    _last_todo_remind["running"] = True
    try:
        today_iso = date.today().strftime("%Y-%m-%d")
        if db.setting_get_bool("master_paused", default=False):
            _last_todo_remind.update(
                at=datetime.now(timezone.utc).isoformat(),
                sent_count=0, due_today=0, overdue=0,
                paused=True, muted=False, error=None, result=None,
            )
            return
        due = db.todo_due_open(today_iso)
        if not due:
            _last_todo_remind.update(
                at=datetime.now(timezone.utc).isoformat(),
                sent_count=0, due_today=0, overdue=0,
                paused=False, muted=False, error=None, result=None,
            )
            return
        due_today = [t for t in due if (t.get("due_date_iso") or "")[:10] == today_iso]
        overdue = [t for t in due if (t.get("due_date_iso") or "")[:10] < today_iso]
        muted = db.setting_get_bool("master_muted", default=False)
        if muted:
            _last_todo_remind.update(
                at=datetime.now(timezone.utc).isoformat(),
                sent_count=0,
                due_today=len(due_today), overdue=len(overdue),
                paused=False, muted=True, error=None,
                result={"discord": "skipped (master_muted)", "email": "skipped (master_muted)"},
            )
            return
        result = notify.send_todo_digest(due_today, overdue)
        # Only stamp notified_at if at least one channel actually sent — that
        # way a transient credential outage doesn't suppress the next day's
        # digest from re-trying these same items.
        if any(str(v).startswith("ok") for v in result.values()):
            db.todo_mark_notified([t["id"] for t in due], today_iso)
        _last_todo_remind.update(
            at=datetime.now(timezone.utc).isoformat(),
            sent_count=len(due),
            due_today=len(due_today), overdue=len(overdue),
            paused=False, muted=False, error=None, result=result,
        )
    except Exception as e:
        _last_todo_remind.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
        traceback.print_exc()
    finally:
        _last_todo_remind["running"] = False
        _todo_remind_lock.release()


def run_jerujam_import():
    if not _jerujam_lock.acquire(blocking=False):
        return
    _last_jerujam["running"] = True
    try:
        counts = import_jerujam.run()
        _last_jerujam.update(
            at=datetime.now(timezone.utc).isoformat(),
            count=counts,
            error=None,
        )
    except Exception as e:
        _last_jerujam.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
        traceback.print_exc()
    finally:
        _last_jerujam["running"] = False
        _jerujam_lock.release()


def run_backup():
    if not _backup_lock.acquire(blocking=False):
        return
    _last_backup["running"] = True
    try:
        stamp = datetime.now().strftime("%Y-%m-%d")
        dest = BACKUP_DIR / f"kartis-{stamp}.db"
        db.backup(dest)
        _prune_old_backups()
        _last_backup.update(
            at=datetime.now(timezone.utc).isoformat(),
            path=str(dest),
            error=None,
        )
    except Exception as e:
        _last_backup.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
        traceback.print_exc()
    finally:
        _last_backup["running"] = False
        _backup_lock.release()


def _prune_old_backups():
    if not BACKUP_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - BACKUP_KEEP_DAYS * 86400
    for f in BACKUP_DIR.glob("kartis-*.db"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def run_scraper():
    if not _run_lock.acquire(blocking=False):
        return
    _last_run["running"] = True
    try:
        counts = scraper.run_and_save()
        _last_run.update(
            at=datetime.now(timezone.utc).isoformat(),
            count=counts,
            error=None,
        )
    except Exception as e:
        _last_run.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
        traceback.print_exc()
    finally:
        _last_run["running"] = False
        _run_lock.release()


def _enrich(rows):
    for r in rows:
        cost = r.get("total_cost")
        lst = r.get("total_list")
        r["profit_loss"] = round(lst - cost, 2) if cost is not None and lst is not None else None
    return rows


def _enrich_viagogo(rows):
    for r in rows:
        avail = r.get("available") or 0
        sold = r.get("sold") or 0
        fv = r.get("face_value") or 0
        r["cost"] = round(avail * fv, 2)
        r["sold_cost"] = round(sold * fv, 2)
    return rows


def _norm(s):
    return (s or "").strip().lower()


_DATE_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%d %b %Y",
    "%d %B %Y",
    # Comma-less variants — e.g. Viagogo's "Jun 14 2026" after stripping the
    # leading day-of-week and trailing time.
    "%b %d %Y",
    "%B %d %Y",
)

_DAYNAMES = {"mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
             "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def _parse_event_date(text):
    if not text:
        return None
    head = (text or "").split("•")[0].split("@")[0].strip().rstrip(",").strip()
    head = head.split(" ")
    # Strip leading day-of-week tokens like "Sun", "Sun,", "Sunday" — Viagogo
    # dates frequently start with these and they aren't part of any strptime
    # format we use.
    while head and head[0].rstrip(",").lower() in _DAYNAMES:
        head.pop(0)
    # Strip trailing time tokens like "08:00PM" if they slipped in
    while head and any(c.isdigit() for c in head[-1]) and (":" in head[-1] or head[-1].lower().endswith(("am", "pm"))):
        head.pop()
    candidate = " ".join(head).strip(", ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    import re
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


def _date_only(iso):
    if not iso:
        return iso
    return iso.split("T", 1)[0]


def _resolve_iso(row):
    return _date_only(row.get("event_date_iso") or _parse_event_date(row.get("event_date")))


def _norm_venue(s):
    """Normalize a venue string for cluster/dedupe matching.
    Drops the trailing ", City" / ", City, State" suffix that some sources
    (Lysted in particular) tack on, strips a leading "The ", and lowercases.
    Lets us treat "The Eastern" / "The Eastern, Atlanta" as the same room
    while keeping "The Eastern" and "District Atlanta" — same artist, same
    night, two distinct venues — apart.
    """
    s = (s or "").lower()
    # Drop city/state suffix after the first comma.
    s = s.split(",", 1)[0]
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _event_key(event_name, iso, venue=None):
    return (_norm(event_name), iso or "", _norm_venue(venue))


_INV_NUMERIC = {"qty_unsold", "cost", "cost_per_unit", "list_price"}
_SALE_NUMERIC = {"qty", "sale_price", "cost"}


def _norm_event_name(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _event_groups():
    """Build a canonical map for (event_name, iso, venue) → group key.

    Cluster fuzzy duplicates ("A Boogie Wit Da Hoodie" vs "A Boogie With Da
    Hoodie") so inventory rows and sales rows share the same group key. The
    UI uses this for its event-grouped click-to-expand behaviour.

    Venue is part of the cluster identity so an artist playing two distinct
    rooms on the same night (e.g. Disclosure at The Eastern AND District
    Atlanta on 2026-05-01) gets two separate groups instead of one merged
    bucket. Venue is normalized via _norm_venue so trailing ", City" and
    leading "The " variations don't fragment the cluster.
    """
    from difflib import SequenceMatcher
    triples = set()
    for r in db.all_lysted_purchases():
        triples.add((r.get("event_name") or "", _date_only(_resolve_iso(r) or ""), _norm_venue(r.get("venue"))))
    for r in db.all_lysted_sales():
        triples.add((r.get("event_name") or "", _date_only(r.get("event_date_iso") or ""), _norm_venue(r.get("venue"))))
    for r in db.all_viagogo():
        triples.add((r.get("event_name") or "", _date_only(_resolve_iso(r) or ""), _norm_venue(r.get("venue"))))
    for r in db.all_viagogo_sales():
        triples.add((r.get("event_name") or "", _date_only(r.get("event_date_iso") or ""), _norm_venue(r.get("venue"))))
    for r in db.all_jerujam_tickets():
        triples.add((r.get("event_name") or "", _date_only(r.get("event_date_iso") or ""), _norm_venue(r.get("venue"))))
    j_tix = {t["id"]: t for t in db.all_jerujam_tickets()}
    for s in db.all_jerujam_sales():
        t = j_tix.get(s.get("ticket_id"), {})
        triples.add((t.get("event_name") or "", _date_only(t.get("event_date_iso") or ""), _norm_venue(t.get("venue"))))
    for r in db.all_crowdvolt_sales():
        triples.add((r.get("event_name") or "", _date_only(r.get("event_date_iso") or ""), _norm_venue(r.get("venue"))))
    # Inventory aggregate is a fallback source for the listing-rows path —
    # include its venues here too so the group key for an aggregate row
    # matches the corresponding sales rows.
    for r in db.all_inventory():
        triples.add((r.get("event_name") or "", _date_only(_resolve_iso(r) or ""), _norm_venue(r.get("venue"))))

    def _substr_match(short, long):
        # Whitespace-bounded substring match. Used for both event names and
        # venues so "Masonic Temple" and "Masonic Temple - Temple Theatre"
        # cluster together while "the eastern" and "district atlanta" stay
        # apart.
        if not short or not long:
            return False
        if short == long:
            return True
        if len(short) < 3:
            return False
        return (
            f" {short} " in f" {long} "
            or long.startswith(short + " ")
            or long.endswith(" " + short)
        )

    def _venues_match(a, b):
        if a == b:
            return True
        if not a or not b:
            # Treat blank venue as a wildcard so a sales row missing venue
            # doesn't fragment off into its own cluster.
            return True
        short, long = (a, b) if len(a) <= len(b) else (b, a)
        return _substr_match(short, long)

    canonicals = []  # list[(norm_name, iso, norm_venue)]
    mapping = {}
    # Sort shorter names first so the canonical for a cluster is the
    # cleanest representation ("jigitz" anchors the cluster, then
    # "jigitz rescheduled from 3 7 26" attaches to it via substring match).
    for name, iso, venue in sorted(triples, key=lambda p: (len(_norm_event_name(p[0])), p[0])):
        norm = _norm_event_name(name)
        if not norm:
            mapping[(name, iso, venue)] = f"|{iso}|{venue}"
            continue
        found = None
        for cn, ciso, cvenue in canonicals:
            if ciso != iso:
                continue
            if not _venues_match(cvenue, venue):
                continue
            # Substring match (with whitespace boundary) catches
            # "jigitz" vs "jigitz rescheduled from 3 7 26" which the
            # SequenceMatcher ratio would otherwise miss because the
            # extra suffix tanks the similarity score.
            short, long = (cn, norm) if len(cn) <= len(norm) else (norm, cn)
            if (
                cn == norm
                or _substr_match(short, long)
                or SequenceMatcher(None, cn, norm).ratio() >= 0.85
            ):
                found = (cn, ciso, cvenue)
                break
        if found:
            mapping[(name, iso, venue)] = f"{found[0]}|{found[1]}|{found[2]}"
        else:
            canonicals.append((norm, iso, venue))
            mapping[(name, iso, venue)] = f"{norm}|{iso}|{venue}"

    # User-driven merges: overwrite the auto-cluster mapping for any raw
    # group_key the user explicitly merged. Multiple raw keys can map to
    # the same canonical key — that's how two unrelated names ("Ishay Ribo
    # — early" + "Ishay Ribo — National Library of Israel") get unified
    # into one display group.
    user_merges = db.all_event_group_merges()
    if user_merges:
        for triple, auto_key in list(mapping.items()):
            if auto_key in user_merges:
                mapping[triple] = user_merges[auto_key]["canonical_group_key"]
    return mapping


def _event_group_displays():
    """Returns {canonical_group_key → {event_name, event_date,
    event_date_iso, venue}} for each user-merged group. Used by row
    builders to overwrite the per-row display fields after grouping so the
    chosen canonical name shows everywhere the merged group appears."""
    out = {}
    for m in db.all_event_group_merges().values():
        out[m["canonical_group_key"]] = {
            "event_name": m.get("canonical_event_name"),
            "event_date": m.get("canonical_event_date"),
            "event_date_iso": m.get("canonical_event_date_iso"),
            "venue": m.get("canonical_venue"),
        }
    return out


def _apply_group_displays(rows, displays):
    """Overwrite event_name/date/venue on rows whose group is in a merge."""
    if not displays:
        return
    for r in rows:
        d = displays.get(r.get("event_group"))
        if not d:
            continue
        for f in ("event_name", "event_date", "event_date_iso", "venue"):
            if d.get(f):
                r[f] = d[f]


def _bought_by_event(groups_map):
    """Total tickets purchased per event_group. Each source gives an
    independent "they bought at least N" signal; we take the max so
    JeruJam-tracked inventory that's *also* listed/sold via Lysted doesn't
    double-count.

    Sources:
      - lysted_purchases.qty (when the order is still on the purchases page)
      - lysted_active = inventory.tickets_count + lysted_sales.qty  (covers
        events where the purchase record is missing — common after a few
        weeks once Lysted clears stale orders — but the tickets are clearly
        still live or were sold)
      - jerujam_tickets.quantity
      - manual_inventory.qty (unmatched)
      - viagogo: available + sold
    """
    per_source = {}  # {group_key: {source: total_qty_bought}}
    # Honor inventory_hidden here too — otherwise a user hiding a listing
    # via × would still see the bought-count include it, leaving the sales
    # page to render a phantom "(no detail)" row that's itself undeletable.
    hidden = db.all_hidden_keys()

    def _add(src, name, iso, venue, qty):
        key = _row_group(groups_map, name, _date_only(iso or ""), venue)
        per_source.setdefault(key, {})
        per_source[key][src] = per_source[key].get(src, 0) + int(qty or 0)

    for r in db.all_lysted_purchases():
        if ("lysted", str(r.get("id"))) in hidden:
            continue
        _add("lysted_purchases", r.get("event_name"), _resolve_iso(r), r.get("venue"), r.get("qty"))
    # Lysted's currently-active scraped inventory + recent sales tells us
    # the same event existed even if the purchases page rolled it off.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    for r in db.all_inventory():
        if ("lysted", str(r.get("id"))) in hidden:
            continue
        # Skip stale inventory for past events. A show that already happened
        # can't have tickets "still listed", but Lysted keeps reporting a
        # nonzero tickets_count on sold-out past listings for a while. Counting
        # it makes bought = stale_listed + sold overcount the true buy and
        # spawns an undeletable phantom "still listed" row on the sales page.
        # Sales still feed lysted_active below, so bought stays >= sold here.
        # (Blank-date rows fall through and are still counted.)
        iso_i = _date_only(_resolve_iso(r) or "")
        if iso_i and iso_i < today_iso:
            continue
        _add("lysted_active", r.get("event_name"), _resolve_iso(r), r.get("venue"), r.get("tickets_count"))
    for r in db.all_lysted_sales():
        _add("lysted_active", r.get("event_name"), r.get("event_date_iso"), r.get("venue"), r.get("qty"))
    for r in db.all_jerujam_tickets():
        if ("jerujam", str(r.get("id"))) in hidden:
            continue
        _add("jerujam", r.get("event_name"), r.get("event_date_iso"), r.get("venue"), r.get("quantity"))
    for r in db.all_manual_inventory():
        if r.get("matched_source"):
            continue  # already represented by the matched lysted/viagogo row
        if ("manual", str(r.get("id"))) in hidden:
            continue
        _add("manual", r.get("event_name"), r.get("event_date_iso"), r.get("venue"), r.get("qty"))
    for r in db.all_viagogo():
        if ("viagogo", str(r.get("id"))) in hidden:
            continue
        _add("viagogo", r.get("event_name"), _resolve_iso(r), r.get("venue"), (r.get("available") or 0) + (r.get("sold") or 0))

    out = {}
    for k, sources in per_source.items():
        out[k] = max(sources.values()) if sources else 0
    return out


def _row_group(mapping, name, iso, venue=None):
    nv = _norm_venue(venue)
    iso_d = _date_only(iso or "")
    return mapping.get((name or "", iso_d, nv), f"{_norm_event_name(name)}|{iso_d}|{nv}")


def _apply_overrides(row, overrides_for_row, numeric_fields):
    if not overrides_for_row:
        return
    edited = []
    for field, value in overrides_for_row.items():
        if field in numeric_fields:
            try:
                row[field] = float(value) if value not in (None, "") else None
            except (ValueError, TypeError):
                row[field] = value
        else:
            row[field] = value
        edited.append(field)
    row["_edited"] = edited


def _ga_like(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    if "general admission" in t:
        return True
    tokens = t.replace("/", " ").split()
    return "ga" in tokens or "pit" in tokens


def _build_unified_inventory():
    """Combine unsold tickets from Lysted + Viagogo + JeruJam.

    JeruJam tickets are deduped against Lysted by (event, section, row) and
    against Viagogo by (event, section). The Viagogo dedupe is intentionally
    coarser since Viagogo listings don't expose row-level data.

    Rows whose (source, source_id) is in inventory_hidden are excluded — that's
    the soft-delete the dashboard uses.
    """
    hidden = db.all_hidden_keys()
    lysted = db.all_lysted_purchases()
    viagogo = _enrich_viagogo(db.all_viagogo())
    jerujam = db.all_jerujam_tickets()
    j_sales = db.all_jerujam_sales()
    groups = _event_groups()

    # Exact StubHub event URLs captured per event by the Lysted inventory-grid
    # scrape (scraper.py:_row_stubhub_url). Keyed by the same canonical group
    # key the UI uses so the grid event and its purchase-order rows line up
    # despite event-date string differences.
    stubhub_by_group = {}
    for inv in db.all_inventory():
        url = inv.get("stubhub_url")
        if url:
            g = _row_group(groups, inv.get("event_name"), inv.get("event_date_iso"), inv.get("venue"))
            stubhub_by_group[g] = url

    sold_per_ticket = {}
    for s in j_sales:
        sold_per_ticket[s["ticket_id"]] = sold_per_ticket.get(s["ticket_id"], 0) + (s.get("quantity") or 0)
    # Cross-source / manual matches keyed by (inv_source, str(inv_source_id)).
    # Same dict feeds:
    #   - the JeruJam branch (was the only one honoring matches before — kept
    #     for backwards compatibility via ext_matched_per_ticket below)
    #   - Lysted / Viagogo / manual branches (NEW: previously ignored matches,
    #     so "Mark as Loss" on a Viagogo or Lysted row left the row sitting
    #     in active inventory because no qty was subtracted).
    matched_qty = {}
    ext_matched_per_ticket = {}
    for m in db.all_matches():
        src = m.get("inv_source")
        sid = m.get("inv_source_id")
        q = m.get("qty_matched") or 0
        matched_qty[(src, str(sid))] = matched_qty.get((src, str(sid)), 0) + q
        if src == "jerujam":
            ext_matched_per_ticket[sid] = ext_matched_per_ticket.get(sid, 0) + q

    # Pool of auto-detected Lysted sales keyed by (event_group, section_norm, row_norm).
    # Lysted's purchase-orders scrape returns the original purchase qty unchanged
    # after partial sales, so without this we'd show "12 unsold" on a listing
    # where Lysted has separately reported 4 sales. Allocated greedily across
    # matching listings as we iterate them below.
    lysted_sales_pool = {}
    for s in db.all_lysted_sales():
        ek_g = _row_group(groups, s.get("event_name"), s.get("event_date_iso"), s.get("venue"))
        bucket = (ek_g, _norm(s.get("section")), _norm(s.get("row_label")))
        lysted_sales_pool[bucket] = lysted_sales_pool.get(bucket, 0) + (s.get("qty") or 0)

    rows = []

    lysted_keys = set()
    lysted_event_groups = set()  # canonical group-level dedup for inventory-aggregate fallback below
    lysted_ga_event_rows = set()  # (event_key, row) for any GA-style Lysted listing
    for r in lysted:
        if (r.get("status") or "").strip().lower() == "sold":
            continue
        iso = _resolve_iso(r)
        ek = _event_key(r.get("event_name"), iso, r.get("venue"))
        sec_n = _norm(r.get("section"))
        row_n = _norm(r.get("row_label"))
        lysted_keys.add((ek, sec_n, row_n))
        lysted_event_groups.add(_row_group(groups, r.get("event_name"), iso, r.get("venue")))
        if _ga_like(sec_n):
            lysted_ga_event_rows.add((ek, row_n))
            lysted_ga_event_rows.add((ek, ""))
        if ("lysted", str(r.get("id"))) in hidden:
            continue
        qty_full = r.get("qty") or 0
        consumed = matched_qty.get(("lysted", str(r.get("id"))), 0)
        # Greedy draw from the auto-sales pool for this (event, section, row) bucket.
        bucket = (
            _row_group(groups, r.get("event_name"), iso, r.get("venue")),
            sec_n,
            row_n,
        )
        pool_left = lysted_sales_pool.get(bucket, 0)
        auto_consumed = min(max(0, qty_full - consumed), pool_left)
        if auto_consumed > 0:
            lysted_sales_pool[bucket] = pool_left - auto_consumed
        qty_remaining = max(0, qty_full - consumed - auto_consumed)
        if qty_remaining <= 0:
            continue
        rows.append({
            "source": "lysted",
            "source_id": r.get("id"),
            "event_name": r.get("event_name"),
            "event_date": r.get("event_date"),
            "event_date_iso": iso,
            "venue": r.get("venue"),
            "section": r.get("section"),
            "row": r.get("row_label"),
            "seats": r.get("seats"),
            "qty_unsold": qty_remaining,
            "cost": r.get("total_cost") or 0,
            "cost_per_unit": r.get("cost_per_unit"),
            "delivery_type": r.get("delivery_type"),
            "list_price": None,
            "status": r.get("status") or "active",
            "stubhub_url": stubhub_by_group.get(
                _row_group(groups, r.get("event_name"), iso, r.get("venue"))
            ),
        })

    # Pre-scan Viagogo + JeruJam canonical group keys so the inventory-aggregate
    # fallback below can dedup against them too. Use _row_group (the same
    # canonical clustering the UI uses) so cross-source venue-string variants
    # like "Mortgage Matchup Center" vs "Mortgage Matchup Center • Phoenix, AZ"
    # collapse to the same group instead of slipping past the dedup.
    viagogo_event_groups = {
        _row_group(groups, r.get("event_name"), _resolve_iso(r), r.get("venue"))
        for r in viagogo if (r.get("available") or 0) > 0
    }
    jerujam_event_groups = {
        _row_group(groups, t.get("event_name"), _resolve_iso(t), t.get("venue"))
        for t in jerujam if (t.get("status") or "").strip().lower() != "sold"
    }

    # Fallback: include the inventory-table aggregate for events not covered
    # by lysted_purchases above. Tickets you've listed on Lysted that don't
    # have a matching purchase order (e.g. purchased outside Lysted, or the
    # purchase row rolled off) only show up here. Without this fallback,
    # those listings are invisible on the sales page and undeletable via × —
    # the renderer falls through to a synthesized phantom row with no
    # source_id. The composite inventory.id is used as source_id so
    # inventory_hidden works the same way.
    for inv in db.all_inventory():
        iso_i = _resolve_iso(inv)
        eg = _row_group(groups, inv.get("event_name"), iso_i, inv.get("venue"))
        if eg in lysted_event_groups or eg in viagogo_event_groups or eg in jerujam_event_groups:
            continue
        qty = inv.get("tickets_count") or 0
        if qty <= 0:
            continue
        if ("lysted", str(inv.get("id"))) in hidden:
            continue
        consumed = matched_qty.get(("lysted", str(inv.get("id"))), 0)
        qty_remaining = max(0, qty - consumed)
        if qty_remaining <= 0:
            continue
        total_cost = inv.get("total_cost") or 0
        cost_per = round(total_cost / qty, 2) if qty else None
        rows.append({
            "source": "lysted",
            "source_id": inv.get("id"),
            "event_name": inv.get("event_name"),
            "event_date": inv.get("event_date"),
            "event_date_iso": iso_i,
            "venue": inv.get("venue"),
            "section": "",
            "row": "",
            "seats": "",
            "qty_unsold": qty_remaining,
            "cost": total_cost,
            "cost_per_unit": cost_per,
            "delivery_type": "",
            "list_price": inv.get("total_list"),
            "status": "active",
            "stubhub_url": inv.get("stubhub_url"),
        })

    viagogo_keys = set()
    viagogo_ga_events = set()  # event_keys for any GA-style Viagogo listing
    for r in viagogo:
        avail = r.get("available") or 0
        if avail <= 0:
            continue
        iso = _resolve_iso(r)
        ek = _event_key(r.get("event_name"), iso, r.get("venue"))
        sec_n = _norm(r.get("section"))
        viagogo_keys.add((ek, sec_n))
        if _ga_like(sec_n):
            viagogo_ga_events.add(ek)
        if ("viagogo", str(r.get("id"))) in hidden:
            continue
        consumed = matched_qty.get(("viagogo", str(r.get("id"))), 0)
        avail_remaining = max(0, avail - consumed)
        if avail_remaining <= 0:
            continue
        rows.append({
            "source": "viagogo",
            "source_id": r.get("id"),
            "event_name": r.get("event_name"),
            "event_date": r.get("event_date"),
            "event_date_iso": iso,
            "venue": r.get("venue"),
            "section": r.get("section"),
            "row": "",
            "seats": r.get("ticket_type"),
            "qty_unsold": avail_remaining,
            "cost": r.get("cost") or 0,
            "cost_per_unit": r.get("face_value"),
            "delivery_type": r.get("ticket_type"),
            "list_price": (r.get("price") or 0) * avail_remaining,
            "status": r.get("visibility") or "active",
        })

    skipped_jerujam = 0
    for t in jerujam:
        if (t.get("status") or "").strip().lower() == "sold":
            continue
        qty = t.get("quantity") or 0
        sold = sold_per_ticket.get(t.get("id"), 0)
        ext_sold = ext_matched_per_ticket.get(t.get("id"), 0)
        remaining = max(0, qty - sold - ext_sold)
        if remaining <= 0:
            continue
        iso_j = _resolve_iso(t)
        ek = _event_key(t.get("event_name"), iso_j, t.get("venue"))
        sec = _norm(t.get("section"))
        row = _norm(t.get("row_label"))
        if (ek, sec, row) in lysted_keys:
            skipped_jerujam += 1
            continue
        # GA/PIT label variants — Lysted is authoritative when both sides are
        # any flavor of GA. JeruJam GA PIT (row blank) collapses into Lysted PIT G5.
        if _ga_like(sec) and ((ek, row) in lysted_ga_event_rows or (ek, "") in lysted_ga_event_rows):
            skipped_jerujam += 1
            continue
        if (ek, sec) in viagogo_keys:
            skipped_jerujam += 1
            continue
        if _ga_like(sec) and ek in viagogo_ga_events:
            skipped_jerujam += 1
            continue
        if ("jerujam", str(t.get("id"))) in hidden:
            continue
        cost_per = t.get("cost_per_ticket") or 0
        rows.append({
            "source": "jerujam",
            "source_id": t.get("id"),
            "event_name": t.get("event_name"),
            "event_date": t.get("event_date"),
            "event_date_iso": iso_j,
            "venue": t.get("venue"),
            "section": t.get("section"),
            "row": t.get("row_label"),
            "seats": t.get("seat_numbers"),
            "qty_unsold": remaining,
            "cost": round(cost_per * remaining, 2),
            "cost_per_unit": cost_per or None,
            "delivery_type": t.get("listing_platform") or "",
            "list_price": (t.get("listing_price") or None) and round((t.get("listing_price") or 0) * remaining, 2),
            "status": t.get("status") or "(none)",
        })

    # Pending (manually-added) tickets the user has but hasn't listed yet
    for m in db.all_manual_inventory():
        if m.get("matched_source"):
            continue  # already showed up on Lysted/Viagogo
        if ("manual", str(m.get("id"))) in hidden:
            continue
        qty = m.get("qty") or 0
        if qty <= 0:
            continue
        consumed = matched_qty.get(("manual", str(m.get("id"))), 0)
        qty = max(0, qty - consumed)
        if qty <= 0:
            continue
        cost_per = m.get("cost_per_unit")
        rows.append({
            "source": "manual",
            "source_id": str(m.get("id")),
            "event_name": m.get("event_name"),
            "event_date": m.get("event_date"),
            "event_date_iso": m.get("event_date_iso"),
            "venue": m.get("venue"),
            "section": m.get("section"),
            "row": m.get("row_label"),
            "seats": m.get("seats"),
            "qty_unsold": qty,
            "cost": round((cost_per or 0) * qty, 2) if cost_per else 0,
            "cost_per_unit": cost_per,
            "delivery_type": m.get("note") or "",
            "list_price": None,
            "status": "pending",
        })

    inv_overrides = db.all_inv_overrides()
    seats_sold_map = db.seats_sold_by_inv()
    for r in rows:
        ov = inv_overrides.get((r.get("source"), str(r.get("source_id"))))
        if ov:
            _apply_overrides(r, ov, _INV_NUMERIC)
        r["event_group"] = _row_group(groups, r.get("event_name"), r.get("event_date_iso"), r.get("venue"))
        r["seats_sold_already"] = seats_sold_map.get((r.get("source"), str(r.get("source_id"))), "")
    # Apply user-merged-group display overrides AFTER per-row overrides so a
    # merged event uses the canonical name even if a single row had its own
    # event_name override pre-merge.
    _apply_group_displays(rows, _event_group_displays())

    # "Didn't Sell" archive — filter rows whose content fingerprint is in
    # inventory_unsold. Done as a final pass so overrides are already applied
    # (so the fingerprint matches what was captured at archive time).
    unsold_fps = db.all_unsold_fingerprints()
    if unsold_fps:
        kept = []
        for r in rows:
            fp = db.unsold_fingerprint(
                r.get("source"), r.get("event_name"), r.get("event_date_iso"),
                r.get("section"), r.get("row"), r.get("seats"), r.get("qty_unsold"),
            )
            if fp in unsold_fps:
                continue
            kept.append(r)
        rows = kept
    return rows, skipped_jerujam


def _migrate_legacy_unsold_overrides():
    """One-shot migration: convert pre-existing inventory_overrides rows
    whose status field matches the "not sold" pattern into proper
    inventory_unsold archive entries.

    Idempotent — once an override is migrated and deleted, subsequent runs
    find no candidates. Safe to call on every startup.

    Returns the number of rows archived.
    """
    candidates = []
    with db.connect() as conn:
        for r in conn.execute(
            "SELECT source, source_id, value FROM inventory_overrides "
            "WHERE field = 'status'"
        ).fetchall():
            if r["value"] and _UNSOLD_RE.match(str(r["value"])):
                candidates.append((r["source"], r["source_id"]))
    if not candidates:
        return 0
    rows, _ = _build_unified_inventory()
    by_key = {(r.get("source"), str(r.get("source_id"))): r for r in rows}
    now_iso = datetime.now(timezone.utc).isoformat()
    archived = 0
    for src, sid in candidates:
        r = by_key.get((src, sid))
        if r:
            fp = db.unsold_fingerprint(
                src, r.get("event_name"), r.get("event_date_iso"),
                r.get("section"), r.get("row"), r.get("seats"), r.get("qty_unsold"),
            )
            snap = {
                "fingerprint": fp, "source": src, "source_id": sid,
                "event_name": r.get("event_name"),
                "event_date": r.get("event_date"),
                "event_date_iso": r.get("event_date_iso"),
                "venue": r.get("venue"),
                "section": r.get("section"),
                "row_label": r.get("row"),
                "seats": r.get("seats"),
                "qty": r.get("qty_unsold"),
                "cost": r.get("cost"),
                "cost_per_unit": r.get("cost_per_unit"),
                "list_price": r.get("list_price"),
                "delivery_type": r.get("delivery_type"),
                "note": "auto-migrated from legacy status='not sold' override",
            }
            db.mark_inventory_unsold(snap, now_iso)
            archived += 1
    # Drop the legacy overrides whether or not the row still exists — if the
    # source row is gone, the override is dead weight; if it was migrated,
    # the override would conflict with the displayed status next render.
    with db.connect() as conn:
        for src, sid in candidates:
            conn.execute(
                "DELETE FROM inventory_overrides "
                "WHERE source = ? AND source_id = ? AND field = 'status'",
                (src, sid),
            )
    return archived


def _matched_cost(matches_idx, jerujam_idx, sale_source, sale_id, qty):
    """If a sale is paired to a JeruJam inventory row, use that ticket's
    cost_per_ticket × qty as the sale's cost."""
    m = matches_idx.get((sale_source, str(sale_id)))
    if not m or m["inv_source"] != "jerujam":
        return None
    j = jerujam_idx.get(m["inv_source_id"])
    if not j:
        return None
    cpt = j.get("cost_per_ticket")
    if cpt is None:
        return None
    return round(cpt * (qty or 0), 2)


def _build_combined_sales(only_canceled=False):
    """Combine sale events across sources. Sources differ in fidelity:
    JeruJam has per-sale rows; Lysted/Viagogo only expose aggregates so
    each sold-row contributes a single coarse entry.

    By default returns the active sales list (excludes both hidden and
    canceled). Pass only_canceled=True to invert the filter and return only
    rows in the canceled archive — used to power the "// CANCELED" section
    on the sales page.
    """
    out = []
    hidden_sales = db.all_hidden_sale_keys()
    canceled_sales = db.all_canceled_sale_keys()
    if only_canceled:
        sale_skip = hidden_sales  # keep canceled, drop hidden
    else:
        sale_skip = hidden_sales | canceled_sales

    def _skip_sale(key):
        if key in sale_skip:
            return True
        if only_canceled and key not in canceled_sales:
            return True
        return False

    j_tickets = {t["id"]: t for t in db.all_jerujam_tickets()}
    matches_idx = {(m["sale_source"], m["sale_id"]): m for m in db.all_matches()}
    jerujam_keys = set()
    for s in db.all_jerujam_sales():
        t = j_tickets.get(s.get("ticket_id"), {})
        jerujam_keys.add((
            _norm(t.get("event_name")),
            (s.get("sale_date") or "")[:10],
            s.get("quantity") or 0,
            round(s.get("sale_price") or 0, 0),
        ))
        if _skip_sale(("jerujam", str(s.get("id")))):
            continue
        sale_price = s.get("sale_price") or 0
        out.append({
            "source": "jerujam",
            "sale_id": str(s.get("id")),
            "order_id": "",
            "payout": sale_price,
            "sale_date": s.get("sale_date") or "",
            "sale_date_iso": (s.get("sale_date") or "")[:10],
            "event_name": t.get("event_name") or "",
            "event_date": t.get("event_date") or "",
            "event_date_iso": _date_only(t.get("event_date_iso") or _parse_event_date(t.get("event_date"))) or "",
            "venue": t.get("venue") or "",
            "section": t.get("section") or "",
            "row": t.get("row_label") or "",
            "platform": s.get("platform") or "",
            "qty": s.get("quantity") or 0,
            "sale_price": s.get("sale_price") or 0,
            "cost": round((t.get("cost_per_ticket") or 0) * (s.get("quantity") or 0), 2),
            "is_new": False,
        })

    purchases_by_id = {r.get("id"): r for r in db.all_lysted_purchases()}
    purchases_cost_by_event = {}
    for r in db.all_lysted_purchases():
        key = (r.get("event_name") or "", r.get("section") or "", r.get("row_label") or "")
        purchases_cost_by_event[key] = r.get("cost_per_unit")
    for r in db.all_lysted_sales():
        qty = r.get("qty") or 0
        # Lysted's API returns cost directly — that's the source of truth.
        cost = r.get("cost")
        if cost is None:
            # Fallback chain for older rows that haven't been re-scraped yet.
            cost_per = None
            match = purchases_by_id.get(r.get("id"))
            if match:
                cost_per = match.get("cost_per_unit")
            if cost_per is None:
                cost_per = purchases_cost_by_event.get(
                    (r.get("event_name") or "", r.get("section") or "", r.get("row_label") or "")
                )
            cost = round((cost_per or 0) * qty, 2) if cost_per is not None else 0
            if cost == 0:
                mc = _matched_cost(matches_idx, j_tickets, "lysted", r.get("id"), qty)
                if mc is not None:
                    cost = mc
        sale_iso_short = (r.get("sale_date_iso") or "")[:10]
        key = (_norm(r.get("event_name")), sale_iso_short, qty, round(r.get("sale_price") or 0, 0))
        if _skip_sale(("lysted", str(r.get("id")))):
            continue
        payout = r.get("payout") if r.get("payout") is not None else r.get("sale_price")
        out.append({
            "source": "lysted",
            "sale_id": str(r.get("id")),
            "order_id": str(r.get("order_id") or "").strip(),
            "payout": payout,
            "sale_date": r.get("sale_date") or "",
            "sale_date_iso": (r.get("sale_date_iso") or "")[:10],
            "event_name": r.get("event_name") or "",
            "event_date": r.get("event_date") or "",
            "event_date_iso": _date_only(r.get("event_date_iso") or _parse_event_date(r.get("event_date"))) or "",
            "venue": r.get("venue") or "",
            "section": r.get("section") or "",
            "row": r.get("row_label") or "",
            "platform": "lysted",
            "qty": qty,
            "sale_price": r.get("sale_price"),
            "cost": cost,
            "is_new": key not in jerujam_keys,
        })

    viagogo_listings = _enrich_viagogo(db.all_viagogo())
    listing_lookup = {}

    def _split_listing_section(text):
        """Viagogo listings cram "Section\\nRow X , Seat Y" into one cell.
        Pull out the section name and row number separately."""
        if not text:
            return ("", "")
        first_line = text.splitlines()[0].strip() if "\n" in text else text.strip()
        m = re.search(r"Row\s+(\S+)", text or "")
        row = m.group(1) if m else ""
        return (first_line, row)

    def _clean_row(text):
        if not text:
            return ""
        m = re.match(r"\s*(\S+)", text)
        return (m.group(1) if m else "").strip()

    for r in viagogo_listings:
        sec, row = _split_listing_section(r.get("section") or "")
        ev_n = _norm(r.get("event_name"))
        # Index by (event, section, row) — most specific
        listing_lookup.setdefault((ev_n, _norm(sec), _norm(row)), r)
        # Also keep an event+section fallback for sales that don't have a row
        listing_lookup.setdefault((ev_n, _norm(sec), ""), r)

    for r in db.all_viagogo_sales():
        ev_n = _norm(r.get("event_name"))
        sec_n = _norm(r.get("section"))
        row_n = _norm(_clean_row(r.get("row_label") or ""))
        listing = (listing_lookup.get((ev_n, sec_n, row_n))
                   or listing_lookup.get((ev_n, sec_n, "")))
        cost_per = listing.get("face_value") if listing else None
        qty = r.get("qty") or 0
        cost = round((cost_per or 0) * qty, 2) if cost_per is not None else 0
        if cost == 0:
            mc = _matched_cost(matches_idx, j_tickets, "viagogo", r.get("id"), qty)
            if mc is not None:
                cost = mc
        key = (_norm(r.get("event_name")), (r.get("sale_date_iso") or "")[:10], qty, round(r.get("sale_price") or 0, 0))
        if _skip_sale(("viagogo", str(r.get("id")))):
            continue
        out.append({
            "source": "viagogo",
            "sale_id": str(r.get("id")),
            "order_id": str(r.get("order_id") or "").strip(),
            "payout": r.get("sale_price"),
            "sale_date": r.get("sale_date") or "",
            "sale_date_iso": (r.get("sale_date_iso") or "")[:10],
            "event_name": r.get("event_name") or "",
            "event_date": r.get("event_date") or "",
            "event_date_iso": _date_only(r.get("event_date_iso") or _parse_event_date(r.get("event_date"))) or "",
            "venue": r.get("venue") or "",
            "section": r.get("section") or "",
            "row": r.get("row_label") or "",
            "platform": "viagogo",
            "qty": qty,
            "sale_price": r.get("sale_price"),
            "cost": cost,
            "is_new": key not in jerujam_keys,
        })

    for r in db.all_crowdvolt_sales():
        qty = r.get("qty") or 0
        sale_price = r.get("sale_price") or 0
        key = (_norm(r.get("event_name")), (r.get("sale_date_iso") or "")[:10], qty, round(sale_price, 0))
        cost = 0
        # CrowdVolt is a last-minute dump for tickets bought via Lysted or
        # Viagogo. Pull cost from a matching purchase/listing in those tables.
        cv_event = r.get("event_name") or ""
        cv_section = r.get("ticket_type") or ""
        cv_qty = qty
        for lp in db.all_lysted_purchases():
            if not matcher._events_match(lp.get("event_name"), cv_event):
                continue
            if (lp.get("qty") or 0) != cv_qty:
                continue
            cost = lp.get("total_cost") or ((lp.get("cost_per_unit") or 0) * cv_qty)
            if cost:
                break
        if not cost:
            for vl in viagogo_listings:
                if not matcher._events_match(vl.get("event_name"), cv_event):
                    continue
                v_sec = (vl.get("section") or "").splitlines()[0] if vl.get("section") else ""
                if cv_section and v_sec and not (
                    _norm(cv_section) == _norm(v_sec)
                    or (_ga_like(cv_section) and _ga_like(v_sec))
                ):
                    continue
                face = vl.get("face_value") or 0
                if face > 0:
                    cost = round(face * cv_qty, 2)
                    break
        if not cost:
            mc = _matched_cost(matches_idx, j_tickets, "crowdvolt", r.get("id"), qty)
            if mc is not None:
                cost = mc
        if _skip_sale(("crowdvolt", str(r.get("id")))):
            continue
        out.append({
            "source": "crowdvolt",
            "sale_id": str(r.get("id")),
            "order_id": str(r.get("order_id") or "").strip(),
            "payout": sale_price,
            "sale_date": r.get("sale_date") or "",
            "sale_date_iso": (r.get("sale_date_iso") or "")[:10],
            "event_name": r.get("event_name") or "",
            "event_date": r.get("event_date") or "",
            "event_date_iso": _date_only(r.get("event_date_iso")) or "",
            "venue": r.get("venue") or "",
            "section": r.get("ticket_type") or "",
            "row": "",
            "platform": "crowdvolt",
            "qty": qty,
            "sale_price": sale_price,
            "cost": cost,
            "is_new": key not in jerujam_keys,
        })

    for m in db.all_manual_sales():
        if _skip_sale(("manual", str(m.get("id")))):
            continue
        qty = m.get("qty") or 0
        sale_price = m.get("sale_price") or 0
        out.append({
            "source": "manual",
            "sale_id": str(m.get("id")),
            "order_id": "",
            "payout": sale_price,
            "sale_date": m.get("sale_date") or "",
            "sale_date_iso": (m.get("sale_date_iso") or "")[:10],
            "event_name": m.get("event_name") or "",
            "event_date": m.get("event_date") or "",
            "event_date_iso": _date_only(m.get("event_date_iso") or _parse_event_date(m.get("event_date"))) or "",
            "venue": m.get("venue") or "",
            "section": m.get("section") or "",
            "row": m.get("row_label") or "",
            "platform": m.get("platform") or "manual",
            "qty": qty,
            "sale_price": sale_price,
            "cost": m.get("cost") or 0,
            "is_loss": bool(m.get("is_loss")),
            "is_new": False,
        })

    sale_overrides = db.all_sale_overrides()
    groups = _event_groups()
    for r in out:
        ov = sale_overrides.get((r.get("source"), str(r.get("sale_id"))))
        if ov:
            _apply_overrides(r, ov, _SALE_NUMERIC)
        r["event_group"] = _row_group(groups, r.get("event_name"), r.get("event_date_iso"), r.get("venue"))
    _apply_group_displays(out, _event_group_displays())
    return out


@app.route("/")
def home():
    return render_template("inventory.html")


@app.route("/sources")
def dashboard():
    return render_template("dashboard.html")


@app.route("/sales")
def sales_page():
    return render_template("sales.html")


@app.route("/mockups")
def mockups_index():
    return render_template("mockups/index.html")


@app.route("/mockups/<name>")
def mockup(name):
    if name not in {"slate", "cocoa", "studio"}:
        return "Unknown mockup", 404
    return render_template(f"mockups/{name}.html")


@app.route("/api/inventory-all")
def api_inventory_all():
    rows, skipped = _build_unified_inventory()
    by_source = {"lysted": 0, "viagogo": 0, "jerujam": 0}
    manual_ids = [str(r.get("source_id")) for r in rows if r.get("source") == "manual"]
    atts_by_owner = db.list_attachments_for_owners("manual_inventory", manual_ids)
    for r in rows:
        if r.get("source") == "manual":
            r["attachments"] = atts_by_owner.get(str(r.get("source_id")), [])
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    totals = {
        "rows": len(rows),
        "tickets": sum(r["qty_unsold"] for r in rows),
        "total_cost": round(sum(r["cost"] or 0 for r in rows), 2),
        "by_source": by_source,
        "jerujam_skipped_dedupe": skipped,
        "auto_matched": len(db.all_matches()),
    }
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run, "last_backup": _last_backup})


@app.route("/api/events/suggest")
def api_events_suggest():
    """Slim event suggestions for the Pending modal autocomplete. Aggregates
    raw (event_name, iso, venue) triples from every source so a typed prefix
    finds events the user has touched anywhere in the app.

    Returns up to 8 suggestions of {event_name, event_date_iso, venue}, sorted
    by date descending so upcoming/recent events surface first.
    """
    from flask import request
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"suggestions": []})
    qn = _norm_event_name(q)
    if not qn:
        return jsonify({"suggestions": []})

    raw = []
    for r in db.all_lysted_purchases():
        raw.append((r.get("event_name") or "", _date_only(_resolve_iso(r) or ""), r.get("venue") or ""))
    for r in db.all_viagogo():
        raw.append((r.get("event_name") or "", _date_only(_resolve_iso(r) or ""), r.get("venue") or ""))
    for r in db.all_jerujam_tickets():
        raw.append((r.get("event_name") or "", _date_only(r.get("event_date_iso") or ""), r.get("venue") or ""))
    for r in db.all_manual_inventory():
        raw.append((r.get("event_name") or "", _date_only(r.get("event_date_iso") or ""), r.get("venue") or ""))
    for r in db.all_inventory():
        raw.append((r.get("event_name") or "", _date_only(_resolve_iso(r) or ""), r.get("venue") or ""))

    # Dedupe by (norm_name, iso, norm_venue) and keep the longest raw name +
    # longest raw venue per cluster — that's our display representative.
    by_key = {}
    for name, iso, venue in raw:
        if not name:
            continue
        nn = _norm_event_name(name)
        if qn not in nn:
            continue
        key = (nn, iso, _norm_venue(venue))
        cur = by_key.get(key)
        if cur is None:
            by_key[key] = (name, iso, venue)
        else:
            cn, ci, cv = cur
            new_name = name if len(name) > len(cn) else cn
            new_venue = venue if len(venue or "") > len(cv or "") else cv
            by_key[key] = (new_name, ci, new_venue)

    items = list(by_key.values())
    items.sort(key=lambda t: t[1] or "", reverse=True)
    items = items[:8]
    return jsonify({
        "suggestions": [
            {"event_name": name, "event_date_iso": iso, "venue": venue}
            for (name, iso, venue) in items
        ]
    })


@app.route("/profit")
def profit_page():
    return render_template("profit.html")


def _profit_response():
    """Build the same payload returned by /api/profit/daily. Reused by the
    Maaser summary so both pages agree on what 'profit' means.
    Adds month["expenses"] / month["net_profit"] derived from the expenses
    table (operating costs reduce the Maaser base per the user's policy)."""
    sales = _build_combined_sales()
    # Lysted payout per sale (we have it on lysted_sales rows directly)
    lysted_payouts_by_id = {str(r.get("id")): (r.get("payout") or 0) for r in db.all_lysted_sales()}
    by_day = {}
    by_month = {}
    by_source = {"lysted": {"count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0},
                 "viagogo": {"count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0},
                 "jerujam": {"count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0},
                 "crowdvolt":{"count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0}}
    for s in sales:
        date = (s.get("sale_date_iso") or "")[:10]
        if not date or not date.startswith("20"):
            continue
        rev = s.get("sale_price") or 0
        cost = s.get("cost") or 0
        qty = s.get("qty") or 0
        bucket = by_day.setdefault(date, {"date": date, "count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0,
                                           "by_source": {"lysted":0,"viagogo":0,"jerujam":0,"crowdvolt":0}})
        bucket["count"] += 1
        bucket["qty"] += qty
        bucket["revenue"] += rev
        bucket["cost"] += cost
        bucket["profit"] += (rev - cost)
        bucket["by_source"][s.get("source", "?")] = bucket["by_source"].get(s.get("source", "?"), 0) + 1
        # Per-source totals
        src = s.get("source") or "?"
        if src in by_source:
            by_source[src]["count"] += 1
            by_source[src]["qty"] += qty
            by_source[src]["revenue"] += rev
            by_source[src]["cost"] += cost
            by_source[src]["profit"] += (rev - cost)

    # Pull Lysted payouts by day directly from lysted_sales (for accurate per-day payout totals)
    for r in db.all_lysted_sales():
        d = (r.get("sale_date_iso") or "")[:10]
        if not d:
            continue
        bucket = by_day.setdefault(d, {"date": d, "count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0,
                                       "by_source": {"lysted":0,"viagogo":0,"jerujam":0,"crowdvolt":0}})
        bucket["payout"] += (r.get("payout") or 0)
        by_source["lysted"]["payout"] += (r.get("payout") or 0)

    # Build month rollup from days (sale-month: bucketed by when the
    # ticket was sold). This is the cash-flow-style view.
    for d, row in by_day.items():
        m = d[:7]  # YYYY-MM
        mb = by_month.setdefault(m, {"month": m, "count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0})
        mb["count"] += row["count"]; mb["qty"] += row["qty"]
        mb["revenue"] += row["revenue"]; mb["cost"] += row["cost"]
        mb["profit"] += row["profit"]; mb["payout"] += row["payout"]

    # Build a parallel month rollup keyed by EVENT date — "May events"
    # means sales for shows that take place in May, regardless of when
    # the sale itself happened. This is the view the dashboard treats as
    # canonical for monthly profit (and what the Maaser obligation reads
    # from). Sales whose event_date is unknown fall back to sale_date so
    # totals still tie out across both views.
    by_month_event = {}
    for s in sales:
        ev_date = (s.get("event_date_iso") or "")[:10]
        if not ev_date or not ev_date.startswith("20"):
            ev_date = (s.get("sale_date_iso") or "")[:10]
        if not ev_date or not ev_date.startswith("20"):
            continue
        m_key = ev_date[:7]
        rev = s.get("sale_price") or 0
        cost = s.get("cost") or 0
        qty = s.get("qty") or 0
        mb = by_month_event.setdefault(m_key, {"month": m_key, "count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0})
        mb["count"] += 1; mb["qty"] += qty
        mb["revenue"] += rev; mb["cost"] += cost
        mb["profit"] += (rev - cost)
    # Lysted payout still tracked per sale-month; carry the per-sale
    # payout into the event-month bucket as well so it stays comparable.
    sales_by_id = {(s.get("source"), s.get("sale_id")): s for s in sales}
    for r in db.all_lysted_sales():
        s_match = sales_by_id.get(("lysted", str(r.get("id"))))
        ev_date_iso = (s_match or {}).get("event_date_iso") or (r.get("sale_date_iso") or "")
        ev_date = (ev_date_iso or "")[:10]
        if not ev_date or not ev_date.startswith("20"):
            continue
        m_key = ev_date[:7]
        mb = by_month_event.setdefault(m_key, {"month": m_key, "count":0,"qty":0,"revenue":0,"cost":0,"profit":0,"payout":0})
        mb["payout"] += (r.get("payout") or 0)

    days = sorted(by_day.values(), key=lambda r: r["date"], reverse=True)
    months = sorted(by_month.values(), key=lambda r: r["month"], reverse=True)
    months_by_event = sorted(by_month_event.values(), key=lambda r: r["month"], reverse=True)

    # Round and add profit_pct
    def _finish(row):
        for k in ("revenue","cost","profit","payout"):
            row[k] = round(row[k] or 0, 2)
        rev = row["revenue"] or 0
        row["profit_pct"] = round((row["profit"] / rev) * 100, 1) if rev else None
        return row
    days = [_finish(r) for r in days]
    months = [_finish(r) for r in months]
    months_by_event = [_finish(r) for r in months_by_event]
    for k, v in by_source.items():
        _finish(v)

    totals = _finish({
        "count": sum(r["count"] for r in days),
        "qty": sum(r["qty"] for r in days),
        "revenue": sum(r["revenue"] for r in days),
        "cost": sum(r["cost"] for r in days),
        "profit": sum(r["profit"] for r in days),
        "payout": sum(r["payout"] for r in days),
    })

    # Layer in operating expenses at month-level (subscriptions + one-offs).
    # Expenses are tied to when they were paid (calendar month), not to any
    # particular event — so the same expense number applies to BOTH views.
    expenses_by_month = {}
    for e in db.all_expenses():
        mk = (e.get("date_iso") or "")[:7]
        if not mk:
            continue
        expenses_by_month[mk] = expenses_by_month.get(mk, 0) + (e.get("amount") or 0)
    # Cost of tickets that didn't sell, bucketed by the month the event
    # took place. For the by-sale-date monthly view this is treated as a
    # realized loss for that month — the show is over, the cost is sunk.
    unsold_cost_by_month = {}
    for u in db.all_unsold():
        mk = (u.get("event_date_iso") or "")[:7]
        if not mk:
            continue
        unsold_cost_by_month[mk] = unsold_cost_by_month.get(mk, 0) + (u.get("cost") or 0)
    for m in months:
        m["expenses"] = round(expenses_by_month.get(m["month"], 0), 2)
        m["unsold_cost"] = round(unsold_cost_by_month.get(m["month"], 0), 2)
        m["net_profit"] = round((m.get("profit") or 0) - m["expenses"] - m["unsold_cost"], 2)
    for m in months_by_event:
        m["expenses"] = round(expenses_by_month.get(m["month"], 0), 2)
        m["net_profit"] = round((m.get("profit") or 0) - m["expenses"], 2)
    totals_expenses = round(sum(expenses_by_month.values()), 2)
    totals_unsold = round(sum(unsold_cost_by_month.values()), 2)
    totals["expenses"] = totals_expenses
    totals["unsold_cost"] = totals_unsold
    totals["net_profit"] = round((totals.get("profit") or 0) - totals_expenses - totals_unsold, 2)

    return {
        "days": days,
        "months": months,                     # by sale date
        "months_by_event": months_by_event,   # by event date — canonical view
        "totals": totals,
        "by_source": by_source,
    }


@app.route("/api/profit/daily")
def api_profit_daily():
    return jsonify(_profit_response())


@app.route("/api/sales-all")
def api_sales_all():
    rows = _build_combined_sales()
    by_source = {"lysted": 0, "viagogo": 0, "jerujam": 0, "crowdvolt": 0}
    new_count = 0
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r.get("is_new"):
            new_count += 1
    totals = {
        "rows": len(rows),
        "qty": sum(r["qty"] or 0 for r in rows),
        "revenue": round(sum((r["sale_price"] or 0) for r in rows), 2),
        "cost": round(sum((r["cost"] or 0) for r in rows), 2),
        "by_source": by_source,
        "new_since_jerujam": new_count,
    }
    # "Didn't Sell" archive — separate top-level array so existing rollups
    # (revenue, profit, maaser) stay clean. Frontend renders these in their
    # own section below the sold table.
    unsold_rows = db.all_unsold()
    unsold_totals = {
        "rows": len(unsold_rows),
        "qty": sum((u.get("qty") or 0) for u in unsold_rows),
        "cost": round(sum((u.get("cost") or 0) for u in unsold_rows), 2),
    }
    # Canceled-sale archive — same pattern. Each row carries the platform's
    # canceled_at + optional reason on top of the regular sale fields.
    canceled_rows = _build_combined_sales(only_canceled=True)
    canceled_meta = {(c["source"], c["sale_id"]): c for c in db.all_canceled_sales()}
    for cr in canceled_rows:
        meta = canceled_meta.get((cr.get("source"), str(cr.get("sale_id"))), {})
        cr["canceled_at"] = meta.get("canceled_at")
        cr["cancel_reason"] = meta.get("reason")
    canceled_totals = {
        "rows": len(canceled_rows),
        "qty": sum((c.get("qty") or 0) for c in canceled_rows),
        "lost_revenue": round(sum((c.get("sale_price") or 0) for c in canceled_rows), 2),
    }
    return jsonify({
        "rows": rows,
        "totals": totals,
        "bought_by_event": _bought_by_event(_event_groups()),
        "last_run": _last_run,
        "unsold": unsold_rows,
        "unsold_totals": unsold_totals,
        "canceled": canceled_rows,
        "canceled_totals": canceled_totals,
    })


@app.route("/api/inventory")
def api_inventory():
    rows = _enrich(db.all_inventory())
    totals = {
        "events": len(rows),
        "listings": sum(r.get("listings_count") or 0 for r in rows),
        "tickets": sum(r.get("tickets_count") or 0 for r in rows),
        "total_cost": round(sum(r.get("total_cost") or 0 for r in rows), 2),
        "total_list": round(sum(r.get("total_list") or 0 for r in rows), 2),
    }
    totals["total_pl"] = round(totals["total_list"] - totals["total_cost"], 2)
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run, "last_backup": _last_backup})


@app.route("/api/lysted-purchases")
def api_lysted_purchases():
    rows = db.all_lysted_purchases()
    def _live(r):
        s = (r.get("status") or "").strip().lower()
        return s != "sold"
    active = [r for r in rows if _live(r)]
    sold = [r for r in rows if not _live(r)]
    totals = {
        "tickets": sum(r.get("qty") or 0 for r in rows),
        "active_tickets": sum(r.get("qty") or 0 for r in active),
        "sold_tickets": sum(r.get("qty") or 0 for r in sold),
        "active_cost": round(sum(r.get("total_cost") or 0 for r in active), 2),
        "sold_cost": round(sum(r.get("total_cost") or 0 for r in sold), 2),
        "total_cost": round(sum(r.get("total_cost") or 0 for r in rows), 2),
    }
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run, "last_backup": _last_backup})


@app.route("/api/viagogo")
def api_viagogo():
    rows = _enrich_viagogo(db.all_viagogo())
    totals = {
        "listings": len(rows),
        "tickets_available": sum(r.get("available") or 0 for r in rows),
        "tickets_sold": sum(r.get("sold") or 0 for r in rows),
        "total_cost": round(sum(r.get("cost") or 0 for r in rows), 2),
        "sold_cost": round(sum(r.get("sold_cost") or 0 for r in rows), 2),
        "total_price": round(sum((r.get("price") or 0) * (r.get("available") or 0) for r in rows), 2),
        "total_proceeds": round(sum((r.get("proceeds") or 0) * (r.get("available") or 0) for r in rows), 2),
    }
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run, "last_backup": _last_backup})


@app.route("/api/sales/hide", methods=["POST"])
def api_sales_hide():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    if not source or not sale_id:
        return jsonify({"error": "source and sale_id required"}), 400
    db.hide_sale(source, sale_id, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True})


@app.route("/api/sales/unhide", methods=["POST"])
def api_sales_unhide():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    if not source or not sale_id:
        return jsonify({"error": "source and sale_id required"}), 400
    db.unhide_sale(source, sale_id)
    return jsonify({"ok": True})


@app.route("/api/sales/cancel", methods=["POST"])
def api_sales_cancel():
    """Mark a sale as canceled. Distinct from hide (× delete):
    - Sale moves out of active sales rollups (revenue / profit / maaser)
    - Surfaced separately on the sales page under "// CANCELED"
    - Any matcher pairing for this sale is cleared, and if the inventory
      row was auto-hidden because of the match, it is restored
    - Matcher blocklist gets the sale_id so the next pass doesn't re-pair
    The platform (Viagogo etc.) typically auto-relists the underlying
    ticket; the regular scrape picks that up — Kartis doesn't synthesize
    a new inventory row.
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    reason = (body.get("reason") or "").strip() or None
    if not source or not sale_id:
        return jsonify({"error": "source and sale_id required"}), 400
    now_iso = datetime.now(timezone.utc).isoformat()
    db.cancel_sale(source, sale_id, now_iso, reason=reason)
    # Match cleanup — find any inventory_matches row for this sale, drop it,
    # and unhide the matched inventory row (matcher.py:247 hides whole-qty
    # matches). Also blocklist the sale so the matcher doesn't immediately
    # re-pair on next run.
    matched_inv = []
    for m in db.all_matches():
        if m.get("sale_source") == source and m.get("sale_id") == sale_id:
            matched_inv.append((m.get("inv_source"), m.get("inv_source_id")))
    for inv_src, inv_sid in matched_inv:
        db.unhide_inventory(inv_src, inv_sid)
    db.delete_match(source, sale_id)
    db.add_blocklist(source, sale_id, now_iso)
    return jsonify({
        "ok": True,
        "source": source, "sale_id": sale_id,
        "released_inventory": [{"source": s, "source_id": i} for s, i in matched_inv],
    })


@app.route("/api/sales/uncancel", methods=["POST"])
def api_sales_uncancel():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    if not source or not sale_id:
        return jsonify({"error": "source and sale_id required"}), 400
    db.uncancel_sale(source, sale_id)
    return jsonify({"ok": True})


@app.route("/api/event-groups/merge", methods=["POST"])
def api_event_groups_merge():
    """Merge 2+ raw event groups into one canonical group with a chosen
    display name + date + venue. Subsequent inventory and sales rows that
    fall in any of the merged raw groups will display under the canonical
    name. Body:
        {
          "group_keys": ["<raw_key_1>", "<raw_key_2>", ...],
          "event_name": "Ishay Ribo at the National Library",
          "event_date_iso": "2026-06-12",       # optional
          "event_date": "Jun 12, 2026",         # optional display text
          "venue": "National Library of Israel" # optional
        }
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    group_keys = body.get("group_keys") or []
    event_name = (body.get("event_name") or "").strip()
    if not isinstance(group_keys, list) or len(group_keys) < 2:
        return jsonify({"error": "group_keys must be a list of 2+ raw group keys"}), 400
    if not event_name:
        return jsonify({"error": "event_name required"}), 400
    iso = (body.get("event_date_iso") or "").strip()[:10]
    raw_date = (body.get("event_date") or "").strip()
    venue = (body.get("venue") or "").strip()
    canonical_group_key = f"{_norm_event_name(event_name)}|{iso}|{_norm_venue(venue)}"
    # Resolve already-merged group_keys: if the user selected a group that
    # was itself a canonical from a prior merge, replace it with all its raw
    # keys so we don't strand old mappings pointing at a now-orphan canonical.
    existing = db.all_event_group_merges()
    canonical_to_raws = {}
    for raw, m in existing.items():
        canonical_to_raws.setdefault(m["canonical_group_key"], []).append(raw)
    expanded = set()
    for k in group_keys:
        expanded.add(k)
        if k in canonical_to_raws:
            for raw in canonical_to_raws[k]:
                expanded.add(raw)
    db.merge_event_groups(
        sorted(expanded), canonical_group_key,
        event_name, raw_date, iso, venue,
        datetime.now(timezone.utc).isoformat(),
    )
    return jsonify({
        "ok": True,
        "canonical_group_key": canonical_group_key,
        "merged_raw_keys": sorted(expanded),
    })


@app.route("/api/event-groups/unmerge", methods=["POST"])
def api_event_groups_unmerge():
    """Drop a merge. Body either:
      {"canonical_group_key": "..."}  → splits the entire merged group
      {"raw_group_key": "..."}        → removes just one raw key from its merge
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    canonical = (body.get("canonical_group_key") or "").strip()
    raw = (body.get("raw_group_key") or "").strip()
    if canonical:
        db.unmerge_canonical(canonical)
    elif raw:
        db.unmerge_event_group(raw)
    else:
        return jsonify({"error": "canonical_group_key or raw_group_key required"}), 400
    return jsonify({"ok": True})


@app.route("/api/event-groups/merges")
def api_event_groups_merges():
    """List all current merges grouped by canonical key."""
    merges = db.all_event_group_merges()
    by_canonical = {}
    for raw, m in merges.items():
        by_canonical.setdefault(m["canonical_group_key"], {
            "canonical_group_key": m["canonical_group_key"],
            "event_name": m.get("canonical_event_name"),
            "event_date": m.get("canonical_event_date"),
            "event_date_iso": m.get("canonical_event_date_iso"),
            "venue": m.get("canonical_venue"),
            "raw_keys": [],
        })["raw_keys"].append(raw)
    return jsonify({"merges": list(by_canonical.values())})


@app.route("/pending")
def pending_page():
    return render_template("pending.html")


@app.route("/listings")
def listings_page():
    return render_template("listings.html")


@app.route("/api/pending-intake")
def api_pending_intake():
    rows = db.all_pending_intake(status="new")
    ids = [r["id"] for r in rows]
    atts = db.list_attachments_for_owners("manual_intake", ids)
    for r in rows:
        r["attachments"] = atts.get(r["id"], [])
    return jsonify({
        "rows": rows,
        "last_intake": _last_intake,
    })


@app.route("/api/pending-intake/poll", methods=["POST"])
def api_pending_intake_poll():
    if _intake_lock.locked():
        return jsonify({"ok": False, "error": "already running"}), 429
    threading.Thread(target=run_mail_intake, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/pending-intake/confirm", methods=["POST"])
def api_pending_intake_confirm():
    """Promote a pending_intake row + its attachments to a manual_inventory
    row. The user can override any of the parsed fields in the request body
    (the UI sends the edited values straight from the modal). Files stay on
    disk where they were saved during intake — we just rename the on-disk
    folder and re-point the DB attachment rows at the new owner_id."""
    from flask import request
    body = request.get_json(silent=True) or {}
    intake_id = (body.get("id") or "").strip()
    if not intake_id:
        return jsonify({"error": "id required"}), 400
    intake = db.get_pending_intake(intake_id)
    if not intake:
        return jsonify({"error": "not found"}), 404
    event_name = (body.get("event_name") or intake.get("event_name") or "").strip()
    if not event_name:
        return jsonify({"error": "event_name required"}), 400
    try:
        qty = int(body.get("qty") or intake.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "qty must be > 0"}), 400
    cost_per = body.get("cost_per_unit") if "cost_per_unit" in body else intake.get("cost_per_unit")
    try:
        cost_per = float(cost_per) if cost_per not in (None, "") else None
    except (TypeError, ValueError):
        cost_per = None
    iso = (body.get("event_date_iso") or intake.get("event_date_iso") or "").strip()
    new_id = "pending-" + uuid.uuid4().hex[:12]
    row = {
        "id": new_id,
        "event_name": event_name,
        "event_date": iso,
        "event_date_iso": iso,
        "venue": (body.get("venue") or intake.get("venue") or "").strip(),
        "section": (body.get("section") or intake.get("section") or "").strip(),
        "row_label": (body.get("row") or body.get("row_label") or intake.get("row_label") or "").strip(),
        "seats": (body.get("seats") or intake.get("seats") or "").strip(),
        "qty": qty,
        "cost_per_unit": cost_per,
        "note": (body.get("note") or "").strip(),
        "email": (body.get("email") or intake.get("email_from") or "").strip(),
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    db.insert_manual_inventory(row, now_iso)
    # Move the on-disk folder so the new owner_id matches the file path.
    old_dir = attachments_mod.ATTACH_DIR / intake_id
    new_dir = attachments_mod.ATTACH_DIR / new_id
    if old_dir.exists():
        try:
            old_dir.rename(new_dir)
        except OSError:
            pass
    # Update each attachment row: change owner_type/owner_id + stored_path prefix.
    for a in db.list_attachments("manual_intake", intake_id):
        new_stored = a["stored_path"].replace(intake_id, new_id, 1)
        with db.connect() as conn:
            conn.execute(
                "UPDATE attachments SET owner_type=?, owner_id=?, stored_path=? WHERE id=?",
                ("manual_inventory", new_id, new_stored, a["id"]),
            )
    db.update_pending_intake(intake_id, {"status": "confirmed"})
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/pending-intake/reject", methods=["POST"])
def api_pending_intake_reject():
    """Drop the intake row + its attachments. Disk files are deleted too —
    rejection means we don't want them; if the user changes their mind they
    can re-forward the email."""
    from flask import request
    body = request.get_json(silent=True) or {}
    intake_id = (body.get("id") or "").strip()
    if not intake_id:
        return jsonify({"error": "id required"}), 400
    for a in db.list_attachments("manual_intake", intake_id):
        attachments_mod.delete(a["id"])
    db.delete_pending_intake(intake_id)
    intake_dir = attachments_mod.ATTACH_DIR / intake_id
    if intake_dir.exists():
        try:
            intake_dir.rmdir()
        except OSError:
            pass
    return jsonify({"ok": True})


# ---------------- viagogo draft-listing push (Kupat -> viagogo) ------------
# Kupat purchase emails get auto-matched to a viagogo event + auto-priced at
# 5x the FX-converted USD cost during intake (see mail_intake._push_kupat_to_
# viagogo), landing here as 'awaiting_approval'. Creating the actual draft
# listing always requires an explicit approve click — see viagogo_listing.
# create_draft_listing's docstring for why.

def _run_viagogo_approve(push_id, event_id, search_query, ticket_type, section,
                          available_tickets, website_price, face_value, row,
                          seat_from, seat_to, venue_for_map, kupat_section,
                          ticket_url=None):
    now = lambda: datetime.now(timezone.utc).isoformat()
    db.viagogo_push_update(push_id, {"status": "creating"}, now())
    try:
        ticket_pdfs = None
        if ticket_url:
            try:
                _qty = (db.viagogo_push_get(push_id) or {}).get("qty") or 1
                ticket_pdfs = viagogo_listing.download_ticket_pdfs(ticket_url, qty=int(_qty))
            except Exception:
                traceback.print_exc()
        viagogo_listing.create_draft_listing(
            event_id=event_id, search_query=search_query, ticket_type=ticket_type,
            section=section, available_tickets=available_tickets,
            website_price=website_price, face_value=face_value,
            row=row, seat_from=seat_from, seat_to=seat_to,
            ticket_pdfs=ticket_pdfs,
        )
        if venue_for_map and kupat_section and section:
            db.viagogo_section_map_set(venue_for_map, kupat_section, section, now())
        # Teach Hebrew→English name mapping so future emails auto-match.
        push_row = db.viagogo_push_get(push_id) or {}
        hebrew_event = push_row.get("event_name") or ""
        english_event = push_row.get("chosen_event_name") or ""
        if hebrew_event and english_event and hebrew_event != english_event:
            db.kupat_name_map_set(hebrew_event, english_event, now())
        db.viagogo_push_update(push_id, {"status": "created", "viagogo_section": section}, now())
    except Exception as e:
        db.viagogo_push_update(push_id, {"status": "error", "error": f"{type(e).__name__}: {e}"}, now())
        traceback.print_exc()


@app.route("/api/viagogo-push")
def api_viagogo_push():
    from flask import request
    status = (request.args.get("status") or "").strip() or None
    rows = db.viagogo_push_all(status=status)
    for r in rows:
        try:
            r["candidates"] = json.loads(r["candidates_json"]) if r.get("candidates_json") else []
        except Exception:
            r["candidates"] = []
        venue_for_map = r.get("chosen_venue") or r.get("venue") or ""
        r["suggested_viagogo_section"] = (
            db.viagogo_section_map_get(venue_for_map, r.get("section") or "")
            if venue_for_map and r.get("section") else None
        )
    return jsonify({"rows": rows})


# Simple in-process cache: (event_id, ticket_type) -> list[str]
_sections_cache: dict = {}


@app.route("/api/viagogo-sections")
def api_viagogo_sections():
    from flask import request
    event_id = (request.args.get("event_id") or "").strip()
    search_query = (request.args.get("search_query") or "").strip()
    ticket_type = (request.args.get("ticket_type") or "E-Tickets").strip()
    if not event_id or not search_query:
        return jsonify({"error": "event_id and search_query required"}), 400
    cache_key = (event_id, ticket_type)
    if cache_key in _sections_cache:
        return jsonify({"sections": _sections_cache[cache_key], "cached": True})
    try:
        sections = viagogo_listing.fetch_sections(event_id, search_query, ticket_type)
        _sections_cache[cache_key] = sections
        return jsonify({"sections": sections})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/viagogo-push/approve", methods=["POST"])
def api_viagogo_push_approve():
    from flask import request
    body = request.get_json(silent=True) or {}
    push_id = (body.get("id") or "").strip()
    if not push_id:
        return jsonify({"error": "id required"}), 400
    push = db.viagogo_push_get(push_id)
    if not push:
        return jsonify({"error": "not found"}), 404
    if push["status"] not in ("awaiting_approval", "error"):
        return jsonify({"error": f"cannot approve from status '{push['status']}'"}), 400
    ticket_url = (push.get("ticket_url") or "").strip() or None
    event_id = (body.get("event_id") or push.get("chosen_event_id") or "").strip()
    if not event_id:
        return jsonify({"error": "event_id required (no matched event)"}), 400
    section = (body.get("section") or "").strip()
    if not section:
        return jsonify({"error": "section required"}), 400
    ticket_type = (body.get("ticket_type") or "E-Tickets").strip()
    try:
        available_tickets = int(body.get("available_tickets") or push.get("qty") or 0)
    except (TypeError, ValueError):
        available_tickets = 0
    if available_tickets <= 0:
        return jsonify({"error": "available_tickets must be > 0"}), 400
    try:
        website_price = float(body.get("website_price") or push.get("website_price_usd") or 0)
    except (TypeError, ValueError):
        website_price = 0
    if website_price <= 0:
        return jsonify({"error": "website_price must be > 0"}), 400
    try:
        fv = body.get("face_value")
        face_value = float(fv) if fv not in (None, "") else float(push.get("cost_usd_per_ticket") or 0)
    except (TypeError, ValueError):
        face_value = 0
    if face_value <= 0:
        return jsonify({"error": "face_value must be > 0"}), 400
    row = (body.get("row") or push.get("row_label") or "").strip() or None
    seat_from = (body.get("seat_from") or "").strip() or None
    seat_to = (body.get("seat_to") or "").strip() or None
    if not seat_from and not seat_to and push.get("seats"):
        m = re.search(r"(\d+)\s*-\s*(\d+)", push["seats"])
        if m:
            seat_from, seat_to = m.group(1), m.group(2)
        elif push["seats"].strip().isdigit():
            seat_from = push["seats"].strip()
    search_query = push.get("chosen_event_name") or push.get("event_name") or push.get("venue") or ""
    venue_for_map = push.get("chosen_venue") or push.get("venue") or ""
    kupat_section = push.get("section") or ""
    threading.Thread(
        target=_run_viagogo_approve,
        args=(push_id, event_id, search_query, ticket_type, section, available_tickets,
              website_price, face_value, row, seat_from, seat_to, venue_for_map, kupat_section,
              ticket_url),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "status": "creating"})


@app.route("/api/viagogo-push/reject", methods=["POST"])
def api_viagogo_push_reject():
    from flask import request
    body = request.get_json(silent=True) or {}
    push_id = (body.get("id") or "").strip()
    if not push_id:
        return jsonify({"error": "id required"}), 400
    db.viagogo_push_update(push_id, {"status": "rejected"}, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True})


@app.route("/api/kupat-name-map", methods=["POST"])
def api_kupat_name_map():
    """Teach a Hebrew→English name mapping and optionally re-run the search
    for an existing no_match push row. Body: {hebrew, english, push_id?}."""
    from flask import request
    import threading
    body = request.get_json(silent=True) or {}
    hebrew = (body.get("hebrew") or "").strip()
    english = (body.get("english") or "").strip()
    if not hebrew or not english:
        return jsonify({"error": "hebrew and english required"}), 400
    now_iso = datetime.now(timezone.utc).isoformat()
    db.kupat_name_map_set(hebrew, english, now_iso)

    push_id = (body.get("push_id") or "").strip()
    if push_id:
        push = db.viagogo_push_get(push_id)
        if push and push.get("status") in ("no_match", "error"):
            # Re-run the search in background with the new mapping.
            def _retry(pid, fields, now):
                import mail_intake as _mi
                _mi._push_kupat_to_viagogo_update(pid, fields, now)
            fields = {k: push.get(k) for k in
                      ("event_name", "venue", "event_date_iso", "section",
                       "row_label", "seats", "qty", "cost", "cost_per_unit",
                       "ticket_url")}
            threading.Thread(
                target=_retry, args=(push_id, fields, now_iso), daemon=True
            ).start()
    return jsonify({"ok": True})


@app.route("/api/inventory/manual-add", methods=["POST"])
def api_inventory_manual_add():
    from flask import request
    import uuid
    body = request.get_json(silent=True) or {}
    event_name = (body.get("event_name") or "").strip()
    if not event_name:
        return jsonify({"error": "event_name required"}), 400
    try:
        qty = int(body.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "qty must be > 0"}), 400
    cost_per = body.get("cost_per_unit")
    try:
        cost_per = float(cost_per) if cost_per not in (None, "") else None
    except (TypeError, ValueError):
        cost_per = None
    event_date = (body.get("event_date") or "").strip()
    iso = (body.get("event_date_iso") or "").strip()
    if not iso and event_date:
        iso = _date_only(_parse_event_date(event_date)) or ""
    row = {
        "id": "pending-" + uuid.uuid4().hex[:12],
        "event_name": event_name,
        "event_date": event_date,
        "event_date_iso": iso,
        "venue": (body.get("venue") or "").strip(),
        "section": (body.get("section") or "").strip(),
        "row_label": (body.get("row") or "").strip(),
        "seats": (body.get("seats") or "").strip(),
        "qty": qty,
        "cost_per_unit": cost_per,
        "note": (body.get("note") or "").strip(),
        "email": (body.get("email") or "").strip(),
    }
    db.insert_manual_inventory(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/inventory/manual-delete", methods=["POST"])
def api_inventory_manual_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_manual_inventory(id_)
    # Drop the DB attachment rows but leave the files on disk so they
    # survive in OneDrive sync (per user preference).
    db.delete_attachments_for_owner("manual_inventory", id_)
    return jsonify({"ok": True})


@app.route("/api/inventory/manual-attach", methods=["POST"])
def api_inventory_manual_attach():
    from flask import request
    ticket_id = (request.form.get("ticket_id") or "").strip()
    if not ticket_id:
        return jsonify({"error": "ticket_id required"}), 400
    # Confirm the pending row actually exists so we don't accept uploads for
    # arbitrary owner_ids.
    existing = {m["id"] for m in db.all_manual_inventory()}
    if ticket_id not in existing:
        return jsonify({"error": "unknown ticket_id"}), 404
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "file required"}), 400
    # Probe size cheaply via stream seek; werkzeug's FileStorage wraps a
    # SpooledTemporaryFile so this is fine even for the 25 MB ceiling.
    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > attachments_mod.MAX_BYTES:
        return jsonify({"error": f"file too large (>{attachments_mod.MAX_BYTES} bytes)"}), 413
    row = attachments_mod.save_upload("manual_inventory", ticket_id, f)
    return jsonify({"ok": True, "attachment": row})


@app.route("/api/attachments/<att_id>/download")
def api_attachment_download(att_id):
    row = db.get_attachment(att_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    try:
        path = attachments_mod.disk_path(row)
    except ValueError:
        return jsonify({"error": "invalid path"}), 400
    if not path.exists():
        return jsonify({"error": "file missing"}), 404
    return send_file(
        str(path),
        as_attachment=True,
        download_name=row.get("filename") or "file",
        mimetype=row.get("content_type") or None,
    )


@app.route("/api/attachments/delete", methods=["POST"])
def api_attachment_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    attachments_mod.delete(id_)
    return jsonify({"ok": True})


# --- Expenses + Maaser ---------------------------------------------------

def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _ensure_subscription_instances():
    """For each active expense subscription, ensure an `expenses` row exists for
    every month from started_at_iso through min(today, ended_at_iso). Idempotent
    via UNIQUE(subscription_id, month_key) — running this on every page load is
    cheap and keeps history accurate even if Kartis was off on the 1st."""
    today = date.today()
    now_iso = datetime.now(timezone.utc).isoformat()
    for sub in db.all_expense_subscriptions():
        start = _parse_iso(sub.get("started_at_iso"))
        if not start:
            continue
        end = _parse_iso(sub.get("ended_at_iso")) or today
        end = min(end, today)
        cursor = date(start.year, start.month, 1)
        last = date(end.year, end.month, 1)
        while cursor <= last:
            month_key = cursor.strftime("%Y-%m")
            row = {
                "id": f"sub-{sub['id']}-{month_key}",
                "date": cursor.strftime("%Y-%m-%d"),
                "date_iso": cursor.strftime("%Y-%m-%d"),
                "vendor": sub.get("name"),
                "category": sub.get("category"),
                "amount": sub.get("amount"),
                "notes": "auto from subscription",
            }
            db.upsert_subscription_instance(sub["id"], month_key, row, now_iso)
            # Advance to first of next month
            nxt = cursor.replace(day=28) + timedelta(days=4)
            cursor = nxt.replace(day=1)


def _maaser_summary():
    """Owed = sum over EVENT-months of max(0, profit − expenses) × 0.10.
    Maaser is calculated against the month each event takes place in,
    not the month its tickets were sold. Given = sum of maaser_payments.
    Outstanding = owed − given."""
    profit_resp = _profit_response()
    owed_by_month = {}
    net_by_month = {}
    for m in profit_resp["months_by_event"]:
        net = (m.get("profit") or 0) - (m.get("expenses") or 0)
        net_by_month[m["month"]] = round(net, 2)
        owed_by_month[m["month"]] = round(max(0, net) * 0.10, 2)
    payments = db.all_maaser()
    given_by_month = {}
    for p in payments:
        key = (p.get("date_iso") or "")[:7]
        if not key:
            continue
        given_by_month[key] = given_by_month.get(key, 0) + (p.get("amount") or 0)
    given_by_month = {k: round(v, 2) for k, v in given_by_month.items()}
    owed_lifetime = round(sum(owed_by_month.values()), 2)
    given_lifetime = round(sum((p.get("amount") or 0) for p in payments), 2)
    this_month_key = date.today().strftime("%Y-%m")
    return {
        "owed_by_month": owed_by_month,
        "given_by_month": given_by_month,
        "net_by_month": net_by_month,
        "summary": {
            "owed_lifetime": owed_lifetime,
            "given_lifetime": given_lifetime,
            "outstanding": round(owed_lifetime - given_lifetime, 2),
            "owed_this_month": owed_by_month.get(this_month_key, 0),
        },
    }


@app.route("/expenses")
def expenses_page():
    return render_template("expenses.html")


@app.route("/maaser")
def maaser_page():
    return render_template("maaser.html")


@app.route("/api/expenses")
def api_expenses():
    _ensure_subscription_instances()
    subs = db.all_expense_subscriptions()
    expenses = db.all_expenses()
    totals_by_month = {}
    for e in expenses:
        mk = (e.get("date_iso") or "")[:7]
        if not mk:
            continue
        totals_by_month[mk] = round(totals_by_month.get(mk, 0) + (e.get("amount") or 0), 2)
    today = date.today()
    this_month = today.strftime("%Y-%m")
    this_year = today.strftime("%Y")
    recurring_monthly = sum((s.get("amount") or 0) for s in subs if not s.get("ended_at_iso"))
    this_month_total = totals_by_month.get(this_month, 0)
    this_year_total = round(sum(v for k, v in totals_by_month.items() if k.startswith(this_year)), 2)
    all_time_total = round(sum(totals_by_month.values()), 2)
    return jsonify({
        "subscriptions": subs,
        "expenses": expenses,
        "totals_by_month": totals_by_month,
        "summary": {
            "recurring_monthly": round(recurring_monthly, 2),
            "this_month": round(this_month_total, 2),
            "this_year": this_year_total,
            "all_time": all_time_total,
        },
        "last_run": _last_run, "last_backup": _last_backup,
    })


@app.route("/api/expenses/sub-add", methods=["POST"])
def api_expenses_sub_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        amount = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({"error": "amount must be > 0"}), 400
    started = (body.get("started_at_iso") or "").strip()
    if not _parse_iso(started):
        return jsonify({"error": "started_at_iso required (YYYY-MM-DD)"}), 400
    row = {
        "id": "sub-" + uuid.uuid4().hex[:12],
        "name": name,
        "category": (body.get("category") or "").strip(),
        "amount": amount,
        "started_at_iso": started,
        "ended_at_iso": (body.get("ended_at_iso") or "").strip() or None,
        "notes": (body.get("notes") or "").strip(),
    }
    db.insert_expense_subscription(row, datetime.now(timezone.utc).isoformat())
    _ensure_subscription_instances()
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/expenses/sub-edit", methods=["POST"])
def api_expenses_sub_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    fields = {}
    for k in ("name", "category", "amount", "started_at_iso", "ended_at_iso", "notes"):
        if k in body:
            v = body[k]
            if k == "amount":
                try:
                    v = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
            elif isinstance(v, str):
                v = v.strip() or None
            fields[k] = v
    db.update_expense_subscription(id_, fields)
    _ensure_subscription_instances()
    return jsonify({"ok": True})


@app.route("/api/expenses/sub-delete", methods=["POST"])
def api_expenses_sub_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    # Past auto-generated rows are intentionally left in `expenses` as historical
    # record; only the subscription template is removed.
    db.delete_expense_subscription(id_)
    return jsonify({"ok": True})


@app.route("/api/expenses/add", methods=["POST"])
def api_expenses_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    date_iso = (body.get("date_iso") or "").strip()
    if not _parse_iso(date_iso):
        return jsonify({"error": "date_iso required (YYYY-MM-DD)"}), 400
    try:
        amount = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({"error": "amount must be > 0"}), 400
    row = {
        "id": "exp-" + uuid.uuid4().hex[:12],
        "date": date_iso,
        "date_iso": date_iso,
        "vendor": (body.get("vendor") or "").strip(),
        "category": (body.get("category") or "").strip(),
        "amount": amount,
        "subscription_id": None,
        "month_key": None,
        "notes": (body.get("notes") or "").strip(),
    }
    db.insert_expense(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/expenses/edit", methods=["POST"])
def api_expenses_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    fields = {}
    for k in ("date_iso", "vendor", "category", "amount", "notes"):
        if k in body:
            v = body[k]
            if k == "amount":
                try:
                    v = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
            elif isinstance(v, str):
                v = v.strip()
            fields[k] = v
    if "date_iso" in fields and fields["date_iso"]:
        fields["date"] = fields["date_iso"]
    db.update_expense(id_, fields)
    return jsonify({"ok": True})


@app.route("/api/expenses/delete", methods=["POST"])
def api_expenses_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_expense(id_)
    return jsonify({"ok": True})


@app.route("/owed")
def owed_page():
    return render_template("owed.html")


@app.route("/api/owed")
def api_owed():
    return jsonify({
        "items": db.all_owed_items(),
        "last_run": _last_run, "last_backup": _last_backup,
    })


@app.route("/api/owed/add", methods=["POST"])
def api_owed_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    date_iso = (body.get("date_iso") or "").strip()
    if not _parse_iso(date_iso):
        return jsonify({"error": "date_iso required (YYYY-MM-DD)"}), 400
    try:
        amount = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({"error": "amount must be > 0"}), 400
    direction = (body.get("direction") or "").strip()
    if direction not in ("charge", "withdrawal"):
        return jsonify({"error": "direction must be 'charge' or 'withdrawal'"}), 400
    row = {
        "id": "owe-" + uuid.uuid4().hex[:12],
        "date_iso": date_iso,
        "amount": amount,
        "description": (body.get("description") or "").strip(),
        "card_account": (body.get("card_account") or "").strip(),
        "direction": direction,
    }
    db.insert_owed_item(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/owed/edit", methods=["POST"])
def api_owed_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    fields = {}
    for k in ("date_iso", "amount", "description", "card_account", "direction"):
        if k in body:
            v = body[k]
            if k == "amount":
                try:
                    v = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
            elif k == "direction":
                v = (v or "").strip()
                if v not in ("charge", "withdrawal"):
                    continue
            elif isinstance(v, str):
                v = v.strip()
            fields[k] = v
    db.update_owed_item(id_, fields)
    return jsonify({"ok": True})


@app.route("/api/owed/delete", methods=["POST"])
def api_owed_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_owed_item(id_)
    return jsonify({"ok": True})


# --- Cashback ledger -----------------------------------------------------
# Manual log of credit-card cashback rewards. Purely informational — does
# not feed into Maaser (user opted out: cashback is treated as a rebate,
# not income). Pattern mirrors /owed exactly.
_CASHBACK_EDITABLE = {"date_iso", "amount", "card_name"}


@app.route("/cashback")
def cashback_page():
    return render_template("cashback.html")


@app.route("/api/cashback")
def api_cashback():
    return jsonify({
        "items": db.all_cashback_entries(),
        "known_cards": db.distinct_cashback_cards(),
    })


@app.route("/api/cashback/add", methods=["POST"])
def api_cashback_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    date_iso = (body.get("date_iso") or "").strip()
    if not _parse_iso(date_iso):
        return jsonify({"error": "date_iso required (YYYY-MM-DD)"}), 400
    try:
        amount = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({"error": "amount must be > 0"}), 400
    card_name = (body.get("card_name") or "").strip()
    if not card_name:
        return jsonify({"error": "card_name required"}), 400
    row = {
        "id": "cb-" + uuid.uuid4().hex[:12],
        "date_iso": date_iso,
        "amount": amount,
        "card_name": card_name,
    }
    db.insert_cashback_entry(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/cashback/edit", methods=["POST"])
def api_cashback_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    fields = {}
    for k in _CASHBACK_EDITABLE:
        if k in body:
            v = body[k]
            if k == "amount":
                try:
                    v = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
                if v is None or v <= 0:
                    continue
            elif isinstance(v, str):
                v = v.strip()
                if k in ("date_iso", "card_name") and not v:
                    continue
            fields[k] = v
    db.update_cashback_entry(id_, fields)
    return jsonify({"ok": True})


@app.route("/api/cashback/delete", methods=["POST"])
def api_cashback_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_cashback_entry(id_)
    return jsonify({"ok": True})


# ---------------- Kupat credits ---------------------------------------------

_CREDIT_EDITABLE = {"issued_date", "ils_amount", "original_usd_cost", "note"}


@app.route("/credits")
def credits_page():
    return render_template("credits.html")


@app.route("/api/credits")
def api_credits():
    return jsonify(kupat_credits.build_credits_view())


@app.route("/api/credits/add", methods=["POST"])
def api_credits_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    issued_date = (body.get("issued_date") or "").strip()
    if not _parse_iso(issued_date):
        return jsonify({"error": "issued_date required (YYYY-MM-DD)"}), 400
    try:
        ils_amount = float(body.get("ils_amount") or 0)
    except (TypeError, ValueError):
        ils_amount = 0
    if ils_amount <= 0:
        return jsonify({"error": "ils_amount must be > 0"}), 400
    original_usd_cost = body.get("original_usd_cost")
    if original_usd_cost in (None, ""):
        original_usd_cost = None
    else:
        try:
            original_usd_cost = float(original_usd_cost)
        except (TypeError, ValueError):
            return jsonify({"error": "original_usd_cost must be numeric"}), 400
        if original_usd_cost < 0:
            return jsonify({"error": "original_usd_cost must be >= 0"}), 400
    note = (body.get("note") or "").strip()
    row = {
        "id": "kc-" + uuid.uuid4().hex[:12],
        "issued_date": issued_date,
        "ils_amount": ils_amount,
        "original_usd_cost": original_usd_cost,
        "note": note,
    }
    db.insert_kupat_credit(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/credits/edit", methods=["POST"])
def api_credits_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    fields = {}
    for k in _CREDIT_EDITABLE:
        if k not in body:
            continue
        v = body[k]
        if k == "ils_amount":
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
        elif k == "original_usd_cost":
            if v in (None, ""):
                v = None
            else:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if v < 0:
                    continue
        elif isinstance(v, str):
            v = v.strip()
            if k == "issued_date" and not v:
                continue
        fields[k] = v
    db.update_kupat_credit(id_, fields)
    return jsonify({"ok": True})


@app.route("/api/credits/delete", methods=["POST"])
def api_credits_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_kupat_credit(id_)
    return jsonify({"ok": True})


@app.route("/api/credits/spend/add", methods=["POST"])
def api_credits_spend_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    credit_id = (body.get("credit_id") or "").strip()
    if not credit_id:
        return jsonify({"error": "credit_id required"}), 400
    spend_date = (body.get("spend_date") or "").strip()
    if not _parse_iso(spend_date):
        return jsonify({"error": "spend_date required (YYYY-MM-DD)"}), 400
    try:
        ils_amount = float(body.get("ils_amount") or 0)
    except (TypeError, ValueError):
        ils_amount = 0
    if ils_amount <= 0:
        return jsonify({"error": "ils_amount must be > 0"}), 400

    credits_by_id = {c["id"]: c for c in db.all_kupat_credits()}
    credit = credits_by_id.get(credit_id)
    if not credit:
        return jsonify({"error": "credit not found"}), 404
    spent_so_far = sum(
        float(s["ils_amount"] or 0)
        for s in db.all_kupat_spends()
        if s["credit_id"] == credit_id
    )
    if spent_so_far + ils_amount > float(credit["ils_amount"] or 0) + 0.001:
        remaining = float(credit["ils_amount"] or 0) - spent_so_far
        return jsonify({
            "error": f"overspend: only {remaining:.2f} ILS remaining on this credit"
        }), 400

    rate, _, _ = kupat_credits.current_fx_rate()
    note = (body.get("note") or "").strip()
    row = {
        "id": "ks-" + uuid.uuid4().hex[:12],
        "credit_id": credit_id,
        "spend_date": spend_date,
        "ils_amount": ils_amount,
        "fx_rate": rate,
        "note": note,
    }
    db.insert_kupat_spend(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"], "fx_rate": rate})


@app.route("/api/credits/spend/delete", methods=["POST"])
def api_credits_spend_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_kupat_spend(id_)
    return jsonify({"ok": True})


@app.route("/api/maaser")
def api_maaser():
    summary = _maaser_summary()
    return jsonify({
        "payments": db.all_maaser(),
        **summary,
        "last_run": _last_run, "last_backup": _last_backup,
    })


@app.route("/api/maaser/add", methods=["POST"])
def api_maaser_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    date_iso = (body.get("date_iso") or "").strip()
    if not _parse_iso(date_iso):
        return jsonify({"error": "date_iso required (YYYY-MM-DD)"}), 400
    try:
        amount = float(body.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return jsonify({"error": "amount must be > 0"}), 400
    row = {
        "id": "maaser-" + uuid.uuid4().hex[:12],
        "date": date_iso,
        "date_iso": date_iso,
        "recipient": (body.get("recipient") or "").strip(),
        "amount": amount,
        "notes": (body.get("notes") or "").strip(),
        # Stored as 0/1 to match the SQLite NOT NULL DEFAULT 0 column.
        "tax_deductible": 1 if body.get("tax_deductible") else 0,
    }
    db.insert_maaser(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/maaser/edit", methods=["POST"])
def api_maaser_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    fields = {}
    for k in ("date_iso", "recipient", "amount", "notes", "tax_deductible"):
        if k in body:
            v = body[k]
            if k == "amount":
                try:
                    v = float(v) if v not in (None, "") else None
                except (TypeError, ValueError):
                    continue
            elif k == "tax_deductible":
                # Accept bool / 0 / 1 / "0" / "1" / "true" / "false" — coerce to 0/1.
                if isinstance(v, str):
                    v = v.strip().lower()
                    v = 1 if v in ("1", "true", "yes", "on") else 0
                else:
                    v = 1 if v else 0
            elif isinstance(v, str):
                v = v.strip()
            fields[k] = v
    if "date_iso" in fields and fields["date_iso"]:
        fields["date"] = fields["date_iso"]
    db.update_maaser(id_, fields)
    return jsonify({"ok": True})


@app.route("/api/maaser/delete", methods=["POST"])
def api_maaser_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.delete_maaser(id_)
    return jsonify({"ok": True})


# ----- To-Do list ----------------------------------------------------------

@app.route("/todos")
def todos_page():
    return render_template("todos.html")


@app.route("/api/todos")
def api_todos():
    from flask import request
    status = (request.args.get("status") or "all").strip().lower()
    rows, stats = todos_mod.list_todos(status=status)
    return jsonify({
        "todos": rows,
        "stats": stats,
        "last_remind": _last_todo_remind,
        "linkable_sources": list(todos_mod.VALID_SOURCES),
        "urgencies": list(todos_mod.VALID_URGENCIES),
        "recurrences": list(todos_mod.VALID_RECURRENCES),
    })


@app.route("/api/todos/add", methods=["POST"])
def api_todos_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    new_id, err = todos_mod.create(body)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/todos/edit", methods=["POST"])
def api_todos_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    ok, err = todos_mod.edit(id_, body)
    if not ok:
        return jsonify({"error": err}), (404 if err == "not found" else 400)
    return jsonify({"ok": True})


@app.route("/api/todos/delete", methods=["POST"])
def api_todos_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.todo_delete(id_)
    return jsonify({"ok": True})


@app.route("/api/todos/toggle", methods=["POST"])
def api_todos_toggle():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    ok, payload = todos_mod.toggle(id_)
    if not ok:
        return jsonify({"error": payload}), 404
    return jsonify({"ok": True, "result": payload})


@app.route("/api/todos/subtask/add", methods=["POST"])
def api_todos_subtask_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    todo_id = (body.get("todo_id") or "").strip()
    title = (body.get("title") or "").strip()
    if not todo_id or not db.todo_get(todo_id):
        return jsonify({"error": "todo_id required"}), 400
    if not title:
        return jsonify({"error": "title required"}), 400
    existing = db.subtask_list(todo_id)
    row = {
        "id": todos_mod.new_subtask_id(),
        "todo_id": todo_id,
        "title": title[:300],
        "done": 0,
        "order_idx": len(existing),
    }
    db.subtask_insert(row, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "id": row["id"]})


@app.route("/api/todos/subtask/toggle", methods=["POST"])
def api_todos_subtask_toggle():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.subtask_toggle(id_)
    return jsonify({"ok": True})


@app.route("/api/todos/subtask/delete", methods=["POST"])
def api_todos_subtask_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    id_ = (body.get("id") or "").strip()
    if not id_:
        return jsonify({"error": "id required"}), 400
    db.subtask_delete(id_)
    return jsonify({"ok": True})


@app.route("/api/todos/suggestions")
def api_todos_suggestions():
    return jsonify({"suggestions": todos_mod.compute_suggestions()})


@app.route("/api/todos/suggestions/accept", methods=["POST"])
def api_todos_suggestions_accept():
    from flask import request
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    new_id, err = todos_mod.accept_suggestion(key)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/todos/suggestions/dismiss", methods=["POST"])
def api_todos_suggestions_dismiss():
    from flask import request
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400
    until = todos_mod.dismiss_suggestion(key, body.get("days") or 7)
    return jsonify({"ok": True, "until": until})


@app.route("/api/todos/remind-now", methods=["POST"])
def api_todos_remind_now():
    """Force-run the digest. Useful for manual testing without waiting for
    the 08:00 cron."""
    threading.Thread(target=run_todo_remind, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/sales/manual-record", methods=["POST"])
def api_sales_manual_record():
    from flask import request
    import uuid
    body = request.get_json(silent=True) or {}
    inv_source = (body.get("inv_source") or "").strip()
    inv_source_id = (body.get("inv_source_id") or "").strip()
    if not inv_source or not inv_source_id:
        return jsonify({"error": "inv_source and inv_source_id required"}), 400
    rows, _ = _build_unified_inventory()
    inv = next((r for r in rows if r.get("source") == inv_source and str(r.get("source_id")) == inv_source_id), None)
    if not inv:
        return jsonify({"error": "inventory row not found (may already be sold or hidden)"}), 404
    try:
        qty = int(body.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0 or qty > (inv.get("qty_unsold") or 0):
        return jsonify({"error": "qty must be 1..remaining"}), 400
    is_loss = bool(body.get("is_loss"))
    sale_price = 0.0 if is_loss else float(body.get("sale_price") or 0)
    sale_date = (body.get("sale_date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    sale_iso = sale_date[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", sale_date) else sale_date
    platform = (body.get("platform") or ("loss" if is_loss else "manual")).strip()
    note = (body.get("note") or "").strip()
    # Optional override: which seat numbers were sold. Falls back to the
    # inventory row's full seat list if the caller leaves it blank.
    seats_sold = (body.get("seats") or "").strip() if "seats" in body else None
    cost_per = inv.get("cost_per_unit")
    if cost_per is None and (inv.get("qty_unsold") or 0) > 0:
        cost_per = (inv.get("cost") or 0) / (inv.get("qty_unsold") or 1)
    cost = round((cost_per or 0) * qty, 2)
    sale_id = "manual-" + uuid.uuid4().hex[:12]
    now_iso = datetime.now(timezone.utc).isoformat()
    db.insert_manual_sale({
        "id": sale_id,
        "inv_source": inv_source,
        "inv_source_id": inv_source_id,
        "event_name": inv.get("event_name") or "",
        "event_date": inv.get("event_date") or "",
        "event_date_iso": inv.get("event_date_iso") or "",
        "venue": inv.get("venue") or "",
        "section": inv.get("section") or "",
        "row_label": inv.get("row") or "",
        "seats": seats_sold if seats_sold is not None else (inv.get("seats") or ""),
        "qty": qty,
        "sale_price": sale_price,
        "cost": cost,
        "sale_date": sale_date,
        "sale_date_iso": sale_iso,
        "platform": platform,
        "is_loss": 1 if is_loss else 0,
        "note": note,
    }, now_iso)
    db.record_match(
        sale_source="manual", sale_id=sale_id,
        inv_source=inv_source, inv_source_id=inv_source_id,
        qty=qty, reason="manual" + ("/loss" if is_loss else ""), now_iso=now_iso,
    )
    return jsonify({"ok": True, "sale_id": sale_id})


@app.route("/api/sales/manual-delete", methods=["POST"])
def api_sales_manual_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    sale_id = (body.get("sale_id") or "").strip()
    if not sale_id:
        return jsonify({"error": "sale_id required"}), 400
    db.delete_manual_sale(sale_id)
    db.delete_match("manual", sale_id)
    return jsonify({"ok": True})


@app.route("/api/match-now", methods=["POST"])
def api_match_now():
    # Pending-match first so manual rows already linked to a Lysted/Viagogo
    # listing get flagged before the auto-matcher considers them.
    pending_listed = matcher.run_pending_match()
    matched, skipped = matcher.run_match_pass()
    return jsonify({"matched": matched, "unmatched": skipped, "pending_listed": pending_listed})


@app.route("/api/sales/unmatched")
def api_sales_unmatched():
    """External (Lysted/Viagogo/CrowdVolt) sales not yet paired with any
    inventory row. Powers the manual pair-with-sale fallback UI for cases
    where the auto-matcher's section/row scoring misses."""
    from flask import request
    q = (request.args.get("event") or "").strip()
    qn = _norm_event_name(q) if q else ""
    matched_keys = {(m["sale_source"], m["sale_id"]) for m in db.all_matches()}
    blocked = db.all_blocklist_keys()
    rows = []
    for s in matcher._collect_external_sales():
        key = (s["source"], s["id"])
        if key in matched_keys or key in blocked:
            continue
        if qn and qn not in _norm_event_name(s.get("event_name") or ""):
            continue
        rows.append({
            "sale_source": s["source"],
            "sale_id": s["id"],
            "event_name": s.get("event_name") or "",
            "event_date_iso": s.get("event_date_iso") or "",
            "section": s.get("section") or "",
            "row": s.get("row") or "",
            "qty": s.get("qty") or 0,
            "sale_date": s.get("sale_date") or "",
        })
    rows.sort(key=lambda r: r.get("sale_date") or "", reverse=True)
    return jsonify({"rows": rows})


@app.route("/api/sales/pair", methods=["POST"])
def api_sales_pair():
    """Manually pair an external sale with an inventory row when auto-match
    couldn't (e.g. section label mismatch). Writes inventory_matches and
    hides the inventory row when fully consumed."""
    from flask import request
    body = request.get_json(silent=True) or {}
    sale_source = (body.get("sale_source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    inv_source = (body.get("inv_source") or "").strip()
    inv_source_id = (body.get("inv_source_id") or "").strip()
    if not (sale_source and sale_id and inv_source and inv_source_id):
        return jsonify({"error": "sale_source, sale_id, inv_source, inv_source_id required"}), 400
    try:
        qty = int(body.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0
    if qty <= 0:
        return jsonify({"error": "qty must be > 0"}), 400
    # Reject duplicate pair (inventory_matches PRIMARY KEY is sale-side).
    existing = {(m["sale_source"], m["sale_id"]) for m in db.all_matches()}
    if (sale_source, sale_id) in existing:
        return jsonify({"error": "sale already paired"}), 409
    rows, _ = _build_unified_inventory()
    inv = next((r for r in rows if r.get("source") == inv_source and str(r.get("source_id")) == inv_source_id), None)
    if not inv:
        return jsonify({"error": "inventory row not found"}), 404
    if qty > (inv.get("qty_unsold") or 0):
        return jsonify({"error": "qty exceeds remaining"}), 400
    now_iso = datetime.now(timezone.utc).isoformat()
    db.record_match(
        sale_source=sale_source, sale_id=sale_id,
        inv_source=inv_source, inv_source_id=inv_source_id,
        qty=qty, reason="manual-pair", now_iso=now_iso,
    )
    if qty >= (inv.get("qty_unsold") or 0):
        db.hide_inventory(inv_source, inv_source_id, now_iso)
    return jsonify({"ok": True})


@app.route("/api/sales/unpair", methods=["POST"])
def api_sales_unpair():
    """Reverse a manual pair: drop the inventory_matches row and unhide the
    inventory row so it returns to the active list."""
    from flask import request
    body = request.get_json(silent=True) or {}
    sale_source = (body.get("sale_source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    if not (sale_source and sale_id):
        return jsonify({"error": "sale_source and sale_id required"}), 400
    target = next(
        (m for m in db.all_matches()
         if m.get("sale_source") == sale_source and m.get("sale_id") == sale_id),
        None,
    )
    if not target:
        return jsonify({"error": "match not found"}), 404
    db.delete_match(sale_source, sale_id)
    inv_src = target.get("inv_source")
    inv_id = target.get("inv_source_id")
    if inv_src and inv_id is not None:
        db.unhide_inventory(inv_src, str(inv_id))
    return jsonify({"ok": True})


_INV_EDITABLE = {"section", "row", "seats", "qty_unsold", "cost", "cost_per_unit", "list_price", "delivery_type", "status", "event_name", "venue", "event_date", "event_date_iso"}
_SALE_EDITABLE = {"event_name", "venue", "section", "row", "qty", "sale_price", "cost", "platform", "sale_date", "sale_date_iso", "event_date", "event_date_iso"}

# Status values that mean "this batch didn't sell" — triggers archive into
# inventory_unsold instead of a normal status override. Editing the Status
# field on the inventory page to any of these (case- and whitespace-
# insensitive) hides the row and surfaces it on the sales page under the
# "Didn't Sell" section.
_UNSOLD_RE = re.compile(r"^\s*(not[\s_-]?sold|didn'?t\s*sell|did\s*not\s*sell|unsold)\s*$", re.IGNORECASE)


@app.route("/api/inventory/edit", methods=["POST"])
def api_inventory_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    source_id = (body.get("source_id") or "").strip()
    field = (body.get("field") or "").strip()
    value = body.get("value")
    if not source or not source_id or field not in _INV_EDITABLE:
        return jsonify({"error": "invalid source/source_id/field"}), 400
    now_iso = datetime.now(timezone.utc).isoformat()

    # "Didn't sell" trigger: archive into inventory_unsold by content
    # fingerprint so the tombstone survives source-id rotation on resync.
    if field == "status" and value and _UNSOLD_RE.match(str(value)):
        rows, _skipped = _build_unified_inventory()
        inv = next(
            (r for r in rows
             if r.get("source") == source and str(r.get("source_id")) == source_id),
            None,
        )
        if not inv:
            return jsonify({"error": "row not found in current inventory"}), 404
        fp = db.unsold_fingerprint(
            source, inv.get("event_name"), inv.get("event_date_iso"),
            inv.get("section"), inv.get("row"), inv.get("seats"), inv.get("qty_unsold"),
        )
        snap = {
            "fingerprint": fp,
            "source": source,
            "source_id": source_id,
            "event_name": inv.get("event_name"),
            "event_date": inv.get("event_date"),
            "event_date_iso": inv.get("event_date_iso"),
            "venue": inv.get("venue"),
            "section": inv.get("section"),
            "row_label": inv.get("row"),
            "seats": inv.get("seats"),
            "qty": inv.get("qty_unsold"),
            "cost": inv.get("cost"),
            "cost_per_unit": inv.get("cost_per_unit"),
            "list_price": inv.get("list_price"),
            "delivery_type": inv.get("delivery_type"),
            "note": None,
        }
        db.mark_inventory_unsold(snap, now_iso)
        # Drop any active status override on this row — the unsold archive
        # is now the source of truth for "this row is gone".
        db.set_inv_override(source, source_id, "status", None, now_iso)
        return jsonify({"ok": True, "archived": True, "fingerprint": fp})

    db.set_inv_override(source, source_id, field, value, now_iso)
    return jsonify({"ok": True})


@app.route("/api/sales/edit", methods=["POST"])
def api_sales_edit():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    sale_id = (body.get("sale_id") or "").strip()
    field = (body.get("field") or "").strip()
    value = body.get("value")
    if not source or not sale_id or field not in _SALE_EDITABLE:
        return jsonify({"error": "invalid source/sale_id/field"}), 400
    db.set_sale_override(source, sale_id, field, value, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True})


@app.route("/api/inventory/hide", methods=["POST"])
def api_inventory_hide():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    source_id = (body.get("source_id") or "").strip()
    if not source or not source_id:
        return jsonify({"error": "source and source_id required"}), 400
    db.hide_inventory(source, source_id, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "source": source, "source_id": source_id})


@app.route("/api/inventory/unhide", methods=["POST"])
def api_inventory_unhide():
    from flask import request
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "").strip()
    source_id = (body.get("source_id") or "").strip()
    if not source or not source_id:
        return jsonify({"error": "source and source_id required"}), 400
    # If an auto-match caused this hide, also blocklist the sale so the
    # next matcher pass doesn't immediately re-pair them.
    match = db.find_match_for_inv(source, source_id)
    if match:
        db.add_blocklist(match["sale_source"], match["sale_id"], datetime.now(timezone.utc).isoformat())
        db.delete_match(match["sale_source"], match["sale_id"])
    db.unhide_inventory(source, source_id)
    return jsonify({"ok": True, "source": source, "source_id": source_id, "from_match": bool(match)})


@app.route("/api/inventory/unmark-unsold", methods=["POST"])
def api_inventory_unmark_unsold():
    """Restore a row from the Didn't Sell archive back to active inventory.

    Removes the content-fingerprint tombstone from inventory_unsold. The row
    will reappear on the next /api/inventory-all load if its source data is
    still present (Lysted/Viagogo/JeruJam still expose the underlying ticket).
    """
    from flask import request
    body = request.get_json(silent=True) or {}
    fp = (body.get("fingerprint") or "").strip()
    if not fp:
        return jsonify({"error": "fingerprint required"}), 400
    db.unmark_inventory_unsold(fp)
    return jsonify({"ok": True, "fingerprint": fp})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=run_scraper, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/backup", methods=["POST"])
def api_backup():
    threading.Thread(target=run_backup, daemon=True).start()
    return jsonify({"started": True})


@app.route("/jerujam")
def jerujam():
    return render_template("jerujam.html")


@app.route("/api/jerujam")
def api_jerujam():
    tickets = db.all_jerujam_tickets()
    sales = db.all_jerujam_sales()
    expenses = db.all_jerujam_expenses()

    def is_sold(t):
        return (t.get("status") or "").strip().lower() == "sold"

    sold = [t for t in tickets if is_sold(t)]
    held = [t for t in tickets if not is_sold(t)]
    total_purchase_cost = sum((t.get("total_purchase_cost") or 0) for t in tickets)
    total_sales = sum((s.get("sale_price") or 0) for s in sales)
    total_seller_fees = sum((t.get("seller_fees") or 0) for t in tickets)
    sold_purchase_cost = sum((t.get("total_purchase_cost") or 0) for t in sold)
    held_purchase_cost = sum((t.get("total_purchase_cost") or 0) for t in held)

    by_status = {}
    for t in tickets:
        s = (t.get("status") or "(none)").strip() or "(none)"
        by_status[s] = by_status.get(s, 0) + 1

    totals = {
        "tickets": len(tickets),
        "sales": len(sales),
        "expenses": len(expenses),
        "qty_total": sum((t.get("quantity") or 0) for t in tickets),
        "qty_sold": sum((s.get("quantity") or 0) for s in sales),
        "total_purchase_cost": round(total_purchase_cost, 2),
        "sold_purchase_cost": round(sold_purchase_cost, 2),
        "held_purchase_cost": round(held_purchase_cost, 2),
        "total_sales": round(total_sales, 2),
        "total_seller_fees": round(total_seller_fees, 2),
        "net_profit": round(total_sales - sold_purchase_cost - total_seller_fees, 2),
        "by_status": by_status,
    }
    return jsonify({
        "tickets": tickets,
        "sales": sales,
        "expenses": expenses,
        "totals": totals,
        "last_import": _last_jerujam,
    })


@app.route("/api/jerujam/import", methods=["POST"])
def api_jerujam_import():
    threading.Thread(target=run_jerujam_import, daemon=True).start()
    return jsonify({"started": True})


@app.route("/export.xlsx")
def export_xlsx():
    wb = Workbook()
    ws_l = wb.active
    ws_l.title = "Lysted"
    ws_l.append([
        "Event", "Date", "Time", "Venue",
        "Listings", "Tickets", "Total Cost", "Total List", "P/L",
    ])
    for r in _enrich(db.all_inventory()):
        ws_l.append([
            r.get("event_name"), r.get("event_date"), r.get("event_time"),
            r.get("venue"),
            r.get("listings_count"), r.get("tickets_count"),
            r.get("total_cost"), r.get("total_list"), r.get("profit_loss"),
        ])

    ws_p = wb.create_sheet("Lysted Tickets")
    ws_p.append([
        "Order", "Order Date", "Event", "Event Date", "Venue",
        "Section", "Row", "Qty", "Seats",
        "Delivery", "Account", "Transaction ID",
        "Total Cost", "Cost/Unit", "Status",
    ])
    for r in db.all_lysted_purchases():
        ws_p.append([
            r.get("order_id"), r.get("order_date"),
            r.get("event_name"), r.get("event_date"), r.get("venue"),
            r.get("section"), r.get("row_label"), r.get("qty"), r.get("seats"),
            r.get("delivery_type"), r.get("account_email"), r.get("transaction_id"),
            r.get("total_cost"), r.get("cost_per_unit"), r.get("status"),
        ])

    ws_v = wb.create_sheet("Viagogo")
    ws_v.append([
        "Event", "Date", "Venue", "Section", "Ticket Type",
        "Visibility", "Face Value", "Available", "Cost (Avail x Face)",
        "Price", "Proceeds", "Sold",
    ])
    for r in _enrich_viagogo(db.all_viagogo()):
        ws_v.append([
            r.get("event_name"), r.get("event_date"), r.get("venue"),
            r.get("section"), r.get("ticket_type"),
            r.get("visibility"),
            r.get("face_value"), r.get("available"), r.get("cost"),
            r.get("price"), r.get("proceeds"), r.get("sold"),
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"kartis-{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _diff_seats_set(prev_keys, curr_seats, key_fn):
    """Source-agnostic set diff. `key_fn` is the source's seat_key callable
    (or `ticketmaster.event_seat_key` for event-level watchers, where the
    perf code is part of the dedup key)."""
    prev = set(prev_keys)
    by_key = {}
    for s in curr_seats:
        by_key.setdefault(key_fn(s), s)
    curr = set(by_key)
    added = [by_key[k] for k in curr - prev]
    removed = list(prev - curr)
    return added, removed


def _check_one_watcher(w, now_iso):
    """One tick for one watcher. Returns (added_count, error_str_or_None).

    The very first tick on a fresh watcher (no recorded state and no prior
    check timestamp) is treated as a baseline: we capture whatever's
    currently available and DO NOT notify, otherwise the user gets a
    spam ping for seats that were already there at watch-create time.

    Event-level watchers (perf_code='ALL', ticketmaster only) aggregate seats
    across every active performance. The same baseline rule applies, plus
    a "tickets just opened" headline fires when the aggregate flips from
    zero to non-zero seats.

    Notification gating: master_muted (db setting), the watcher's own
    `muted` flag, and `notify_channels` all apply here. When notifications
    are suppressed the drop is still recorded in `tm_drops` so the
    dashboard history shows what was missed.
    """
    import json as _json
    wid = w["id"]
    label = w.get("label") or f"{w['event_code']}/{w['perf_code']}"
    src_name = w.get("source") or "ticketmaster"
    src = WATCHER_SOURCES.get(src_name, ticketmaster)

    event_level = src is ticketmaster and ticketmaster.is_event_level(w)
    if event_level:
        perf_url = ticketmaster.event_url(w["event_code"])
        key_fn = ticketmaster.event_seat_key
    else:
        perf_url = src.perf_url(w["event_code"], w["perf_code"])
        key_fn = src.seat_key

    try:
        if event_level:
            seats, perf_errors = ticketmaster.fetch_event_seats(w["event_code"])
            if perf_errors:
                print(f"[{now_iso}] {label}: per-perf errors {perf_errors}", flush=True)
        else:
            seats = src.fetch_selectable_seats(w["event_code"], w["perf_code"])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        db.tm_update_watcher(wid, {
            "last_check_at": now_iso, "last_check_error": err,
        })
        return 0, err

    prev_keys = db.tm_get_seat_keys(wid)
    is_baseline = not prev_keys and not w.get("last_check_at")
    was_empty = (w.get("last_seat_count") or 0) == 0
    added, removed = _diff_seats_set(prev_keys, seats, key_fn)
    db.tm_replace_seat_state(wid, seats)
    db.tm_update_watcher(wid, {
        "last_check_at": now_iso,
        "last_check_error": None,
        "last_seat_count": len(seats),
    })

    if is_baseline:
        return 0, None

    if added:
        probe_block = next((s.get("block") or s.get("b") for s in added if s.get("block") or s.get("b")), None)
        if event_level:
            probe_perf = next((s.get("_perf") for s in added if s.get("_perf")), None)
            try:
                labels = src.get_labels(w["event_code"], probe_perf, lang="iw", missing_block=probe_block) if probe_perf else None
            except Exception:
                labels = None
        else:
            try:
                labels = src.get_labels(w["event_code"], w["perf_code"], lang="iw", missing_block=probe_block)
            except Exception:
                labels = None

        # Apply the watcher's filters (min consecutive seats, exclude sections,
        # price range). Diff against `seats` (current full availability) so
        # adjacency reflects the live snapshot, not historical state.
        matched = watcher_filters.apply(added, seats, w.get("filters"), labels=labels)

        master_muted = db.setting_get_bool("master_muted", default=False)
        watcher_muted = bool(w.get("muted"))
        channels_csv = (w.get("notify_channels") or "discord,email").strip().lower()
        enabled = {c for c in (s.strip() for s in channels_csv.split(",")) if c}
        if master_muted or watcher_muted:
            enabled = set()

        # Status flip — fires on every sold-out → available transition (since
        # last_seat_count returns to 0 between flips), satisfying the
        # "keep notifying" requirement for event-level watchers.
        status_flipped = event_level and was_empty and len(seats) > 0
        headline = f"🎟️ {w['event_code']} — tickets just opened" if status_flipped and matched else None
        # Festival/hub + kupat-GA watchers carry a status flag per tick (the
        # seat key encodes it), so any transition shows up as an `added`
        # seat — phrase the ping for the new status.
        _fest = next((s for s in added if s.get("festival") or s.get("ga")), None)
        if _fest:
            _nm = label or w["event_code"]
            headline = {
                "soldout": f"❌ Sold out — {_nm}",
                "lasttickets": f"⚠️ Last tickets — {_nm}",
                "available": f"🎟️ Available again — {_nm}",
            }.get(_fest.get("status"), headline)

        if enabled and matched:
            result = notify.notify_drop(
                label=label, perf_url=perf_url,
                added_seats=matched, removed_count=len(removed),
                total_now=len(seats), labels=labels,
                channels=enabled,
                headline=headline,
            )
        elif not matched and added:
            # All new seats filtered out — record the drop so the user sees
            # the filter is working, but skip the notification.
            result = {"discord": "skipped (filtered)", "email": "skipped (filtered)"}
        else:
            reason = "master-muted" if master_muted else ("watcher-muted" if watcher_muted else "channels-empty")
            result = {"discord": f"skipped ({reason})", "email": f"skipped ({reason})"}

        db.tm_record_drop(
            wid, len(added), len(removed),
            _json.dumps(added, ensure_ascii=False)[:8000],
            _json.dumps(result)[:1000],
            now_iso,
            notify_count=len(matched),
        )
    return len(added), None


def _purge_past_watchers():
    """Auto-delete watchers whose event date has already passed.

    For each watcher we read the cached label payload (no network call) and
    check meta.firstPerfMs / firstPerfText. Once today's local date is past
    the event's date, the watcher has nothing left to watch, so we drop it.

    Returns the number of watchers deleted. Soft-fails on any per-watcher
    error so a single broken cache file can't block the cleanup pass.
    """
    today = datetime.now().date()
    deleted = 0
    for w in db.tm_all_watchers():
        try:
            src_name = w.get("source") or "ticketmaster"
            src = WATCHER_SOURCES.get(src_name, ticketmaster)
            # Event-level watchers don't have a single perf — use the last
            # performance under the event as the date probe (so we only
            # purge after the LAST date of a multi-date show has passed).
            if src is ticketmaster and ticketmaster.is_event_level(w):
                try:
                    perfs = ticketmaster.list_performances(w["event_code"])
                    perfs_sorted = sorted(perfs, key=lambda p: p.get("performanceDate") or 0)
                    probe_perf = str(perfs_sorted[-1].get("performanceCode") or "") if perfs_sorted else None
                except Exception:
                    probe_perf = None
                if not probe_perf:
                    continue
                lbls = src.get_labels(w["event_code"], probe_perf, lang="iw")
            else:
                lbls = src.get_labels(w["event_code"], w["perf_code"], lang="iw")
            meta = (lbls or {}).get("meta") or {}
            event_date = None
            ms = meta.get("firstPerfMs")
            if ms:
                try:
                    event_date = datetime.fromtimestamp(int(ms) / 1000).date()
                except (TypeError, ValueError, OSError):
                    event_date = None
            if event_date is None:
                txt = (meta.get("firstPerfText") or "")[:10]
                try:
                    event_date = datetime.strptime(txt, "%Y-%m-%d").date()
                except ValueError:
                    event_date = None
            if event_date and event_date < today:
                db.tm_delete_watcher(w["id"])
                deleted += 1
        except Exception:
            # Don't let one broken watcher block the rest of the purge.
            traceback.print_exc()
    return deleted


def run_tm_check():
    """Iterate active watchers, run one check each. Lock prevents overlap if
    a tick exceeds the interval. Honors the master_paused setting — when
    set, the tick records the timestamp but skips polling entirely."""
    if not _tm_lock.acquire(blocking=False):
        return
    _last_tm["running"] = True
    try:
        if db.setting_get_bool("master_paused", default=False):
            _last_tm.update(
                at=datetime.now(timezone.utc).isoformat(),
                checked=0, drops=0, errors=0, paused=True,
            )
            return
        # Drop any watchers whose event date has passed before we burn
        # network calls on them.
        _purge_past_watchers()
        watchers = db.tm_active_watchers()
        now_iso = datetime.now(timezone.utc).isoformat()
        drops = 0
        errors = 0
        for w in watchers:
            try:
                added, err = _check_one_watcher(w, now_iso)
                if err:
                    errors += 1
                if added:
                    drops += 1
            except Exception:
                errors += 1
                traceback.print_exc()
        _last_tm.update(
            at=now_iso, checked=len(watchers), drops=drops, errors=errors, paused=False,
        )
    finally:
        _last_tm["running"] = False
        _tm_lock.release()


@app.route("/watchers")
def watchers_page():
    return render_template("watchers.html")


# ---------------- Festival + GA Tracker pages -------------------------------
# Both pages share one snapshot store and one sync job. "Festival" = tickchak
# hub events (have capacity → released/sold/left). "GA Tracker" = kupat GA
# events (only tickets-left, no total). Sold-velocity is the drop in
# `available` between snapshots — works for both with or without a total.

# Rolling windows for the "tickets sold in the last …" box. (label, seconds).
FESTIVAL_WINDOWS = [
    ("1h", 3600), ("6h", 6 * 3600), ("24h", 24 * 3600),
    ("3d", 3 * 86400), ("7d", 7 * 86400),
]
FESTIVAL_SYNC_MINUTES = int(os.getenv("KARTIS_FESTIVAL_SYNC_MINUTES") or 10)
_last_sales_sync = {"at": None, "recorded": 0, "error": None, "running": False}
_sales_sync_lock = threading.Lock()


def _counted_fresh_labels(w):
    """Fresh labels for a count-tracked watcher, or None if it isn't one.
    tickchak festival is cheap HTTP; for kupat we only force the (browser)
    fetch once we've confirmed via cache that it's a GA event."""
    src = w.get("source") or ""
    if src == "tickchak":
        lbls = tickchak.get_labels(w["event_code"], w["perf_code"], lang="iw", force=True)
        return lbls if (lbls.get("meta") or {}).get("festival") else None
    if src == "kupat":
        cached = kupat.get_labels(w["event_code"], w["perf_code"], lang="iw")
        if not (cached.get("meta") or {}).get("ga"):
            return None
        return kupat.get_labels(w["event_code"], w["perf_code"], lang="iw", force=True)
    return None


def run_sales_sync():
    """Snapshot each festival/GA event's availability so the Festival / GA
    Tracker pages can show how many sold over rolling windows."""
    if not _sales_sync_lock.acquire(blocking=False):
        return
    _last_sales_sync["running"] = True
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        recorded = 0
        for w in db.tm_all_watchers():
            try:
                lbls = _counted_fresh_labels(w)
            except Exception:
                continue
            if lbls is None:
                continue
            meta = lbls.get("meta") or {}
            avail = meta.get("availSeats")
            if avail is None:
                continue
            cap = meta.get("totalSeats")
            sold = max(0, cap - avail) if cap is not None else None
            db.sales_snapshot_insert(w["source"], w["event_code"], w["perf_code"],
                                     cap, avail, sold, now_iso)
            recorded += 1
        db.sales_snapshot_prune((now - timedelta(days=8)).isoformat())
        _last_sales_sync.update(at=now_iso, recorded=recorded, error=None)
    except Exception as e:
        _last_sales_sync.update(error=str(e))
        traceback.print_exc()
    finally:
        _last_sales_sync["running"] = False
        _sales_sync_lock.release()


def _sales_windows(source, ec, pc, now, cur_available):
    """Per-window {sold, partial} from `available` deltas. Falls back to the
    earliest snapshot (flagged partial) until history covers the window."""
    earliest = db.sales_snapshot_earliest(source, ec, pc)
    windows = {}
    for key, secs in FESTIVAL_WINDOWS:
        base = db.sales_snapshot_asof(source, ec, pc, (now - timedelta(seconds=secs)).isoformat()) or earliest
        if base is not None and cur_available is not None and base.get("available") is not None:
            base_age = (now - datetime.fromisoformat(base["captured_at"])).total_seconds()
            windows[key] = {"sold": max(0, base["available"] - cur_available),
                            "partial": base_age < secs * 0.9}
        else:
            windows[key] = {"sold": None, "partial": True}
    return windows, earliest


def _sales_totals(shows):
    totals = {k: {"sold": 0, "partial": False} for k, _ in FESTIVAL_WINDOWS}
    for s in shows:
        for key, _ in FESTIVAL_WINDOWS:
            wv = s["windows"][key]
            if wv["sold"] is None:
                totals[key]["partial"] = True
            else:
                totals[key]["sold"] += wv["sold"]
                if wv["partial"]:
                    totals[key]["partial"] = True
    return totals


def _counted_shows(source_name, flag_key, status_key):
    """Build the per-show payload list for one page (festival or GA)."""
    now = datetime.now(timezone.utc)
    src = WATCHER_SOURCES.get(source_name)
    shows = []
    for w in db.tm_all_watchers():
        if (w.get("source") or "") != source_name:
            continue
        try:
            lbls = src.get_labels(w["event_code"], w["perf_code"], lang="iw")
        except Exception:
            continue
        meta = (lbls or {}).get("meta") or {}
        if not meta.get(flag_key):
            continue
        ec, pc = w["event_code"], w["perf_code"]
        total = meta.get("totalSeats")
        avail = meta.get("availSeats")
        latest = db.sales_snapshot_latest(source_name, ec, pc)
        cur_avail = avail if avail is not None else (latest or {}).get("available")
        windows, earliest = _sales_windows(source_name, ec, pc, now, cur_avail)
        sold = max(0, total - cur_avail) if (total is not None and cur_avail is not None) else None
        shows.append({
            "event_code": ec, "perf_code": pc, "label": w.get("label"),
            "event_name": meta.get("eventName"), "when": meta.get("firstPerfText"),
            "status": meta.get(status_key),
            "total": total, "available": cur_avail, "sold": sold,
            "festival_types": lbls.get("blocks") or {},
            "windows": windows, "tracking_since": (earliest or {}).get("captured_at"),
            # Watcher controls (manage from the page itself).
            "id": w["id"],
            "notify_channels": w.get("notify_channels") or "",
            "muted": bool(w.get("muted")),
            "paused": bool(w.get("paused")),
            "last_check_error": w.get("last_check_error"),
        })
    shows.sort(key=lambda s: (s.get("when") or ""))
    return shows, now


@app.route("/festival")
def festival_page():
    return render_template("festival.html")


@app.route("/api/festival")
def api_festival():
    shows, now = _counted_shows("tickchak", "festival", "festivalStatus")
    return jsonify({
        "shows": shows, "totals": _sales_totals(shows),
        "windows": [k for k, _ in FESTIVAL_WINDOWS],
        "last_sync": _last_sales_sync, "now": now.isoformat(),
    })


@app.route("/ga")
def ga_page():
    return render_template("ga.html")


@app.route("/api/ga")
def api_ga():
    shows, now = _counted_shows("kupat", "ga", "gaStatus")
    return jsonify({
        "shows": shows, "totals": _sales_totals(shows),
        "windows": [k for k, _ in FESTIVAL_WINDOWS],
        "last_sync": _last_sales_sync, "now": now.isoformat(),
    })


@app.route("/tools")
def tools_page():
    return render_template("tools.html")


@app.route("/api/kupat-pdf", methods=["POST"])
def api_kupat_pdf():
    """Drive kupat_pdf.render_pdfs from the dashboard form. Synchronous —
    the request body is the URL, the response is the manifest (or error).
    A future iteration could background this via a job queue if 30-second
    requests start being a problem, but for one-off ticket prints the
    synchronous flow keeps the UI dead simple."""
    from flask import request
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        manifest = kupat_pdf.render_pdfs(url)
    except kupat_pdf.KupatPdfError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Unexpected: {e}"}), 500
    return jsonify(manifest)


@app.route("/api/tickchak-pdf", methods=["POST"])
def api_tickchak_pdf():
    """Redact + split an uploaded Tickchak ticket PDF. Multipart upload
    (field name "file") rather than JSON because the PDF can be a few
    hundred KB. Returns the same manifest shape as /api/kupat-pdf."""
    from flask import request
    f = request.files.get("file")
    if not f or not (f.filename or "").lower().endswith(".pdf"):
        return jsonify({"error": "upload a .pdf file"}), 400
    # fitz reads from disk reliably; spool the upload to a tempfile so
    # we don't have to worry about Flask's stream pointer state.
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        f.save(tmp.name)
        tmp.close()
        manifest = tickchak_pdf.redact_and_split(tmp.name)
    except tickchak_pdf.TickchakPdfError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Unexpected: {e}"}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return jsonify(manifest)


_PACHA_TMP = Path(tempfile.gettempdir()) / "kartis_pacha"


@app.route("/api/pacha-fetch", methods=["POST"])
def api_pacha_fetch():
    """Fetch Pacha tickets from Gmail and bundle the first-page PDFs into a zip
    for browser download. The dashboard runs on the VPS, which can't write to
    the user's PC — so we hand the tickets back through the browser instead of
    saving to disk. Synchronous, lock-guarded. Body: {days: int}. Returns the
    summary plus a download_url, or {error}."""
    from flask import request
    import time
    body = request.get_json(silent=True) or {}
    try:
        days = int(body.get("days") or pacha_tickets.DEFAULT_LOOKBACK_DAYS)
    except (TypeError, ValueError):
        return jsonify({"error": "days must be a number"}), 400
    days = max(1, min(days, 365))
    if not _pacha_lock.acquire(blocking=False):
        return jsonify({"error": "a Pacha fetch is already running"}), 429
    try:
        zip_bytes, summary = pacha_tickets.fetch_zip(days=days)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    finally:
        _pacha_lock.release()

    if zip_bytes:
        _PACHA_TMP.mkdir(parents=True, exist_ok=True)
        # Sweep zips older than an hour so the temp dir doesn't grow.
        cutoff = time.time() - 3600
        for old in _PACHA_TMP.glob("*.zip"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
        token = uuid.uuid4().hex
        (_PACHA_TMP / f"{token}.zip").write_bytes(zip_bytes)
        groups = summary.get("groups") or []
        if len(groups) == 1:
            g = groups[0]
            summary["download_name"] = f"{g['event']} {g['date'] or ''}".strip() + " tickets.zip"
        else:
            summary["download_name"] = "Pacha tickets.zip"
        summary["download_url"] = f"/api/pacha-download/{token}"
    return jsonify(summary)


@app.route("/api/pacha-download/<token>")
def api_pacha_download(token):
    """Serve a previously-built Pacha ticket zip (see api_pacha_fetch)."""
    from flask import request
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return jsonify({"error": "bad token"}), 400
    path = _PACHA_TMP / f"{token}.zip"
    if not path.exists():
        return jsonify({"error": "download expired — fetch again"}), 404
    name = request.args.get("name") or "Pacha tickets.zip"
    return send_file(str(path), as_attachment=True, download_name=name,
                     mimetype="application/zip")


# systemd unit for the scraper's Chrome on the VPS. Override via env if the
# unit is named differently. Exposed on the Tools page so finalizing a
# Lysted/Viagogo/CrowdVolt re-login (done in the noVNC window) needs no SSH.
CHROME_SERVICE = os.getenv("KARTIS_CHROME_SERVICE", "kartis-chrome")
_chrome_restart_lock = threading.Lock()


@app.route("/api/chrome/restart", methods=["POST"])
def api_chrome_restart():
    """Restart the scraper's Chrome service (`sudo systemctl restart
    kartis-chrome`). After re-logging into a source in the noVNC window, Chrome
    must be bounced so the scraper re-attaches with the fresh session — this
    lets the user do that from the dashboard instead of SSHing in. Only
    meaningful on the Linux/systemd VPS; returns a friendly error elsewhere.
    Uses `sudo -n` so a missing NOPASSWD rule fails fast instead of hanging."""
    import shutil
    if not _chrome_restart_lock.acquire(blocking=False):
        return jsonify({"error": "a restart is already running"}), 429
    try:
        if not shutil.which("systemctl"):
            return jsonify({"error": "systemctl not found — this control only works on the VPS"}), 400
        try:
            proc = subprocess.run(
                ["sudo", "-n", "systemctl", "restart", CHROME_SERVICE],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return jsonify({"error": "restart timed out after 60s"}), 504
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
            return jsonify({"error": f"systemctl failed: {msg}"}), 500
        return jsonify({"ok": True, "service": CHROME_SERVICE})
    finally:
        _chrome_restart_lock.release()


@app.route("/api/chrome/open-logins", methods=["POST"])
def api_chrome_open_logins():
    """Open the Lysted/Viagogo/CrowdVolt login tabs in the box's Chrome (via the
    CDP /json/new endpoint on :9222) so they're already waiting when the user
    opens the noVNC window to re-sign-in. Dedupes against tabs already open —
    repeat clicks (or a source you're still logged into) won't spam duplicates.
    Reuses login.DEFAULT_URLS so the tab list stays in one place."""
    import urllib.request
    from urllib.parse import urlparse
    import login as login_mod

    cdp = os.getenv("KARTIS_CDP_URL", "http://localhost:9222")
    if not login_mod.is_chrome_running():
        return jsonify({"error": "Chrome isn't running — click Restart Chrome first."}), 503

    def base_domain(url_or_host):
        host = urlparse(url_or_host).hostname if "//" in url_or_host else url_or_host
        parts = (host or "").split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")

    # Domains already open, so we don't reopen a source the user is on/into.
    open_domains = set()
    try:
        with urllib.request.urlopen(f"{cdp}/json/list", timeout=8) as r:
            for t in json.load(r):
                if t.get("type") == "page":
                    open_domains.add(base_domain(t.get("url", "")))
    except Exception:
        pass

    opened, skipped, errors = [], [], []
    for url in login_mod.DEFAULT_URLS:
        if base_domain(url) in open_domains:
            skipped.append(url)
            continue
        try:
            req = urllib.request.Request(f"{cdp}/json/new?{url}", method="PUT")
            with urllib.request.urlopen(req, timeout=8) as r:
                (opened if r.status == 200 else errors).append(url)
        except Exception as e:
            errors.append(f"{url} ({type(e).__name__})")

    if errors and not opened and not skipped:
        return jsonify({"error": "; ".join(errors)}), 502
    return jsonify({"ok": True, "opened": opened, "skipped": skipped, "errors": errors})


@app.route("/api/watchers")
def api_watchers():
    # Sweep past-event watchers on every page load so the UI doesn't have to
    # wait for the next periodic tick to clean them out.
    _purge_past_watchers()
    watchers = db.tm_all_watchers()
    drops = db.tm_recent_drops(limit=200)
    settings = db.all_settings()
    # Enrich each watcher with venue capacity from its cached labels — pure
    # disk read, no network. Lets the UI show "53 / 4125 (98.7% sold)"
    # without holding a venue total in tm_watchers.
    for w in watchers:
        src_name = w.get("source") or "ticketmaster"
        src = WATCHER_SOURCES.get(src_name, ticketmaster)
        try:
            # Event-level watchers span multiple perfs and don't have a single
            # venue-capacity number — skip the capacity enrichment for them.
            if src is ticketmaster and ticketmaster.is_event_level(w):
                w["total_seats"] = None
                w["available_seats"] = None
                continue
            lbls = src.get_labels(w["event_code"], w["perf_code"], lang="iw")
            meta = (lbls or {}).get("meta") or {}
            w["total_seats"] = meta.get("totalSeats")
            # Sources that expose a per-tick available QUANTITY (rather
            # than just a per-type list count) — currently tickchak —
            # surface it here so the dashboard can show real "X / Y"
            # ratios. None for sources where last_seat_count is already
            # the right number.
            w["available_seats"] = meta.get("availSeats")
            # Festival/hub tickchak events: surface the status flag and the
            # per-type released/sold/left breakdown so the watchers table
            # can show a badge + an expandable type list.
            if meta.get("festival"):
                w["festival_status"] = meta.get("festivalStatus")
                w["festival_types"] = lbls.get("blocks") or {}
        except Exception:
            w["total_seats"] = None
            w["available_seats"] = None
    return jsonify({
        "watchers": watchers,
        "drops": drops,
        "last_tm": _last_tm,
        "interval_seconds": TM_CHECK_INTERVAL_SECONDS,
        "check_enabled": TM_CHECK_ENABLED,
        "settings": {
            "master_paused": settings.get("master_paused", "false") in ("1", "true", "True"),
            "master_muted": settings.get("master_muted", "false") in ("1", "true", "True"),
        },
        "sources": list(WATCHER_SOURCES.keys()),
    })


def _add_one_watcher(url, label=None):
    """Shared logic between single-add and bulk-add. Returns (watcher_dict, warning) on success
    or raises ValueError with a user-readable message on failure."""
    url = (url or "").strip()
    src_name, src = _detect_source(url)
    try:
        event_code, perf_code = src.parse_url(url)
    except Exception as e:
        raise ValueError(f"{src_name}: {e}")
    for existing in db.tm_all_watchers():
        if (
            (existing.get("source") or "ticketmaster") == src_name
            and existing["event_code"] == event_code
            and existing["perf_code"] == perf_code
        ):
            raise ValueError(f"already watching {src_name} {event_code}/{perf_code}")
    wid = "tmw-" + uuid.uuid4().hex[:12]
    final_label = (label or "").strip()
    if not final_label:
        try:
            # Event-level watchers don't have a single perf for labels —
            # probe the first real perf so we still get the event name +
            # venue, then tag it as covering all dates.
            if src is ticketmaster and ticketmaster.is_event_level({"perf_code": perf_code}):
                perfs = ticketmaster.list_performances(event_code)
                probe_perf = str((perfs[0] or {}).get("performanceCode") or "")
                lbls = src.get_labels(event_code, probe_perf, lang="iw") if probe_perf else None
                base = src.event_summary(lbls) if lbls else event_code
                final_label = f"{base} — all dates" if base else f"{event_code} — all dates"
            else:
                lbls = src.get_labels(event_code, perf_code, lang="iw")
                final_label = src.event_summary(lbls) or f"{event_code}/{perf_code}"
        except Exception:
            final_label = f"{event_code}/{perf_code}"
    db.tm_insert_watcher({
        "id": wid, "label": final_label, "source": src_name,
        "event_code": event_code, "perf_code": perf_code,
        "paused": 0, "muted": 0, "notify_channels": "discord,email",
        # Each source picks its own default filter. Seated sources
        # (ticketmaster, kupat) default to {min_group_size: 2} (exclude
        # singles); GA sources (tickchak) override to {min_group_size: 1}
        # since adjacency doesn't apply to ticket-type buckets.
        "filters": json.dumps(getattr(src, "DEFAULT_FILTERS", {"min_group_size": 2})),
    }, datetime.now(timezone.utc).isoformat())
    w = db.tm_get_watcher(wid)
    warning = None
    try:
        _check_one_watcher(w, datetime.now(timezone.utc).isoformat())
    except Exception as e:
        traceback.print_exc()
        warning = str(e)
    return db.tm_get_watcher(wid), warning


@app.route("/api/watchers/add", methods=["POST"])
def api_watchers_add():
    from flask import request
    body = request.get_json(silent=True) or {}
    try:
        w, warning = _add_one_watcher(body.get("url"), body.get("label"))
    except ValueError as e:
        # Use 409 for dedupe, 400 for parse errors — caller distinguishes.
        msg = str(e)
        return jsonify({"error": msg}), (409 if "already watching" in msg else 400)
    return jsonify({"ok": True, "id": w["id"], "warning": warning})


@app.route("/api/watchers/bulk-add", methods=["POST"])
def api_watchers_bulk_add():
    """Accept a textarea of URLs (one per line) and add each. Returns a
    per-line outcome so the UI can show what worked + what didn't."""
    from flask import request
    body = request.get_json(silent=True) or {}
    text = body.get("urls") or ""
    results = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            w, warning = _add_one_watcher(line)
            results.append({"url": line, "ok": True, "id": w["id"], "label": w.get("label"), "warning": warning})
        except ValueError as e:
            results.append({"url": line, "ok": False, "error": str(e)})
        except Exception as e:
            traceback.print_exc()
            results.append({"url": line, "ok": False, "error": f"{type(e).__name__}: {e}"})
    added = sum(1 for r in results if r["ok"])
    return jsonify({"added": added, "results": results})


@app.route("/api/watchers/update", methods=["POST"])
def api_watchers_update():
    """Change per-watcher settings: muted, notify_channels, label, filters.
    The UI uses this for the inline mute toggle, channel dropdown, and
    the filters modal save button."""
    from flask import request
    body = request.get_json(silent=True) or {}
    wid = (body.get("id") or "").strip()
    if not wid or not db.tm_get_watcher(wid):
        return jsonify({"error": "watcher not found"}), 404
    fields = {}
    if "muted" in body:
        fields["muted"] = 1 if body["muted"] else 0
    if "notify_channels" in body:
        chans = body["notify_channels"]
        if isinstance(chans, list):
            chans = ",".join(c.strip().lower() for c in chans if c)
        chans = (chans or "").strip().lower()
        valid = {c for c in chans.split(",") if c in {"discord", "email"}}
        fields["notify_channels"] = ",".join(sorted(valid))
    if "label" in body:
        new_label = (body["label"] or "").strip()
        if new_label:
            fields["label"] = new_label
    if "filters" in body:
        # Validate + normalize the filter object before storing.
        f = body["filters"] or {}
        if not isinstance(f, dict):
            return jsonify({"error": "filters must be an object"}), 400
        out_f = {}
        try:
            mgs = int(f.get("min_group_size") or 1)
        except (TypeError, ValueError):
            mgs = 1
        if mgs > 1:
            out_f["min_group_size"] = mgs
        excl = f.get("exclude_sections") or []
        if isinstance(excl, list) and excl:
            out_f["exclude_sections"] = [str(x) for x in excl if x]
        for k in ("min_price", "max_price"):
            v = f.get(k)
            if v in (None, ""):
                continue
            try:
                out_f[k] = float(v)
            except (TypeError, ValueError):
                pass
        fields["filters"] = json.dumps(out_f) if out_f else None
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    db.tm_update_watcher(wid, fields)
    return jsonify({"ok": True, "fields": fields})


@app.route("/api/watchers/sections")
def api_watchers_sections():
    """Returns the list of {code, name, price} for a watcher's blocks/sections,
    so the filter modal can populate its multi-select. Pulls from the cached
    labels — fast, no extra network calls in the common case."""
    from flask import request
    wid = (request.args.get("id") or "").strip()
    w = db.tm_get_watcher(wid) if wid else None
    if not w:
        return jsonify({"error": "watcher not found"}), 404
    src_name = w.get("source") or "ticketmaster"
    src = WATCHER_SOURCES.get(src_name, ticketmaster)
    try:
        # Event-level watchers don't have a single perf — probe the first
        # performance under the event so the filter modal still gets a
        # representative section/price list. Most multi-date shows share
        # the same venue + block layout across perfs.
        if src is ticketmaster and ticketmaster.is_event_level(w):
            perfs = ticketmaster.list_performances(w["event_code"])
            probe_perf = str((perfs[0] or {}).get("performanceCode") or "")
            labels = src.get_labels(w["event_code"], probe_perf, lang="iw") if probe_perf else {}
        else:
            labels = src.get_labels(w["event_code"], w["perf_code"], lang="iw")
    except Exception as e:
        return jsonify({"error": f"label fetch failed: {e}"}), 500
    blocks = (labels or {}).get("blocks") or {}
    rows = [
        {"code": code, "name": info.get("name") or code, "price": info.get("price")}
        for code, info in blocks.items()
    ]
    # Priced sections first (sorted by price ascending), then unpriced ones
    # alphabetically — keeps the relevant inventory on top for the user.
    rows.sort(key=lambda r: (r["price"] is None, r["price"] or 0, r["code"]))
    return jsonify({"sections": rows, "filters": json.loads(w.get("filters")) if w.get("filters") else None})


@app.route("/api/watchers/settings", methods=["POST"])
def api_watchers_settings():
    """Master pause / master mute. Body: {key: 'master_paused'|'master_muted', value: bool}."""
    from flask import request
    body = request.get_json(silent=True) or {}
    key = (body.get("key") or "").strip()
    if key not in {"master_paused", "master_muted"}:
        return jsonify({"error": "key must be master_paused or master_muted"}), 400
    value = "true" if body.get("value") else "false"
    db.setting_set(key, value, datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, "key": key, "value": value})


@app.route("/api/watchers/stop-everything", methods=["POST"])
def api_watchers_stop_everything():
    """One-click flip both master_paused and master_muted to true. Used by
    the big STOP button on the dashboard."""
    now_iso = datetime.now(timezone.utc).isoformat()
    db.setting_set("master_paused", "true", now_iso)
    db.setting_set("master_muted", "true", now_iso)
    return jsonify({"ok": True})


@app.route("/api/watchers/resume-everything", methods=["POST"])
def api_watchers_resume_everything():
    now_iso = datetime.now(timezone.utc).isoformat()
    db.setting_set("master_paused", "false", now_iso)
    db.setting_set("master_muted", "false", now_iso)
    return jsonify({"ok": True})


@app.route("/api/watchers/toggle", methods=["POST"])
def api_watchers_toggle():
    from flask import request
    body = request.get_json(silent=True) or {}
    wid = (body.get("id") or "").strip()
    paused = 1 if body.get("paused") else 0
    if not wid:
        return jsonify({"error": "id required"}), 400
    db.tm_update_watcher(wid, {"paused": paused})
    return jsonify({"ok": True})


@app.route("/api/watchers/delete", methods=["POST"])
def api_watchers_delete():
    from flask import request
    body = request.get_json(silent=True) or {}
    wid = (body.get("id") or "").strip()
    if not wid:
        return jsonify({"error": "id required"}), 400
    db.tm_delete_watcher(wid)
    return jsonify({"ok": True})


@app.route("/api/watchers/test-notify", methods=["POST"])
def api_watchers_test_notify():
    from flask import request
    body = request.get_json(silent=True) or {}
    label = (body.get("label") or "Kartis test").strip()
    result = notify.send_test(label=label)
    return jsonify({"ok": True, "result": result})


@app.route("/api/watchers/check-now", methods=["POST"])
def api_watchers_check_now():
    """Force a single tick for one watcher (or all if no id given). Useful
    when the user wants to verify a watcher right after adding it without
    waiting for the next scheduled tick."""
    from flask import request
    body = request.get_json(silent=True) or {}
    wid = (body.get("id") or "").strip()
    now_iso = datetime.now(timezone.utc).isoformat()
    if wid:
        w = db.tm_get_watcher(wid)
        if not w:
            return jsonify({"error": "watcher not found"}), 404
        added, err = _check_one_watcher(w, now_iso)
        return jsonify({"ok": True, "added": added, "error": err})
    threading.Thread(target=run_tm_check, daemon=True).start()
    return jsonify({"started": True})


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(run_scraper, "interval", hours=1, id="scrape")
scheduler.add_job(run_backup, "cron", hour=3, minute=0, id="backup")
if TM_CHECK_ENABLED:
    scheduler.add_job(run_tm_check, "interval", seconds=TM_CHECK_INTERVAL_SECONDS, id="tm_check")
else:
    print("[tm_check] disabled via TM_CHECK_ENABLED=0 — drop-checking runs elsewhere (e.g. the VPS watcher)")
scheduler.add_job(run_mail_intake, "interval", minutes=INTAKE_INTERVAL_MINUTES, id="mail_intake")
scheduler.add_job(run_todo_remind, "cron", hour=8, minute=0, id="todo_remind")
# Festival/GA sales snapshots — fire one immediately (next_run_time) so the
# Festival / GA Tracker pages have a baseline right after a restart, then
# every N minutes.
scheduler.add_job(run_sales_sync, "interval", minutes=FESTIVAL_SYNC_MINUTES,
                  id="sales_sync", next_run_time=datetime.now())

# One-shot: archive any pre-existing inventory_overrides rows whose status
# value was already typed as "not sold" (or a variant). Idempotent so
# subsequent restarts find no candidates and do nothing.
try:
    _migrated = _migrate_legacy_unsold_overrides()
    if _migrated:
        print(f"[unsold-migration] archived {_migrated} legacy 'not sold' status override(s)")
except Exception:
    traceback.print_exc()

scheduler.start()

threading.Thread(target=run_backup, daemon=True).start()


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
