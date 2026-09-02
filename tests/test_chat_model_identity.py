import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from fastapi.responses import JSONResponse

from ai_runtime.channel_turns import ChannelContext
from ai_runtime import messages as runtime_messages
from chat.routes import conversations as conversation_routes
from chat.routes import messages as message_routes
from chat.routes import pages
from integrations import platform_routes


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


async def _ensure_model_identity_schema(conn):
    cursor = await conn.execute("PRAGMA table_info(CONVERSATIONS)")
    conversation_columns = {row[1] for row in await cursor.fetchall()}
    if "llm_id" not in conversation_columns:
        await conn.execute("ALTER TABLE CONVERSATIONS ADD COLUMN llm_id INTEGER")

    cursor = await conn.execute("PRAGMA table_info(PROMPTS)")
    prompt_columns = {row[1] for row in await cursor.fetchall()}
    for column, definition in (
        ("forced_llm_id", "INTEGER"),
        ("hide_llm_name", "INTEGER DEFAULT 0"),
        ("allowed_llms", "TEXT"),
    ):
        if column not in prompt_columns:
            await conn.execute(f"ALTER TABLE PROMPTS ADD COLUMN {column} {definition}")


@pytest.mark.asyncio
async def test_conversation_details_preserve_exact_llm_identity(mock_db, monkeypatch):
    monkeypatch.setattr(pages, "get_db_connection", mock_db)

    async with mock_db() as conn:
        await _ensure_model_identity_schema(conn)
        await conn.execute(
            "INSERT INTO LLM (id, machine, model) VALUES (?, ?, ?)",
            (817, "GPTSub", "gpt-5.6-luna"),
        )
        await conn.execute(
            "INSERT INTO PROMPTS (id, name, prompt) VALUES (?, ?, ?)",
            (19, "AVA", "Test prompt"),
        )
        await conn.execute(
            """
            INSERT INTO CONVERSATIONS (id, user_id, role_id, llm_id)
            VALUES (?, ?, ?, ?)
            """,
            (2193, 1, 19, 817),
        )
        await conn.commit()

    response = await pages.get_conversation_details(
        2193,
        current_user=SimpleNamespace(id=1),
    )
    payload = json.loads(response.body)

    assert payload["llm_id"] == 817
    assert payload["model"] == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_only_if_empty_never_changes_chat_with_messages(mock_db, monkeypatch):
    monkeypatch.setattr(pages, "get_db_connection", mock_db)

    async with mock_db() as conn:
        await _ensure_model_identity_schema(conn)
        await conn.executemany(
            "INSERT INTO LLM (id, machine, model) VALUES (?, ?, ?)",
            (
                (605, "GPT", "gpt-5.6-luna"),
                (817, "GPTSub", "gpt-5.6-luna"),
            ),
        )
        await conn.execute(
            """
            INSERT INTO CONVERSATIONS (id, user_id, llm_id)
            VALUES (?, ?, ?)
            """,
            (2193, 1, 605),
        )
        await conn.execute(
            """
            INSERT INTO MESSAGES (conversation_id, user_id, message, type)
            VALUES (?, ?, ?, ?)
            """,
            (2193, 1, "already sent", "user"),
        )
        await conn.commit()

    response = await pages.update_conversation_model(
        2193,
        _JsonRequest({"llm_id": 817, "only_if_empty": True}),
        current_user=SimpleNamespace(id=1),
    )
    payload = json.loads(response.body)

    assert payload == {"success": True, "updated": False}
    async with mock_db() as conn:
        cursor = await conn.execute(
            "SELECT llm_id FROM CONVERSATIONS WHERE id = ?",
            (2193,),
        )
        assert (await cursor.fetchone())[0] == 605


