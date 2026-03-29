"""
Database helpers for the multi-user web application.
Manages applications, seen messages, and password encryption.
"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/app/data/hub.db")

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    hu_email         TEXT NOT NULL UNIQUE,
    hu_password_enc  TEXT NOT NULL,
    gmail_target     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    note             TEXT DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seen_messages (
    app_id     INTEGER NOT NULL,
    message_id TEXT    NOT NULL,
    seen_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (app_id, message_id)
);
"""

# --------------------------------------------------------------------------- #
# Connection & init
# --------------------------------------------------------------------------- #


def init_db() -> None:
    """Create tables if they don't exist."""
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


def get_db() -> sqlite3.Connection:
    """Return a connection with Row factory enabled."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------- #
# Application CRUD
# --------------------------------------------------------------------------- #


def list_applications(status: str | None = None) -> list[sqlite3.Row]:
    with get_db() as conn:
        if status is None:
            rows = conn.execute(
                "SELECT * FROM applications ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
    return rows


def get_application(app_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()


def create_application(
    hu_email: str, hu_password_enc: str, gmail_target: str
) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO applications (hu_email, hu_password_enc, gmail_target) "
            "VALUES (?, ?, ?)",
            (hu_email, hu_password_enc, gmail_target),
        )
        conn.commit()
        return cur.lastrowid


def update_status(app_id: int, status: str, note: str = "") -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE applications SET status = ?, note = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, note, app_id),
        )
        conn.commit()


def delete_application(app_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM seen_messages WHERE app_id = ?", (app_id,))
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        conn.commit()


# --------------------------------------------------------------------------- #
# Seen messages
# --------------------------------------------------------------------------- #


def is_seen(app_id: int, message_id: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_messages WHERE app_id = ? AND message_id = ?",
            (app_id, message_id),
        ).fetchone()
    return row is not None


def mark_seen(app_id: int, message_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_messages (app_id, message_id) VALUES (?, ?)",
            (app_id, message_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Password encryption (Fernet)
# --------------------------------------------------------------------------- #


def get_fernet():
    """Return a Fernet instance, generating a key if FERNET_KEY is not set."""
    from cryptography.fernet import Fernet

    key = os.environ.get("FERNET_KEY", "")
    if not key:
        key = Fernet.generate_key().decode()
        logger.warning(
            "FERNET_KEY is not set in environment. A temporary key has been generated. "
            "Encrypted passwords will NOT survive a restart. "
            "Set FERNET_KEY=%s in your .env file to persist sessions.",
            key,
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_password(plain: str) -> str:
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt_password(enc: str) -> str:
    return get_fernet().decrypt(enc.encode()).decode()
