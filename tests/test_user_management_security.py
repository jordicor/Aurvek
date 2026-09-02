"""Regression coverage for user-management authorization boundaries."""

import asyncio
import re
import sqlite3
import time
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

import app as app_module
import user_deletion as user_deletion_module
from user_accounts import InvalidInitialBalanceError, validate_managed_balance


class DummyUser:
    def __init__(
        self,
        user_id: int,
        username: str,
        *,
        admin: bool = False,
        auth_time: int | None = None,
    ):
        self.id = user_id
        self.username = username
        self._admin = admin
        self.auth_time = int(time.time()) if auth_time is None else auth_time

    @property
    async def is_admin(self):
        return self._admin

    @property
    async def is_user(self):
        return not self._admin


def _request(path: str = "/edit-user") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "scheme": "https",
            "server": ("example.test", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def _ajax_request(path: str = "/api/edit-profile") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"x-requested-with", b"XMLHttpRequest")],
            "scheme": "https",
            "server": ("example.test", 443),
            "client": ("127.0.0.1", 12345),
        }
    )


def _json_request(payload: bytes, path: str) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-type", b"application/json")],
            "scheme": "https",
            "server": ("example.test", 443),
            "client": ("127.0.0.1", 12345),
        },
        receive=receive,
    )


@pytest.fixture()
def user_management_db(tmp_path, monkeypatch):
    db_path = tmp_path / "user-management.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE USERS (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            role_id INTEGER NOT NULL,
            phone_number TEXT,
            email TEXT,
            password TEXT,
            is_enabled INTEGER DEFAULT 1,
            auth_provider TEXT,
            phone_verified INTEGER DEFAULT 0,
            user_info TEXT,
            profile_picture TEXT,
            session_version INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE USER_DETAILS (
            user_id INTEGER PRIMARY KEY,
            current_prompt_id INTEGER,
            llm_id INTEGER,
            allow_file_upload INTEGER DEFAULT 0,
            allow_image_generation INTEGER DEFAULT 0,
            balance REAL DEFAULT 0,
            all_prompts_access INTEGER DEFAULT 0,
            public_prompts_access INTEGER DEFAULT 0,
            can_change_password INTEGER DEFAULT 0,
            authentication_mode TEXT DEFAULT 'magic_link_only',
            api_key_mode TEXT DEFAULT 'both_prefer_own',
            user_api_keys TEXT,
            category_access TEXT,
            billing_account_id INTEGER,
            billing_limit REAL,
            billing_limit_action TEXT DEFAULT 'block',
            billing_auto_refill_amount REAL DEFAULT 10,
            billing_max_limit REAL,
            created_by INTEGER,
            current_alter_ego_id INTEGER DEFAULT 0,
            web_search_mode TEXT DEFAULT 'native',
            storage_quota_bytes INTEGER DEFAULT NULL,
            subscription_auth TEXT DEFAULT NULL
        );

        CREATE TABLE PHONE_VERIFICATION_CHALLENGES (
            id TEXT PRIMARY KEY,
            actor_user_id INTEGER NOT NULL,
            phone_number TEXT NOT NULL,
            purpose TEXT NOT NULL,
            request_ip TEXT NOT NULL,
            status TEXT NOT NULL,
            verification_attempts INTEGER NOT NULL DEFAULT 0,
            provider_sid TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            approved_at INTEGER,
            consumed_at INTEGER,
            last_attempt_at INTEGER
        );

        CREATE TABLE USER_ROLES (
            id INTEGER PRIMARY KEY,
            role_name TEXT NOT NULL
        );

        CREATE TABLE USER_CREATOR_RELATIONSHIPS (
            user_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            relationship_type TEXT NOT NULL,
            source_type TEXT,
            source_id INTEGER,
            is_primary INTEGER DEFAULT 0,
            last_interaction_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, creator_id, relationship_type)
        );

        CREATE TABLE LLM (
            id INTEGER PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            machine TEXT
        );

        CREATE TABLE CATEGORIES (
            id INTEGER PRIMARY KEY,
            name TEXT,
            icon TEXT,
            is_age_restricted INTEGER DEFAULT 0,
            display_order INTEGER DEFAULT 0
        );

        CREATE TABLE magic_links (
            user_id INTEGER
        );

        CREATE TABLE TRANSACTIONS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            balance_before REAL,
            balance_after REAL,
            description TEXT,
            reference_id TEXT
        );

        CREATE TABLE BILLING_USAGE_RESERVATIONS (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            billing_account_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO USER_ROLES (id, role_name) VALUES (?, ?)",
        [(1, "admin"), (2, "user"), (3, "customer")],
    )
    conn.execute("INSERT INTO LLM (id, enabled, machine) VALUES (1, 1, 'GPT')")
    conn.executemany(
        "INSERT INTO USERS (id, username, role_id, email) VALUES (?, ?, ?, ?)",
        [
            (1, "admin", 1, "admin@example.test"),
            (10, "creator", 2, "creator@example.test"),
            (20, "assigned-customer", 3, "assigned@example.test"),
            (30, "unassigned-customer", 3, "unassigned@example.test"),
            (40, "other-admin", 1, "other-admin@example.test"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO USER_DETAILS (user_id, current_prompt_id, llm_id, balance)
        VALUES (?, 10, 1, ?)
        """,
        [(1, 50), (10, 50), (20, 10), (30, 10), (40, 50)],
    )
    conn.execute(
        """
        INSERT INTO USER_CREATOR_RELATIONSHIPS
            (user_id, creator_id, relationship_type, source_type)
        VALUES (20, 10, 'assigned_by', 'manual')
        """
    )
    conn.commit()
    conn.close()

    @asynccontextmanager
    async def _get_test_conn(readonly=False):
        mode = "ro" if readonly else "rwc"
        test_conn = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        test_conn.row_factory = aiosqlite.Row
        try:
            yield test_conn
        finally:
            await test_conn.close()

    monkeypatch.setattr(app_module, "get_db_connection", _get_test_conn)
    monkeypatch.setattr(user_deletion_module, "get_db_connection", _get_test_conn)

    async def _no_stale_reservations():
        return 0

    monkeypatch.setattr(
        app_module,
        "reconcile_stale_usage_reservations",
        _no_stale_reservations,
    )
    monkeypatch.setattr(
        user_deletion_module,
        "reconcile_stale_usage_reservations",
        _no_stale_reservations,
    )

    async def _accessible_prompts(*args, **kwargs):
        return [10]

    monkeypatch.setattr(
        app_module, "get_user_role_accessible_prompts", _accessible_prompts
    )
    return db_path


def _update_kwargs(username: str, balance: float) -> dict:
    return {
        "request": _request(),
        "current_user": DummyUser(10, "creator"),
        "username": username,
        "new_username": username,
        "phone_number": None,
        "email": None,
        "new_password": None,
        "prompt_id": 10,
        "machine": "1",
        "allow_file_upload": False,
        "allow_image_generation": False,
        "balance": balance,
        "all_prompts_access": False,
        "public_prompts_access": False,
        "can_change_password": False,
        "api_key_mode": "both_prefer_own",
        "category_ids": None,
        "allow_all_categories": False,
        "billing_mode": "customer_pays",
        "billing_limit": None,
        "billing_limit_action": "block",
        "billing_auto_refill_amount": None,
        "billing_max_limit": None,
        "user_role_id": None,
        "authentication_mode": "magic_link_only",
    }


@pytest.mark.asyncio
async def test_edit_user_form_rejects_unassigned_customer(
    user_management_db, monkeypatch
):
    async def _no_prompts(*args, **kwargs):
        return []

    async def _empty_context(*args, **kwargs):
        return {}

    monkeypatch.setattr(app_module, "get_user_accessible_prompts", _no_prompts)
    monkeypatch.setattr(app_module, "get_selector_llms", _no_prompts)
    monkeypatch.setattr(app_module, "get_template_context", _empty_context)
    monkeypatch.setattr(
        app_module.templates,
        "TemplateResponse",
        lambda *args, **kwargs: JSONResponse({"rendered": True}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await app_module.edit_user_form(
            _request("/edit-user/unassigned-customer"),
            username="unassigned-customer",
            current_user=DummyUser(10, "creator"),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_edit_user_rejects_unassigned_customer(user_management_db):
    with pytest.raises(HTTPException) as exc_info:
        await app_module.update_user(**_update_kwargs("unassigned-customer", 10))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_edit_user_allows_assigned_by_customer(user_management_db):
    response = await app_module.update_user(**_update_kwargs("assigned-customer", 10))

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_edit_user_blocks_billing_changes_during_active_usage(
    user_management_db,
):
    with sqlite3.connect(user_management_db) as conn:
        conn.execute(
            """
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, status
            ) VALUES ('active-hold', 20, 1, 'active')
            """
        )
        conn.commit()

    kwargs = _update_kwargs("assigned-customer", 10)
    kwargs.update(
        current_user=DummyUser(1, "admin", admin=True),
        billing_mode="user_pays",
        billing_limit=10,
        billing_limit_action="auto_refill",
        billing_auto_refill_amount=5,
        billing_max_limit=20,
    )
    response = await app_module.update_user(**kwargs)

    assert response.status_code == 409
    with sqlite3.connect(user_management_db) as conn:
        config = conn.execute(
            """
            SELECT billing_account_id, billing_limit,
                   billing_limit_action, billing_max_limit
            FROM USER_DETAILS WHERE user_id = 20
            """
        ).fetchone()
    assert config == (None, None, "block", None)


@pytest.mark.asyncio
async def test_edit_user_reconciles_expired_usage_before_billing_change(
    user_management_db,
    monkeypatch,
):
    with sqlite3.connect(user_management_db) as conn:
        conn.execute(
            """
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, status
            ) VALUES ('expired-hold', 20, 1, 'active')
            """
        )
        conn.commit()

    async def _reconcile_expired_hold():
        with sqlite3.connect(user_management_db) as conn:
            conn.execute(
                """
                UPDATE BILLING_USAGE_RESERVATIONS
                SET status = 'refunded'
                WHERE id = 'expired-hold'
                """
            )
            conn.execute(
                "UPDATE USER_DETAILS SET balance = balance + 5 WHERE user_id = 20"
            )
            conn.commit()
        return 1

    monkeypatch.setattr(
        app_module,
        "reconcile_stale_usage_reservations",
        _reconcile_expired_hold,
    )
    kwargs = _update_kwargs("assigned-customer", 10)
    kwargs.update(
        current_user=DummyUser(1, "admin", admin=True),
        billing_mode="user_pays",
        billing_limit=10,
        billing_limit_action="block",
    )

    response = await app_module.update_user(**kwargs)

    assert response.status_code == 200
    with sqlite3.connect(user_management_db) as conn:
        config = conn.execute(
            """
            SELECT billing_account_id, billing_limit, balance
            FROM USER_DETAILS WHERE user_id = 20
            """
        ).fetchone()
        hold_status = conn.execute(
            """
            SELECT status FROM BILLING_USAGE_RESERVATIONS
            WHERE id = 'expired-hold'
            """
        ).fetchone()[0]
    assert config == (1, 10.0, 15.0)
    assert hold_status == "refunded"


@pytest.mark.asyncio
async def test_edit_user_rejects_auto_refill_max_below_current_limit(
    user_management_db,
):
    kwargs = _update_kwargs("assigned-customer", 10)
    kwargs.update(
        current_user=DummyUser(1, "admin", admin=True),
        billing_mode="user_pays",
        billing_limit=100,
        billing_limit_action="auto_refill",
        billing_auto_refill_amount=10,
        billing_max_limit=50,
    )
    response = await app_module.update_user(**kwargs)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_rejected_admin_role_change_does_not_write_audit_log(
    user_management_db,
    monkeypatch,
):
    with sqlite3.connect(user_management_db) as conn:
        conn.execute(
            """
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, status
            ) VALUES ('admin-hold', 40, 1, 'active')
            """
        )
        conn.commit()

    audit_calls = []

    async def _elevated(*_args, **_kwargs):
        return True

    async def _record_audit(**kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(app_module, "is_elevated", _elevated)
    monkeypatch.setattr(app_module, "log_admin_action", _record_audit)

    kwargs = _update_kwargs("other-admin", 50)
    kwargs.update(
        current_user=DummyUser(1, "admin", admin=True),
        user_role_id="2",
        billing_mode="user_pays",
        billing_limit=10,
    )
    response = await app_module.update_user(**kwargs)

    assert response.status_code == 409
    assert audit_calls == []
    with sqlite3.connect(user_management_db) as conn:
        role_id = conn.execute(
            "SELECT role_id FROM USERS WHERE id = 40"
        ).fetchone()[0]
    assert role_id == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reservation_user_id", "reservation_billing_account_id"),
    [
        (20, 1),
        (30, 20),
    ],
)
async def test_delete_user_blocks_active_usage_before_side_effects(
    user_management_db,
    monkeypatch,
    reservation_user_id,
    reservation_billing_account_id,
):
    with sqlite3.connect(user_management_db) as conn:
        conn.execute(
            """
            INSERT INTO BILLING_USAGE_RESERVATIONS (
                id, user_id, billing_account_id, status
            ) VALUES ('delete-hold', ?, ?, 'active')
            """,
            (reservation_user_id, reservation_billing_account_id),
        )
        conn.commit()

    side_effects = []
    reconciliation_calls = []

    async def _not_elevated(*_args, **_kwargs):
        return False

    async def _record_revocation(*_args, **_kwargs):
        side_effects.append("revoked")

    async def _record_audit(*_args, **_kwargs):
        side_effects.append("audit")

    async def _record_reconciliation():
        reconciliation_calls.append("reconciled")
        return 0

    def _record_filesystem_check(*_args, **_kwargs):
        side_effects.append("filesystem")
        return False

    monkeypatch.setattr(app_module, "is_elevated", _not_elevated)
    monkeypatch.setattr(app_module, "add_revoked_user", _record_revocation)
    monkeypatch.setattr(user_deletion_module, "add_revoked_user", _record_revocation)
    monkeypatch.setattr(app_module, "log_admin_action", _record_audit)
    # The active-usage guard (and its stale-hold reconciliation) lives in the
    # user_deletion service, inside its write transaction.
    monkeypatch.setattr(
        user_deletion_module,
        "reconcile_stale_usage_reservations",
        _record_reconciliation,
    )
    monkeypatch.setattr(app_module.os.path, "exists", _record_filesystem_check)

    with pytest.raises(HTTPException) as exc_info:
        await app_module.delete_user(
            "assigned-customer",
            DummyUser(1, "admin", admin=True),
            request_ip="127.0.0.1",
        )

    assert exc_info.value.status_code == 409
    assert reconciliation_calls == ["reconciled"]
    assert side_effects == []
    with sqlite3.connect(user_management_db) as conn:
        assert conn.execute(
            "SELECT 1 FROM USERS WHERE id = 20"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT status FROM BILLING_USAGE_RESERVATIONS WHERE id = 'delete-hold'"
        ).fetchone() == ("active",)


@pytest.mark.asyncio
async def test_delete_account_preserves_active_usage_conflict(monkeypatch):
    async def _blocked_delete(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="Usage is still active")

    monkeypatch.setattr(app_module, "delete_user", _blocked_delete)

    with pytest.raises(HTTPException) as exc_info:
        await app_module.delete_account(
            _request("/api/delete-account"),
            DummyUser(20, "assigned-customer"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Usage is still active"


@pytest.mark.asyncio
async def test_batch_delete_preserves_active_usage_conflict(monkeypatch):
    class _SelectedUsers:
        def getlist(self, key):
            assert key == "selected_users"
            return ["assigned-customer"]

    class _BatchRequest:
        headers = {}
        client = None

        async def form(self):
            return _SelectedUsers()

    async def _blocked_delete(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="Usage is still active")

    monkeypatch.setattr(app_module, "delete_user", _blocked_delete)

    response = await app_module.delete_users(
        _BatchRequest(),
        DummyUser(1, "admin", admin=True),
    )

    assert response.status_code == 409
    assert b"Usage is still active" in response.body


@pytest.mark.asyncio
async def test_edit_user_rejects_username_rename_even_for_admin(user_management_db):
    kwargs = _update_kwargs("assigned-customer", 10)
    kwargs.update(
        current_user=DummyUser(1, "admin", admin=True),
        new_username="Assigned-Customer",
    )

    with pytest.raises(HTTPException) as exc_info:
        await app_module.update_user(**kwargs)

    assert exc_info.value.status_code == 400
    conn = sqlite3.connect(user_management_db)
    try:
        username = conn.execute(
            "SELECT username FROM USERS WHERE id = 20"
        ).fetchone()[0]
    finally:
        conn.close()
    assert username == "assigned-customer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "attacker@example.test"),
        ("phone_number", "+34600000000"),
        ("new_password", "attacker-password"),
    ],
)
async def test_creator_cannot_change_managed_user_credentials(
    user_management_db,
    field,
    value,
):
    kwargs = _update_kwargs("assigned-customer", 10)
    kwargs[field] = value

    with pytest.raises(HTTPException) as exc_info:
        await app_module.update_user(**kwargs)

    assert exc_info.value.status_code == 403
    conn = sqlite3.connect(user_management_db)
    try:
        credentials = conn.execute(
            "SELECT email, phone_number, password FROM USERS WHERE id = 20"
        ).fetchone()
    finally:
        conn.close()
    assert credentials == ("assigned@example.test", None, None)


@pytest.mark.asyncio
async def test_admin_phone_change_resets_verification_and_invalidates_sessions(
    user_management_db,
):
    conn = sqlite3.connect(user_management_db)
    conn.execute(
        "UPDATE USERS SET phone_number = '+34111111111', phone_verified = 1 WHERE id = 20"
    )
    conn.commit()
    conn.close()

    kwargs = _update_kwargs("assigned-customer", 10)
    kwargs.update(
        current_user=DummyUser(1, "admin", admin=True),
        phone_number="+34222222222",
    )
    response = await app_module.update_user(**kwargs)

    assert response.status_code == 200
    conn = sqlite3.connect(user_management_db)
    try:
        phone, verified, version = conn.execute(
            """
            SELECT phone_number, phone_verified, session_version
            FROM USERS
            WHERE id = 20
            """
        ).fetchone()
    finally:
        conn.close()
    assert phone == "+34222222222"
    assert verified == 0
    assert version == 2


async def _edit_profile(user_management_db, **overrides):
    values = {
        "request": _ajax_request(),
        "username": "assigned-customer",
        "phone_number": None,
        "email": None,
        "new_password": None,
        "phone_verification_id": None,
        "sample_voice_id": None,
        "user_info": None,
        "profile_picture": None,
        "alter_ego_id": None,
        "current_user": DummyUser(20, "assigned-customer"),
    }
    values.update(overrides)
    return await app_module.edit_profile(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("username", "Assigned-Customer", "Username cannot be changed"),
        ("email", "new@example.test", "Email changes require"),
        ("phone_number", "+34600000000", "Use phone verification"),
        ("new_password", "attacker-password", "Use the password form"),
    ],
)
async def test_generic_profile_update_rejects_identity_and_credential_changes(
    user_management_db,
    field,
    value,
    message,
):
    response = await _edit_profile(user_management_db, **{field: value})

    assert response.status_code == 400
    assert message in response.body.decode("utf-8")
    conn = sqlite3.connect(user_management_db)
    try:
        identity = conn.execute(
            """
            SELECT username, email, phone_number, password
            FROM USERS
            WHERE id = 20
            """
        ).fetchone()
    finally:
        conn.close()
    assert identity == ("assigned-customer", "assigned@example.test", None, None)


@pytest.mark.asyncio
async def test_generic_profile_update_still_updates_profile_content(
    user_management_db,
):
    response = await _edit_profile(
        user_management_db,
        user_info="A short, editable profile.",
    )

    assert response.status_code == 200
    conn = sqlite3.connect(user_management_db)
    try:
        user_info = conn.execute(
            "SELECT user_info FROM USERS WHERE id = 20"
        ).fetchone()[0]
    finally:
        conn.close()
    assert user_info == "A short, editable profile."


@pytest.mark.asyncio
async def test_edit_user_role_user_cannot_increase_customer_balance(user_management_db):
    with pytest.raises(HTTPException) as exc_info:
        await app_module.update_user(**_update_kwargs("assigned-customer", 25))

    assert exc_info.value.status_code == 403

    conn = sqlite3.connect(user_management_db)
    try:
        balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 20"
        ).fetchone()[0]
    finally:
        conn.close()
    assert balance == 10


async def _create_customer(
    current_user,
    *,
    username: str,
    balance: float,
    phone: str | None = None,
    phone_verification_id: str | None = None,
    skip_verification: bool = False,
):
    return await app_module.create_user_post(
        request=_request("/create-user"),
        current_user=current_user,
        prompt_id=10,
        all_prompts_access=False,
        public_prompts_access=False,
        machine="1",
        allow_file_upload=False,
        allow_image_generation=False,
        balance=balance,
        phone=phone,
        skip_verification=skip_verification,
        phone_verification_id=phone_verification_id,
        user_type="customer",
        username=username,
        use_random_username=False,
        authentication_mode="password_only",
        initial_password="secure-password",
        can_change_password=False,
        email=f"{username}@example.test",
        api_key_mode="both_prefer_own",
        category_ids=None,
        billing_mode="customer_pays",
        billing_limit=None,
        billing_limit_action="block",
        billing_auto_refill_amount=None,
        billing_max_limit=None,
    )


@pytest.fixture()
def create_user_validation_mocks(monkeypatch):
    async def _username_available(*args, **kwargs):
        return False

    monkeypatch.setattr(app_module, "username_exists", _username_available)


@pytest.mark.parametrize("balance", [float("nan"), float("inf"), float("-inf")])
def test_managed_balance_rejects_non_finite_values(balance):
    with pytest.raises(InvalidInitialBalanceError):
        validate_managed_balance(balance)


@pytest.mark.asyncio
async def test_create_user_role_user_funds_initial_balance_atomically(
    user_management_db, create_user_validation_mocks
):
    response = await _create_customer(
        DummyUser(10, "creator"), username="funded-customer", balance=15
    )

    assert response.status_code == 200
    conn = sqlite3.connect(user_management_db)
    try:
        creator_balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 10"
        ).fetchone()[0]
        customer = conn.execute(
            """
            SELECT u.id, ud.balance
            FROM USERS u
            JOIN USER_DETAILS ud ON ud.user_id = u.id
            WHERE u.username = 'funded-customer'
            """
        ).fetchone()
        transfer_rows = conn.execute(
            """
            SELECT user_id, type, amount, description, reference_id
            FROM TRANSACTIONS
            WHERE type IN ('balance_transfer_out', 'balance_transfer_in')
            ORDER BY id
            """
        ).fetchall()
        assignment = conn.execute(
            """
            SELECT relationship_type, source_type
            FROM USER_CREATOR_RELATIONSHIPS
            WHERE user_id = ? AND creator_id = 10
            """,
            (customer[0],),
        ).fetchone()
    finally:
        conn.close()

    assert creator_balance == 35
    assert customer[1] == 15
    assert [(row[0], row[1], row[2]) for row in transfer_rows] == [
        (10, "balance_transfer_out", 15),
        (customer[0], "balance_transfer_in", 15),
    ]
    assert transfer_rows[0][3] == "Balance transfer sent"
    assert transfer_rows[1][3] == "Balance transfer received"
    assert transfer_rows[0][4] == transfer_rows[1][4]
    assert re.fullmatch(r"initial_balance_v2_[0-9a-f]{32}", transfer_rows[0][4])
    assert assignment == ("assigned_by", "manual")


@pytest.mark.asyncio
async def test_create_user_insufficient_creator_balance_rolls_back_customer(
    user_management_db, create_user_validation_mocks
):
    conn = sqlite3.connect(user_management_db)
    conn.execute("UPDATE USER_DETAILS SET balance = 5 WHERE user_id = 10")
    conn.commit()
    conn.close()

    with pytest.raises(HTTPException) as exc_info:
        await _create_customer(
            DummyUser(10, "creator"), username="unfunded-customer", balance=15
        )

    assert exc_info.value.status_code == 402
    assert (
        exc_info.value.detail
        == "Insufficient balance to fund the customer's initial balance."
    )

    conn = sqlite3.connect(user_management_db)
    try:
        creator_balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 10"
        ).fetchone()[0]
        customer_count = conn.execute(
            "SELECT COUNT(*) FROM USERS WHERE username = 'unfunded-customer'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert creator_balance == 5
    assert customer_count == 0


@pytest.mark.asyncio
async def test_concurrent_initial_balance_transfers_cannot_overspend_creator(
    user_management_db, create_user_validation_mocks
):
    results = await asyncio.gather(
        _create_customer(
            DummyUser(10, "creator"), username="concurrent-one", balance=30
        ),
        _create_customer(
            DummyUser(10, "creator"), username="concurrent-two", balance=30
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]

    assert len(successes) == 1
    assert successes[0].status_code == 200
    assert len(failures) == 1
    assert isinstance(failures[0], HTTPException)
    assert failures[0].status_code == 402

    conn = sqlite3.connect(user_management_db)
    try:
        creator_balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 10"
        ).fetchone()[0]
        created_wallets = conn.execute(
            """
            SELECT u.username, ud.balance
            FROM USERS u
            JOIN USER_DETAILS ud ON ud.user_id = u.id
            WHERE u.username IN ('concurrent-one', 'concurrent-two')
            """
        ).fetchall()
        transfer_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM TRANSACTIONS
            WHERE type IN ('balance_transfer_out', 'balance_transfer_in')
            """
        ).fetchone()[0]
    finally:
        conn.close()

    assert creator_balance == 20
    assert len(created_wallets) == 1
    assert created_wallets[0][1] == 30
    assert transfer_count == 2


