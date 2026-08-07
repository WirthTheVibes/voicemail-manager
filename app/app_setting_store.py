"""
DB-backed key/value store for settings that used to live only in .env --
non-secret deployment config (PJSUA/SMTP/MS Auth connection details, Whisper
tuning) plus a handful of secrets (SIP AUTH_ID/PASSWORD, SMTP_PASSWORD)
encrypted at rest with Fernet, keyed by APP_SECRETS_KEY.

Deliberately self-contained (no `from . import app_db`, no `from . import
config` for the DB path) rather than routed through app_db.py: config.py
reads from here at import time, and config.py is imported by app_db.py
itself (app_db.get_conn() needs config.APP_DB_PATH) -- going through app_db
would be a circular import. This module re-reads its own env directly and
creates its own table on first use instead, so it has no dependency on
either module's import order.
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

_DB_PATH = os.environ.get("APP_DB_PATH", "/opt/vm-manager/vm_manager.db")
_SECRETS_KEY = os.environ.get("APP_SECRETS_KEY")
_fernet = Fernet(_SECRETS_KEY.encode()) if _SECRETS_KEY else None

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_setting (
    key TEXT PRIMARY KEY,
    value TEXT,
    encrypted INTEGER NOT NULL DEFAULT 0
)
"""


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_TABLE_SQL)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value, encrypted FROM app_setting WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    value = row["value"]
    if row["encrypted"]:
        if _fernet is None:
            raise RuntimeError(f"app_setting '{key}' is encrypted but APP_SECRETS_KEY is not set in .env")
        try:
            value = _fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            raise RuntimeError(f"app_setting '{key}' could not be decrypted -- APP_SECRETS_KEY may have changed")
    return value


def set(key: str, value, encrypted: bool = False):
    stored = value
    if encrypted and value is not None:
        if _fernet is None:
            raise RuntimeError("APP_SECRETS_KEY is not set in .env; cannot store encrypted settings")
        stored = _fernet.encrypt(str(value).encode()).decode()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO app_setting (key, value, encrypted) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, encrypted = excluded.encrypted",
            (key, stored, int(encrypted)),
        )


def seed_if_absent(key: str, value, encrypted: bool = False):
    """First-run migration helper: only writes if the key has never been set,
    so a later admin edit (or an unset .env var on a later boot) never gets
    clobbered back to whatever .env happened to hold at this boot."""
    with _conn() as conn:
        row = conn.execute("SELECT 1 FROM app_setting WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return
    set(key, value, encrypted=encrypted)