@pytest.mark.asyncio
async def test_external_platform_payload_preserves_exact_model_identity(monkeypatch):
    conversation = (
        2193,
        1,
        "2026-08-15T00:00:00Z",
        "External chat",
        "whatsapp",
        1,
        "gpt-5.6-luna",
        1,
        1,
        605,
        1,
        "[605,817]",
        1,
        "2026-08-15T00:00:00Z",
        817,
        "GPTSub",
        33,
        7,
    )

    class _Cursor:
        def __init__(self):
            self._fetch = "many"

        async def execute(self, _statement, _params=()):
            self._fetch = "one" if self._fetch == "many" else self._fetch

        async def fetchall(self):
            return [conversation]

        async def fetchone(self):
            return conversation

    class _Connection:
        async def cursor(self):
            return _Cursor()

    @asynccontextmanager
    async def _db_connection(*_args, **_kwargs):
        yield _Connection()

    async def _false(**_kwargs):
        return False

    async def _set_external(*_args, **_kwargs):
        return {"success": True}

    async def _noop(*_args, **_kwargs):
        return None

    async def _bindings(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(platform_routes, "get_db_connection", _db_connection)
    monkeypatch.setattr(
        platform_routes,
        "conversation_has_external_device_bindings",
        _false,
    )
    monkeypatch.setattr(platform_routes, "set_external_conversation", _set_external)
    monkeypatch.setattr(platform_routes, "ensure_conversation_privacy_schema", _noop)
    monkeypatch.setattr(
        platform_routes,
        "get_conversation_binding_summaries",
        _bindings,
    )
    monkeypatch.setattr(platform_routes, "validate_platform", lambda _value: True)

    response = await platform_routes.update_external_platform(
        2193,
        {"platform": "whatsapp", "action": "add", "visible_count": 10},
        current_user=SimpleNamespace(id=1),
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    assert len(payload["updatedConversations"]) == 1
    assert payload["updatedConversations"][0] == {
        "id": 2193,
        "user_id": 1,
        "start_date": "2026-08-15T00:00:00Z",
        "chat_name": "External chat",
        "external_platform": "whatsapp",
        "external_channels": ["whatsapp"],
        "phone_binding": None,
        "locked": True,
        "llm_model": "gpt-5.6-luna",
        "web_search_allowed": False,
        "web_search_forced": True,
        "forced_llm_id": 605,
        "hide_llm_name": True,
        "allowed_llms": [605, 817],
        "is_paid": True,
        "last_activity": "2026-08-15T00:00:00Z",
        "llm_id": 817,
        "machine": "GPTSub",
        "prompt_id": 33,
        "folder_id": 7,
        "external_bindings": None,
    }


def test_external_platform_id_comparison_accepts_legacy_string_ids():
    assert platform_routes._conversation_ids_match("2193", 2193) is True
    assert platform_routes._conversation_ids_match("not-an-id", 2193) is False


@pytest.mark.asyncio
async def test_stale_external_conversation_id_cannot_expose_another_user(monkeypatch):
    foreign_conversation = (
        999,
        2,
        "2026-08-15T00:00:00Z",
        "Foreign chat",
        "whatsapp",
        0,
        "gpt-5.6-luna",
        0,
        0,
        None,
        0,
        None,
        0,
        "2026-08-15T00:00:00Z",
        817,
    )

    class _Cursor:
        def __init__(self):
            self.statement = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, statement, _params=()):
            self.statement = statement

        async def fetchone(self):
            if "json_extract" in self.statement:
                return {"whatsapp_conv_id": 999, "telegram_conv_id": None}
            if "WHERE c.id = ?" in self.statement:
                # Model a corrupt/stale pointer: the row is visible by id alone,
                # but must disappear when the owner predicate is present.
                if "c.user_id = ?" in self.statement:
                    return None
                return foreign_conversation
            return None

        async def fetchall(self):
            return []

    class _Connection:
        def cursor(self):
            return _Cursor()

    @asynccontextmanager
    async def _db_connection(*_args, **_kwargs):
        yield _Connection()

    async def _noop(*_args, **_kwargs):
        return None

    async def _bindings(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(
        conversation_routes,
        "get_db_connection",
        _db_connection,
    )
    monkeypatch.setattr(
        conversation_routes,
        "ensure_conversation_privacy_schema",
        _noop,
    )
    monkeypatch.setattr(
        conversation_routes,
        "get_conversation_binding_summaries",
        _bindings,
    )

    response = await conversation_routes.get_conversations(
        request=SimpleNamespace(),
        current_user=SimpleNamespace(id=1),
        user_id=1,
        before_activity=None,
        before_id=None,
        limit=25,
        folder_id=None,
    )

    assert json.loads(response.body) == []


def test_branch_payload_keeps_exact_llm_identity():
    source = (
        Path(__file__).resolve().parents[1] / "chat" / "routes" / "branching.py"
    ).read_text(encoding="utf-8")

    assert source.count('"llm_id": source_llm_id') == 2


@pytest.mark.asyncio
async def test_message_send_requires_exact_model_identity_for_non_phone_inference(monkeypatch):
    class IdentityRow:
        values = [0, 605, "GPT", "gpt-5.6-luna", "{}"]
        names = {"model": "gpt-5.6-luna"}

        def __getitem__(self, key):
            return self.names[key] if isinstance(key, str) else self.values[key]

    class Cursor:
        async def fetchone(self):
            return IdentityRow()

    class Connection:
        async def execute(self, *_args, **_kwargs):
            return Cursor()

    @asynccontextmanager
    async def db_connection(*_args, **_kwargs):
        yield Connection()

    async def capture_non_phone(**_kwargs):
        return SimpleNamespace(
            decision=SimpleNamespace(phone_active=False),
            context=ChannelContext(channel="web", persistence="immediate"),
        )

    async def no_guard_response(**_kwargs):
        return None

    monkeypatch.setattr(message_routes, "get_db_connection", db_connection)
    monkeypatch.setattr(message_routes, "validate_message_request", no_guard_response)
    monkeypatch.setattr(
        message_routes,
        "capture_non_phone_channel_turn",
        capture_non_phone,
    )

    response = await message_routes.save_message(
        request=SimpleNamespace(),
        conversation_id=2193,
        current_user=SimpleNamespace(id=1),
        expected_llm_id=None,
        multi_ai_models=None,
        attachment_refs=None,
    )
    payload = json.loads(response.body)

    assert response.status_code == 428
    assert payload["error_code"] == "expected_llm_id_required"


@pytest.mark.asyncio
async def test_message_send_rejects_model_changed_in_another_tab(mock_db, monkeypatch):
    monkeypatch.setattr(message_routes, "get_db_connection", mock_db)

    async def capture_non_phone(**_kwargs):
        return SimpleNamespace(
            decision=SimpleNamespace(phone_active=False),
            context=ChannelContext(channel="web", persistence="immediate"),
        )

    async def no_guard_response(**_kwargs):
        return None

    monkeypatch.setattr(message_routes, "validate_message_request", no_guard_response)
    monkeypatch.setattr(
        message_routes,
        "capture_non_phone_channel_turn",
        capture_non_phone,
    )

    async with mock_db() as conn:
        await _ensure_model_identity_schema(conn)
        await conn.execute(
            "INSERT INTO LLM (id, machine, model) VALUES (?, ?, ?)",
            (605, "GPT", "gpt-5.6-luna"),
        )
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id, llm_id, locked) VALUES (?, ?, ?, ?)",
            (2193, 1, 605, 0),
        )
        await conn.commit()

    response = await message_routes.save_message(
        request=SimpleNamespace(),
        conversation_id=2193,
        current_user=SimpleNamespace(id=1),
        expected_llm_id=817,
        multi_ai_models=None,
        attachment_refs=None,
    )
    payload = json.loads(response.body)

    assert response.status_code == 409
    assert payload == {
        "success": False,
        "error_code": "conversation_model_changed",
        "message": "The AI model changed in another session. Review it and send again.",
        "llm_id": 605,
        "model": "gpt-5.6-luna",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_llm_id", [None, 817])
async def test_phone_active_web_turn_skips_inference_credentials_and_reasoning_gate(
    monkeypatch,
    expected_llm_id,
):
    class IdentityRow:
        values = [0, 605, "GPT", "gpt-5.6-luna", "{}"]
        names = {
            "machine": "GPT",
            "model": "gpt-5.6-luna",
            "capabilities_json": "{}",
        }

        def __getitem__(self, key):
            return self.names[key] if isinstance(key, str) else self.values[key]

    class Cursor:
        async def fetchone(self):
            return IdentityRow()

    class Connection:
        async def execute(self, *_args, **_kwargs):
            return Cursor()

    @asynccontextmanager
    async def db_connection(*_args, **_kwargs):
        yield Connection()

    async def no_guard_response(**_kwargs):
        return None

    async def capture_phone_turn(**_kwargs):
        return SimpleNamespace(
            decision=SimpleNamespace(phone_active=True),
            context=ChannelContext(channel="web", persistence="ingest_only"),
        )

    async def process_message(**kwargs):
        assert kwargs["channel_context"].ingest_only is True
        assert kwargs["user_api_keys"] is None
        return JSONResponse({"accepted": True})

    async def passthrough(_user_id, response_awaitable):
        return await response_awaitable

    async def inference_gate_must_not_run(*_args, **_kwargs):
        raise AssertionError("phone-active ingest must not run inference gates")

    monkeypatch.setattr(message_routes, "get_db_connection", db_connection)
    monkeypatch.setattr(message_routes, "validate_message_request", no_guard_response)
    monkeypatch.setattr(
        message_routes,
        "capture_non_phone_channel_turn",
        capture_phone_turn,
    )
    monkeypatch.setattr(message_routes, "process_save_message", process_message)
    monkeypatch.setattr(
        message_routes,
        "serialize_user_billing_response",
        passthrough,
    )
    monkeypatch.setattr(
        message_routes,
        "_request_reasoning_capabilities",
        inference_gate_must_not_run,
    )
    monkeypatch.setattr(
        message_routes,
        "get_user_api_key_mode",
        inference_gate_must_not_run,
    )

    response = await message_routes.save_message(
        request=SimpleNamespace(headers={}),
        conversation_id=2193,
        current_user=SimpleNamespace(id=1, can_send_files=True),
        text_plain="queue me",
        text_compressed=None,
        file=None,
        attachment_refs=None,
        full_response=False,
        is_whatsapp=False,
        expected_llm_id=expected_llm_id,
        multi_ai_models="not-json",
        reasoning_mode="unsupported-during-inference",
        reasoning_budget_tokens=None,
        thinking_budget_tokens=None,
        pdf_page_start=None,
        pdf_page_end=None,
        pdf_retry_token=None,
    )

    assert json.loads(response.body) == {"accepted": True}


def test_browser_send_posts_exact_model_identity_and_runtime_rechecks_it():
    root = Path(__file__).resolve().parents[1]
    frontend = (root / "data" / "static" / "js" / "chat" / "chat.js").read_text(
        encoding="utf-8"
    )
    runtime = (root / "ai_runtime" / "messages.py").read_text(encoding="utf-8")

    assert "formData.append('expected_llm_id', String(expectedLlmId))" in frontend
    assert "expected_llm_id: Optional[int] = None" in runtime
    assert "conversation_model_fence_id != int(expected_llm_id)" in runtime


@pytest.mark.asyncio
async def test_non_phone_turn_cannot_supply_runtime_model_override():
    response = await runtime_messages.process_save_message(
        request=None,
        conversation_id=2193,
        current_user=SimpleNamespace(id=1, can_send_files=True),
        text_plain="hello",
        prevalidated=True,
        runtime_llm_id=817,
        channel_context=ChannelContext(channel="web", persistence="immediate"),
    )
    payload = json.loads(response.body)

    assert response.status_code == 403
    assert payload["error_code"] == "runtime_model_override_forbidden"


@pytest.mark.asyncio
async def test_runtime_model_cas_stops_before_provider_or_persistence(monkeypatch):
    row = (
        0,  # locked
        605,  # llm_id currently stored
        1,  # owner
        "Exact identity",
        None,  # effective prompt
        None,  # extension
        0,  # last message
        "GPTSub",
        "gpt-5.6-luna",
        0,
        0,
        0,
        "{}",  # capabilities_json
        0,
        0,
        0,
        0,
    )

    class _Cursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def fetchone(self):
            return row

    class _Connection:
        def execute(self, _statement, _params=()):
            return _Cursor()

    @asynccontextmanager
    async def _db_connection(*_args, **_kwargs):
        yield _Connection()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime_messages, "get_db_connection", _db_connection)
    monkeypatch.setattr(
        runtime_messages,
        "ensure_conversation_privacy_schema",
        _noop,
    )

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/conversations/2193/messages",
        "headers": [],
        "query_string": b"",
        "server": ("aurvek.test", 443),
        "scheme": "https",
    })
    response = await runtime_messages.process_save_message(
        request=request,
        conversation_id=2193,
        current_user=SimpleNamespace(id=1, can_send_files=True),
        text_plain="must not reach a provider",
        prevalidated=True,
        expected_llm_id=817,
    )
    payload = json.loads(response.body)

    assert response.status_code == 409
    assert payload["error_code"] == "conversation_model_changed"
    assert payload["llm_id"] == 605

    def inference_gate_must_not_run(*_args, **_kwargs):
        raise AssertionError("ingest-only runtime must not validate reasoning")

    monkeypatch.setattr(
        runtime_messages,
        "resolve_and_validate",
        inference_gate_must_not_run,
    )
    ingest_response = await runtime_messages.process_save_message(
        request=request,
        conversation_id=2193,
        current_user=SimpleNamespace(id=1, can_send_files=True),
        text_plain="persist despite stale rendered model",
        files=[{
            "data": b"x",
            "content_type": "text/plain",
            "filename": "still-validated.txt",
        }],
        prevalidated=True,
        expected_llm_id=817,
        channel_context=ChannelContext(channel="web", persistence="ingest_only"),
    )
    ingest_payload = json.loads(ingest_response.body)

    assert ingest_response.status_code == 400
    assert "subscription models currently support text messages only" in ingest_payload["message"]
