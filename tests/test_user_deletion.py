"""Coverage for the coordinated user-deletion service and message-deletion helper.

Focus areas:
- admin accounts and cross-user financial entanglement are blocked (no mutation),
  while single-party wallet rows (TRANSACTIONS only) no longer block;
- deleting a user pseudonymizes their TRANSACTIONS into FINANCIAL_ARCHIVE (keyed,
  deterministic, with no plaintext identity-bearing fields) and deletes the
  originals;
- a clean user with Atagia links triggers the remote erase BEFORE any local
  deletion, and leaves no messages or provider links behind;
- captured ElevenLabs conversations are purged remotely post-commit (best effort:
  a purge failure yields completed_with_cleanup_errors, never a silent drop);
- an unreachable Atagia aborts with no local mutation;
- a second deletion of an already-gone user is a clean no-op;
- the shared message-deletion helper clears provider links and watchdog rows in
  FK-safe order (the bug that made rollback raise FOREIGN KEY constraint failed).
"""

import builtins
import json
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest

import user_deletion
from chat.services.privacy import delete_message_rows

_SCHEMA = """
CREATE TABLE USER_ROLES (id INTEGER PRIMARY KEY, role_name TEXT);
CREATE TABLE USERS (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    role_id INTEGER,
    email TEXT
);
CREATE TABLE USER_DETAILS (
    user_id INTEGER PRIMARY KEY,
    billing_account_id INTEGER,
    created_by INTEGER,
    current_alter_ego_id INTEGER,
    current_prompt_id INTEGER,
    subscription_auth TEXT,
    FOREIGN KEY(user_id) REFERENCES USERS(id)
);
CREATE TABLE CONVERSATIONS (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    role_id INTEGER,
    active_extension_id INTEGER,
    elevenlabs_session_id TEXT,
    FOREIGN KEY(user_id) REFERENCES USERS(id)
);
CREATE TABLE MESSAGES (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER,
    user_id INTEGER,
    message TEXT,
    type TEXT,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id)
);
CREATE TABLE MEMORY_PROVIDER_MESSAGE_LINKS (
    message_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_message_id TEXT NOT NULL DEFAULT '',
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY(message_id, provider),
    FOREIGN KEY(message_id) REFERENCES MESSAGES(id)
);
CREATE TABLE MEMORY_PROVIDER_CONVERSATION_LINKS (
    provider TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY(provider, conversation_id)
);
CREATE TABLE MEMORY_PROVIDER_SYNC_STATE (
    provider TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY(provider, conversation_id)
);
CREATE TABLE MEMORY_USER_PREFERENCES (
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    PRIMARY KEY(user_id, provider)
);
CREATE TABLE WATCHDOG_EVENTS (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER,
    prompt_id INTEGER,
    user_message_id INTEGER,
    bot_message_id INTEGER
);
CREATE TABLE WATCHDOG_STATE (
    conversation_id INTEGER PRIMARY KEY,
    prompt_id INTEGER,
    last_evaluated_message_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE ELEVENLABS_CALL_SESSIONS (
    session_id TEXT PRIMARY KEY,
    conversation_id INTEGER,
    user_id INTEGER,
    agent_id TEXT,
    status TEXT
);
CREATE TABLE ADMIN_AUDIT_LOG (
    id INTEGER PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    action_type TEXT,
    target_user_id INTEGER,
    FOREIGN KEY(admin_id) REFERENCES USERS(id),
    FOREIGN KEY(target_user_id) REFERENCES USERS(id)
);
CREATE TABLE PROMPTS (id INTEGER PRIMARY KEY, created_by_user_id INTEGER);
CREATE TABLE PACKS (id INTEGER PRIMARY KEY, created_by_user_id INTEGER);
CREATE TABLE TRANSACTIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount DECIMAL NOT NULL,
    balance_before DECIMAL NOT NULL,
    balance_after DECIMAL NOT NULL,
    description TEXT,
    reference_id TEXT,
    discount_code TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES USERS(id)
);
CREATE TABLE FINANCIAL_ARCHIVE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_user_ref TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_row_id INTEGER NOT NULL,
    amount DECIMAL,
    currency TEXT,
    payment_reference TEXT,
    occurred_at TIMESTAMP,
    row_snapshot TEXT NOT NULL,
    archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE PROMPT_PURCHASES (id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_user_id INTEGER, prompt_id INTEGER);
CREATE TABLE PACK_PURCHASES (id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_user_id INTEGER, pack_id INTEGER);
CREATE TABLE CREATOR_EARNINGS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER,
    consumer_id INTEGER,
    referral_id INTEGER,
    prompt_id INTEGER
);
CREATE TABLE ENTITLEMENTS (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, asset_type TEXT, asset_id INTEGER);
CREATE TABLE PROMPT_PERMISSIONS (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, prompt_id INTEGER);
CREATE TABLE USER_CREATOR_RELATIONSHIPS (
    user_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL,
    PRIMARY KEY(user_id, creator_id, relationship_type)
);
CREATE TABLE MAGIC_LINKS (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER);
CREATE TABLE CHAT_FOLDERS (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER);
"""


