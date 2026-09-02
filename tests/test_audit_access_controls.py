import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.sessions import SessionMiddleware

import app as app_module
import prompts
from billing.usage_reservations import InsufficientBalanceError
from chat.routes import conversations as conversation_routes
from chat.routes import voice_io
from common import ALGORITHM, SECRET_KEY, generate_user_hash
from integrations import media
from save_images import generate_img_token
import request_security
from request_security import ensure_csrf_token


def _user_path(username: str, suffix: str) -> str:
    prefix1, prefix2, user_hash = generate_user_hash(username)
    return f"users/{prefix1}/{prefix2}/{user_hash}/{suffix}"


@pytest.mark.asyncio
async def test_auth_image_token_is_bound_to_the_exact_path():
    user = SimpleNamespace(username="image_owner_a")
    path_a = _user_path(user.username, "profile/a_128.webp")
    path_b = _user_path("image_owner_b", "profile/b_128.webp")
    token = generate_img_token(
        path_a,
        datetime.now(timezone.utc) + timedelta(minutes=5),
        user,
    )

    response = await app_module.auth_image(None, token=token, request_uri=f"/{path_a}")
    assert response.status_code == 200

    with pytest.raises(HTTPException) as denied:
        await app_module.auth_image(None, token=token, request_uri=f"/{path_b}")
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_legacy_image_token_cannot_cross_user_prefixes():
    username = "legacy_image_owner"
    own_path = _user_path(username, "profile/own_128.webp")
    other_path = _user_path("different_image_owner", "profile/other_128.webp")
    token = jwt.encode(
        {
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            "username": username,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

    response = await app_module.auth_image(None, token=token, request_uri=f"/{own_path}")
    assert response.status_code == 200

    with pytest.raises(HTTPException) as denied:
        await app_module.auth_image(None, token=token, request_uri=f"/{other_path}")
    assert denied.value.status_code == 403


class _WelcomeConnection:
    async def cursor(self):
        return object()


@pytest.mark.asyncio
async def test_welcome_assets_require_the_same_prompt_access(monkeypatch, tmp_path):
    @asynccontextmanager
    async def get_connection(readonly=False):
        yield _WelcomeConnection()

    world_dir = tmp_path / "prompt-world"
    static_dir = world_dir / "welcome" / "static"
    static_dir.mkdir(parents=True)
    asset = static_dir / "style.css"
    asset.write_text("body {}", encoding="utf-8")
    sibling_dir = world_dir / "welcome" / "static-private"
    sibling_dir.mkdir()
    (sibling_dir / "secret.txt").write_text("private", encoding="utf-8")

    access = AsyncMock(side_effect=lambda user, entity_id, cursor: user.id == 1)
    monkeypatch.setattr(app_module, "get_db_connection", get_connection)
    monkeypatch.setattr(app_module, "can_user_access_prompt", access)
    monkeypatch.setattr(
        app_module,
        "get_prompt_info",
        AsyncMock(return_value={"name": "World", "created_by_username": "owner"}),
    )
    monkeypatch.setattr(app_module, "get_prompt_path", lambda entity_id, info: str(world_dir))

    owner_response = await app_module.serve_welcome_static_scoped(
        "p7",
        "style.css",
        SimpleNamespace(),
        SimpleNamespace(id=1),
    )
    assert isinstance(owner_response, FileResponse)
    assert str(owner_response.path) == str(asset)

    with pytest.raises(HTTPException) as stranger:
        await app_module.serve_welcome_static_scoped(
            "p7",
            "style.css",
            SimpleNamespace(),
            SimpleNamespace(id=2),
        )
    with pytest.raises(HTTPException) as traversal:
        await app_module.serve_welcome_static_scoped(
            "p7",
            "../static-private/secret.txt",
            SimpleNamespace(),
            SimpleNamespace(id=1),
        )
    with pytest.raises(HTTPException) as missing:
        await app_module.serve_welcome_static_scoped(
            "p999",
            "style.css",
            SimpleNamespace(),
            SimpleNamespace(id=2),
        )

    assert (stranger.value.status_code, stranger.value.detail) == (
        missing.value.status_code,
        missing.value.detail,
    ) == (404, "Welcome resource not found")
    assert traversal.value.status_code == 403


@pytest.mark.asyncio
async def test_welcome_pack_assets_use_pack_access_helper(monkeypatch):
    @asynccontextmanager
    async def get_connection(readonly=False):
        yield _WelcomeConnection()

    pack_access = AsyncMock(return_value=False)
    monkeypatch.setattr(app_module, "get_db_connection", get_connection)
    monkeypatch.setattr(app_module, "can_user_access_pack", pack_access)
    monkeypatch.setattr(
        app_module,
        "can_user_access_prompt",
        AsyncMock(side_effect=AssertionError("prompt access helper should not be used")),
    )

    with pytest.raises(HTTPException) as denied:
        await app_module.serve_welcome_static_scoped(
            "k8",
            "image.webp",
            SimpleNamespace(),
            SimpleNamespace(id=2),
        )

    assert denied.value.status_code == 404
    pack_access.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcribe_preserves_bad_request_http_exception():
    with pytest.raises(HTTPException) as error:
        await voice_io.transcribe(
            SimpleNamespace(headers={}),
            audio=None,
            user_id=1,
        )
    assert error.value.status_code == 400
    assert error.value.detail == "No audio or media URL provided"


@pytest.mark.asyncio
async def test_transcribe_preserves_insufficient_balance(monkeypatch):
    class AudioUpload:
        async def read(self):
            return b"audio"

    monkeypatch.setattr(voice_io, "get_browser", lambda user_agent: "chrome")
    monkeypatch.setattr(
        voice_io.AudioSegment,
        "from_file",
        lambda *args, **kwargs: SimpleNamespace(duration_seconds=60),
    )
    monkeypatch.setattr(
        media,
        "reserve_fixed_usage",
        AsyncMock(side_effect=InsufficientBalanceError("Insufficient balance")),
    )

    with pytest.raises(HTTPException) as error:
        await voice_io.transcribe(
            SimpleNamespace(headers={"user-agent": "test"}),
            audio=AudioUpload(),
            user_id=1,
        )
    assert error.value.status_code == 402
    assert error.value.detail == "Insufficient balance"


@pytest_asyncio.fixture
async def conversation_db(tmp_path):
    db_path = tmp_path / "conversation_access.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE CONVERSATIONS (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL
            );
            CREATE TABLE MESSAGES (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                date TEXT NOT NULL
            );
            INSERT INTO CONVERSATIONS (id, user_id) VALUES (10, 1);
            INSERT INTO MESSAGES (id, conversation_id, date)
            VALUES (100, 10, '2026-01-01'), (101, 10, '2026-01-02');
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def get_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    return get_connection


@pytest.mark.asyncio
async def test_conversation_metadata_is_owner_scoped_and_uniform(
    monkeypatch,
    conversation_db,
):
    monkeypatch.setattr(conversation_routes, "get_db_connection", conversation_db)
    monkeypatch.setattr(conversation_routes, "is_admin", AsyncMock(return_value=False))
    owner = SimpleNamespace(id=1)
    stranger = SimpleNamespace(id=2)

    assert await conversation_routes.get_last_message_id(10, owner) == {
        "message_id": 101
    }
    status = await conversation_routes.conversation_status(10, owner)
    assert json.loads(status.body) == {"isActive": True}

    errors = []
    for conversation_id, user in ((10, stranger), (999, stranger)):
        with pytest.raises(HTTPException) as last_message_error:
            await conversation_routes.get_last_message_id(conversation_id, user)
        with pytest.raises(HTTPException) as status_error:
            await conversation_routes.conversation_status(conversation_id, user)
        errors.extend([last_message_error.value, status_error.value])

    assert {
        (error.status_code, error.detail)
        for error in errors
    } == {(404, "Conversation not found")}


class _PromptCursor:
    def __init__(self, connection, sql=None, params=()):
        self.connection = connection
        self.row = None
        self.rows = []
        self.rowcount = -1
        if sql is not None:
            self._set_query(sql, params)

    def _set_query(self, sql, params):
        normalized = " ".join(sql.split())
        self.connection.queries.append((normalized, params))
        self.row = None
        self.rows = []
        lower = normalized.lower()
        if "select role_name from user_roles" in lower:
            self.row = ("user",)
        elif "select user_id from prompt_permissions" in lower and "permission_level = 'owner'" in lower:
            self.row = (1,)
        elif "select public_id from prompts" in lower:
            self.row = ("public-id",)
        elif "select p.voice_id as configured_voice_id" in lower:
            self.row = {
                "configured_voice_id": self.connection.current_voice_id,
                "default_voice_id": self.connection.default_voice_id,
            }
        elif "select v.id,v.voice_code,v.tts_service" in lower and "where v.id" in lower:
            self.row = {
                "id": params[0],
                "voice_code": self.connection.catalog_voice_code,
                "tts_service": self.connection.catalog_tts_service,
                "deprecated": self.connection.catalog_deprecated,
                "service_name": self.connection.catalog_service_name,
            }
        elif "select v.id,v.voice_code,v.tts_service" in lower and "where v.voice_code" in lower:
            self.rows = list(self.connection.legacy_voice_rows)
        elif "select id from voices" in lower:
            self.row = {"id": 5}
        elif "select pack_notice_period_days, allow_in_packs" in lower:
            self.row = (0, 0)
        elif lower.startswith("update prompts set"):
            if self.connection.prompt_update_rowcount is not None:
                self.rowcount = self.connection.prompt_update_rowcount
            else:
                submitted_voice_id = params[3]
                phone_revision_exists = (
                    self.connection.active_audio_revision
                    or self.connection.pending_audio_revision
                )
                self.rowcount = int(
                    submitted_voice_id == self.connection.current_voice_id
                    or not phone_revision_exists
                )
        elif "select 1 from prompts where id" in lower:
            self.row = (1,)
        else:
            self.row = None

    async def execute(self, sql, params=()):
        self._set_query(sql, params)
        return self

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PromptConnection:
    def __init__(
        self,
        *,
        prompt_update_rowcount=None,
        current_voice_id=5,
        default_voice_id=5,
        catalog_voice_code="voice-code",
        catalog_tts_service=10,
        catalog_deprecated=False,
        catalog_service_name="TTS-FUTURE",
        legacy_voice_rows=(
            {
                "id": 5,
                "voice_code": "voice-code",
                "tts_service": 10,
                "deprecated": 0,
                "service_name": "TTS-FUTURE",
            },
        ),
        active_audio_revision=False,
        pending_audio_revision=False,
    ):
        self.queries = []
        self.commits = 0
        self.prompt_update_rowcount = prompt_update_rowcount
        self.current_voice_id = current_voice_id
        self.default_voice_id = default_voice_id
        self.catalog_voice_code = catalog_voice_code
        self.catalog_tts_service = catalog_tts_service
        self.catalog_deprecated = catalog_deprecated
        self.catalog_service_name = catalog_service_name
        self.legacy_voice_rows = legacy_voice_rows
        self.active_audio_revision = active_audio_revision
        self.pending_audio_revision = pending_audio_revision

    def execute(self, sql, params=()):
        return _PromptCursor(self, sql, params)

    def cursor(self):
        return _PromptCursor(self)

    async def commit(self):
        self.commits += 1


async def _save_prompt_as_editor(
    monkeypatch,
    can_manage=True,
    prompt_update_rowcount=None,
    current_voice_id=5,
    default_voice_id=5,
    sample_voice_catalog_id=5,
    catalog_voice_code="voice-code",
    catalog_tts_service=10,
    catalog_deprecated=False,
    catalog_service_name="TTS-FUTURE",
    legacy_voice_rows=(
        {
            "id": 5,
            "voice_code": "voice-code",
            "tts_service": 10,
            "deprecated": 0,
            "service_name": "TTS-FUTURE",
        },
    ),
    active_audio_revision=False,
    pending_audio_revision=False,
):
    connection = _PromptConnection(
        prompt_update_rowcount=prompt_update_rowcount,
        current_voice_id=current_voice_id,
        default_voice_id=default_voice_id,
        catalog_voice_code=catalog_voice_code,
        catalog_tts_service=catalog_tts_service,
        catalog_deprecated=catalog_deprecated,
        catalog_service_name=catalog_service_name,
        legacy_voice_rows=legacy_voice_rows,
        active_audio_revision=active_audio_revision,
        pending_audio_revision=pending_audio_revision,
    )

    @asynccontextmanager
    async def get_connection(readonly=False):
        yield connection

    monkeypatch.setattr(prompts, "get_db_connection", get_connection)
    monkeypatch.setattr(
        prompts,
        "get_prompt_info",
        AsyncMock(return_value={"name": "Original"}),
    )
    monkeypatch.setattr(
        prompts,
        "can_manage_prompt",
        AsyncMock(return_value=can_manage),
    )
    monkeypatch.setattr(prompts, "invalidate_landing_cache", Mock())
    monkeypatch.setattr(
        request_security,
        "validate_mutation_request",
        Mock(return_value=None),
    )

    response = await prompts.update_prompt(
        request=SimpleNamespace(),
        prompt_id=7,
        current_user=SimpleNamespace(id=2, role_id=2),
        csrf_token="test-csrf-token",
        name="Edited",
        prompt="Edited prompt",
        description="Edited description",
        sample_voice_id="voice-code",
        sample_voice_catalog_id=sample_voice_catalog_id,
        public=False,
        image=None,
        editor_ids=None,
        new_owner_id=None,
        category_ids="[]",
        is_paid=0,
        markup_per_mtokens=0.0,
        llm_mode="any",
        forced_llm_id=None,
        hide_llm_name=False,
        allowed_llms=None,
        disable_web_search=False,
        force_web_search=False,
        enable_moderation=False,
        watchdog_config=None,
        allow_in_packs=False,
        pack_notice_period_days=0,
        extensions_enabled=False,
        extensions_auto_advance=False,
        extensions_free_selection=True,
        purchase_price=None,
        gransabio_enabled=False,
        gransabio_config=None,
    )
    return response, connection


@pytest.mark.asyncio
async def test_prompt_editor_can_save_without_replacing_permissions(monkeypatch):
    response, connection = await _save_prompt_as_editor(monkeypatch)
    queries = [query.lower() for query, _ in connection.queries]

    assert response.status_code == 303
    assert any(query.startswith("update prompts set") for query in queries)
    assert any(
        "active_audio_revision is not null" in query
        for query in queries
        if query.startswith("update prompts set")
    )
    assert any(
        "pending_audio_revision is not null" in query
        for query in queries
        if query.startswith("update prompts set")
    )
    assert any(
        "voice_id is ? or not exists" in query
        for query in queries
        if query.startswith("update prompts set")
    )
    assert not any(
        query.startswith("delete from prompt_permissions")
        for query in queries
    )
    assert not any(
        query.startswith("update prompt_permissions set user_id")
        for query in queries
    )


@pytest.mark.asyncio
async def test_user_without_prompt_permission_cannot_save(monkeypatch):
    with pytest.raises(HTTPException) as denied:
        await _save_prompt_as_editor(monkeypatch, can_manage=False)
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_prompt_editor_can_save_other_fields_while_audio_is_pending(
    monkeypatch,
):
    response, _connection = await _save_prompt_as_editor(
        monkeypatch,
        current_voice_id=5,
        pending_audio_revision=True,
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_prompt_editor_preserves_inherited_default_with_active_phone_audio(
    monkeypatch,
):
    response, connection = await _save_prompt_as_editor(
        monkeypatch,
        current_voice_id=None,
        default_voice_id=5,
        active_audio_revision=True,
    )
    assert response.status_code == 303
    update = next(
        params
        for query, params in connection.queries
        if query.lower().startswith("update prompts set")
    )
    assert update[3] is None
    assert update[-1] is None


@pytest.mark.asyncio
async def test_inherited_prompt_rejects_distinct_voice_without_atomic_activation(
    monkeypatch,
):
    with pytest.raises(HTTPException) as conflict:
        await _save_prompt_as_editor(
            monkeypatch,
            current_voice_id=None,
            default_voice_id=4,
            active_audio_revision=True,
        )
    assert conflict.value.status_code == 409
    assert "atomic phone-audio activation" in conflict.value.detail


@pytest.mark.asyncio
async def test_prompt_update_requires_exact_identity_for_ambiguous_voice_code(
    monkeypatch,
):
    with pytest.raises(HTTPException) as ambiguous:
        await _save_prompt_as_editor(
            monkeypatch,
            sample_voice_catalog_id=None,
            legacy_voice_rows=({"id": 5}, {"id": 6}),
        )
    assert ambiguous.value.status_code == 409
    assert "ambiguous" in ambiguous.value.detail

    with pytest.raises(HTTPException) as mismatch:
        await _save_prompt_as_editor(
            monkeypatch,
            sample_voice_catalog_id=5,
            catalog_voice_code="other-code",
        )
    assert mismatch.value.status_code == 409
    assert "identity" in mismatch.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_voice",
    (
        {"catalog_deprecated": True},
        {"catalog_tts_service": None},
        {"catalog_service_name": ""},
    ),
)
async def test_prompt_update_rejects_structurally_invalid_voice(
    monkeypatch,
    invalid_voice,
):
    with pytest.raises(HTTPException) as unavailable:
        await _save_prompt_as_editor(monkeypatch, **invalid_voice)
    assert unavailable.value.status_code == 409
    assert "unavailable" in unavailable.value.detail


@pytest.mark.asyncio
async def test_real_prompt_update_keeps_null_voice_with_active_phone_audio(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "inherited-prompt.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE USER_ROLES(id INTEGER PRIMARY KEY,role_name TEXT);
            CREATE TABLE SERVICES(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE VOICES(
                id INTEGER PRIMARY KEY,voice_code TEXT,is_default INTEGER DEFAULT 0,
                tts_service INTEGER,deprecated INTEGER DEFAULT 0
            );
            CREATE TABLE PROMPTS(
                id INTEGER PRIMARY KEY,name TEXT,prompt TEXT,description TEXT,
                voice_id INTEGER,public INTEGER,is_paid INTEGER,
                markup_per_mtokens REAL,forced_llm_id INTEGER,
                forced_reasoning_json TEXT,phone_llm_id INTEGER,
                phone_reasoning_json TEXT,phone_realtime_voice TEXT,
                hide_llm_name INTEGER,disable_web_search INTEGER,
                force_web_search INTEGER,enable_moderation INTEGER,
                watchdog_config TEXT,allow_in_packs INTEGER,
                pack_notice_period_days INTEGER,allowed_llms TEXT,
                extensions_enabled INTEGER,extensions_auto_advance INTEGER,
                extensions_free_selection INTEGER,purchase_price REAL,
                gransabio_enabled INTEGER,gransabio_config TEXT,public_id TEXT
            );
            CREATE TABLE PROMPT_PERMISSIONS(
                prompt_id INTEGER,user_id INTEGER,permission_level TEXT
            );
            CREATE TABLE PROMPT_PHONE_SETTINGS(
                prompt_id INTEGER PRIMARY KEY,active_audio_revision INTEGER,
                pending_audio_revision INTEGER
            );
            CREATE TABLE CONVERSATIONS(
                id INTEGER PRIMARY KEY,role_id INTEGER,active_extension_id INTEGER
            );
            CREATE TABLE PROMPT_CATEGORIES(
                prompt_id INTEGER,category_id INTEGER
            );
            INSERT INTO USER_ROLES VALUES(2,'user');
            INSERT INTO SERVICES VALUES(10,'TTS-FUTURE');
            INSERT INTO VOICES VALUES(5,'voice-code',1,10,0);
            INSERT INTO PROMPTS(
                id,name,prompt,description,voice_id,public,is_paid,
                markup_per_mtokens,hide_llm_name,disable_web_search,
                force_web_search,enable_moderation,allow_in_packs,
                pack_notice_period_days,extensions_enabled,
                extensions_auto_advance,extensions_free_selection,
                gransabio_enabled
            ) VALUES(
                7,'Original','Original prompt','Original description',NULL,0,0,
                0,0,0,0,0,0,0,0,0,1,0
            );
            INSERT INTO PROMPT_PERMISSIONS VALUES(7,1,'owner');
            INSERT INTO PROMPT_PHONE_SETTINGS VALUES(7,3,NULL);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def real_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(prompts, "get_db_connection", real_connection)
    monkeypatch.setattr(
        prompts,
        "get_prompt_info",
        AsyncMock(return_value={"name": "Original"}),
    )
    monkeypatch.setattr(prompts, "can_manage_prompt", AsyncMock(return_value=True))
    monkeypatch.setattr(prompts, "invalidate_landing_cache", Mock())
    monkeypatch.setattr(
        request_security,
        "validate_mutation_request",
        Mock(return_value=None),
    )

    response = await prompts.update_prompt(
        request=SimpleNamespace(),
        prompt_id=7,
        current_user=SimpleNamespace(id=2, role_id=2),
        csrf_token="test-csrf-token",
        name="Edited",
        prompt="Edited prompt",
        description="Edited description",
        sample_voice_id="voice-code",
        sample_voice_catalog_id=5,
        public=False,
        image=None,
        editor_ids=None,
        new_owner_id=None,
        category_ids="[]",
        is_paid=0,
        markup_per_mtokens=0.0,
        llm_mode="any",
        forced_llm_id=None,
        hide_llm_name=False,
        allowed_llms=None,
        disable_web_search=False,
        force_web_search=False,
        enable_moderation=False,
        watchdog_config=None,
        allow_in_packs=False,
        pack_notice_period_days=0,
        extensions_enabled=False,
        extensions_auto_advance=False,
        extensions_free_selection=True,
        purchase_price=None,
        gransabio_enabled=False,
        gransabio_config=None,
    )
    assert response.status_code == 303
    async with aiosqlite.connect(db_path) as conn:
        row = await (
            await conn.execute(
                "SELECT name,voice_id FROM PROMPTS WHERE id=7"
            )
        ).fetchone()
    assert row == ("Edited", None)


@pytest.mark.asyncio
async def test_prompt_editor_loads_deprecated_current_voice_for_repair(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "deprecated-editor.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE USER_ROLES(id INTEGER PRIMARY KEY,role_name TEXT);
            CREATE TABLE USERS(id INTEGER PRIMARY KEY,username TEXT,role_id INTEGER);
            CREATE TABLE SERVICES(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE VOICES(
                id INTEGER PRIMARY KEY,name TEXT,voice_code TEXT,
                is_default INTEGER DEFAULT 0,deprecated INTEGER DEFAULT 0,
                tts_service INTEGER
            );
            CREATE TABLE PROMPTS(
                id INTEGER PRIMARY KEY,name TEXT,prompt TEXT,description TEXT,
                voice_id INTEGER,image TEXT,created_by_user_id INTEGER,
                public INTEGER,is_paid INTEGER,markup_per_mtokens REAL,
                forced_llm_id INTEGER,forced_reasoning_json TEXT,
                phone_llm_id INTEGER,phone_reasoning_json TEXT,
                phone_realtime_voice TEXT,hide_llm_name INTEGER,
                disable_web_search INTEGER,force_web_search INTEGER,
                enable_moderation INTEGER,watchdog_config TEXT,
                allow_in_packs INTEGER,pack_notice_period_days INTEGER,
                allowed_llms TEXT,extensions_enabled INTEGER,
                extensions_auto_advance INTEGER,extensions_free_selection INTEGER,
                purchase_price REAL,gransabio_enabled INTEGER,
                gransabio_config TEXT
            );
            CREATE TABLE PROMPT_PERMISSIONS(
                prompt_id INTEGER,user_id INTEGER,permission_level TEXT
            );
            CREATE TABLE WELCOME_MESSAGES(
                entity_type TEXT,entity_id INTEGER,is_active INTEGER,content TEXT
            );
            INSERT INTO USER_ROLES VALUES(2,'user');
            INSERT INTO USERS VALUES(2,'owner',2);
            INSERT INTO SERVICES VALUES(10,'TTS-FUTURE');
            INSERT INTO VOICES VALUES(1,'Retired','retired-code',0,1,10);
            INSERT INTO VOICES VALUES(2,'Replacement','valid-code',1,0,10);
            INSERT INTO PROMPTS(
                id,name,prompt,description,voice_id,created_by_user_id,public,
                is_paid,markup_per_mtokens,hide_llm_name,disable_web_search,
                force_web_search,enable_moderation,allow_in_packs,
                pack_notice_period_days,extensions_enabled,
                extensions_auto_advance,extensions_free_selection,
                gransabio_enabled
            ) VALUES(
                7,'Repair me','Prompt','Description',1,2,0,0,0,0,0,0,0,0,0,0,0,1,0
            );
            INSERT INTO PROMPT_PERMISSIONS VALUES(7,2,'owner');
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def real_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    captured = {}

    def template_response(template_name, context):
        captured.update(context)
        return SimpleNamespace(template_name=template_name, context=context)

    monkeypatch.setattr(prompts, "get_db_connection", real_connection)
    monkeypatch.setattr(
        prompts,
        "get_template_context",
        AsyncMock(return_value={"base": True}),
    )
    monkeypatch.setattr(prompts.templates, "TemplateResponse", template_response)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/prompts/edit/7",
            "headers": [],
            "session": {},
        }
    )

    response = await prompts.edit_prompt(
        request=request,
        prompt_id=7,
        current_user=SimpleNamespace(id=2, role_id=2),
    )

    assert response.template_name == "prompts/edit_prompt.html"
    assert captured["voice_catalog_id"] == 1
    assert captured["voice_code"] == "retired-code"


@pytest.mark.asyncio
async def test_real_prompt_creation_uses_exact_voice_and_rejects_ambiguous_legacy(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "create-prompt.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE SERVICES(id INTEGER PRIMARY KEY,name TEXT);
            CREATE TABLE VOICES(
                id INTEGER PRIMARY KEY,voice_code TEXT,is_default INTEGER DEFAULT 0,
                tts_service INTEGER,deprecated INTEGER DEFAULT 0
            );
            CREATE TABLE PROMPTS(
                id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,prompt TEXT,
                description TEXT,voice_id INTEGER,created_by_user_id INTEGER,
                created_at TEXT,public INTEGER,is_paid INTEGER,
                markup_per_mtokens REAL,forced_llm_id INTEGER,
                forced_reasoning_json TEXT,phone_llm_id INTEGER,
                phone_reasoning_json TEXT,phone_realtime_voice TEXT,
                hide_llm_name INTEGER,disable_web_search INTEGER,
                force_web_search INTEGER,enable_moderation INTEGER,
                watchdog_config TEXT,allowed_llms TEXT,extensions_enabled INTEGER,
                extensions_auto_advance INTEGER,extensions_free_selection INTEGER,
                gransabio_enabled INTEGER,gransabio_config TEXT
            );
            CREATE TABLE PROMPT_PERMISSIONS(
                prompt_id INTEGER,user_id INTEGER,permission_level TEXT
            );
            CREATE TABLE PROMPT_CATEGORIES(
                prompt_id INTEGER,category_id INTEGER
            );
            INSERT INTO SERVICES VALUES(10,'TTS-FUTURE');
            INSERT INTO VOICES VALUES(5,'voice-code',1,10,0);
            INSERT INTO VOICES VALUES(6,'voice-code',0,10,0);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def real_connection(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    class Creator:
        id = 2
        username = "creator"

        @property
        async def is_admin(self):
            return False

        @property
        async def is_user(self):
            return True

    monkeypatch.setattr(prompts, "get_db_connection", real_connection)
    monkeypatch.setattr(
        prompts,
        "get_prompt_info",
        AsyncMock(return_value={"name": "Created"}),
    )
    monkeypatch.setattr(prompts, "create_prompt_directory", Mock())
    monkeypatch.setattr(
        "marketplace.services.storefronts.ensure_creator_profile",
        AsyncMock(),
    )
    create_args = {
        "name": "Created",
        "prompt": "Prompt",
        "description": "Description",
        "sample_voice_id": "voice-code",
        "public": False,
        "image": None,
        "category_ids": "[]",
        "is_paid": 0,
        "markup_per_mtokens": 0.0,
        "llm_mode": "any",
        "forced_llm_id": None,
        "hide_llm_name": False,
        "allowed_llms": None,
        "disable_web_search": False,
        "force_web_search": False,
        "enable_moderation": False,
        "watchdog_config": None,
        "extensions_enabled": False,
        "extensions_auto_advance": False,
        "extensions_free_selection": True,
        "gransabio_enabled": False,
        "gransabio_config": None,
    }
    response = await prompts.create_prompt_post(
        request=SimpleNamespace(),
        current_user=Creator(),
        sample_voice_catalog_id=6,
        **create_args,
    )
    assert response.status_code == 303
    async with aiosqlite.connect(db_path) as conn:
        row = await (
            await conn.execute("SELECT name,voice_id FROM PROMPTS")
        ).fetchone()
    assert row == ("Created", 6)

    with pytest.raises(HTTPException) as ambiguous:
        await prompts.create_prompt_post(
            request=SimpleNamespace(),
            current_user=Creator(),
            sample_voice_catalog_id=None,
            **create_args,
        )
    assert ambiguous.value.status_code == 409
    assert "ambiguous" in ambiguous.value.detail

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("UPDATE VOICES SET deprecated=1 WHERE id=6")
        await conn.commit()
    with pytest.raises(HTTPException) as unavailable:
        await prompts.create_prompt_post(
            request=SimpleNamespace(),
            current_user=Creator(),
            sample_voice_catalog_id=6,
            **create_args,
        )
    assert unavailable.value.status_code == 409
    assert "unavailable" in unavailable.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_audio_revision", "pending_audio_revision"),
    ((True, False), (False, True)),
)
async def test_prompt_voice_update_cas_rejects_phone_revision(
    monkeypatch,
    active_audio_revision,
    pending_audio_revision,
):
    with pytest.raises(HTTPException) as conflict:
        await _save_prompt_as_editor(
            monkeypatch,
            current_voice_id=4,
            active_audio_revision=active_audio_revision,
            pending_audio_revision=pending_audio_revision,
        )
    assert conflict.value.status_code == 409
    assert "atomic phone-audio activation" in conflict.value.detail


def test_prompt_editor_form_submits_the_canonical_csrf_token():
    source = (
        prompts.templates.env.loader.get_source(
            prompts.templates.env, "prompts/edit_prompt.html"
        )[0]
    )
    assert 'name="csrf_token"' in source
    assert 'value="{{ phone_settings_csrf_token }}"' in source


@pytest.mark.asyncio
async def test_legacy_prompt_update_requires_valid_same_origin_csrf(monkeypatch):
    connection = _PromptConnection()

    @asynccontextmanager
    async def get_connection(readonly=False):
        yield connection

    monkeypatch.setattr(prompts, "get_db_connection", get_connection)
    monkeypatch.setattr(
        prompts,
        "get_prompt_info",
        AsyncMock(return_value={"name": "Original"}),
    )
    monkeypatch.setattr(
        prompts,
        "can_manage_prompt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(prompts, "invalidate_landing_cache", Mock())
    monkeypatch.setattr(request_security, "PRIMARY_APP_DOMAIN", "")

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="legacy-prompt-csrf-test")
    app.include_router(prompts.router)
    app.dependency_overrides[prompts.get_current_user] = lambda: SimpleNamespace(
        id=2,
        role_id=2,
    )

    @app.get("/seed-legacy-prompt-csrf")
    async def seed_csrf(request: Request):
        return {"token": ensure_csrf_token(request)}

    base_form = {
        "name": "Edited",
        "prompt": "Edited prompt",
        "description": "Edited description",
        "sample_voice_id": "voice-code",
        "category_ids": "[]",
    }

    def multipart(values):
        return {key: (None, str(value)) for key, value in values.items()}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        token = (await client.get("/seed-legacy-prompt-csrf")).json()["token"]
        missing = await client.post(
            "/prompts/update/7",
            files=multipart(base_form),
            headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
        )
        mismatch = await client.post(
            "/prompts/update/7",
            files=multipart({**base_form, "csrf_token": "x" * 32}),
            headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
        )
        cross_origin = await client.post(
            "/prompts/update/7",
            files=multipart({**base_form, "csrf_token": token}),
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        assert not any(
            query.lower().startswith("update prompts set")
            for query, _params in connection.queries
        )
        valid = await client.post(
            "/prompts/update/7",
            files=multipart({**base_form, "csrf_token": token}),
            headers={"Origin": "http://test", "Sec-Fetch-Site": "same-origin"},
        )

    assert [missing.status_code, mismatch.status_code, cross_origin.status_code] == [
        403,
        403,
        403,
    ]
    assert valid.status_code == 303
    assert valid.headers["location"] == "/prompts/edit/7?saved=1"
    assert sum(
        query.lower().startswith("update prompts set")
        for query, _params in connection.queries
    ) == 1
