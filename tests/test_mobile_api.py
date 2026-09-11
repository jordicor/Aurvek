import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiosqlite
import pytest

import auth_apple
from billing.routes import wallet
from chat.routes import bookmarks, conversations, messages, warmup
from content_reports import routes as content_report_routes
from database import get_public_packs
from legal import routes as legal_routes
from marketplace.routes import checkout, packs
from mobile import routes as mobile_routes
from mobile.client import (
    IOS_PURCHASE_DISABLED_ERROR,
    IOS_PURCHASE_DISABLED_REASON,
    mobile_config_payload,
    purchase_metadata_for_request,
)


class DummyUrl:
    scheme = "https"
    hostname = "example.com"
    port = None


class DummyRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.url = DummyUrl()
        self.base_url = "https://example.com/"
        self.state = SimpleNamespace()
        self.cookies = {}


class DummyUser:
    id = 7
    username = "ios-user"
    role_id = 1
    is_enabled = True
    can_send_files = True
    can_generate_images = False
    all_prompts_access = False
    public_prompts_access = True
    current_prompt_id = None
    authentication_mode = "google"
    can_change_password = False


def _json_body(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_public_packs_include_entitlement_access_for_purchase_metadata(tmp_path):
    db_path = tmp_path / "packs.db"
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(
            """
            CREATE TABLE USERS (
                id INTEGER PRIMARY KEY,
                username TEXT
            );
            CREATE TABLE PACKS (
                id INTEGER PRIMARY KEY,
                name TEXT,
                slug TEXT,
                description TEXT,
                cover_image TEXT,
                is_paid INTEGER,
                price REAL,
                tags TEXT,
                public_id TEXT,
                created_at TEXT,
                created_by_user_id INTEGER,
                is_public INTEGER,
                status TEXT,
                has_custom_landing INTEGER,
                ranking_score REAL
            );
            CREATE TABLE PACK_ITEMS (
                pack_id INTEGER,
                is_active INTEGER,
                disable_at TEXT
            );
            CREATE TABLE ENTITLEMENTS (
                user_id INTEGER,
                asset_type TEXT,
                asset_id INTEGER,
                status TEXT,
                starts_at TEXT,
                expires_at TEXT
            );
            """
        )
        await conn.execute("INSERT INTO USERS (id, username) VALUES (1, 'creator')")
        await conn.execute(
            """
            INSERT INTO PACKS
            (id, name, slug, description, cover_image, is_paid, price, tags, public_id,
             created_at, created_by_user_id, is_public, status, has_custom_landing, ranking_score)
            VALUES (10, 'Paid Pack', 'paid-pack', 'Demo', NULL, 1, 9.99, '[]', 'packpub1',
                    '2026-06-04', 1, 1, 'published', 1, 10)
            """
        )
        await conn.execute(
            "INSERT INTO PACK_ITEMS (pack_id, is_active, disable_at) VALUES (10, 1, NULL)"
        )
        await conn.execute(
            """
            INSERT INTO ENTITLEMENTS
            (user_id, asset_type, asset_id, status, starts_at, expires_at)
            VALUES (7, 'pack', 10, 'active', NULL, NULL)
            """
        )
        await conn.commit()

        public_packs, total = await get_public_packs(conn, user_id=7)

        assert total == 1
        assert dict(public_packs[0])["user_has_access"] == 1
    finally:
        await conn.close()


def test_mobile_config_marks_ios_purchases_unavailable_by_default(monkeypatch):
    monkeypatch.setenv("PRIMARY_APP_DOMAIN", "example.com")
    monkeypatch.delenv("IOS_PURCHASES_ENABLED", raising=False)

    payload = mobile_config_payload(DummyRequest({"x-aurvek-client": "ios"}))

    assert payload["platform"]["is_ios_client"] is True
    assert payload["features"]["native_purchases"] is False
    assert payload["purchase_policy"]["ios_purchases_unavailable_reason"] == IOS_PURCHASE_DISABLED_REASON
    assert payload["auth"]["apple_sign_in_required_for_ios_review"] is True
    assert payload["auth"]["apple_sign_in_available"] is False
    assert payload["auth"]["apple_sign_in"]["missing_env"]
    assert payload["legal"]["privacy_policy_url"] == "https://example.com/privacy"
    assert payload["legal"]["terms_url"] == "https://example.com/terms"
    assert payload["legal"]["support_url"] == "https://example.com/support"


def test_purchase_metadata_hides_paid_items_for_ios_without_storekit(monkeypatch):
    monkeypatch.delenv("IOS_PURCHASES_ENABLED", raising=False)

    metadata = purchase_metadata_for_request(
        DummyRequest({"x-aurvek-client": "ios"}),
        is_paid=True,
        user_has_access=False,
        price=9.99,
    )

    assert metadata == {
        "purchase_available": False,
        "purchase_provider": None,
        "purchase_unavailable_reason": IOS_PURCHASE_DISABLED_REASON,
    }


@pytest.mark.asyncio
async def test_mobile_config_and_bootstrap_routes_return_stable_json(monkeypatch):
    monkeypatch.setenv("PRIMARY_APP_DOMAIN", "example.com")
    request = DummyRequest({"x-aurvek-client": "ios"})

    config_response = await mobile_routes.mobile_config(request)
    assert config_response.status_code == 200
    assert _json_body(config_response)["api_version"] == "mobile-v1"

    unauth_bootstrap = await mobile_routes.mobile_bootstrap(request, current_user=None)
    unauth_payload = _json_body(unauth_bootstrap)
    assert unauth_payload["session"] == {
        "authenticated": False,
        "expired": True,
        "reason": "unauthenticated",
    }
    assert unauth_payload["user"] is None

    auth_bootstrap = await mobile_routes.mobile_bootstrap(request, current_user=DummyUser())
    auth_payload = _json_body(auth_bootstrap)
    assert auth_payload["session"]["authenticated"] is True
    assert auth_payload["user"]["username"] == "ios-user"


@pytest.mark.asyncio
async def test_chat_api_unauthenticated_responses_are_json():
    request = DummyRequest()

    responses = [
        await conversations.get_conversations(request, current_user=None, user_id=1),
        await conversations.start_new_conversation(current_user=None),
        await messages.save_message(request, conversation_id=1, current_user=None),
        await warmup.warmup_conversation_context(request, conversation_id=1, current_user=None),
        await bookmarks.bookmark_message(conversation_id=1, request=request, current_user=None),
        await bookmarks.get_bookmarked_messages(request=request, current_user=None),
    ]

    for response in responses:
        assert response.status_code == 401
        assert _json_body(response)["error"] == "unauthenticated"


@pytest.mark.asyncio
async def test_legal_routes_serve_clean_urls(monkeypatch):
    monkeypatch.delenv("SUPPORT_URL", raising=False)
    monkeypatch.setenv("SUPPORT_EMAIL", "help@example.com")

    privacy = await legal_routes.privacy_page(DummyRequest(), None)
    terms = await legal_routes.terms_page(DummyRequest(), None)
    support = await legal_routes.support_page(DummyRequest(), None)

    assert privacy.status_code == 200
    assert terms.status_code == 200
    assert support.status_code == 200
    assert "help@example.com" in support.body.decode("utf-8")


@pytest.mark.asyncio
async def test_apple_sign_in_skeleton_reports_missing_configuration(monkeypatch):
    for name in (
        "APPLE_SIGN_IN_ENABLED",
        "APPLE_TEAM_ID",
        "APPLE_CLIENT_ID",
        "APPLE_KEY_ID",
        "APPLE_PRIVATE_KEY",
        "APPLE_PRIVATE_KEY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)

    status_response = await auth_apple.apple_sign_in_status()
    status = _json_body(status_response)["apple_sign_in"]
    assert status["enabled"] is False
    assert status["configured"] is False
    assert "APPLE_TEAM_ID" in status["missing_env"]

    callback_response = await auth_apple.apple_native_callback(DummyRequest())
    callback = _json_body(callback_response)
    assert callback_response.status_code == 503
    assert callback["error"] == "apple_sign_in_unavailable"


@pytest.mark.asyncio
async def test_content_report_endpoint_creates_report(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(
            """
            CREATE TABLE PROMPTS (
                id INTEGER PRIMARY KEY,
                public INTEGER,
                is_unlisted INTEGER,
                created_by_user_id INTEGER
            );
            CREATE TABLE PACKS (
                id INTEGER PRIMARY KEY,
                is_public INTEGER,
                status TEXT,
                created_by_user_id INTEGER
            );
            CREATE TABLE CONVERSATIONS (
                id INTEGER PRIMARY KEY,
                user_id INTEGER
            );
            CREATE TABLE MESSAGES (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER
            );
            INSERT INTO PROMPTS (id, public, is_unlisted, created_by_user_id)
            VALUES (12, 1, 0, 99);
            """
        )
        await conn.commit()
    finally:
        await conn.close()

    @asynccontextmanager
    async def test_db(readonly=False):
        test_conn = await aiosqlite.connect(db_path)
        test_conn.row_factory = aiosqlite.Row
        try:
            yield test_conn
        finally:
            await test_conn.close()

    monkeypatch.setattr(content_report_routes, "get_db_connection", test_db)

    payload = content_report_routes.ContentReportRequest(
        target_type="prompt",
        target_id=12,
        reason="spam",
        details="Looks like spam",
    )
    response = await content_report_routes.report_content(payload, current_user=DummyUser())

    assert response.status_code == 201
    body = _json_body(response)
    assert body["success"] is True
    assert body["status"] == "open"

    verify_conn = await aiosqlite.connect(db_path)
    verify_conn.row_factory = aiosqlite.Row
    try:
        row = await (await verify_conn.execute("SELECT * FROM CONTENT_REPORTS")).fetchone()
        assert row["reporter_user_id"] == DummyUser.id
        assert row["target_type"] == "prompt"
        assert row["target_id"] == 12
        assert row["target_owner_user_id"] == 99
        assert row["reason"] == "spam"
    finally:
        await verify_conn.close()


@pytest.mark.asyncio
async def test_ios_purchase_endpoints_return_storekit_required(monkeypatch):
    request = DummyRequest({"x-aurvek-client": "ios"})
    user = DummyUser()

    monkeypatch.setattr(checkout, "require_checkout_enabled", lambda translator=None: None)
    monkeypatch.setattr(packs, "require_checkout_enabled", lambda translator=None: None)
    monkeypatch.delenv("IOS_PURCHASES_ENABLED", raising=False)

    responses = [
        await checkout.api_purchase_prompt(prompt_id=1, request=request, current_user=user),
        await packs.api_purchase_pack(pack_id=1, request=request, current_user=user),
        await wallet.create_stripe_checkout_session(request=request, current_user=user),
    ]

    for response in responses:
        assert response.status_code == 409
        payload = _json_body(response)
        assert payload["error"] == IOS_PURCHASE_DISABLED_ERROR
        assert payload["reason"] == IOS_PURCHASE_DISABLED_REASON
        assert payload["purchase_available"] is False
