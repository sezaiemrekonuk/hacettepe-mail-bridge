"""
Persistent store for seen message IDs using SQLite.
"""
import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/app/data/seen.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_messages "
        "(message_id TEXT PRIMARY KEY, seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    return conn


def is_seen(message_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row is not None


def mark_seen(message_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_messages (message_id) VALUES (?)",
            (message_id,),
        )
        conn.commit()
