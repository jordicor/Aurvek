import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

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
        "2026-08-15T00:00:00Z",
        817,
        "gpt-5.6-luna",
        "GPTSub",
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
        "last_activity": "2026-08-15T00:00:00Z",
        "llm_id": 817,
        "llm_model": "gpt-5.6-luna",
        "machine": "GPTSub",
        "external_bindings": None,
    }


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
async def test_message_send_requires_exact_model_identity_before_side_effects(monkeypatch):
    async def _unexpected_db(*_args, **_kwargs):
        raise AssertionError("identity-free send must stop before database work")

    monkeypatch.setattr(message_routes, "get_db_connection", _unexpected_db)

    response = await message_routes.save_message(
        request=SimpleNamespace(),
        conversation_id=2193,
        current_user=SimpleNamespace(id=1),
        expected_llm_id=None,
        multi_ai_models=None,
    )
    payload = json.loads(response.body)

    assert response.status_code == 428
    assert payload["error_code"] == "expected_llm_id_required"


@pytest.mark.asyncio
async def test_message_send_rejects_model_changed_in_another_tab(mock_db, monkeypatch):
    monkeypatch.setattr(message_routes, "get_db_connection", mock_db)

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


def test_browser_send_posts_exact_model_identity_and_runtime_rechecks_it():
    root = Path(__file__).resolve().parents[1]
    frontend = (root / "data" / "static" / "js" / "chat" / "chat.js").read_text(
        encoding="utf-8"
    )
    runtime = (root / "ai_runtime" / "messages.py").read_text(encoding="utf-8")

    assert "formData.append('expected_llm_id', String(expectedLlmId))" in frontend
    assert "expected_llm_id: Optional[int] = None" in runtime
    assert "int(conversation_llm_id) != int(expected_llm_id)" in runtime


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
        "GPT",
        "gpt-5.6-luna",
        0,
        0,
        0,
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