@pytest.mark.asyncio
async def test_create_user_admin_grant_does_not_debit_admin(
    user_management_db, create_user_validation_mocks
):
    response = await _create_customer(
        DummyUser(1, "admin", admin=True), username="admin-funded", balance=15
    )

    assert response.status_code == 200
    conn = sqlite3.connect(user_management_db)
    try:
        admin_balance = conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = 1"
        ).fetchone()[0]
        customer_balance = conn.execute(
            """
            SELECT ud.balance
            FROM USERS u
            JOIN USER_DETAILS ud ON ud.user_id = u.id
            WHERE u.username = 'admin-funded'
            """
        ).fetchone()[0]
        grant_transaction = conn.execute(
            """
            SELECT t.description, t.reference_id
            FROM TRANSACTIONS t
            JOIN USERS u ON u.id = t.user_id
            WHERE u.username = 'admin-funded' AND t.type = 'balance_credit'
            """
        ).fetchone()
    finally:
        conn.close()

    assert admin_balance == 50
    assert customer_balance == 15
    assert grant_transaction[0] == "Platform initial balance credit"
    assert re.fullmatch(r"initial_balance_v2_[0-9a-f]{32}", grant_transaction[1])


def _insert_approved_phone_challenge(
    db_path,
    *,
    challenge_id: str,
    actor_user_id: int,
    phone_number: str,
    purpose: str,
):
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO PHONE_VERIFICATION_CHALLENGES (
            id, actor_user_id, phone_number, purpose, request_ip,
            status, created_at, expires_at, approved_at
        ) VALUES (?, ?, ?, ?, '127.0.0.1', 'approved', ?, ?, ?)
        """,
        (
            challenge_id,
            actor_user_id,
            phone_number,
            purpose,
            now,
            now + 600,
            now,
        ),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_creator_cannot_bypass_phone_verification(
    user_management_db,
    create_user_validation_mocks,
):
    with pytest.raises(HTTPException) as exc_info:
        await _create_customer(
            DummyUser(10, "creator"),
            username="skip-phone-check",
            balance=0,
            phone="+34600111222",
            skip_verification=True,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_create_user_consumes_exact_phone_grant_atomically(
    user_management_db,
    create_user_validation_mocks,
):
    _insert_approved_phone_challenge(
        user_management_db,
        challenge_id="create-grant",
        actor_user_id=10,
        phone_number="+34600111222",
        purpose="create_user",
    )

    response = await _create_customer(
        DummyUser(10, "creator"),
        username="verified-phone-user",
        balance=0,
        phone="+34 600 111 222",
        phone_verification_id="create-grant",
    )

    assert response.status_code == 200
    conn = sqlite3.connect(user_management_db)
    try:
        phone_row = conn.execute(
            """
            SELECT phone_number, phone_verified
            FROM USERS
            WHERE username = 'verified-phone-user'
            """
        ).fetchone()
        challenge_status = conn.execute(
            """
            SELECT status
            FROM PHONE_VERIFICATION_CHALLENGES
            WHERE id = 'create-grant'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    assert phone_row == ("+34600111222", 1)
    assert challenge_status == "consumed"


