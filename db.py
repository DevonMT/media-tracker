"""
Database layer for the movie tracker.

Migrated from SQLite to the mini-PC warehouse (Postgres). A thin adapter keeps
the app's original sqlite3-style interface working unchanged:
    with get_conn() as conn:
        rows = conn.execute("SELECT ... WHERE id = ?", (x,)).fetchall()
        r["column"]  # dict-style row access

Connection comes from env (set by docker-compose): PGHOST/PGPORT/PGDATABASE/
PGUSER/PGPASSWORD, plus MOVIE_SCHEMA (default 'movie'). The app's tables live in
that schema via search_path, so the SQL stays schema-unqualified.
"""
import os
import re

import psycopg2
from psycopg2.extras import RealDictCursor

SCHEMA = os.environ.get("MOVIE_SCHEMA", "movie")

DSN = os.environ.get("WAREHOUSE_DSN") or (
    f"host={os.environ.get('PGHOST', 'postgres')} "
    f"port={os.environ.get('PGPORT', '5432')} "
    f"dbname={os.environ.get('PGDATABASE', 'warehouse')} "
    f"user={os.environ['PGUSER']} "
    f"password={os.environ['PGPASSWORD']}"
)


def _translate(sql: str) -> str:
    """SQLite SQL -> Postgres: positional params and INSERT OR IGNORE."""
    sql = sql.replace("?", "%s")
    if re.search(r"INSERT\s+OR\s+IGNORE", sql, re.IGNORECASE):
        sql = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT", sql, flags=re.IGNORECASE)
        if "ON CONFLICT" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return sql


class _Conn:
    """sqlite3-compatible wrapper around a psycopg2 connection."""

    def __init__(self):
        self.conn = psycopg2.connect(DSN)
        self.conn.autocommit = False
        with self.conn.cursor() as c:
            c.execute(f"SET search_path TO {SCHEMA}, public")

    def execute(self, sql, params=()):
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(_translate(sql), tuple(params) if params else None)
        return cur  # supports .fetchone()/.fetchall()/iteration -> dict rows

    def executemany(self, sql, seq):
        cur = self.conn.cursor()
        cur.executemany(_translate(sql), list(seq))
        return cur

    def executescript(self, script):
        with self.conn.cursor() as c:
            c.execute(script)

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


def init_db():
    ddl = f"""
        CREATE SCHEMA IF NOT EXISTS {SCHEMA};

        CREATE TABLE IF NOT EXISTS {SCHEMA}.media (
            id          SERIAL PRIMARY KEY,
            title       TEXT NOT NULL,
            type        TEXT NOT NULL CHECK (type IN ('movie', 'show')),
            genre       TEXT,
            year        INTEGER,
            liked       INTEGER NOT NULL DEFAULT 1,
            rating      INTEGER,
            notes       TEXT,
            date_added  TEXT DEFAULT to_char(current_date, 'YYYY-MM-DD'),
            who         TEXT DEFAULT 'both'
        );

        CREATE TABLE IF NOT EXISTS {SCHEMA}.platforms (
            id           SERIAL PRIMARY KEY,
            name         TEXT NOT NULL UNIQUE,
            active       INTEGER NOT NULL DEFAULT 1,
            monthly_cost REAL DEFAULT 0.0,
            can_rent     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS {SCHEMA}.settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS {SCHEMA}.saved_recommendations (
            id               SERIAL PRIMARY KEY,
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
            date_saved       TEXT DEFAULT to_char(current_date, 'YYYY-MM-DD')
        );

        CREATE TABLE IF NOT EXISTS {SCHEMA}.exclusions (
            id          SERIAL PRIMARY KEY,
            title       TEXT NOT NULL,
            year        INTEGER,
            type        TEXT,
            reason      TEXT,
            date_added  TEXT DEFAULT to_char(current_date, 'YYYY-MM-DD')
        );
    """
    with get_conn() as conn:
        conn.executescript(ddl)
        defaults = [
            ("Netflix", 1, 19.74, 0),
            ("Amazon Prime", 1, 8.99, 1),
            ("Apple TV+", 0, 9.99, 0),
            ("Hulu", 0, 7.99, 0),
            ("Disney+", 0, 13.99, 0),
            ("Digital Rental", 1, 0.0, 1),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO platforms (name, active, monthly_cost, can_rent) VALUES (?,?,?,?)",
            defaults,
        )
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('rent_budget', '5.00')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('sensitivities', '')")
