"""
database.py — SQLite persistence for Swipe Swap.
"""

import sqlite3
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "swipe_swap.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    phone       TEXT PRIMARY KEY,
    role        TEXT DEFAULT 'unknown',   -- 'donor', 'receiver', 'unknown'
    state       TEXT DEFAULT 'new',       -- FSM state
    seen        INTEGER DEFAULT 0,        -- 1 after first welcome sent
    on_updates  INTEGER DEFAULT 0,
    availability TEXT,                   -- free-text availability
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    receiver_phone  TEXT NOT NULL,
    hall            TEXT NOT NULL,
    req_time        TEXT NOT NULL,   -- HH:MM string
    req_day         TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',  -- pending, fulfilled, canceled, expired
    donor_phone     TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    fulfilled_at    TEXT,
    expires_at      TEXT NOT NULL   -- created_at + 30 min
);

CREATE TABLE IF NOT EXISTS donor_offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  INTEGER NOT NULL,
    donor_phone TEXT NOT NULL,
    sent_at     TEXT DEFAULT (datetime('now')),
    response    TEXT,               -- NULL, 'accepted', 'declined'
    responded_at TEXT,
    UNIQUE(request_id, donor_phone)
);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            phone       TEXT PRIMARY KEY,
            role        TEXT DEFAULT 'unknown',
            state       TEXT DEFAULT 'new',
            seen        INTEGER DEFAULT 0,
            on_updates  INTEGER DEFAULT 0,
            availability TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            receiver_phone  TEXT NOT NULL,
            hall            TEXT NOT NULL,
            req_time        TEXT NOT NULL,
            req_day         TEXT NOT NULL,
            status          TEXT DEFAULT 'pending',
            donor_phone     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            fulfilled_at    TEXT,
            expires_at      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS donor_offers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id  INTEGER NOT NULL,
            donor_phone TEXT NOT NULL,
            sent_at     TEXT DEFAULT (datetime('now')),
            response    TEXT,
            responded_at TEXT,
            UNIQUE(request_id, donor_phone)
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized")

# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------
def ensure_user(phone: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (phone) VALUES (?)", (phone,)
        )

def is_new_user(phone: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT seen FROM users WHERE phone=?", (phone,)).fetchone()
        return row is None or row["seen"] == 0

def mark_seen(phone: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET seen=1 WHERE phone=?", (phone,))

def get_state(phone: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT state FROM users WHERE phone=?", (phone,)).fetchone()
        return row["state"] if row else None

def set_state(phone: str, state: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET state=? WHERE phone=?", (state, phone))

def set_role(phone: str, role: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET role=? WHERE phone=?", (role, phone))

def save_availability(phone: str, text: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET availability=? WHERE phone=?", (text, phone)
        )

def add_to_updates(phone: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET on_updates=1, state='on_updates_list' WHERE phone=?",
            (phone,)
        )

def remove_from_updates(phone: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET on_updates=0 WHERE phone=?", (phone,))

def get_all_donors():
    with get_conn() as conn:
        return conn.execute(
            "SELECT phone FROM users WHERE role='donor'"
        ).fetchall()

# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def create_request(receiver_phone: str, hall: str, req_time, req_day: str) -> int:
    from datetime import datetime, timedelta
    now     = datetime.utcnow()
    expires = now + timedelta(minutes=30)
    t_str   = req_time.strftime("%I:%M %p") if hasattr(req_time, "strftime") else str(req_time)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO requests
               (receiver_phone, hall, req_time, req_day, expires_at)
               VALUES (?,?,?,?,?)""",
            (receiver_phone, hall, t_str, req_day, expires.isoformat())
        )
        return cur.lastrowid

def get_request(request_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()

def cancel_request(request_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE requests SET status='canceled' WHERE id=?", (request_id,)
        )

def fulfill_request(request_id: int, donor_phone: str):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE requests SET status='fulfilled', donor_phone=?, fulfilled_at=?
               WHERE id=?""",
            (donor_phone, now, request_id)
        )
        conn.execute(
            """UPDATE donor_offers SET response='accepted', responded_at=?
               WHERE request_id=? AND donor_phone=?""",
            (now, request_id, donor_phone)
        )

def mark_donor_declined(request_id: int, donor_phone: str):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """UPDATE donor_offers SET response='declined', responded_at=?
               WHERE request_id=? AND donor_phone=?""",
            (now, request_id, donor_phone)
        )

def get_pending_donor_offer(donor_phone: str):
    """Get the most recent unanswered offer for this donor."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT do.*, r.hall, r.req_time
               FROM donor_offers do
               JOIN requests r ON r.id = do.request_id
               WHERE do.donor_phone=? AND do.response IS NULL
               AND r.status='pending'
               ORDER BY do.sent_at DESC LIMIT 1""",
            (donor_phone,)
        ).fetchone()

def record_donor_offer(request_id: int, donor_phone: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO donor_offers (request_id, donor_phone)
               VALUES (?,?)""",
            (request_id, donor_phone)
        )

def get_already_contacted(request_id: int) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT donor_phone FROM donor_offers WHERE request_id=?",
            (request_id,)
        ).fetchall()
        return [r["donor_phone"] for r in rows]

def get_pending_donor_offers(request_id: int) -> list:
    """Donors who were texted but haven't responded yet."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT donor_phone FROM donor_offers
               WHERE request_id=? AND response IS NULL""",
            (request_id,)
        ).fetchall()

def get_stalled_requests(timeout_minutes: int = 10) -> list:
    """Requests that are still pending and haven't had a donor offer sent recently."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(minutes=timeout_minutes)).isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.* FROM requests r
               WHERE r.status = 'pending'
               AND r.expires_at > datetime('now')
               AND (
                   NOT EXISTS (
                       SELECT 1 FROM donor_offers do
                       WHERE do.request_id = r.id AND do.response IS NULL
                   )
                   AND EXISTS (
                       SELECT 1 FROM donor_offers do2
                       WHERE do2.request_id = r.id
                       AND do2.sent_at < ?
                   )
               )""",
            (cutoff,)
        ).fetchall()

def get_available_donors(hall: str = None) -> list:
    """
    Return donors with availability set.
    Real-world: parse availability text or use a structured availability table.
    For prototype, returns all donors (matching is done on free-text availability).
    """
    with get_conn() as conn:
        return conn.execute(
            """SELECT phone, availability FROM users
               WHERE role='donor' AND availability IS NOT NULL"""
        ).fetchall()
