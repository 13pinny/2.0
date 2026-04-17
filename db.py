import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "kartis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    event_name TEXT,
    event_date TEXT,
    section TEXT,
    row TEXT,
    seat TEXT,
    purchase_price REAL,
    sale_price REAL,
    status TEXT,
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


def upsert_tickets(tickets, now_iso):
    with connect() as conn:
        for t in tickets:
            conn.execute(
                """
                INSERT INTO tickets (id, event_name, event_date, section, row, seat,
                                     purchase_price, sale_price, status, last_seen_at)
                VALUES (:id, :event_name, :event_date, :section, :row, :seat,
                        :purchase_price, :sale_price, :status, :last_seen_at)
                ON CONFLICT(id) DO UPDATE SET
                    event_name=excluded.event_name,
                    event_date=excluded.event_date,
                    section=excluded.section,
                    row=excluded.row,
                    seat=excluded.seat,
                    purchase_price=excluded.purchase_price,
                    sale_price=excluded.sale_price,
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at
                """,
                {**t, "last_seen_at": now_iso},
            )


def all_tickets():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY event_date IS NULL, event_date, event_name"
        ).fetchall()
    return [dict(r) for r in rows]
