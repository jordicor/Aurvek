"""
Shared fixtures for watchdog tests.

Provides an in-memory SQLite database with the required schema,
mock LLM responses, and helper utilities.
"""

import asyncio
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
import pytest_asyncio

# Tests must not inherit production cookie policy or attempt a real Redis
# backend merely because the developer's .env points at production-like hosts.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECURITY_REDIS_MODE", "off")
os.environ.setdefault("USE_EMAIL_SERVICE", "false")

# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Temporary database
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS PROMPTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    prompt TEXT,
    user_id INTEGER,
    watchdog_config TEXT DEFAULT NULL,
    is_paid INTEGER DEFAULT 0,
    markup_per_mtokens REAL DEFAULT 0,
    created_by_user_id INTEGER
);

CREATE TABLE IF NOT EXISTS LLM (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_key TEXT,
    provider_model_id TEXT,
    enabled INTEGER DEFAULT 1,
    context_window_tokens INTEGER DEFAULT 0,
    max_input_tokens INTEGER DEFAULT 0,
    max_output_tokens INTEGER DEFAULT 0,
    input_token_cost REAL DEFAULT 0,
    output_token_cost REAL DEFAULT 0,
    capabilities_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS USERS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    role_id INTEGER
);

CREATE TABLE IF NOT EXISTS CONVERSATIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    chat_name TEXT,
    role_id INTEGER,
    locked INTEGER DEFAULT 0,
    locked_reason TEXT,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS USER_DETAILS (
    user_id INTEGER PRIMARY KEY,
    balance REAL DEFAULT 1000,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    input_token_cost REAL DEFAULT 0,
    output_token_cost REAL DEFAULT 0,
    total_cost REAL DEFAULT 0,
    tokens_spent INTEGER DEFAULT 0,
    pending_earnings REAL DEFAULT 0,
    created_by INTEGER,
    referral_markup_per_mtokens REAL DEFAULT 0,
    billing_account_id INTEGER,
    billing_limit REAL,
    billing_limit_action TEXT DEFAULT 'block',
    billing_current_month_spent REAL DEFAULT 0,
    billing_month_reset_date TEXT,
    billing_auto_refill_amount REAL DEFAULT 10,
    billing_max_limit REAL,
    billing_auto_refill_count INTEGER DEFAULT 0,
    api_key_mode TEXT DEFAULT 'system_only',
    user_api_keys TEXT,
    allow_file_upload INTEGER DEFAULT 0,
    storage_quota_bytes INTEGER DEFAULT NULL
        CHECK(storage_quota_bytes IS NULL OR storage_quota_bytes >= 0)
);

CREATE TABLE IF NOT EXISTS SYSTEM_CONFIG (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS GENERATED_MEDIA_FILES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'video', 'pdf', 'mp3', 'wav')),
    rel_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_generated_media_user ON GENERATED_MEDIA_FILES(user_id);
CREATE INDEX IF NOT EXISTS idx_generated_media_conversation ON GENERATED_MEDIA_FILES(conversation_id);

CREATE TABLE IF NOT EXISTS BILLING_USAGE_RESERVATIONS (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    billing_account_id INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('ai', 'image', 'stt', 'video')),
    service_id INTEGER,
    usage_quantity REAL CHECK(usage_quantity IS NULL OR usage_quantity > 0),
    amount REAL NOT NULL CHECK(amount > 0),
    settled_amount REAL CHECK(settled_amount IS NULL OR settled_amount >= 0),
    accumulated_input_tokens INTEGER NOT NULL DEFAULT 0,
    accumulated_output_tokens INTEGER NOT NULL DEFAULT 0,
    accumulated_components TEXT NOT NULL DEFAULT '[]',
    billing_limit_delta REAL NOT NULL DEFAULT 0,
    billing_refill_count_delta INTEGER NOT NULL DEFAULT 0,
    billing_month TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'settled', 'refunded')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    provider_started_at TIMESTAMP,
    provider_succeeded_at TIMESTAMP,
    settled_at TIMESTAMP,
    refunded_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS USAGE_DAILY (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date DATE NOT NULL,
    type TEXT NOT NULL,
    operations INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    units REAL DEFAULT 0,
    total_cost REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, date, type)
);