@asynccontextmanager
async def _connect(db_path):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        await conn.close()


class PurgeSpy:
    """Stand-in for integrations.elevenlabs.service.delete_remote_conversation.

    Records every remote conversation id it is asked to purge and, for ids added
    to ``fail_ids``, raises to simulate a third-party outage.
    """

    def __init__(self):
        self.calls = []
        self.fail_ids = set()

    async def __call__(self, conversation_id):
        self.calls.append(conversation_id)
        if conversation_id in self.fail_ids:
            raise RuntimeError(f"remote purge failed for {conversation_id}")


class FakeBridge:
    """Minimal stand-in for the Atagia bridge used by the deletion service."""

    def __init__(self, db_path, *, enabled=True, fail=False):
        self._db_path = db_path
        self.enabled = enabled
        self._fail = fail
        self.erase_calls = []
        self.user_present_at_erase = None

    async def is_enabled(self):
        return self.enabled

    async def erase_user(self, user_id):
        self.erase_calls.append(user_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT 1 FROM USERS WHERE id = ?", (user_id,)).fetchone()
        self.user_present_at_erase = row is not None
        if self._fail:
            raise RuntimeError("atagia unreachable")
        return {"user_id": f"aurvek:user:{user_id}", "already_erased": False}


def _seed_base(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO USER_ROLES (id, role_name) VALUES (?, ?)",
            [(1, "admin"), (2, "user"), (3, "customer")],
        )
        conn.commit()
    finally:
        conn.close()


def _add_user(db_path, user_id, username, role_id=2):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO USERS (id, username, role_id, email) VALUES (?, ?, ?, ?)",
            (user_id, username, role_id, f"{username}@example.test"),
        )
        conn.execute("INSERT INTO USER_DETAILS (user_id) VALUES (?)", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _add_conversation_with_atagia(db_path, *, conv_id, user_id, message_ids):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, elevenlabs_session_id) VALUES (?, ?, ?)",
            (conv_id, user_id, f"eleven-{conv_id}"),
        )
        for mid in message_ids:
            conn.execute(
                "INSERT INTO MESSAGES (id, conversation_id, user_id, message, type) VALUES (?, ?, ?, ?, ?)",
                (mid, conv_id, user_id, "hi", "user"),
            )
            conn.execute(
                "INSERT INTO MEMORY_PROVIDER_MESSAGE_LINKS "
                "(message_id, provider, conversation_id, user_id, role) VALUES (?, 'atagia', ?, ?, 'user')",
                (mid, conv_id, user_id),
            )
        conn.execute(
            "INSERT INTO MEMORY_PROVIDER_CONVERSATION_LINKS (provider, conversation_id, user_id) "
            "VALUES ('atagia', ?, ?)",
            (conv_id, user_id),
        )
        conn.execute(
            "INSERT INTO MEMORY_PROVIDER_SYNC_STATE (provider, conversation_id, last_message_id) "
            "VALUES ('atagia', ?, ?)",
            (conv_id, max(message_ids)),
        )
        conn.execute(
            "INSERT INTO ELEVENLABS_CALL_SESSIONS (session_id, conversation_id, user_id, agent_id, status) "
            "VALUES (?, ?, ?, 'agent', 'completed')",
            (f"remote-{conv_id}", conv_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def _add_transactions(db_path, user_id, rows):
    """Insert TRANSACTIONS rows for a user. ``rows`` is a list of dicts."""
    conn = sqlite3.connect(db_path)
    try:
        for row in rows:
            conn.execute(
                "INSERT INTO TRANSACTIONS "
                "(user_id, type, amount, balance_before, balance_after, "
                "description, reference_id, discount_code, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    row["type"],
                    row["amount"],
                    row["balance_before"],
                    row["balance_after"],
                    row.get("description"),
                    row.get("reference_id"),
                    row.get("discount_code"),
                    row.get("created_at", "2026-01-01 00:00:00"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _count(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def purge_spy(monkeypatch):
    """Default ElevenLabs remote-purge stub, installed for every deletion test.

    Prevents any test from performing a real HTTP purge and lets tests that care
    inspect (or configure failures on) the same recorded instance.
    """
    spy = PurgeSpy()
    monkeypatch.setattr(user_deletion, "delete_remote_conversation", spy)
    return spy


@pytest.fixture
def deletion_db(tmp_path, monkeypatch, purge_spy):
    db_path = str(tmp_path / "deletion.db")
    _seed_base(db_path)

    @asynccontextmanager
    async def _get_conn(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            await conn.close()

    async def _noop(*_args, **_kwargs):
        return None

    async def _no_stale_reservations():
        return 0

    monkeypatch.setattr(user_deletion, "get_db_connection", _get_conn)
    # The private build routes GPTSub lifecycle helpers to this hermetic DB too.
    # The public mirror deliberately omits the package while retaining the nullable
    # schema column, so absence of that exact top-level package is an expected no-op.
    try:
        from subscription_auth import token_store as subscription_token_store
    except ModuleNotFoundError as exc:
        if exc.name != "subscription_auth":
            raise
    else:
        monkeypatch.setattr(subscription_token_store, "get_db_connection", _get_conn)
    monkeypatch.setattr(user_deletion, "add_revoked_user", _noop)
    monkeypatch.setattr(user_deletion, "prune_unreferenced_blobs", _noop)
    # Stale-hold reconciliation has its own suite and its own connection source;
    # keep these tests hermetic to the tmp deletion DB.
    monkeypatch.setattr(
        user_deletion, "reconcile_stale_usage_reservations", _no_stale_reservations
    )
    monkeypatch.setattr(
        user_deletion,
        "get_user_directory",
        lambda username: str(tmp_path / "nonexistent" / username),
    )
    return db_path


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_target_blocked(deletion_db):
    _add_user(deletion_db, 1, "admin", role_id=1)
    bridge = FakeBridge(deletion_db, enabled=False)

    result = await user_deletion.delete_user_account(user_id=1, bridge=bridge)

    assert result.status == "blocked"
    assert "admin_account" in result.reason_codes
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 1") == 1
    assert bridge.erase_calls == []


@pytest.mark.asyncio
async def test_admin_actor_blocked_by_audit_log(deletion_db):
    _add_user(deletion_db, 50, "ops", role_id=2)
    _add_user(deletion_db, 51, "victim", role_id=2)
    conn = sqlite3.connect(deletion_db)
    conn.execute(
        "INSERT INTO ADMIN_AUDIT_LOG (id, admin_id, action_type, target_user_id) VALUES (1, 50, 'view', 51)"
    )
    conn.commit()
    conn.close()

    result = await user_deletion.delete_user_account(user_id=50, bridge=FakeBridge(deletion_db, enabled=False))

    assert result.status == "blocked"
    assert "admin_account" in result.reason_codes
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 50") == 1


@pytest.mark.asyncio
async def test_creator_earnings_entanglement_blocked(deletion_db):
    _add_user(deletion_db, 60, "creator", role_id=2)
    conn = sqlite3.connect(deletion_db)
    conn.execute(
        "INSERT INTO CREATOR_EARNINGS (creator_id, consumer_id, referral_id, prompt_id) VALUES (60, 99, NULL, 5)"
    )
    conn.commit()
    conn.close()

    result = await user_deletion.delete_user_account(user_id=60, bridge=FakeBridge(deletion_db, enabled=False))

    assert result.status == "blocked"
    assert "financial_entanglement" in result.reason_codes
    assert any(block["table"] == "CREATOR_EARNINGS" for block in result.blocking_tables)
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 60") == 1


@pytest.mark.asyncio
async def test_transactions_only_no_longer_blocks(deletion_db):
    # Single-party wallet rows must NOT block: they are archived, not reviewed.
    _add_user(deletion_db, 61, "walletonly", role_id=2)
    _add_transactions(
        deletion_db,
        61,
        [
            {
                "type": "credit",
                "amount": 10.0,
                "balance_before": 0.0,
                "balance_after": 10.0,
                "description": "top up",
                "reference_id": "ref-1",
            }
        ],
    )
    bridge = FakeBridge(deletion_db, enabled=False)

    result = await user_deletion.delete_user_account(user_id=61, dry_run=True, bridge=bridge)

    assert result.status == "dry_run"
    assert "financial_entanglement" not in result.reason_codes
    assert not any(block["table"] == "TRANSACTIONS" for block in result.blocking_tables)
    assert result.archived_transaction_count == 1


@pytest.mark.asyncio
async def test_prompt_purchase_as_buyer_blocks(deletion_db):
    _add_user(deletion_db, 62, "buyer", role_id=2)
    conn = sqlite3.connect(deletion_db)
    conn.execute("INSERT INTO PROMPT_PURCHASES (buyer_user_id, prompt_id) VALUES (62, 5)")
    conn.commit()
    conn.close()

    result = await user_deletion.delete_user_account(
        user_id=62, bridge=FakeBridge(deletion_db, enabled=False)
    )

    assert result.status == "blocked"
    assert "financial_entanglement" in result.reason_codes
    assert any(block["table"] == "PROMPT_PURCHASES" for block in result.blocking_tables)
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 62") == 1


@pytest.mark.asyncio
async def test_prompt_purchase_as_owner_blocks(deletion_db):
    # Another user bought THIS user's prompt: two-party cross-user entanglement.
    _add_user(deletion_db, 63, "seller", role_id=2)
    conn = sqlite3.connect(deletion_db)
    conn.execute("INSERT INTO PROMPTS (id, created_by_user_id) VALUES (77, 63)")
    conn.execute("INSERT INTO PROMPT_PURCHASES (buyer_user_id, prompt_id) VALUES (999, 77)")
    conn.commit()
    conn.close()

    result = await user_deletion.delete_user_account(
        user_id=63, bridge=FakeBridge(deletion_db, enabled=False)
    )

    assert result.status == "blocked"
    assert "financial_entanglement" in result.reason_codes
    assert any(block["table"] == "PROMPT_PURCHASES" for block in result.blocking_tables)


@pytest.mark.asyncio
async def test_pack_purchase_as_buyer_blocks(deletion_db):
    _add_user(deletion_db, 64, "packbuyer", role_id=2)
    conn = sqlite3.connect(deletion_db)
    conn.execute("INSERT INTO PACK_PURCHASES (buyer_user_id, pack_id) VALUES (64, 3)")
    conn.commit()
    conn.close()

    result = await user_deletion.delete_user_account(
        user_id=64, bridge=FakeBridge(deletion_db, enabled=False)
    )

    assert result.status == "blocked"
    assert "financial_entanglement" in result.reason_codes
    assert any(block["table"] == "PACK_PURCHASES" for block in result.blocking_tables)


@pytest.mark.asyncio
async def test_atagia_presence_without_bridge_blocked(deletion_db):
    _add_user(deletion_db, 70, "hasmemory", role_id=2)
    _add_conversation_with_atagia(deletion_db, conv_id=700, user_id=70, message_ids=[7001, 7002])

    result = await user_deletion.delete_user_account(
        user_id=70, bridge=FakeBridge(deletion_db, enabled=False)
    )

    assert result.status == "blocked"
    assert "atagia_unpurgeable" in result.reason_codes
    assert result.atagia_link_count > 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 70") == 1


# --------------------------------------------------------------------------- #
# Happy path and idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_clean_user_with_atagia_erased_then_deleted(deletion_db, purge_spy):
    _add_user(deletion_db, 80, "alice", role_id=2)
    _add_conversation_with_atagia(deletion_db, conv_id=800, user_id=80, message_ids=[8001, 8002])
    bridge = FakeBridge(deletion_db, enabled=True)

    result = await user_deletion.delete_user_account(user_id=80, bridge=bridge)

    assert result.status == "completed", result.summary
    # Remote erase happened, and it ran while the account still existed locally.
    assert bridge.erase_calls == [80]
    assert bridge.user_present_at_erase is True
    assert result.atagia_report is not None
    # Local rows are gone.
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 80") == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM CONVERSATIONS WHERE user_id = 80") == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM MESSAGES WHERE conversation_id = 800") == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE user_id = 80") == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM MEMORY_PROVIDER_CONVERSATION_LINKS WHERE user_id = 80") == 0
    # ElevenLabs remote conversations were captured and purged post-commit.
    assert "remote-800" in result.elevenlabs_session_ids
    assert "eleven-800" in result.elevenlabs_session_ids
    assert set(purge_spy.calls) == set(result.elevenlabs_session_ids)
    assert result.cleanup_errors == []


@pytest.mark.asyncio
async def test_atagia_unreachable_aborts_without_local_mutation(deletion_db):
    _add_user(deletion_db, 90, "bob", role_id=2)
    _add_conversation_with_atagia(deletion_db, conv_id=900, user_id=90, message_ids=[9001])
    bridge = FakeBridge(deletion_db, enabled=True, fail=True)

    result = await user_deletion.delete_user_account(user_id=90, bridge=bridge)

    assert result.status == "atagia_unreachable"
    assert bridge.erase_calls == [90]
    # Nothing was deleted, so a retry is still possible.
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 90") == 1
    assert _count(deletion_db, "SELECT COUNT(*) FROM MESSAGES WHERE conversation_id = 900") == 1
    assert _count(deletion_db, "SELECT COUNT(*) FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE user_id = 90") == 1


@pytest.mark.asyncio
async def test_second_deletion_is_clean_not_found(deletion_db):
    _add_user(deletion_db, 81, "carol", role_id=2)
    bridge = FakeBridge(deletion_db, enabled=True)

    first = await user_deletion.delete_user_account(user_id=81, bridge=bridge)
    assert first.status == "completed"

    second = await user_deletion.delete_user_account(user_id=81, bridge=bridge)
    assert second.status == "not_found"
    assert "not_found" in second.reason_codes


@pytest.mark.asyncio
async def test_dry_run_reports_without_mutation(deletion_db):
    _add_user(deletion_db, 82, "dave", role_id=2)
    _add_conversation_with_atagia(deletion_db, conv_id=820, user_id=82, message_ids=[8201])
    bridge = FakeBridge(deletion_db, enabled=True)

    result = await user_deletion.delete_user_account(user_id=82, dry_run=True, bridge=bridge)

    assert result.status == "dry_run"
    assert bridge.erase_calls == []
    assert result.table_counts.get("messages") == 1
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 82") == 1


# --------------------------------------------------------------------------- #
# Pseudonymized financial archive
# --------------------------------------------------------------------------- #


def test_deleted_user_ref_is_deterministic_and_keyed():
    # Same user -> same ref (rows group together); different users -> different ref.
    assert user_deletion._deleted_user_ref(500) == user_deletion._deleted_user_ref(500)
    assert user_deletion._deleted_user_ref(500) != user_deletion._deleted_user_ref(501)


def test_archive_field_fingerprint_has_stable_versioned_domain(monkeypatch):
    monkeypatch.setattr(user_deletion, "PEPPER", "unit-test-pepper")

    reference = user_deletion._archive_field_fingerprint(
        "transaction-reference", "discount_WELCOME_user_84"
    )

    # Independent regression vector: changing the key derivation, framing, domain,
    # version, or digest encoding must be an intentional archive-format decision.
    assert reference == (
        "hmac-sha256:fa-v1:"
        "77c1f28cde5c3fdab6856a0f4bf9fd4d31d3e213bf2947d205d9d40755e87ce1"
    )
    assert reference != user_deletion._archive_field_fingerprint(
        "transaction-discount-code", "discount_WELCOME_user_84"
    )
    assert user_deletion._archive_field_fingerprint("transaction-reference", None) is None
    assert user_deletion._archive_field_fingerprint(
        "transaction-reference", ""
    ) is not None
    assert (
        user_deletion._archive_field_fingerprint(
            "transaction-reference", reference, preserve_existing=True
        )
        == reference
    )


@pytest.mark.asyncio
async def test_transactions_archived_on_deletion(deletion_db):
    _add_user(deletion_db, 84, "frank", role_id=2)
    _add_transactions(
        deletion_db,
        84,
        [
            {
                "type": "credit",
                "amount": 25.5,
                "balance_before": 0.0,
                "balance_after": 25.5,
                "description": "Initial credit for user 84",
                "reference_id": "discount_WELCOME_user_84",
                "discount_code": "WELCOME",
            },
            {
                "type": "debit",
                "amount": 5.0,
                "balance_before": 25.5,
                "balance_after": 20.5,
                "description": "spend",
                "reference_id": None,
            },
        ],
    )
    bridge = FakeBridge(deletion_db, enabled=False)

    result = await user_deletion.delete_user_account(user_id=84, bridge=bridge)

    assert result.status == "completed", result.summary
    assert result.archived_transaction_count == 2
    # Originals gone.
    assert _count(deletion_db, "SELECT COUNT(*) FROM TRANSACTIONS WHERE user_id = 84") == 0
    # Archived under the deterministic, keyed pseudonym.
    expected_ref = user_deletion._deleted_user_ref(84)
    assert (
        _count(
            deletion_db,
            "SELECT COUNT(*) FROM FINANCIAL_ARCHIVE WHERE deleted_user_ref = ?",
            (expected_ref,),
        )
        == 2
    )
    assert (
        _count(
            deletion_db,
            "SELECT COUNT(*) FROM FINANCIAL_ARCHIVE WHERE source_table = 'TRANSACTIONS'",
        )
        == 2
    )

    conn = sqlite3.connect(deletion_db)
    conn.row_factory = sqlite3.Row
    archived = conn.execute(
        "SELECT amount, currency, payment_reference, row_snapshot "
        "FROM FINANCIAL_ARCHIVE WHERE deleted_user_ref = ? ORDER BY source_row_id",
        (expected_ref,),
    ).fetchall()
    conn.close()

    first = dict(archived[0])
    # TRANSACTIONS has no currency column; mapping leaves it NULL.
    assert first["currency"] is None
    expected_reference_fingerprint = user_deletion._archive_field_fingerprint(
        "transaction-reference", "discount_WELCOME_user_84"
    )
    assert first["payment_reference"] == expected_reference_fingerprint
    assert first["payment_reference"].startswith("hmac-sha256:")

    # Pseudonymization: free-form source fields never survive in plaintext.
    assert "discount_WELCOME_user_84" not in first["row_snapshot"]
    assert "Initial credit for user 84" not in first["row_snapshot"]
    assert "WELCOME" not in first["row_snapshot"]
    snapshot = json.loads(first["row_snapshot"])
    assert set(snapshot) == {
        "archive_format_version",
        "type",
        "amount",
        "balance_before",
        "balance_after",
        "description_fingerprint",
        "reference_id_fingerprint",
        "discount_code_fingerprint",
        "created_at",
    }
    assert snapshot["archive_format_version"] == 2
    assert snapshot["type"] == "credit"
    assert snapshot["reference_id_fingerprint"] == expected_reference_fingerprint
    expected_description_fingerprint = user_deletion._archive_field_fingerprint(
        "transaction-description", "Initial credit for user 84"
    )
    expected_discount_fingerprint = user_deletion._archive_field_fingerprint(
        "transaction-discount-code", "WELCOME"
    )
    assert snapshot["description_fingerprint"] == expected_description_fingerprint
    assert snapshot["discount_code_fingerprint"] == expected_discount_fingerprint

    second = dict(archived[1])
    second_snapshot = json.loads(second["row_snapshot"])
    assert set(second_snapshot) == set(snapshot)
    assert second["payment_reference"] is None
    assert second_snapshot["reference_id_fingerprint"] is None
    assert second_snapshot["discount_code_fingerprint"] is None
    assert second_snapshot["description_fingerprint"] is not None


@pytest.mark.asyncio
async def test_missing_pepper_fails_before_external_or_local_erasure(
    deletion_db, monkeypatch
):
    _add_user(deletion_db, 87, "pepperless", role_id=2)
    _add_transactions(
        deletion_db,
        87,
        [
            {
                "type": "credit",
                "amount": 10.0,
                "balance_before": 0.0,
                "balance_after": 10.0,
                "reference_id": "identity-bearing-reference",
            }
        ],
    )
    bridge = FakeBridge(deletion_db, enabled=True)
    monkeypatch.setattr(user_deletion, "PEPPER", "")

    with pytest.raises(RuntimeError, match="PEPPER is required"):
        await user_deletion.delete_user_account(user_id=87, bridge=bridge)

    assert bridge.erase_calls == []
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 87") == 1
    assert _count(deletion_db, "SELECT COUNT(*) FROM TRANSACTIONS WHERE user_id = 87") == 1
    assert _count(deletion_db, "SELECT COUNT(*) FROM FINANCIAL_ARCHIVE") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "deleted_user_id",
        "surviving_user_id",
        "deleted_type",
        "surviving_type",
        "safe_description",
    ),
    [
        (
            84,
            8,
            "balance_transfer_in",
            "balance_transfer_out",
            "Balance transfer sent",
        ),
        (
            8,
            84,
            "balance_transfer_out",
            "balance_transfer_in",
            "Balance transfer received",
        ),
    ],
)
async def test_deletion_redacts_exact_surviving_balance_transfer(
    deletion_db,
    deleted_user_id,
    surviving_user_id,
    deleted_type,
    surviving_type,
    safe_description,
):
    _add_user(deletion_db, deleted_user_id, f"deleted-{deleted_user_id}", role_id=2)
    _add_user(deletion_db, surviving_user_id, f"survivor-{surviving_user_id}", role_id=2)
    shared_reference = "initial_balance_84_deadbeef"
    _add_transactions(
        deletion_db,
        deleted_user_id,
        [
            {
                "type": deleted_type,
                "amount": 12.0,
                "balance_before": 0.0,
                "balance_after": 12.0,
                "description": f"Transfer involving user {surviving_user_id}",
                "reference_id": shared_reference,
            }
        ],
    )
    _add_transactions(
        deletion_db,
        surviving_user_id,
        [
            {
                "type": surviving_type,
                "amount": 12.0,
                "balance_before": 20.0,
                "balance_after": 8.0,
                "description": f"Transfer involving user {deleted_user_id}",
                "reference_id": shared_reference,
            },
            {
                "type": surviving_type,
                "amount": 1.0,
                "balance_before": 8.0,
                "balance_after": 7.0,
                "description": "Unrelated transfer to user 184",
                "reference_id": "initial_balance_184_decoy",
            },
        ],
    )

    dry_run = await user_deletion.delete_user_account(
        user_id=deleted_user_id,
        dry_run=True,
        bridge=FakeBridge(deletion_db, enabled=False),
    )
    assert dry_run.status == "dry_run"
    conn = sqlite3.connect(deletion_db)
    try:
        assert conn.execute(
            "SELECT reference_id FROM TRANSACTIONS "
            "WHERE user_id = ? AND amount = 12",
            (surviving_user_id,),
        ).fetchone()[0] == shared_reference
    finally:
        conn.close()

    result = await user_deletion.delete_user_account(
        user_id=deleted_user_id,
        bridge=FakeBridge(deletion_db, enabled=False),
    )

    assert result.status == "completed", result.summary
    expected_reference = user_deletion._archive_field_fingerprint(
        "transaction-reference", shared_reference
    )
    conn = sqlite3.connect(deletion_db)
    try:
        surviving_rows = conn.execute(
            "SELECT type, amount, balance_before, balance_after, description, "
            "reference_id FROM TRANSACTIONS WHERE user_id = ? ORDER BY amount DESC",
            (surviving_user_id,),
        ).fetchall()
        archive_reference = conn.execute(
            "SELECT payment_reference FROM FINANCIAL_ARCHIVE "
            "WHERE deleted_user_ref = ?",
            (user_deletion._deleted_user_ref(deleted_user_id),),
        ).fetchone()[0]
    finally:
        conn.close()

    assert surviving_rows[0] == (
        surviving_type,
        12,
        20,
        8,
        safe_description,
        expected_reference,
    )
    assert archive_reference == expected_reference
    # Exact matching must not confuse the target with an ID containing it.
    assert surviving_rows[1][4:] == (
        "Unrelated transfer to user 184",
        "initial_balance_184_decoy",
    )

    # Deleting the counterparty later preserves the shared fingerprint instead of
    # applying HMAC a second time, so the two archive entries remain reconcilable.
    second_result = await user_deletion.delete_user_account(
        user_id=surviving_user_id,
        bridge=FakeBridge(deletion_db, enabled=False),
    )
    assert second_result.status == "completed", second_result.summary
    assert (
        _count(
            deletion_db,
            "SELECT COUNT(*) FROM FINANCIAL_ARCHIVE WHERE payment_reference = ?",
            (expected_reference,),
        )
        == 2
    )


@pytest.mark.asyncio
async def test_balance_transfer_redaction_rolls_back_with_archive_failure(
    deletion_db, monkeypatch
):
    _add_user(deletion_db, 91, "rollback-funder", role_id=2)
    _add_user(deletion_db, 92, "rollback-recipient", role_id=2)
    shared_reference = "initial_balance_92_aaaaaaaaaaaaaaaa"
    _add_transactions(
        deletion_db,
        91,
        [
            {
                "type": "balance_transfer_out",
                "amount": 5.0,
                "balance_before": 10.0,
                "balance_after": 5.0,
                "description": "Initial balance transferred to user 92",
                "reference_id": shared_reference,
            }
        ],
    )
    _add_transactions(
        deletion_db,
        92,
        [
            {
                "type": "balance_transfer_in",
                "amount": 5.0,
                "balance_before": 0.0,
                "balance_after": 5.0,
                "description": "Initial balance funded by user 91",
                "reference_id": shared_reference,
            }
        ],
    )
    real_archive = user_deletion._archive_transactions

    async def _fail_after_archive(conn, user_id):
        await real_archive(conn, user_id)
        raise RuntimeError("forced archive rollback")

    monkeypatch.setattr(user_deletion, "_archive_transactions", _fail_after_archive)

    with pytest.raises(RuntimeError, match="forced archive rollback"):
        await user_deletion.delete_user_account(
            user_id=92,
            bridge=FakeBridge(deletion_db, enabled=False),
        )

    conn = sqlite3.connect(deletion_db)
    try:
        rows = conn.execute(
            "SELECT user_id, description, reference_id FROM TRANSACTIONS ORDER BY user_id"
        ).fetchall()
        archive_count = conn.execute(
            "SELECT COUNT(*) FROM FINANCIAL_ARCHIVE"
        ).fetchone()[0]
    finally:
        conn.close()
    assert rows == [
        (91, "Initial balance transferred to user 92", shared_reference),
        (92, "Initial balance funded by user 91", shared_reference),
    ]
    assert archive_count == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 92") == 1


@pytest.mark.asyncio
async def test_dry_run_reports_would_archive_without_writing(deletion_db):
    _add_user(deletion_db, 85, "grace", role_id=2)
    _add_transactions(
        deletion_db,
        85,
        [
            {"type": "credit", "amount": 3.0, "balance_before": 0.0, "balance_after": 3.0},
            {"type": "credit", "amount": 4.0, "balance_before": 3.0, "balance_after": 7.0},
        ],
    )
    bridge = FakeBridge(deletion_db, enabled=False)

    result = await user_deletion.delete_user_account(user_id=85, dry_run=True, bridge=bridge)

    assert result.status == "dry_run"
    assert result.archived_transaction_count == 2
    # Nothing written or deleted during a dry run.
    assert _count(deletion_db, "SELECT COUNT(*) FROM TRANSACTIONS WHERE user_id = 85") == 2
    assert _count(deletion_db, "SELECT COUNT(*) FROM FINANCIAL_ARCHIVE") == 0


# --------------------------------------------------------------------------- #
# ElevenLabs remote purge (best-effort post-commit)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_elevenlabs_purge_failure_yields_cleanup_errors(deletion_db, purge_spy):
    _add_user(deletion_db, 86, "heidi", role_id=2)
    _add_conversation_with_atagia(deletion_db, conv_id=860, user_id=86, message_ids=[8601])
    bridge = FakeBridge(deletion_db, enabled=True)
    # Simulate a third-party outage for one of the two captured conversation ids.
    purge_spy.fail_ids.add("eleven-860")

    result = await user_deletion.delete_user_account(user_id=86, bridge=bridge)

    assert result.status == "completed_with_cleanup_errors", result.summary
    # The account is still fully deleted despite the remote purge failure.
    assert _count(deletion_db, "SELECT COUNT(*) FROM USERS WHERE id = 86") == 0
    # Every captured id was attempted.
    assert set(purge_spy.calls) == {"eleven-860", "remote-860"}
    # The failing id is recorded (never silent) and tagged as an elevenlabs session.
    failing = [e for e in result.cleanup_errors if e.get("target") == "eleven-860"]
    assert len(failing) == 1
    assert failing[0]["kind"] == "elevenlabs_session"


@pytest.mark.asyncio
async def test_no_elevenlabs_sessions_skips_purge(deletion_db, purge_spy):
    _add_user(deletion_db, 87, "ivan", role_id=2)
    bridge = FakeBridge(deletion_db, enabled=False)

    result = await user_deletion.delete_user_account(user_id=87, bridge=bridge)

    assert result.status == "completed", result.summary
    assert result.elevenlabs_session_ids == []
    # Zero session ids means the remote API is never touched (no key requirement).
    assert purge_spy.calls == []


def _hide_private_subscription_package(monkeypatch):
    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "subscription_auth.linking":
            raise ModuleNotFoundError(
                "private subscription package is absent",
                name="subscription_auth",
            )
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)


@pytest.mark.asyncio
async def test_public_build_deletion_accepts_legacy_schema_without_private_column(
    deletion_db, monkeypatch
):
    _add_user(deletion_db, 88, "public-legacy")
    with sqlite3.connect(deletion_db) as conn:
        conn.execute("ALTER TABLE USER_DETAILS DROP COLUMN subscription_auth")
    _hide_private_subscription_package(monkeypatch)

    lifecycle = await user_deletion._begin_subscription_deletion_lifecycle(88)

    assert lifecycle is None


@pytest.mark.asyncio
async def test_public_build_deletion_accepts_null_private_column(
    deletion_db, monkeypatch
):
    _add_user(deletion_db, 89, "public-null")
    _hide_private_subscription_package(monkeypatch)

    lifecycle = await user_deletion._begin_subscription_deletion_lifecycle(89)

    assert lifecycle is None


@pytest.mark.asyncio
async def test_public_build_deletion_rejects_orphaned_private_credential(
    deletion_db, monkeypatch
):
    _add_user(deletion_db, 90, "public-orphan")
    with sqlite3.connect(deletion_db) as conn:
        conn.execute(
            "UPDATE USER_DETAILS SET subscription_auth = ? WHERE user_id = ?",
            ("opaque-private-credential", 90),
        )
    _hide_private_subscription_package(monkeypatch)

    with pytest.raises(RuntimeError, match="cleanup module is absent"):
        await user_deletion._begin_subscription_deletion_lifecycle(90)


# --------------------------------------------------------------------------- #
# Shared message-deletion helper (rollback / auto-repair path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_delete_message_rows_is_fk_safe_and_cleans_state(deletion_db):
    _add_user(deletion_db, 83, "erin", role_id=2)
    _add_conversation_with_atagia(deletion_db, conv_id=830, user_id=83, message_ids=[1, 2, 3])
    conn = sqlite3.connect(deletion_db)
    conn.execute(
        "INSERT INTO WATCHDOG_EVENTS (id, conversation_id, user_message_id, bot_message_id) VALUES (1, 830, 2, 3)"
    )
    conn.execute(
        "INSERT INTO WATCHDOG_STATE (conversation_id, last_evaluated_message_id) VALUES (830, 3)"
    )
    conn.commit()
    conn.close()

    # A naive direct delete would hit the enforced FK on MESSAGES.
    async with _connect(deletion_db) as raw:
        with pytest.raises(aiosqlite.IntegrityError):
            await raw.execute("DELETE FROM messages WHERE id IN (2, 3)")
            await raw.commit()
        await raw.rollback()

    async with _connect(deletion_db) as conn:
        await delete_message_rows(conn, conversation_id=830, message_ids=[2, 3])
        await conn.commit()

    assert _count(deletion_db, "SELECT COUNT(*) FROM MESSAGES WHERE id IN (2, 3)") == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM MESSAGES WHERE id = 1") == 1
    assert _count(deletion_db, "SELECT COUNT(*) FROM MEMORY_PROVIDER_MESSAGE_LINKS WHERE message_id IN (2, 3)") == 0
    assert _count(deletion_db, "SELECT COUNT(*) FROM WATCHDOG_EVENTS WHERE id = 1") == 0
    # Sync watermark clamped down to the surviving tail (message 1).
    assert _count(
        deletion_db,
        "SELECT last_message_id FROM MEMORY_PROVIDER_SYNC_STATE WHERE conversation_id = 830",
    ) == 1
    # Stale watchdog state (pointed at deleted message 3) removed so it regenerates.
    assert _count(deletion_db, "SELECT COUNT(*) FROM WATCHDOG_STATE WHERE conversation_id = 830") == 0