@pytest.mark.asyncio
async def test_failed_user_creation_does_not_consume_phone_grant(
    user_management_db,
    create_user_validation_mocks,
):
    conn = sqlite3.connect(user_management_db)
    conn.execute("UPDATE USER_DETAILS SET balance = 0 WHERE user_id = 10")
    conn.commit()
    conn.close()
    _insert_approved_phone_challenge(
        user_management_db,
        challenge_id="rollback-grant",
        actor_user_id=10,
        phone_number="+34600111223",
        purpose="create_user",
    )

    with pytest.raises(HTTPException) as exc_info:
        await _create_customer(
            DummyUser(10, "creator"),
            username="phone-rollback-user",
            balance=15,
            phone="+34600111223",
            phone_verification_id="rollback-grant",
        )

    assert exc_info.value.status_code == 402
    conn = sqlite3.connect(user_management_db)
    try:
        challenge_status = conn.execute(
            """
            SELECT status
            FROM PHONE_VERIFICATION_CHALLENGES
            WHERE id = 'rollback-grant'
            """
        ).fetchone()[0]
        created = conn.execute(
            "SELECT COUNT(*) FROM USERS WHERE username = 'phone-rollback-user'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert challenge_status == "approved"
    assert created == 0


@pytest.mark.asyncio
async def test_admin_phone_bypass_stores_number_as_unverified(
    user_management_db,
    create_user_validation_mocks,
):
    response = await _create_customer(
        DummyUser(1, "admin", admin=True),
        username="admin-phone-skip",
        balance=0,
        phone="+34600111224",
        skip_verification=True,
    )

    assert response.status_code == 200
    conn = sqlite3.connect(user_management_db)
    try:
        verified = conn.execute(
            """
            SELECT phone_verified
            FROM USERS
            WHERE username = 'admin-phone-skip'
            """
        ).fetchone()[0]
    finally:
        conn.close()
    assert verified == 0


@pytest.mark.asyncio
async def test_profile_phone_change_consumes_grant_and_revokes_sessions(
    user_management_db,
):
    _insert_approved_phone_challenge(
        user_management_db,
        challenge_id="profile-grant",
        actor_user_id=20,
        phone_number="+34600111225",
        purpose="profile_phone_change",
    )

    response = await _edit_profile(
        user_management_db,
        phone_number="+34 600 111 225",
        phone_verification_id="profile-grant",
    )

    assert response.status_code == 200
    assert b'"reauthenticate":true' in response.body
    assert "session=" in response.headers.get("set-cookie", "")
    conn = sqlite3.connect(user_management_db)
    try:
        user_row = conn.execute(
            """
            SELECT phone_number, phone_verified, session_version
            FROM USERS
            WHERE id = 20
            """
        ).fetchone()
        challenge_status = conn.execute(
            """
            SELECT status
            FROM PHONE_VERIFICATION_CHALLENGES
            WHERE id = 'profile-grant'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    assert user_row == ("+34600111225", 1, 2)
    assert challenge_status == "consumed"


@pytest.mark.asyncio
async def test_existing_unverified_profile_phone_can_be_verified_in_place(
    user_management_db,
):
    conn = sqlite3.connect(user_management_db)
    try:
        conn.execute(
            "UPDATE USERS SET phone_number = ?, phone_verified = 0 WHERE id = 20",
            ("+34600111225",),
        )
        conn.commit()
    finally:
        conn.close()

    _insert_approved_phone_challenge(
        user_management_db,
        challenge_id="existing-profile-grant",
        actor_user_id=20,
        phone_number="+34600111225",
        purpose="profile_phone_change",
    )

    response = await _edit_profile(
        user_management_db,
        phone_number="+34 600 111 225",
        phone_verification_id="existing-profile-grant",
    )

    assert response.status_code == 200
    assert b'"reauthenticate":false' in response.body
    conn = sqlite3.connect(user_management_db)
    try:
        user_row = conn.execute(
            "SELECT phone_number, phone_verified, session_version FROM USERS WHERE id = 20"
        ).fetchone()
        challenge_status = conn.execute(
            "SELECT status FROM PHONE_VERIFICATION_CHALLENGES WHERE id = ?",
            ("existing-profile-grant",),
        ).fetchone()[0]
    finally:
        conn.close()

    assert user_row == ("+34600111225", 1, 1)
    assert challenge_status == "consumed"


@pytest.mark.asyncio
async def test_profile_phone_change_requires_recent_authentication(
    user_management_db,
):
    _insert_approved_phone_challenge(
        user_management_db,
        challenge_id="stale-profile-grant",
        actor_user_id=20,
        phone_number="+34600111229",
        purpose="profile_phone_change",
    )

    response = await _edit_profile(
        user_management_db,
        phone_number="+34600111229",
        phone_verification_id="stale-profile-grant",
        current_user=DummyUser(
            20,
            "assigned-customer",
            auth_time=int(time.time()) - 601,
        ),
    )

    assert response.status_code == 403
    assert b"Please sign in again" in response.body
    conn = sqlite3.connect(user_management_db)
    try:
        user_row = conn.execute(
            "SELECT phone_number, phone_verified FROM USERS WHERE id = 20"
        ).fetchone()
        challenge_status = conn.execute(
            """
            SELECT status
            FROM PHONE_VERIFICATION_CHALLENGES
            WHERE id = 'stale-profile-grant'
            """
        ).fetchone()[0]
    finally:
        conn.close()

    assert user_row == (None, 0)
    assert challenge_status == "approved"


@pytest.mark.asyncio
async def test_phone_uniqueness_check_ignores_forged_user_id(
    user_management_db,
):
    conn = sqlite3.connect(user_management_db)
    conn.execute(
        "UPDATE USERS SET phone_number = '+34600111226' WHERE id = 20"
    )
    conn.commit()
    conn.close()

    response = await app_module.check_phone_number(
        _json_request(
            b'{"phone":"+34600111226","user_id":20}',
            "/api/check-phone-number",
        ),
        current_user=DummyUser(10, "creator"),
    )

    assert response.status_code == 200
    assert b'"exists":true' in response.body


@pytest.mark.asyncio
async def test_phone_verification_routes_require_an_authenticated_actor():
    request = _request("/api/send-verification-code")
    send_response = await app_module.send_verification_code(
        app_module.PhoneVerificationRequest(
            phone="+34600111227",
            purpose="profile_phone_change",
        ),
        request,
        current_user=None,
    )
    verify_response = await app_module.verify_code(
        app_module.VerificationCodeRequest(
            challenge_id="challenge",
            phone="+34600111227",
            purpose="profile_phone_change",
            code="123456",
        ),
        current_user=None,
    )

    assert send_response.status_code == 401
    assert verify_response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["send", "verify"])
async def test_profile_phone_verification_requires_recent_authentication(operation):
    stale_user = DummyUser(
        20,
        "assigned-customer",
        auth_time=int(time.time()) - 601,
    )

    with pytest.raises(HTTPException) as exc_info:
        if operation == "send":
            await app_module.send_verification_code(
                app_module.PhoneVerificationRequest(
                    phone="+34600111230",
                    purpose="profile_phone_change",
                ),
                _request("/api/send-verification-code"),
                current_user=stale_user,
            )
        else:
            await app_module.verify_code(
                app_module.VerificationCodeRequest(
                    challenge_id="challenge",
                    phone="+34600111230",
                    purpose="profile_phone_change",
                    code="123456",
                ),
                current_user=stale_user,
            )

    assert exc_info.value.status_code == 403
    assert "sign in again" in exc_info.value.detail


@pytest.mark.asyncio
async def test_customer_cannot_request_create_user_phone_challenge(
    user_management_db,
):
    with pytest.raises(HTTPException) as exc_info:
        await app_module.send_verification_code(
            app_module.PhoneVerificationRequest(
                phone="+34600111228",
                purpose="create_user",
            ),
            _request("/api/send-verification-code"),
            current_user=DummyUser(20, "assigned-customer"),
        )

    assert exc_info.value.status_code == 403
