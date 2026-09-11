"""The delegated contract creates real native conversations with real entitlements."""
import base64
import hashlib
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from integrations.embed.models import AppConfig, EmbedError, InterviewBrief
from integrations.embed.store import EmbedStore


@pytest_asyncio.fixture
async def native_pilot(tmp_path, monkeypatch):
    import auth
    import database
    import models
    from marketplace.services.entitlements import grant_prompt_entitlement

    path = tmp_path / "native-pilot.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript((Path(__file__).parents[1] / "aurvek_schema.sql").read_text(encoding="utf-8"))
        connection.executescript("""
            INSERT INTO USER_ROLES(id,role_name) VALUES(1,'admin'),(3,'customer');
            INSERT INTO USERS(id,username,role_id,is_enabled) VALUES(1,'pilot-member',3,1),(2,'prompt-owner',1,1);
            INSERT INTO LLM(id,machine,model,enabled) VALUES(7,'GPT','app-model',1),(8,'GPT','native-model',1);
            INSERT INTO PROMPTS(id,name,prompt,forced_llm_id,created_by_user_id) VALUES
                (10,'Interview','Ask one question at a time.',7,2),(99,'Native prompt','Native prompt',8,2);
            INSERT INTO USER_DETAILS(user_id,llm_id,current_prompt_id,balance,authentication_mode) VALUES
                (1,8,99,10.0,'password_only'),(2,8,99,10.0,'password_only');
        """)

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(path, timeout=5) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys=ON")
            yield connection

    for module in (auth, database, models):
        monkeypatch.setattr(module, "get_db_connection", connect)

    async def not_revoked(_user_id):
        return False

    store = EmbedStore(connect, revoked_checker=not_revoked)
    await store.initialize()
    configs = {}
    for app_id in ("first", "second"):
        config = AppConfig(app_id=app_id, display_name=app_id, issuer="https://identity.example",
                           embed_origins=[f"https://chat.{app_id}.example"],
                           parent_origins=[f"https://{app_id}.example"],
                           redirect_uris=[f"https://{app_id}.example/callback"], prompt_id=10, enabled=True)
        await store.register_app(config, "secret" + "a" * 40)
        await store.provision_membership(app_id, 1)
        configs[app_id] = config
    async with connect() as connection:
        await grant_prompt_entitlement(connection, user_id=1, prompt_id=10, source="embed_pilot")
        await connection.commit()
    return SimpleNamespace(store=store, connect=connect, configs=configs)


async def _native_login(pilot, app_id="first"):
    from auth import get_user_by_id
    user = await get_user_by_id(1)
    assert not await user.is_admin
    user.session_expires_at = int(time.time()) + 3600
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    tx = await pilot.store.authorize_begin(app_id, pilot.configs[app_id].redirect_uris[0], "s" * 32, challenge)
    code = await pilot.store.authorize_complete(tx, user, "verified-native-source-session")
    response = await pilot.store.exchange_code(app_id, code["code"], verifier, code["redirect_uri"])
    return await pilot.store.inspect_access(app_id, response["delegated_credential"])


@pytest.mark.asyncio
async def test_full_schema_native_creator_keeps_user_preferences_and_selects_prompt_model(native_pilot):
    first = await _native_login(native_pilot)
    second = await _native_login(native_pilot, "second")
    brief = InterviewBrief(revision=1, language="es")
    first_state = await native_pilot.store.ensure_interview(first, "story", "idempotency-story-first", brief)
    repeated = await native_pilot.store.ensure_interview(first, "story", "idempotency-story-first", brief)
    second_state = await native_pilot.store.ensure_interview(second, "story", "idempotency-story-second", brief)
    assert first_state["conversation_id"] == repeated["conversation_id"]
    assert first_state["conversation_id"] != second_state["conversation_id"]
    async with native_pilot.connect() as connection:
        rows = await (await connection.execute("SELECT user_id,llm_id,role_id FROM CONVERSATIONS ORDER BY id")).fetchall()
        assert [tuple(row) for row in rows] == [(1, 7, 10), (1, 7, 10)]
        details = await (await connection.execute("SELECT llm_id,current_prompt_id,balance FROM USER_DETAILS WHERE user_id=1")).fetchone()
        assert tuple(details) == (8, 99, 10)
    with pytest.raises(EmbedError, match="not_found"):
        await native_pilot.store.authorize_conversation(first, second_state["conversation_id"], "story")


@pytest.mark.asyncio
async def test_ordinary_membership_does_not_replace_native_entitlement(native_pilot):
    principal = await _native_login(native_pilot)
    async with native_pilot.connect() as connection:
        await connection.execute("UPDATE ENTITLEMENTS SET status='revoked' WHERE user_id=1")
        await connection.commit()
    with pytest.raises(EmbedError, match="membership_inactive"):
        await native_pilot.store.ensure_interview(principal, "story", "idempotency-story-first", InterviewBrief(revision=1, language="es"))
    async with native_pilot.connect() as connection:
        count = await (await connection.execute("SELECT COUNT(*) FROM CONVERSATIONS")).fetchone()
        assert count[0] == 0
