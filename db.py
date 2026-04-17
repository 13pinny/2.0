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
