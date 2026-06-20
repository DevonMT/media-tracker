import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "tracker.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('movie', 'show')),
                genre TEXT,
                year INTEGER,
                liked INTEGER NOT NULL DEFAULT 1,
                rating INTEGER,
                notes TEXT,
                date_added TEXT DEFAULT (date('now')),
                who TEXT DEFAULT 'both'
            );

            CREATE TABLE IF NOT EXISTS platforms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                monthly_cost REAL DEFAULT 0.0,
                can_rent INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS saved_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                year INTEGER,
                type TEXT,
                platform TEXT,
                overview TEXT,
                cast_list TEXT,
                vibe_match TEXT,
                reason TEXT,
                confidence INTEGER,
                sensitivity_flag INTEGER DEFAULT 0,
                sensitivity_note TEXT,
                status TEXT DEFAULT 'pending',
                date_saved TEXT DEFAULT (date('now'))
            );
        """)

        # Migrations for existing DBs
        for stmt in [
            "ALTER TABLE media ADD COLUMN rating INTEGER",
            "ALTER TABLE saved_recommendations ADD COLUMN cast_list TEXT",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass

        # Seed platforms
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