CREATE TABLE IF NOT EXISTS MESSAGES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER,
    message TEXT,
    type TEXT NOT NULL,
    input_tokens_used INTEGER DEFAULT 0,
    output_tokens_used INTEGER DEFAULT 0,
    date TEXT,
    llm_id INTEGER DEFAULT NULL,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id)
);

CREATE TABLE IF NOT EXISTS WATCHDOG_EVENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    prompt_id INTEGER,
    user_message_id INTEGER,
    bot_message_id INTEGER,
    event_type TEXT NOT NULL CHECK(event_type IN ('drift','rabbit_hole','stuck','inconsistency','saturation','none','error','security','role_breach','role_breach_hard','role_breach_soft')),
    severity TEXT NOT NULL CHECK(severity IN ('info','nudge','redirect','alert')),
    analysis TEXT,
    hint TEXT,
    action_taken TEXT DEFAULT 'none' CHECK(action_taken IN ('hint_generated','none','error','blocked','force_locked','takeover','takeover_locked')),
    source TEXT DEFAULT 'post' CHECK(source IN ('pre','post')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id)
);

CREATE TABLE IF NOT EXISTS WATCHDOG_STATE (
    conversation_id INTEGER PRIMARY KEY,
    prompt_id INTEGER,
    pending_hint TEXT,
    hint_severity TEXT,
    last_evaluated_message_id INTEGER NOT NULL DEFAULT 0,
    consecutive_hint_count INTEGER NOT NULL DEFAULT 0,
    pending_hint_event_type TEXT DEFAULT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id)
);

