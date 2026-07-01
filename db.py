import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "kartis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    event_time TEXT,
    event_date_iso TEXT,
    venue TEXT,
    listings_count INTEGER,
    tickets_count INTEGER,
    total_cost REAL,
    total_list REAL,
    stubhub_url TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lysted_purchases (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    order_date TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    qty INTEGER,
    seats TEXT,
    delivery_type TEXT,
    account_email TEXT,
    transaction_id TEXT,
    total_cost REAL,
    cost_per_unit REAL,
    status TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viagogo_listings (
    id TEXT PRIMARY KEY,
    event_id TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    ticket_type TEXT,
    visibility TEXT,
    face_value REAL,
    price REAL,
    proceeds REAL,
    available INTEGER,
    sold INTEGER,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_hidden (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS inventory_unsold (
    fingerprint TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost REAL,
    cost_per_unit REAL,
    list_price REAL,
    delivery_type TEXT,
    marked_at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS sales_hidden (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (source, sale_id)
);
CREATE TABLE IF NOT EXISTS sales_canceled (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    canceled_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (source, sale_id)
);
CREATE TABLE IF NOT EXISTS event_group_merges (
    -- raw_group_key is the auto-clustered group key Kartis would assign on
    -- its own (norm_name|iso|norm_venue). When the user merges two or more
    -- groups, every raw key in that merge gets a row pointing to the
    -- canonical_group_key — a single shared key across all merged groups.
    raw_group_key TEXT PRIMARY KEY,
    canonical_group_key TEXT NOT NULL,
    canonical_event_name TEXT NOT NULL,
    canonical_event_date TEXT,
    canonical_event_date_iso TEXT,
    canonical_venue TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_group_merges_canonical
    ON event_group_merges(canonical_group_key);
CREATE TABLE IF NOT EXISTS inventory_overrides (
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id, field)
);
CREATE TABLE IF NOT EXISTS sales_overrides (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, sale_id, field)
);
CREATE TABLE IF NOT EXISTS manual_inventory (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost_per_unit REAL,
    note TEXT,
    matched_source TEXT,
    matched_source_id TEXT,
    matched_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_sales (
    id TEXT PRIMARY KEY,
    inv_source TEXT,
    inv_source_id TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    sale_price REAL,
    cost REAL,
    sale_date TEXT,
    sale_date_iso TEXT,
    platform TEXT,
    is_loss INTEGER DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_matches (
    sale_source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    inv_source TEXT NOT NULL,
    inv_source_id TEXT NOT NULL,
    qty_matched INTEGER,
    matched_at TEXT NOT NULL,
    match_reason TEXT,
    PRIMARY KEY (sale_source, sale_id)
);
CREATE TABLE IF NOT EXISTS match_blocklist (
    sale_source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    blocked_at TEXT NOT NULL,
    PRIMARY KEY (sale_source, sale_id)
);
CREATE TABLE IF NOT EXISTS lysted_sales (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    sale_date_iso TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    qty INTEGER,
    seats TEXT,
    delivery_type TEXT,
    sale_price REAL,
    payout REAL,
    fees REAL,
    cost REAL,
    status TEXT,
    raw_cells TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS viagogo_sales (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    sale_date_iso TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    ticket_type TEXT,
    sale_price REAL,
    upload_deadline TEXT,
    tab TEXT,
    status TEXT,
    raw_cells TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crowdvolt_sales (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    sale_date TEXT,
    sale_date_iso TEXT,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    qty INTEGER,
    ticket_type TEXT,
    price_per_ticket REAL,
    sale_price REAL,
    status TEXT,
    raw_cells TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jerujam_tickets (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    event_date_iso TEXT,
    venue TEXT,
    city TEXT,
    section TEXT,
    row_label TEXT,
    seat_numbers TEXT,
    quantity INTEGER,
    cost_per_ticket REAL,
    total_purchase_cost REAL,
    purchase_platform TEXT,
    purchase_account TEXT,
    purchase_date TEXT,
    listing_price REAL,
    listing_platform TEXT,
    sold_price REAL,
    sold_count INTEGER,
    sale_date TEXT,
    seller_fees REAL,
    status TEXT,
    notes TEXT,
    credit_card TEXT,
    created_at TEXT,
    updated_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jerujam_sales (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL,
    quantity INTEGER,
    sale_price REAL,
    sale_date TEXT,
    platform TEXT,
    created_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expense_subscriptions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    amount REAL NOT NULL,
    started_at_iso TEXT NOT NULL,
    ended_at_iso TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expenses (
    id TEXT PRIMARY KEY,
    date TEXT,
    date_iso TEXT NOT NULL,
    vendor TEXT,
    category TEXT,
    amount REAL NOT NULL,
    subscription_id TEXT,
    month_key TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(subscription_id, month_key)
);
CREATE TABLE IF NOT EXISTS owed_items (
    id TEXT PRIMARY KEY,
    date_iso TEXT NOT NULL,
    amount REAL NOT NULL,
    description TEXT,
    card_account TEXT,
    direction TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cashback_entries (
    id TEXT PRIMARY KEY,
    date_iso TEXT NOT NULL,
    amount REAL NOT NULL,
    card_name TEXT NOT NULL,
    source_ref TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cashback_date ON cashback_entries(date_iso);
CREATE TABLE IF NOT EXISTS kupat_credits (
    id TEXT PRIMARY KEY,
    issued_date TEXT NOT NULL,
    ils_amount REAL NOT NULL,
    original_usd_cost REAL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kupat_credits_date ON kupat_credits(issued_date);
CREATE TABLE IF NOT EXISTS kupat_credit_spends (
    id TEXT PRIMARY KEY,
    credit_id TEXT NOT NULL,
    spend_date TEXT NOT NULL,
    ils_amount REAL NOT NULL,
    fx_rate REAL,
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kupat_spends_credit ON kupat_credit_spends(credit_id);
CREATE TABLE IF NOT EXISTS maaser_payments (
    id TEXT PRIMARY KEY,
    date TEXT,
    date_iso TEXT NOT NULL,
    recipient TEXT,
    amount REAL NOT NULL,
    notes TEXT,
    tax_deductible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jerujam_expenses (
    id TEXT PRIMARY KEY,
    date TEXT,
    vendor TEXT,
    category TEXT,
    description TEXT,
    amount REAL,
    notes TEXT,
    created_at TEXT,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_intake (
    id TEXT PRIMARY KEY,
    message_id TEXT UNIQUE,
    provider TEXT,
    email_from TEXT,
    email_subject TEXT,
    email_received_at TEXT,
    event_name TEXT,
    event_date_iso TEXT,
    venue TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost REAL,
    cost_per_unit REAL,
    raw_text TEXT,
    parse_warnings TEXT,
    ticket_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_intake_status ON pending_intake(status, created_at);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER,
    content_type TEXT,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_owner ON attachments(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS tm_watchers (
    id TEXT PRIMARY KEY,
    label TEXT,
    event_code TEXT NOT NULL,
    perf_code TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_check_at TEXT,
    last_check_error TEXT,
    last_seat_count INTEGER,
    UNIQUE(event_code, perf_code)
);
CREATE TABLE IF NOT EXISTS tm_seat_state (
    watcher_id TEXT NOT NULL,
    seat_key TEXT NOT NULL,
    block TEXT,
    row_label TEXT,
    seat_num TEXT,
    PRIMARY KEY (watcher_id, seat_key)
);
CREATE TABLE IF NOT EXISTS tm_drops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watcher_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    added_count INTEGER NOT NULL,
    removed_count INTEGER NOT NULL,
    seats_json TEXT,
    notify_result TEXT
);
CREATE INDEX IF NOT EXISTS idx_tm_drops_watcher ON tm_drops(watcher_id, detected_at DESC);
-- Periodic snapshots of GA / count-tracked events' availability (tickchak
-- festival hub events AND kupat GA events). Lets the Festival / GA Tracker
-- pages compute "sold in the last hour / 6h / 24h / 3d / 7d" as deltas in
-- `available` between snapshots (the DB otherwise only holds current state).
-- capacity/sold are NULL for sources that only expose tickets-left (kupat GA).
CREATE TABLE IF NOT EXISTS tickchak_sales_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    event_code TEXT NOT NULL,
    perf_code TEXT,
    captured_at TEXT NOT NULL,
    capacity INTEGER,
    available INTEGER,
    sold INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tcsnap ON tickchak_sales_snapshots(event_code, captured_at);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    notes TEXT,
    due_date_iso TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'open',
    linked_source TEXT,
    linked_source_id TEXT,
    tags TEXT,
    amount REAL,
    amount_currency TEXT,
    recurrence TEXT,
    auto_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_status_due ON todos(status, due_date_iso);
CREATE INDEX IF NOT EXISTS idx_todos_auto_source ON todos(auto_source);
CREATE TABLE IF NOT EXISTS todo_subtasks (
    id TEXT PRIMARY KEY,
    todo_id TEXT NOT NULL,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    order_idx INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todo_subtasks_parent ON todo_subtasks(todo_id, order_idx);
CREATE TABLE IF NOT EXISTS todo_suggestion_dismissals (
    suggestion_key TEXT PRIMARY KEY,
    dismissed_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Kupat section name (Hebrew) -> viagogo section name, per venue. Not a
-- literal translation (e.g. Caesarea's "יציע תחתון 1" lists as viagogo's
-- "Middle Tier 1") so this is a taught lookup, not a hardcoded formula.
CREATE TABLE IF NOT EXISTS viagogo_section_map (
    venue TEXT NOT NULL,
    kupat_section TEXT NOT NULL,
    viagogo_section TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, kupat_section)
);
-- Hebrew Kupat artist/venue name → English viagogo search term.
-- Looked up before every search_event() call so Hebrew-named events match.
-- Auto-populated on approve; user can add rows via the /pending UI.
CREATE TABLE IF NOT EXISTS kupat_name_map (
    hebrew_name TEXT PRIMARY KEY,
    english_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- One row per Kupat purchase-confirmation email we're trying to push into a
-- draft viagogo listing. status lifecycle:
--   searching -> awaiting_approval -> approved -> creating -> created
--                                  -> rejected
--                       (any stage) -> error
--   no_match  (no candidate viagogo event found)
CREATE TABLE IF NOT EXISTS viagogo_push (
    id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'searching',
    event_name TEXT,
    venue TEXT,
    event_date_iso TEXT,
    section TEXT,
    row_label TEXT,
    seats TEXT,
    qty INTEGER,
    cost REAL,
    cost_per_unit REAL,
    candidates_json TEXT,
    chosen_event_id TEXT,
    chosen_event_name TEXT,
    chosen_venue TEXT,
    chosen_event_date TEXT,
    viagogo_section TEXT,
    fx_rate REAL,
    cost_usd_per_ticket REAL,
    website_price_usd REAL,
    ticket_url TEXT,
    listing_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_viagogo_push_status ON viagogo_push(status, created_at);
CREATE INDEX IF NOT EXISTS idx_viagogo_push_intake ON viagogo_push(intake_id);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)
        for _sql in (
            "ALTER TABLE pending_intake ADD COLUMN ticket_url TEXT",
            "ALTER TABLE viagogo_push ADD COLUMN ticket_url TEXT",
        ):
            try:
                conn.executescript(_sql)
            except Exception:
                pass
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(inventory)").fetchall()}
        if "event_date_iso" not in cols:
            conn.execute("ALTER TABLE inventory ADD COLUMN event_date_iso TEXT")
        if "stubhub_url" not in cols:
            conn.execute("ALTER TABLE inventory ADD COLUMN stubhub_url TEXT")
        ls_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lysted_sales)").fetchall()}
        if "cost" not in ls_cols:
            conn.execute("ALTER TABLE lysted_sales ADD COLUMN cost REAL")
        mi_cols = {row["name"] for row in conn.execute("PRAGMA table_info(manual_inventory)").fetchall()}
        if "email" not in mi_cols:
            conn.execute("ALTER TABLE manual_inventory ADD COLUMN email TEXT")
        # tm_watchers grew multi-source + per-watcher mute + channel routing.
        # SQLite can't add NOT NULL with default to existing rows in one shot,
        # so we add nullable columns and backfill.
        tw_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tm_watchers)").fetchall()}
        if "source" not in tw_cols:
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN source TEXT")
            conn.execute("UPDATE tm_watchers SET source = 'ticketmaster' WHERE source IS NULL")
        if "muted" not in tw_cols:
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
        if "notify_channels" not in tw_cols:
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN notify_channels TEXT")
            conn.execute("UPDATE tm_watchers SET notify_channels = 'discord,email' WHERE notify_channels IS NULL")
        if "filters" not in tw_cols:
            # JSON blob: {min_group_size, exclude_sections, min_price, max_price}.
            # Existing watchers stay unfiltered (NULL) so behavior doesn't
            # change under them; new watchers get min_group_size=2 from the
            # insert path's default.
            conn.execute("ALTER TABLE tm_watchers ADD COLUMN filters TEXT")
        td_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tm_drops)").fetchall()}
        if "notify_count" not in td_cols:
            conn.execute("ALTER TABLE tm_drops ADD COLUMN notify_count INTEGER")
        # Sales snapshots grew multi-source (kupat GA alongside tickchak
        # festival): add source + perf_code and backfill the tickchak rows
        # (perf_code is always '0' for tickchak).
        ss_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tickchak_sales_snapshots)").fetchall()}
        if "source" not in ss_cols:
            conn.execute("ALTER TABLE tickchak_sales_snapshots ADD COLUMN source TEXT")
            conn.execute("UPDATE tickchak_sales_snapshots SET source = 'tickchak' WHERE source IS NULL")
        if "perf_code" not in ss_cols:
            conn.execute("ALTER TABLE tickchak_sales_snapshots ADD COLUMN perf_code TEXT")
            conn.execute("UPDATE tickchak_sales_snapshots SET perf_code = '0' WHERE perf_code IS NULL")
        # Composite index created here (not in SCHEMA) so it lands AFTER the
        # source/perf_code columns exist on pre-existing tables.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tcsnap2 ON "
            "tickchak_sales_snapshots(source, event_code, perf_code, captured_at)"
        )
        # Maaser tax-deductible flag — existing rows default to 0 (not
        # claimed) which is a safe default; user can edit any back-history
        # entries to flip them on.
        mp_cols = {row["name"] for row in conn.execute("PRAGMA table_info(maaser_payments)").fetchall()}
        if "tax_deductible" not in mp_cols:
            conn.execute("ALTER TABLE maaser_payments ADD COLUMN tax_deductible INTEGER NOT NULL DEFAULT 0")
        # Cash-back entries auto-captured from Capital One emails carry a
        # source_ref (the rewards order number) so re-polls don't duplicate
        # them. Manual entries leave it NULL; the partial unique index ignores
        # NULLs so it never conflicts with hand-added rows.
        cb_cols = {row["name"] for row in conn.execute("PRAGMA table_info(cashback_entries)").fetchall()}
        if "source_ref" not in cb_cols:
            conn.execute("ALTER TABLE cashback_entries ADD COLUMN source_ref TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cashback_source_ref "
            "ON cashback_entries(source_ref) WHERE source_ref IS NOT NULL"
        )


def upsert_lysted_purchases(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO lysted_purchases (id, order_id, order_date, event_name,
                                              event_date, event_date_iso, venue,
                                              section, row_label, qty, seats,
                                              delivery_type, account_email,
                                              transaction_id, total_cost,
                                              cost_per_unit, status, last_seen_at)
                VALUES (:id, :order_id, :order_date, :event_name,
                        :event_date, :event_date_iso, :venue,
                        :section, :row_label, :qty, :seats,
                        :delivery_type, :account_email,
                        :transaction_id, :total_cost,
                        :cost_per_unit, :status, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    order_date=excluded.order_date,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    row_label=excluded.row_label,
                    qty=excluded.qty,
                    seats=excluded.seats,
                    delivery_type=excluded.delivery_type,
                    account_email=excluded.account_email,
                    transaction_id=excluded.transaction_id,
                    total_cost=excluded.total_cost,
                    cost_per_unit=excluded.cost_per_unit,
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_lysted_purchases():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lysted_purchases "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_viagogo(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO viagogo_listings (id, event_id, event_name, event_date,
                                              event_date_iso, venue, section,
                                              ticket_type, visibility,
                                              face_value, price, proceeds,
                                              available, sold, last_seen_at)
                VALUES (:id, :event_id, :event_name, :event_date,
                        :event_date_iso, :venue, :section,
                        :ticket_type, :visibility,
                        :face_value, :price, :proceeds,
                        :available, :sold, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    event_id=excluded.event_id,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    ticket_type=excluded.ticket_type,
                    visibility=excluded.visibility,
                    face_value=excluded.face_value,
                    price=excluded.price,
                    proceeds=excluded.proceeds,
                    available=excluded.available,
                    sold=excluded.sold,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_viagogo():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_listings "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name, section"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_inventory(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO inventory (id, event_name, event_date, event_time,
                                       event_date_iso, venue,
                                       listings_count, tickets_count,
                                       total_cost, total_list, stubhub_url, last_seen_at)
                VALUES (:id, :event_name, :event_date, :event_time,
                        :event_date_iso, :venue,
                        :listings_count, :tickets_count,
                        :total_cost, :total_list, :stubhub_url, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_time=excluded.event_time,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    listings_count=excluded.listings_count,
                    tickets_count=excluded.tickets_count,
                    total_cost=excluded.total_cost,
                    total_list=excluded.total_list,
                    stubhub_url=COALESCE(excluded.stubhub_url, inventory.stubhub_url),
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "stubhub_url": r.get("stubhub_url"), "last_seen_at": now_iso},
            )


def all_inventory():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM inventory "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def hide_inventory(source, source_id, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO inventory_hidden (source, source_id, hidden_at) VALUES (?, ?, ?)",
            (source, source_id, now_iso),
        )


def unhide_inventory(source, source_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM inventory_hidden WHERE source = ? AND source_id = ?",
            (source, source_id),
        )


def all_hidden_keys():
    with connect() as conn:
        rows = conn.execute("SELECT source, source_id FROM inventory_hidden").fetchall()
    return {(r["source"], r["source_id"]) for r in rows}


# --- "Didn't Sell" archive ---
# Inventory rows whose status is set to "not sold" (or a variant) get
# archived into inventory_unsold and filtered out of the active inventory
# view. Keyed by a content fingerprint (event + section + row + seats + qty)
# rather than (source, source_id) so the tombstone survives Lysted text
# drift and Viagogo listing-id rotation across resyncs.
import re as _re

_WS_RE = _re.compile(r"\s+")


def _unsold_norm(s):
    return _WS_RE.sub(" ", str(s or "").strip().lower())


def unsold_fingerprint(source, event_name, event_date_iso, section, row_label, seats, qty):
    """Stable across resyncs — no source_id, just content."""
    parts = [
        _unsold_norm(source),
        _unsold_norm(event_name),
        (event_date_iso or "").strip()[:10],
        _unsold_norm(section),
        _unsold_norm(row_label),
        _unsold_norm(seats),
        str(int(qty or 0)),
    ]
    return "|".join(parts)


def mark_inventory_unsold(snap, now_iso):
    """snap should already include 'fingerprint'."""
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO inventory_unsold
                (fingerprint, source, source_id, event_name, event_date,
                 event_date_iso, venue, section, row_label, seats, qty,
                 cost, cost_per_unit, list_price, delivery_type, marked_at, note)
            VALUES (:fingerprint, :source, :source_id, :event_name, :event_date,
                    :event_date_iso, :venue, :section, :row_label, :seats, :qty,
                    :cost, :cost_per_unit, :list_price, :delivery_type, :marked_at, :note)
            """,
            {**snap, "marked_at": now_iso},
        )


def unmark_inventory_unsold(fingerprint):
    with connect() as conn:
        conn.execute("DELETE FROM inventory_unsold WHERE fingerprint = ?", (fingerprint,))


def all_unsold():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM inventory_unsold "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def all_unsold_fingerprints():
    with connect() as conn:
        rows = conn.execute("SELECT fingerprint FROM inventory_unsold").fetchall()
    return {r["fingerprint"] for r in rows}


def hide_sale(source, sale_id, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sales_hidden (source, sale_id, hidden_at) VALUES (?, ?, ?)",
            (source, sale_id, now_iso),
        )


def unhide_sale(source, sale_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM sales_hidden WHERE source = ? AND sale_id = ?",
            (source, sale_id),
        )


def all_hidden_sale_keys():
    with connect() as conn:
        rows = conn.execute("SELECT source, sale_id FROM sales_hidden").fetchall()
    return {(r["source"], r["sale_id"]) for r in rows}


# --- Canceled sales archive ---
# Distinct from sales_hidden ("× delete from view"). Canceled means the sale
# was reversed by the platform — the ticket goes back to inventory (which the
# platform's own scrape will pick up again on relist). Excluded from totals
# and surfaced in a separate archive on the sales page.

def cancel_sale(source, sale_id, now_iso, reason=None):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sales_canceled (source, sale_id, canceled_at, reason) "
            "VALUES (?, ?, ?, ?)",
            (source, sale_id, now_iso, reason),
        )


def uncancel_sale(source, sale_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM sales_canceled WHERE source = ? AND sale_id = ?",
            (source, sale_id),
        )


def all_canceled_sale_keys():
    with connect() as conn:
        rows = conn.execute("SELECT source, sale_id FROM sales_canceled").fetchall()
    return {(r["source"], r["sale_id"]) for r in rows}


def all_canceled_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT source, sale_id, canceled_at, reason FROM sales_canceled "
            "ORDER BY canceled_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# --- Event-group merges ---
# When the user manually merges two or more event groups (different names
# for the same show), each raw group_key gets a row pointing to the same
# canonical group_key + display fields. The grouping logic in app.py
# rewrites raw_group_key → canonical_group_key and overrides the row's
# event_name/date/venue display fields.

def merge_event_groups(raw_group_keys, canonical_group_key,
                      canonical_event_name, canonical_event_date,
                      canonical_event_date_iso, canonical_venue, now_iso):
    with connect() as conn:
        for raw in raw_group_keys:
            conn.execute(
                """
                INSERT OR REPLACE INTO event_group_merges
                    (raw_group_key, canonical_group_key,
                     canonical_event_name, canonical_event_date,
                     canonical_event_date_iso, canonical_venue, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (raw, canonical_group_key, canonical_event_name,
                 canonical_event_date, canonical_event_date_iso,
                 canonical_venue, now_iso),
            )


def unmerge_event_group(raw_group_key):
    """Remove a single raw_group_key from its merge."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM event_group_merges WHERE raw_group_key = ?",
            (raw_group_key,),
        )


def unmerge_canonical(canonical_group_key):
    """Drop the entire merge for a canonical group (all raw keys split apart)."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM event_group_merges WHERE canonical_group_key = ?",
            (canonical_group_key,),
        )


def all_event_group_merges():
    """Returns {raw_group_key: dict_of_canonical_fields}."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT raw_group_key, canonical_group_key, canonical_event_name, "
            "canonical_event_date, canonical_event_date_iso, canonical_venue, "
            "created_at FROM event_group_merges"
        ).fetchall()
    return {r["raw_group_key"]: dict(r) for r in rows}


def set_inv_override(source, source_id, field, value, now_iso):
    with connect() as conn:
        if value in (None, ""):
            conn.execute(
                "DELETE FROM inventory_overrides WHERE source=? AND source_id=? AND field=?",
                (source, source_id, field),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO inventory_overrides (source, source_id, field, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (source, source_id, field, str(value), now_iso),
            )


def all_inv_overrides():
    with connect() as conn:
        rows = conn.execute("SELECT source, source_id, field, value FROM inventory_overrides").fetchall()
    out = {}
    for r in rows:
        out.setdefault((r["source"], r["source_id"]), {})[r["field"]] = r["value"]
    return out


def set_sale_override(source, sale_id, field, value, now_iso):
    with connect() as conn:
        if value in (None, ""):
            conn.execute(
                "DELETE FROM sales_overrides WHERE source=? AND sale_id=? AND field=?",
                (source, sale_id, field),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO sales_overrides (source, sale_id, field, value, updated_at) VALUES (?, ?, ?, ?, ?)",
                (source, sale_id, field, str(value), now_iso),
            )


def insert_manual_inventory(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_inventory (id, event_name, event_date, event_date_iso,
                venue, section, row_label, seats, qty, cost_per_unit, note, email,
                matched_source, matched_source_id, matched_at, created_at)
            VALUES (:id, :event_name, :event_date, :event_date_iso,
                :venue, :section, :row_label, :seats, :qty, :cost_per_unit, :note, :email,
                NULL, NULL, NULL, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_manual_inventory():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_inventory "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_manual_inventory(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE manual_inventory SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_manual_inventory(id_):
    with connect() as conn:
        conn.execute("DELETE FROM manual_inventory WHERE id = ?", (id_,))


def mark_manual_inventory_listed(id_, source, source_id, now_iso):
    with connect() as conn:
        conn.execute(
            "UPDATE manual_inventory SET matched_source=?, matched_source_id=?, matched_at=? WHERE id=?",
            (source, source_id, now_iso, id_),
        )


def insert_manual_sale(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_sales (id, inv_source, inv_source_id, event_name,
                event_date, event_date_iso, venue, section, row_label, seats,
                qty, sale_price, cost, sale_date, sale_date_iso, platform,
                is_loss, note, created_at)
            VALUES (:id, :inv_source, :inv_source_id, :event_name,
                :event_date, :event_date_iso, :venue, :section, :row_label, :seats,
                :qty, :sale_price, :cost, :sale_date, :sale_date_iso, :platform,
                :is_loss, :note, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_manual_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manual_sales ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_manual_sale(sale_id):
    with connect() as conn:
        conn.execute("DELETE FROM manual_sales WHERE id = ?", (sale_id,))


def seats_sold_by_inv():
    """Map (inv_source, inv_source_id) -> combined seats string from manual_sales,
    so the pending modal can hide seats that were already recorded as sold."""
    out = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT inv_source, inv_source_id, seats FROM manual_sales "
            "WHERE seats IS NOT NULL AND seats <> ''"
        ).fetchall()
    for r in rows:
        key = (r["inv_source"], str(r["inv_source_id"]))
        out.setdefault(key, []).append(r["seats"])
    return {k: ", ".join(v) for k, v in out.items()}


def all_sale_overrides():
    with connect() as conn:
        rows = conn.execute("SELECT source, sale_id, field, value FROM sales_overrides").fetchall()
    out = {}
    for r in rows:
        out.setdefault((r["source"], r["sale_id"]), {})[r["field"]] = r["value"]
    return out


def record_match(sale_source, sale_id, inv_source, inv_source_id, qty, reason, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO inventory_matches
               (sale_source, sale_id, inv_source, inv_source_id, qty_matched, matched_at, match_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sale_source, sale_id, inv_source, inv_source_id, qty, now_iso, reason),
        )


def all_matches():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM inventory_matches").fetchall()
    return [dict(r) for r in rows]


def find_match_for_inv(inv_source, inv_source_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inventory_matches WHERE inv_source = ? AND inv_source_id = ? LIMIT 1",
            (inv_source, inv_source_id),
        ).fetchone()
    return dict(row) if row else None


def delete_match(sale_source, sale_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM inventory_matches WHERE sale_source = ? AND sale_id = ?",
            (sale_source, sale_id),
        )


def add_blocklist(sale_source, sale_id, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO match_blocklist (sale_source, sale_id, blocked_at)
               VALUES (?, ?, ?)""",
            (sale_source, sale_id, now_iso),
        )


def all_blocklist_keys():
    with connect() as conn:
        rows = conn.execute("SELECT sale_source, sale_id FROM match_blocklist").fetchall()
    return {(r["sale_source"], r["sale_id"]) for r in rows}


def upsert_lysted_sales(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO lysted_sales (id, order_id, sale_date, sale_date_iso,
                    event_name, event_date, event_date_iso, venue, section,
                    row_label, qty, seats, delivery_type, sale_price, payout,
                    fees, cost, status, raw_cells, last_seen_at)
                VALUES (:id, :order_id, :sale_date, :sale_date_iso,
                    :event_name, :event_date, :event_date_iso, :venue, :section,
                    :row_label, :qty, :seats, :delivery_type, :sale_price, :payout,
                    :fees, :cost, :status, :raw_cells, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    sale_date=excluded.sale_date,
                    sale_date_iso=excluded.sale_date_iso,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    row_label=excluded.row_label,
                    qty=excluded.qty,
                    seats=excluded.seats,
                    delivery_type=excluded.delivery_type,
                    sale_price=excluded.sale_price,
                    payout=excluded.payout,
                    fees=excluded.fees,
                    cost=excluded.cost,
                    status=excluded.status,
                    raw_cells=excluded.raw_cells,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_lysted_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lysted_sales "
            "ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_viagogo_sales(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO viagogo_sales (id, order_id, sale_date, sale_date_iso,
                    event_name, event_date, event_date_iso, venue, section,
                    row_label, seats, qty, ticket_type, sale_price,
                    upload_deadline, tab, status, raw_cells, last_seen_at)
                VALUES (:id, :order_id, :sale_date, :sale_date_iso,
                    :event_name, :event_date, :event_date_iso, :venue, :section,
                    :row_label, :seats, :qty, :ticket_type, :sale_price,
                    :upload_deadline, :tab, :status, :raw_cells, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    sale_date=excluded.sale_date,
                    sale_date_iso=excluded.sale_date_iso,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    section=excluded.section,
                    row_label=excluded.row_label,
                    seats=excluded.seats,
                    qty=excluded.qty,
                    ticket_type=excluded.ticket_type,
                    sale_price=excluded.sale_price,
                    upload_deadline=excluded.upload_deadline,
                    tab=excluded.tab,
                    status=excluded.status,
                    raw_cells=excluded.raw_cells,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_viagogo_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_sales "
            "ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_crowdvolt_sales(rows, now_iso):
    with connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO crowdvolt_sales (id, order_id, sale_date, sale_date_iso,
                    event_name, event_date, event_date_iso, venue, qty, ticket_type,
                    price_per_ticket, sale_price, status, raw_cells, last_seen_at)
                VALUES (:id, :order_id, :sale_date, :sale_date_iso,
                    :event_name, :event_date, :event_date_iso, :venue, :qty, :ticket_type,
                    :price_per_ticket, :sale_price, :status, :raw_cells, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    order_id=excluded.order_id,
                    sale_date=excluded.sale_date,
                    sale_date_iso=excluded.sale_date_iso,
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    event_date_iso=excluded.event_date_iso,
                    venue=excluded.venue,
                    qty=excluded.qty,
                    ticket_type=excluded.ticket_type,
                    price_per_ticket=excluded.price_per_ticket,
                    sale_price=excluded.sale_price,
                    status=excluded.status,
                    raw_cells=excluded.raw_cells,
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
            )


def all_crowdvolt_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM crowdvolt_sales "
            "ORDER BY sale_date_iso IS NULL, sale_date_iso DESC, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def replace_jerujam(tickets, sales, expenses, now_iso):
    with connect() as conn:
        conn.execute("DELETE FROM jerujam_tickets")
        conn.execute("DELETE FROM jerujam_sales")
        conn.execute("DELETE FROM jerujam_expenses")
        for t in tickets:
            conn.execute(
                """
                INSERT INTO jerujam_tickets (id, event_name, event_date, event_date_iso,
                    venue, city, section, row_label, seat_numbers, quantity,
                    cost_per_ticket, total_purchase_cost, purchase_platform,
                    purchase_account, purchase_date, listing_price, listing_platform,
                    sold_price, sold_count, sale_date, seller_fees, status,
                    notes, credit_card, created_at, updated_at, last_seen_at)
                VALUES (:id, :event_name, :event_date, :event_date_iso,
                    :venue, :city, :section, :row_label, :seat_numbers, :quantity,
                    :cost_per_ticket, :total_purchase_cost, :purchase_platform,
                    :purchase_account, :purchase_date, :listing_price, :listing_platform,
                    :sold_price, :sold_count, :sale_date, :seller_fees, :status,
                    :notes, :credit_card, :created_at, :updated_at, :last_seen_at)
                """,
                {**t, "last_seen_at": now_iso},
            )
        for s in sales:
            conn.execute(
                """
                INSERT INTO jerujam_sales (id, ticket_id, quantity, sale_price,
                    sale_date, platform, created_at, last_seen_at)
                VALUES (:id, :ticket_id, :quantity, :sale_price,
                    :sale_date, :platform, :created_at, :last_seen_at)
                """,
                {**s, "last_seen_at": now_iso},
            )
        for e in expenses:
            conn.execute(
                """
                INSERT INTO jerujam_expenses (id, date, vendor, category,
                    description, amount, notes, created_at, last_seen_at)
                VALUES (:id, :date, :vendor, :category,
                    :description, :amount, :notes, :created_at, :last_seen_at)
                """,
                {**e, "last_seen_at": now_iso},
            )


def all_jerujam_tickets():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jerujam_tickets "
            "ORDER BY event_date_iso IS NULL, event_date_iso, event_name"
        ).fetchall()
    return [dict(r) for r in rows]


def all_jerujam_sales():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jerujam_sales ORDER BY sale_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def all_jerujam_expenses():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jerujam_expenses ORDER BY date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def backup(dest_path):
    # SQLite online backup API: safe to run while writes are in flight.
    # To restore: stop Kartis, replace kartis.db with the dated snapshot, restart.
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def insert_expense_subscription(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO expense_subscriptions (id, name, category, amount,
                started_at_iso, ended_at_iso, notes, created_at)
            VALUES (:id, :name, :category, :amount,
                :started_at_iso, :ended_at_iso, :notes, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_expense_subscriptions():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM expense_subscriptions ORDER BY ended_at_iso IS NOT NULL, started_at_iso, name"
        ).fetchall()
    return [dict(r) for r in rows]


def update_expense_subscription(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE expense_subscriptions SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_expense_subscription(id_):
    with connect() as conn:
        conn.execute("DELETE FROM expense_subscriptions WHERE id = ?", (id_,))


def insert_expense(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO expenses (id, date, date_iso, vendor, category, amount,
                subscription_id, month_key, notes, created_at)
            VALUES (:id, :date, :date_iso, :vendor, :category, :amount,
                :subscription_id, :month_key, :notes, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_expenses():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM expenses ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_expense(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE expenses SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_expense(id_):
    with connect() as conn:
        conn.execute("DELETE FROM expenses WHERE id = ?", (id_,))


def upsert_subscription_instance(sub_id, month_key, row, now_iso):
    """Idempotent: relies on UNIQUE(subscription_id, month_key) — if a row already
    exists for this sub+month, the INSERT is silently ignored. The user can still
    edit/delete the existing row through normal expense endpoints."""
    with connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO expenses (id, date, date_iso, vendor, category, amount,
                subscription_id, month_key, notes, created_at)
            VALUES (:id, :date, :date_iso, :vendor, :category, :amount,
                :subscription_id, :month_key, :notes, :created_at)
            """,
            {**row, "subscription_id": sub_id, "month_key": month_key, "created_at": now_iso},
        )


def insert_owed_item(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO owed_items (id, date_iso, amount, description,
                card_account, direction, created_at)
            VALUES (:id, :date_iso, :amount, :description,
                :card_account, :direction, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_owed_items():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM owed_items ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_owed_item(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE owed_items SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_owed_item(id_):
    with connect() as conn:
        conn.execute("DELETE FROM owed_items WHERE id = ?", (id_,))


def insert_cashback_entry(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO cashback_entries (id, date_iso, amount, card_name, source_ref, created_at)
            VALUES (:id, :date_iso, :amount, :card_name, :source_ref, :created_at)
            """,
            {"source_ref": None, **row, "created_at": now_iso},
        )


def has_cashback_source_ref(ref):
    if not ref:
        return False
    with connect() as conn:
        r = conn.execute(
            "SELECT 1 FROM cashback_entries WHERE source_ref = ? LIMIT 1", (ref,)
        ).fetchone()
    return r is not None


def all_cashback_entries():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM cashback_entries ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_cashback_entry(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE cashback_entries SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_cashback_entry(id_):
    with connect() as conn:
        conn.execute("DELETE FROM cashback_entries WHERE id = ?", (id_,))


def distinct_cashback_cards():
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT card_name FROM cashback_entries WHERE card_name <> '' ORDER BY card_name"
        ).fetchall()
    return [r["card_name"] for r in rows]


def insert_attachment(row):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO attachments (id, owner_type, owner_id, filename,
                stored_path, size_bytes, content_type, uploaded_at)
            VALUES (:id, :owner_type, :owner_id, :filename,
                :stored_path, :size_bytes, :content_type, :uploaded_at)
            """,
            row,
        )


def get_attachment(id_):
    with connect() as conn:
        r = conn.execute("SELECT * FROM attachments WHERE id = ?", (id_,)).fetchone()
    return dict(r) if r else None


def list_attachments(owner_type, owner_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE owner_type = ? AND owner_id = ? "
            "ORDER BY uploaded_at",
            (owner_type, owner_id),
        ).fetchall()
    return [dict(r) for r in rows]


def list_attachments_for_owners(owner_type, owner_ids):
    """Batched version of list_attachments — used by /api/inventory-all to
    avoid an N+1 query when enriching every pending row."""
    ids = list({str(x) for x in owner_ids if x is not None})
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM attachments WHERE owner_type = ? "
            f"AND owner_id IN ({placeholders}) ORDER BY uploaded_at",
            (owner_type, *ids),
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["owner_id"], []).append(dict(r))
    return out


def delete_attachment(id_):
    with connect() as conn:
        conn.execute("DELETE FROM attachments WHERE id = ?", (id_,))


def delete_attachments_for_owner(owner_type, owner_id):
    with connect() as conn:
        conn.execute(
            "DELETE FROM attachments WHERE owner_type = ? AND owner_id = ?",
            (owner_type, owner_id),
        )


def reassign_attachments_owner(owner_type, old_id, new_id):
    """Move attachment DB rows from one owner_id to another. Used when a
    pending_intake row is confirmed and promoted to a manual_inventory row —
    the files on disk stay where they are; we just update the owner pointer
    and (caller is responsible for) optionally moving the disk dir too."""
    with connect() as conn:
        conn.execute(
            "UPDATE attachments SET owner_id = ? WHERE owner_type = ? AND owner_id = ?",
            (new_id, owner_type, old_id),
        )


def insert_pending_intake(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_intake (id, message_id, provider, email_from,
                email_subject, email_received_at, event_name, event_date_iso,
                venue, section, row_label, seats, qty, cost, cost_per_unit,
                raw_text, parse_warnings, ticket_url, status, created_at)
            VALUES (:id, :message_id, :provider, :email_from,
                :email_subject, :email_received_at, :event_name, :event_date_iso,
                :venue, :section, :row_label, :seats, :qty, :cost, :cost_per_unit,
                :raw_text, :parse_warnings, :ticket_url, :status, :created_at)
            ON CONFLICT(message_id) DO NOTHING
            """,
            {**row, "created_at": now_iso},
        )


def get_pending_intake(id_):
    with connect() as conn:
        r = conn.execute("SELECT * FROM pending_intake WHERE id = ?", (id_,)).fetchone()
    return dict(r) if r else None


def all_pending_intake(status="new"):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_intake WHERE status = ? "
            "ORDER BY email_received_at DESC, created_at DESC",
            (status,),
        ).fetchall()
    return [dict(r) for r in rows]


def has_intake_message(message_id):
    if not message_id:
        return False
    with connect() as conn:
        r = conn.execute(
            "SELECT 1 FROM pending_intake WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
    return r is not None


def update_pending_intake(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE pending_intake SET {sets} WHERE id=:_id",
            {**fields, "_id": id_},
        )


def delete_pending_intake(id_):
    with connect() as conn:
        conn.execute("DELETE FROM pending_intake WHERE id = ?", (id_,))


def insert_maaser(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO maaser_payments (id, date, date_iso, recipient, amount, notes, tax_deductible, created_at)
            VALUES (:id, :date, :date_iso, :recipient, :amount, :notes, :tax_deductible, :created_at)
            """,
            {**{"tax_deductible": 0}, **row, "created_at": now_iso},
        )


def all_maaser():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM maaser_payments ORDER BY date_iso DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_maaser(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE maaser_payments SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_maaser(id_):
    with connect() as conn:
        conn.execute("DELETE FROM maaser_payments WHERE id = ?", (id_,))


# --- Ticketmaster drop watchers ----------------------------------------

def tm_insert_watcher(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tm_watchers (id, label, source, event_code, perf_code,
                paused, muted, notify_channels, filters, created_at)
            VALUES (:id, :label, :source, :event_code, :perf_code,
                :paused, :muted, :notify_channels, :filters, :created_at)
            """,
            {
                "muted": 0,
                "notify_channels": "discord,email",
                "source": "ticketmaster",
                "filters": None,
                **row,
                "created_at": now_iso,
            },
        )


def tm_all_watchers():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tm_watchers ORDER BY paused, created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def tm_get_watcher(id_):
    with connect() as conn:
        row = conn.execute("SELECT * FROM tm_watchers WHERE id = ?", (id_,)).fetchone()
    return dict(row) if row else None


def tm_active_watchers():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tm_watchers WHERE paused = 0 ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def tm_update_watcher(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE tm_watchers SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def tm_delete_watcher(id_):
    with connect() as conn:
        conn.execute("DELETE FROM tm_watchers WHERE id = ?", (id_,))
        conn.execute("DELETE FROM tm_seat_state WHERE watcher_id = ?", (id_,))
        conn.execute("DELETE FROM tm_drops WHERE watcher_id = ?", (id_,))


def tm_get_seat_keys(watcher_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT seat_key FROM tm_seat_state WHERE watcher_id = ?", (watcher_id,)
        ).fetchall()
    return {r["seat_key"] for r in rows}


def tm_replace_seat_state(watcher_id, seats):
    """Atomically replace the watcher's known seat set. Seats must be in the
    normalized shape with `block`, `row`, `seat` keys (each source module
    produces this shape from its raw API response).
    """
    with connect() as conn:
        conn.execute("DELETE FROM tm_seat_state WHERE watcher_id = ?", (watcher_id,))
        seen = set()
        for s in seats:
            block = str(s.get("block") or s.get("b") or "")
            row = str(s.get("row") or s.get("r") or "")
            num = str(s.get("seat") if s.get("seat") is not None else (s.get("l") if s.get("l") is not None else ""))
            key = f"{block}|{row}|{num}"
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                "INSERT INTO tm_seat_state (watcher_id, seat_key, block, row_label, seat_num) "
                "VALUES (?, ?, ?, ?, ?)",
                (watcher_id, key, block, row, num),
            )


# --- App-level settings (master pause / mute / future flags) -------------

def setting_get(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key, value, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value) if value is not None else None, now_iso),
        )


def setting_get_bool(key, default=False):
    v = setting_get(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def all_settings():
    with connect() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM app_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def tm_record_drop(watcher_id, added_count, removed_count, seats_json, notify_result, now_iso, notify_count=None):
    with connect() as conn:
        conn.execute(
            """INSERT INTO tm_drops (watcher_id, detected_at, added_count,
               removed_count, seats_json, notify_result, notify_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (watcher_id, now_iso, added_count, removed_count, seats_json, notify_result,
             notify_count if notify_count is not None else added_count),
        )


def tm_recent_drops(watcher_id=None, limit=200):
    with connect() as conn:
        if watcher_id:
            rows = conn.execute(
                "SELECT * FROM tm_drops WHERE watcher_id = ? ORDER BY detected_at DESC LIMIT ?",
                (watcher_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tm_drops ORDER BY detected_at DESC LIMIT ?", (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------- viagogo section name mapping (taught, not literal) -------

def viagogo_section_map_get(venue, kupat_section):
    with connect() as conn:
        row = conn.execute(
            "SELECT viagogo_section FROM viagogo_section_map WHERE venue = ? AND kupat_section = ?",
            (venue, kupat_section),
        ).fetchone()
    return row["viagogo_section"] if row else None


def viagogo_section_map_set(venue, kupat_section, viagogo_section, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO viagogo_section_map (venue, kupat_section, viagogo_section, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(venue, kupat_section) DO UPDATE SET
                   viagogo_section = excluded.viagogo_section,
                   updated_at = excluded.updated_at""",
            (venue, kupat_section, viagogo_section, now_iso),
        )


def kupat_name_map_get(hebrew_name):
    """Return the English search term for a Hebrew Kupat artist/venue name, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT english_name FROM kupat_name_map WHERE hebrew_name = ?",
            (hebrew_name,),
        ).fetchone()
    return row["english_name"] if row else None


def kupat_name_map_set(hebrew_name, english_name, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO kupat_name_map (hebrew_name, english_name, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(hebrew_name) DO UPDATE SET
                   english_name = excluded.english_name,
                   updated_at = excluded.updated_at""",
            (hebrew_name, english_name, now_iso),
        )


def kupat_name_map_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT hebrew_name, english_name, updated_at FROM kupat_name_map ORDER BY hebrew_name"
        ).fetchall()
    return [dict(r) for r in rows]


def viagogo_section_map_all():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM viagogo_section_map ORDER BY venue, kupat_section"
        ).fetchall()
    return [dict(r) for r in rows]


def viagogo_section_map_delete(venue, kupat_section):
    with connect() as conn:
        conn.execute(
            "DELETE FROM viagogo_section_map WHERE venue = ? AND kupat_section = ?",
            (venue, kupat_section),
        )


# ---------------- viagogo draft-listing push pipeline ----------------------
# Kupat purchase-confirmation email -> matched viagogo event -> (user
# approval) -> draft (unpublished) viagogo listing. See viagogo_listing.py.

def viagogo_push_insert(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO viagogo_push (id, intake_id, status, event_name, venue,
                event_date_iso, section, row_label, seats, qty, cost, cost_per_unit,
                candidates_json, chosen_event_id, chosen_event_name, chosen_venue,
                chosen_event_date, viagogo_section, fx_rate, cost_usd_per_ticket,
                website_price_usd, ticket_url, listing_id, error, created_at, updated_at)
            VALUES (:id, :intake_id, :status, :event_name, :venue,
                :event_date_iso, :section, :row_label, :seats, :qty, :cost, :cost_per_unit,
                :candidates_json, :chosen_event_id, :chosen_event_name, :chosen_venue,
                :chosen_event_date, :viagogo_section, :fx_rate, :cost_usd_per_ticket,
                :website_price_usd, :ticket_url, :listing_id, :error, :created_at, :updated_at)
            """,
            {
                "status": "searching",
                "candidates_json": None, "chosen_event_id": None, "chosen_event_name": None,
                "chosen_venue": None, "chosen_event_date": None, "viagogo_section": None,
                "fx_rate": None, "cost_usd_per_ticket": None, "website_price_usd": None,
                "ticket_url": None, "listing_id": None, "error": None,
                **row,
                "created_at": now_iso, "updated_at": now_iso,
            },
        )


def viagogo_push_get(id_):
    with connect() as conn:
        row = conn.execute("SELECT * FROM viagogo_push WHERE id = ?", (id_,)).fetchone()
    return dict(row) if row else None


def viagogo_push_all(status=None):
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM viagogo_push WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM viagogo_push ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def viagogo_push_get_by_intake(intake_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM viagogo_push WHERE intake_id = ? ORDER BY created_at DESC LIMIT 1",
            (intake_id,),
        ).fetchone()
    return dict(row) if row else None


def viagogo_push_update(id_, fields, now_iso):
    if not fields:
        return
    fields = {**fields, "updated_at": now_iso}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE viagogo_push SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def viagogo_push_delete(id_):
    with connect() as conn:
        conn.execute("DELETE FROM viagogo_push WHERE id = ?", (id_,))


# ---------------- GA / festival sales snapshots ----------------------------
# Keyed by (source, event_code, perf_code) so tickchak festival (perf '0')
# and kupat GA (real presentation ids) share one table. `capacity`/`sold`
# may be NULL (kupat GA only exposes tickets-left); velocity is computed
# from `available` deltas, which both sources have.

def sales_snapshot_insert(source, event_code, perf_code, capacity, available, sold, now_iso):
    with connect() as conn:
        conn.execute(
            """INSERT INTO tickchak_sales_snapshots
               (source, event_code, perf_code, captured_at, capacity, available, sold)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, str(event_code), str(perf_code), now_iso, capacity, available, sold),
        )


def sales_snapshot_latest(source, event_code, perf_code):
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM tickchak_sales_snapshots WHERE source = ? AND event_code = ? "
            "AND perf_code = ? ORDER BY captured_at DESC LIMIT 1",
            (source, str(event_code), str(perf_code)),
        ).fetchone()
    return dict(r) if r else None


def sales_snapshot_earliest(source, event_code, perf_code):
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM tickchak_sales_snapshots WHERE source = ? AND event_code = ? "
            "AND perf_code = ? ORDER BY captured_at ASC LIMIT 1",
            (source, str(event_code), str(perf_code)),
        ).fetchone()
    return dict(r) if r else None


def sales_snapshot_asof(source, event_code, perf_code, ts_iso):
    """Latest snapshot at or before ts_iso — the baseline for a rolling window."""
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM tickchak_sales_snapshots WHERE source = ? AND event_code = ? "
            "AND perf_code = ? AND captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
            (source, str(event_code), str(perf_code), ts_iso),
        ).fetchone()
    return dict(r) if r else None


def sales_snapshot_prune(cutoff_iso):
    """Drop snapshots older than cutoff_iso (we only need ~7 days of history)."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM tickchak_sales_snapshots WHERE captured_at < ?", (cutoff_iso,),
        )


# ---------------- Kupat credits ---------------------------------------------

def insert_kupat_credit(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kupat_credits (id, issued_date, ils_amount, original_usd_cost, note, created_at)
            VALUES (:id, :issued_date, :ils_amount, :original_usd_cost, :note, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_kupat_credits():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kupat_credits ORDER BY issued_date DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_kupat_credit(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE kupat_credits SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def delete_kupat_credit(id_):
    with connect() as conn:
        conn.execute("DELETE FROM kupat_credit_spends WHERE credit_id = ?", (id_,))
        conn.execute("DELETE FROM kupat_credits WHERE id = ?", (id_,))


def insert_kupat_spend(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO kupat_credit_spends (id, credit_id, spend_date, ils_amount, fx_rate, note, created_at)
            VALUES (:id, :credit_id, :spend_date, :ils_amount, :fx_rate, :note, :created_at)
            """,
            {**row, "created_at": now_iso},
        )


def all_kupat_spends():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM kupat_credit_spends ORDER BY spend_date DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_kupat_spend(id_):
    with connect() as conn:
        conn.execute("DELETE FROM kupat_credit_spends WHERE id = ?", (id_,))


# ---------------- To-Do list -----------------------------------------------

def todo_insert(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO todos (id, title, notes, due_date_iso, urgency, status,
                linked_source, linked_source_id, tags, amount, amount_currency,
                recurrence, auto_source, created_at, updated_at, completed_at,
                notified_at)
            VALUES (:id, :title, :notes, :due_date_iso, :urgency, :status,
                :linked_source, :linked_source_id, :tags, :amount, :amount_currency,
                :recurrence, :auto_source, :created_at, :updated_at, :completed_at,
                :notified_at)
            """,
            {
                "notes": None, "due_date_iso": None, "urgency": "normal",
                "status": "open", "linked_source": None, "linked_source_id": None,
                "tags": None, "amount": None, "amount_currency": None,
                "recurrence": None, "auto_source": None,
                "completed_at": None, "notified_at": None,
                **row,
                "created_at": now_iso, "updated_at": now_iso,
            },
        )


def todo_get(id_):
    with connect() as conn:
        r = conn.execute("SELECT * FROM todos WHERE id = ?", (id_,)).fetchone()
    return dict(r) if r else None


def todo_all(status=None):
    sql = "SELECT * FROM todos"
    args = ()
    if status and status != "all":
        sql += " WHERE status = ?"
        args = (status,)
    sql += (
        " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,"
        " due_date_iso IS NULL, due_date_iso,"
        " CASE urgency WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
        " WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,"
        " created_at"
    )
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def todo_update(id_, fields, now_iso):
    if not fields:
        return
    fields = {**fields, "updated_at": now_iso}
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE todos SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def todo_delete(id_):
    with connect() as conn:
        conn.execute("DELETE FROM todo_subtasks WHERE todo_id = ?", (id_,))
        conn.execute("DELETE FROM todos WHERE id = ?", (id_,))


def todo_open_by_auto_source(auto_source):
    """Returns the open todo (if any) linked to a given suggestion key — used
    by the suggestion engine to avoid double-surfacing once the user has
    converted a suggestion into a real task."""
    if not auto_source:
        return None
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM todos WHERE auto_source = ? AND status = 'open' LIMIT 1",
            (auto_source,),
        ).fetchone()
    return dict(r) if r else None


def todo_due_open(today_iso):
    """Tasks that are open and due today or earlier and not already notified
    today — feeds the daily reminder digest."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM todos WHERE status = 'open' "
            "AND due_date_iso IS NOT NULL AND due_date_iso <= ? "
            "AND (notified_at IS NULL OR notified_at < ?) "
            "ORDER BY due_date_iso, urgency",
            (today_iso, today_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def todo_mark_notified(ids, today_iso):
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        conn.execute(
            f"UPDATE todos SET notified_at = ? WHERE id IN ({placeholders})",
            (today_iso, *ids),
        )


def subtask_insert(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO todo_subtasks (id, todo_id, title, done, order_idx, created_at)
            VALUES (:id, :todo_id, :title, :done, :order_idx, :created_at)
            """,
            {"done": 0, "order_idx": 0, **row, "created_at": now_iso},
        )


def subtask_list(todo_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM todo_subtasks WHERE todo_id = ? ORDER BY order_idx, created_at",
            (todo_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def subtask_list_for_todos(todo_ids):
    """Batched: {todo_id: [subtask, ...]} — avoids N+1 on the list page."""
    ids = [str(x) for x in todo_ids if x]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM todo_subtasks WHERE todo_id IN ({placeholders}) "
            f"ORDER BY order_idx, created_at",
            ids,
        ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["todo_id"], []).append(dict(r))
    return out


def subtask_toggle(id_):
    with connect() as conn:
        conn.execute(
            "UPDATE todo_subtasks SET done = 1 - done WHERE id = ?", (id_,)
        )


def subtask_update(id_, fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE todo_subtasks SET {sets} WHERE id=:_id", {**fields, "_id": id_})


def subtask_delete(id_):
    with connect() as conn:
        conn.execute("DELETE FROM todo_subtasks WHERE id = ?", (id_,))


def suggestion_dismiss(key, dismissed_until_iso, now_iso):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO todo_suggestion_dismissals "
            "(suggestion_key, dismissed_until, created_at) VALUES (?, ?, ?)",
            (key, dismissed_until_iso, now_iso),
        )


def suggestion_active_dismissals(today_iso):
    """Returns {key: dismissed_until} for dismissals that haven't expired."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT suggestion_key, dismissed_until FROM todo_suggestion_dismissals "
            "WHERE dismissed_until > ?",
            (today_iso,),
        ).fetchall()
    return {r["suggestion_key"]: r["dismissed_until"] for r in rows}
