"""Test-only SQLite backend that mimics db.py's interface.

The production db.py talks to the mini-PC Postgres warehouse and builds its DSN
from PGUSER at import time, so it can't be imported (let alone written to) during
local tests. Tests register THIS module as `sys.modules['db']` before importing
the app, giving `from db import get_conn, init_db` an isolated SQLite database
instead — no Postgres, no psycopg2, no touching Devon's real data.

The connection wrapper matches the sqlite3-style contract the app relies on:
    with get_conn() as conn:
        rows = conn.execute("... WHERE id = ?", (x,)).fetchall()
        r["column"]          # dict-style access (sqlite3.Row)
        dict(r)              # row -> dict
"""
import os
import sqlite3

# Tests point this at a fresh temp file per case via set_path(). A single shared
# path (not ":memory:") is required because each get_conn() opens a NEW
# connection, and separate ":memory:" connections would each get their own DB.
DB_PATH = os.environ.get("MOVIE_TEST_DB", "")


def set_path(path: str) -> None:
    global DB_PATH
    DB_PATH = path


class _Conn:
    def __init__(self):
        if not DB_PATH:
            raise RuntimeError("_sqlite_db.set_path() must be called before get_conn()")
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        return self.conn.execute(sql, tuple(params) if params else ())

    def executemany(self, sql, seq):
        return self.conn.executemany(sql, list(seq))

    def executescript(self, script):
        self.conn.executescript(script)

    def commit(self):
        self.conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.conn.close()
        return False


def get_conn():
    return _Conn()


# SQLite translation of db.init_db()'s Postgres DDL: SERIAL -> AUTOINCREMENT,
# to_char(current_date,...) -> date('now'), no schemas.
_DDL = """
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('movie','show')),
    genre       TEXT,
    year        INTEGER,
    liked       INTEGER NOT NULL DEFAULT 1,
    rating      INTEGER,
    notes       TEXT,
    date_added  TEXT DEFAULT (date('now')),
    who         TEXT DEFAULT 'both'
);
CREATE TABLE IF NOT EXISTS platforms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    active       INTEGER NOT NULL DEFAULT 1,
    monthly_cost REAL DEFAULT 0.0,
    can_rent     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS saved_recommendations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    year             INTEGER,
    type             TEXT,
    platform         TEXT,
    overview         TEXT,
    cast_list        TEXT,
    vibe_match       TEXT,
    reason           TEXT,
    confidence       INTEGER,
    sensitivity_flag INTEGER DEFAULT 0,
    sensitivity_note TEXT,
    status           TEXT DEFAULT 'pending',
    date_saved       TEXT DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS exclusions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    year        INTEGER,
    type        TEXT,
    reason      TEXT,
    date_added  TEXT DEFAULT (date('now'))
);
"""

# Mirrors the production seed so build_context() sees the same default platforms.
_DEFAULT_PLATFORMS = [
    ("Netflix", 1, 19.74, 0),
    ("Amazon Prime", 1, 8.99, 1),
    ("Apple TV+", 0, 9.99, 0),
    ("Hulu", 0, 7.99, 0),
    ("Disney+", 0, 13.99, 0),
    ("Digital Rental", 1, 0.0, 1),
]


def init_db():
    with get_conn() as conn:
        conn.executescript(_DDL)
        conn.executemany(
            "INSERT OR IGNORE INTO platforms (name, active, monthly_cost, can_rent) VALUES (?,?,?,?)",
            _DEFAULT_PLATFORMS,
        )
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('rent_budget', '5.00')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('sensitivities', '')")
