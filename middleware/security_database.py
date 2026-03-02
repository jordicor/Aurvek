"""
Security database connection helpers for db/security.db.

Simplified async connection manager for the IP Reputation system.
Separate from the main database.py to keep security data isolated.
"""

import os
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite

SECURITY_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "db", "security.db"
)

_wal_initialized = False


def ensure_security_wal_mode():
    """Set WAL journal mode on security.db (sync, called once at startup)."""
    global _wal_initialized
    if _wal_initialized:
        return
    if not os.path.exists(SECURITY_DB_PATH):
        return
    conn = sqlite3.connect(SECURITY_DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()
    _wal_initialized = True


@asynccontextmanager
async def get_security_db():
    """Async context manager for security.db connections."""
    conn = await aiosqlite.connect(SECURITY_DB_PATH, timeout=5.0)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA temp_store=MEMORY")
    try:
        yield conn
    finally:
        await conn.close()
