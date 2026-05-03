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
CREATE TABLE IF NOT EXISTS sales_hidden (
    source TEXT NOT NULL,
    sale_id TEXT NOT NULL,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (source, sale_id)
);
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
CREATE TABLE IF NOT EXISTS maaser_payments (
    id TEXT PRIMARY KEY,
    date TEXT,
    date_iso TEXT NOT NULL,
    recipient TEXT,
    amount REAL NOT NULL,
    notes TEXT,
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
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
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
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(inventory)").fetchall()}
        if "event_date_iso" not in cols:
            conn.execute("ALTER TABLE inventory ADD COLUMN event_date_iso TEXT")
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
                                       total_cost, total_list, last_seen_at)
                VALUES (:id, :event_name, :event_date, :event_time,
                        :event_date_iso, :venue,
                        :listings_count, :tickets_count,
                        :total_cost, :total_list, :last_seen_at)
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
                    last_seen_at=excluded.last_seen_at
                """,
                {**r, "last_seen_at": now_iso},
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


def insert_maaser(row, now_iso):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO maaser_payments (id, date, date_iso, recipient, amount, notes, created_at)
            VALUES (:id, :date, :date_iso, :recipient, :amount, :notes, :created_at)
            """,
            {**row, "created_at": now_iso},
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