CREATE INDEX IF NOT EXISTS idx_watchdog_events_conv_date ON WATCHDOG_EVENTS(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_watchdog_events_type_severity ON WATCHDOG_EVENTS(event_type, severity);
CREATE INDEX IF NOT EXISTS idx_watchdog_events_prompt ON WATCHDOG_EVENTS(prompt_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv_type_id ON MESSAGES(conversation_id, type, id);
"""


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    """Create a temporary SQLite database with the watchdog schema."""
    db_dir = tmp_path_factory.mktemp("watchdog_test")
    path = str(db_dir / "test_watchdog.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA_SQL)
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _clean_tables(db_path):
    """Wipe all data between tests (keeps schema)."""
    conn = sqlite3.connect(db_path)
    for table in (
        "USER_WELLBEING_EVENTS",
        "USER_ACTIVITY_SESSION_CONVERSATIONS",
        "USER_ACTIVITY_SESSIONS",
        "USER_WELLBEING_PREFERENCES",
        "MEMORY_PROVIDER_SYNC_STATE",
        "MEMORY_PROVIDER_SYNC_RUNS",
        "MEMORY_PROVIDER_CONVERSATION_LINKS",
        "MEMORY_PROVIDER_MESSAGE_LINKS",
        "MEMORY_USER_PREFERENCES",
        "WATCHDOG_STATE",
        "WATCHDOG_EVENTS",
        "FILE_LEGACY_CLEANUP_CANDIDATES",
        "FILE_ATTACHMENTS",
        "FILE_BLOB_VARIANTS",
        "FILE_BLOBS",
        "GENERATED_MEDIA_FILES",
        "MESSAGES",
        "CONVERSATIONS",
        "USERS",
        "PROMPTS",
        "LLM",
        "USER_DETAILS",
        "SYSTEM_CONFIG",
        "BILLING_USAGE_RESERVATIONS",
        "USAGE_DAILY",
    ):
        try:
            conn.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    try:
        import atagia_config
        from memory.config import invalidate_memory_config_cache

        atagia_config.invalidate_atagia_config_cache()
        invalidate_memory_config_cache()
    except Exception:
        pass


@pytest.fixture()
def mock_db(db_path):
    """Patch database.get_db_connection to use the temp DB."""

    @asynccontextmanager
    async def _get_test_conn(readonly=False):
        # Always use rwc for the test DB (no real multi-process concurrency)
        conn = await aiosqlite.connect(
            f"file:{db_path}?mode=rwc", uri=True, timeout=5.0
        )
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            await conn.close()

    patcher = patch("database.get_db_connection", _get_test_conn)
    # Also patch in the modules that import it directly. These modules do
    # `from database import get_db_connection`, which binds the symbol at import
    # time; patching only `database.get_db_connection` would not reach them once
    # they have been imported (which happens at collection time for any test that
    # imports the chat routes), so each must be patched explicitly.
    patcher2 = patch("tools.watchdog.get_db_connection", _get_test_conn)
    patcher3 = patch("common.get_db_connection", _get_test_conn)
    patcher4 = patch("wellbeing_service.get_db_connection", _get_test_conn)
    patcher5 = patch("chat.services.deletion.get_db_connection", _get_test_conn)
    patcher6 = patch(
        "billing.usage_reservations.get_db_connection",
        _get_test_conn,
    )
    patcher.start()
    patcher2.start()
    patcher3.start()
    patcher4.start()
    patcher5.start()
    patcher6.start()
    yield _get_test_conn
    patcher6.stop()
    patcher5.stop()
    patcher4.stop()
    patcher3.stop()
    patcher2.stop()
    patcher.stop()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def seed_llm(db_path, llm_id=1, machine="GPT", model="gpt-4o"):
    """Insert a test LLM row."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO LLM (id, machine, model, provider_key, provider_model_id) VALUES (?, ?, ?, ?, ?)",
        (llm_id, machine, model, machine.lower(), model),
    )
    conn.commit()
    conn.close()


def seed_prompt(db_path, prompt_id=1, name="Test Prompt", watchdog_config=None):
    """Insert a test prompt with optional watchdog config (as JSON string)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO PROMPTS (id, name, prompt, watchdog_config, is_paid, markup_per_mtokens, created_by_user_id) VALUES (?, ?, ?, ?, 0, 0, ?)",
        (prompt_id, name, "You are a helpful assistant.", watchdog_config, 1),
    )
    conn.commit()
    conn.close()


def seed_conversation(db_path, conv_id=1, user_id=1, role_id=1):
    """Insert a test conversation."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT OR IGNORE INTO USER_DETAILS
           (user_id, balance, api_key_mode)
           VALUES (?, 1000, 'system_only')""",
        (user_id,),
    )
    conn.execute(
        "INSERT INTO CONVERSATIONS (id, user_id, chat_name, role_id) VALUES (?, ?, ?, ?)",
        (conv_id, user_id, "Test Chat", role_id),
    )
    conn.commit()
    conn.close()


def seed_messages(db_path, conv_id=1, user_id=1, count=6):
    """Insert alternating user/bot message pairs. Returns list of (id, type) tuples."""
    conn = sqlite3.connect(db_path)
    ids = []
    for i in range(count):
        msg_type = "user" if i % 2 == 0 else "bot"
        content = f"Test message {i + 1} ({msg_type})"
        cursor = conn.execute(
            "INSERT INTO MESSAGES (conversation_id, user_id, message, type, date) VALUES (?, ?, ?, ?, datetime('now'))",
            (conv_id, user_id, content, msg_type),
        )
        ids.append((cursor.lastrowid, msg_type))
    conn.commit()
    conn.close()
    return ids


def get_watchdog_events(db_path, conv_id=None):
    """Read all watchdog events, optionally filtered by conversation_id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if conv_id is not None:
        rows = conn.execute(
            "SELECT * FROM WATCHDOG_EVENTS WHERE conversation_id = ? ORDER BY id", (conv_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM WATCHDOG_EVENTS ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_watchdog_state(db_path, conv_id):
    """Read the watchdog state for a conversation."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM WATCHDOG_STATE WHERE conversation_id = ?", (conv_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None
