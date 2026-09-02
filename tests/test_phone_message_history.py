from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI

from auth import get_current_user
from chat.routes import messages as messages_module
from chat.services.phone_history import load_phone_history_page


def make_history_database(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE MESSAGES(
            id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL,
            date TEXT NOT NULL
        );
        CREATE TABLE PHONE_CALL_JOBS(
            id TEXT PRIMARY KEY, last_error_code TEXT
        );
        CREATE TABLE PHONE_CALLS(
            id TEXT PRIMARY KEY, job_id TEXT, owner_user_id INTEGER NOT NULL,
            conversation_id INTEGER NOT NULL, direction TEXT, status TEXT,
            answered_by TEXT, initiated_at TEXT, ringing_at TEXT, answered_at TEXT,
            ended_at TEXT, duration_seconds INTEGER, termination_reason TEXT,
            estimated_cost REAL, final_cost REAL, currency TEXT,
            recording_enabled INTEGER, amd_enabled INTEGER,
            created_at TEXT, updated_at TEXT, deleted_at TEXT
        );
        CREATE TABLE PHONE_CALL_MESSAGE_LINKS(
            id INTEGER PRIMARY KEY, call_id TEXT NOT NULL, message_id INTEGER NOT NULL,
            participant TEXT, turn_id TEXT, origin_channel TEXT, interrupted INTEGER,
            played_ms INTEGER, delivery_state TEXT, created_at TEXT
        );
        CREATE TABLE PHONE_CALL_COST_COMPONENTS(
            id INTEGER PRIMARY KEY, call_id TEXT, provider TEXT,
            component_type TEXT, quantity REAL, unit TEXT, provider_cost REAL,
            customer_charge REAL, currency TEXT, state TEXT, occurred_at TEXT
        );
        CREATE TABLE PHONE_RECORDINGS(
            id INTEGER PRIMARY KEY, call_id TEXT, status TEXT,
            participant_path TEXT, assistant_path TEXT, mixed_path TEXT
        );

        INSERT INTO MESSAGES(id,conversation_id,date) VALUES
            (10,10,'2030-01-01 10:00:00'),
            (20,10,'2030-01-01 10:20:00'),
            (30,10,'2030-01-01 10:30:00'),
            (40,10,'2030-01-01 10:40:00'),
            (99,99,'2030-01-01 10:30:00');
        INSERT INTO PHONE_CALL_JOBS(id,last_error_code)
            VALUES('job-span',NULL),('job-empty','no_answer');
        INSERT INTO PHONE_CALLS(
            id,job_id,owner_user_id,conversation_id,direction,status,answered_by,
            initiated_at,ringing_at,answered_at,ended_at,duration_seconds,
            termination_reason,estimated_cost,final_cost,currency,
            recording_enabled,amd_enabled,created_at,updated_at,deleted_at
        ) VALUES
            ('call-span','job-span',1,10,'outbound','completed','human',
             '2030-01-01 10:19:00','2030-01-01 10:19:10','2030-01-01 10:19:20',
             '2030-01-01 10:31:00',700,'completed',0.50,0.42,'USD',1,0,
             '2030-01-01 10:19:00','2030-01-01 10:31:00',NULL),
            ('call-single',NULL,1,10,'inbound','busy',NULL,
             '2030-01-01 10:39:00',NULL,NULL,'2030-01-01 10:41:00',0,
             'busy',0.10,NULL,'USD',0,0,
             '2030-01-01 10:39:00','2030-01-01 10:41:00',NULL),
            ('call-active',NULL,1,10,'inbound','in_progress','human',
             '2030-01-01 10:38:00','2030-01-01 10:38:05',
             '2030-01-01 10:38:10',NULL,NULL,NULL,0.07,NULL,'USD',0,0,
             '2030-01-01 10:38:00','2030-01-01 10:42:00',NULL),
            ('call-empty','job-empty',1,10,'outbound','no_answer',NULL,
             '2030-01-01 10:25:00',NULL,NULL,'2030-01-01 10:26:00',0,
             'no_answer',0.03,0.03,'USD',0,0,
             '2030-01-01 10:25:00','2030-01-01 10:26:00',NULL),
            ('call-empty-span',NULL,1,10,'inbound','completed','human',
             '2030-01-01 10:15:00',NULL,'2030-01-01 10:15:05',
             '2030-01-01 10:35:00',1195,'completed',0.20,0.20,'USD',0,0,
             '2030-01-01 10:15:00','2030-01-01 10:35:00',NULL),
            ('call-empty-old',NULL,1,10,'outbound','no_answer',NULL,
             '2030-01-01 09:40:00',NULL,NULL,'2030-01-01 09:41:00',0,
             'no_answer',0.01,0.01,'USD',0,0,
             '2030-01-01 09:40:00','2030-01-01 09:41:00',NULL),
            ('call-latest',NULL,1,10,'outbound','failed',NULL,
             '2030-01-01 11:00:00',NULL,NULL,'2030-01-01 11:00:03',0,
             'provider_error',0.01,NULL,'USD',0,0,
             '2030-01-01 11:00:00','2030-01-01 11:00:03',NULL),
            ('call-other-channel',NULL,1,10,'inbound','completed','human',
             '2030-01-01 09:59:00',NULL,'2030-01-01 09:59:05',
             '2030-01-01 10:01:00',56,'completed',0.20,0.20,'USD',0,0,
             '2030-01-01 09:59:00','2030-01-01 10:01:00',NULL),
            ('call-other-owner',NULL,2,10,'inbound','completed','human',
             '2030-01-01 10:00:00',NULL,NULL,'2030-01-01 10:01:00',60,
             'completed',0,0,'USD',0,0,
             '2030-01-01 10:00:00','2030-01-01 10:01:00',NULL),
            ('call-deleted',NULL,1,10,'inbound','completed','human',
             '2030-01-01 10:00:00',NULL,NULL,'2030-01-01 10:01:00',60,
             'completed',0,0,'USD',0,0,
             '2030-01-01 10:00:00','2030-01-01 10:01:00','2030-01-02');

        INSERT INTO PHONE_CALL_MESSAGE_LINKS(
            id,call_id,message_id,participant,turn_id,origin_channel,
            interrupted,played_ms,delivery_state,created_at
        ) VALUES
            (1,'call-span',20,'caller','turn-1','phone',0,NULL,'consumed','2030-01-01 10:20:01'),
            (2,'call-span',30,'assistant','turn-1','phone',1,1300,'consumed','2030-01-01 10:30:01'),
            (3,'call-single',40,'caller','turn-2','phone',0,NULL,'consumed','2030-01-01 10:40:01'),
            (4,'call-other-channel',10,'other_channel',NULL,'whatsapp',0,NULL,'released','2030-01-01 10:00:01'),
            (5,'call-other-owner',20,'caller','foreign','phone',0,NULL,'consumed','2030-01-01 10:20:01'),
            (6,'call-deleted',20,'caller','deleted','phone',0,NULL,'consumed','2030-01-01 10:20:01');

        INSERT INTO PHONE_CALL_COST_COMPONENTS(
            id,call_id,provider,component_type,quantity,unit,provider_cost,
            customer_charge,currency,state,occurred_at
        ) VALUES(1,'call-span','twilio','pstn',700,'seconds',0.30,0.42,'USD','final','2030-01-01 10:31:00');
        INSERT INTO PHONE_RECORDINGS(
            id,call_id,status,participant_path,assistant_path,mixed_path
        ) VALUES(1,'call-span','available','D:/private/caller.ulaw',NULL,'D:/private/mix.mp3');
        """
    )
    conn.commit()
    conn.close()

    @asynccontextmanager
    async def connection_factory(readonly=False):
        mode = "ro" if readonly else "rw"
        db = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    return connection_factory


@pytest.mark.asyncio
async def test_phone_history_enriches_messages_and_keeps_boundaries_stable(
    tmp_path,
) -> None:
    factory = make_history_database(tmp_path)
    newest = await load_phone_history_page(
        factory,
        conversation_id=10,
        owner_user_id=1,
        message_ids=[30, 40],
        newest_page=True,
    )
    older = await load_phone_history_page(
        factory,
        conversation_id=10,
        owner_user_id=1,
        message_ids=[10, 20],
        newest_page=False,
    )

    assert set(newest.message_metadata) == {30, 40}
    assert set(older.message_metadata) == {10, 20}
    assert newest.message_metadata[30] == {
        "phone_call_id": "call-span",
        "channel": "phone",
        "participant": "assistant",
        "provenance": {
            "phone_call_id": "call-span",
            "channel": "phone",
            "origin_channel": "phone",
            "participant": "assistant",
            "turn_id": "turn-1",
        },
        "interrupted": True,
        "played_ms": 1300,
        "delivery_state": "consumed",
        "phone_timestamps": {
            "linked_at": "2030-01-01 10:30:01",
            "call_started_at": "2030-01-01 10:19:00",
            "call_answered_at": "2030-01-01 10:19:20",
            "call_ended_at": "2030-01-01 10:31:00",
        },
    }
    assert older.message_metadata[10]["channel"] == "whatsapp"
    assert older.message_metadata[10]["provenance"]["channel"] == "phone"
    assert older.message_metadata[10]["delivery_state"] == "released"

    newest_markers = {marker["id"]: marker for marker in newest.markers}
    older_markers = {marker["id"]: marker for marker in older.markers}
    assert "phone-call:call-span:start" not in newest_markers
    assert newest_markers["phone-call:call-span:end"]["anchor_message_id"] == 30
    assert older_markers["phone-call:call-span:start"]["anchor_message_id"] == 20
    assert "phone-call:call-span:end" not in older_markers

    assert newest_markers["phone-call:call-empty:start"]["anchor_message_id"] == 30
    assert newest_markers["phone-call:call-empty:start"]["transcript_present"] is False
    assert newest_markers["phone-call:call-empty:end"]["placement"] == "before"
    assert newest_markers["phone-call:call-latest:start"]["anchor_message_id"] is None
    assert "phone-call:call-latest:start" not in older_markers
    assert newest_markers["phone-call:call-active:start"]["anchor_message_id"] == 40
    assert "phone-call:call-active:end" not in newest_markers
    assert "phone-call:call-active:end" not in older_markers
    assert "phone-call:call-empty-span:start" not in newest_markers
    assert newest_markers["phone-call:call-empty-span:end"]["anchor_message_id"] == 40
    assert older_markers["phone-call:call-empty-span:start"]["anchor_message_id"] == 20
    assert "phone-call:call-empty-span:end" not in older_markers
    assert older_markers["phone-call:call-other-channel:start"]["transcript_present"] is False

    all_marker_ids = [marker["id"] for marker in newest.markers + older.markers]
    assert len(all_marker_ids) == len(set(all_marker_ids))
    assert "call-other-channel" not in json.dumps(newest.public_payload())
    assert "call-empty-old" not in json.dumps(newest.public_payload())
    assert "call-empty-old" in json.dumps(older.public_payload())
    assert "call-other-owner" not in json.dumps(newest.public_payload())
    assert "call-deleted" not in json.dumps(older.public_payload())


@pytest.mark.asyncio
async def test_phone_history_exposes_safe_detail_without_paths_or_provider_ids(
    tmp_path,
) -> None:
    factory = make_history_database(tmp_path)
    async with factory() as conn:
        await conn.executescript(
            """
            CREATE TABLE PHONE_DATA_PURGE_JOBS(
                id TEXT PRIMARY KEY,owner_user_id_snapshot INTEGER,
                conversation_id_snapshot INTEGER,call_id_snapshot TEXT,
                purge_scope TEXT,status TEXT,attempt_count INTEGER,last_error TEXT,
                created_at TEXT
            );
            INSERT INTO PHONE_DATA_PURGE_JOBS VALUES
                ('purge-span-old',1,10,'call-span','recording','scheduled',0,NULL,
                 '2030-01-02 10:00:00'),
                ('purge-span-new',1,10,'call-span','recording','needs_attention',3,
                 'D:/private/mix.mp3 CA11111111111111111111111111111111',
                 '2030-01-02 10:01:00'),
                ('purge-empty',1,10,'call-empty','call','scheduled',0,NULL,
                 '2030-01-02 10:02:00'),
                ('purge-other-owner',2,10,'call-span','call','running',9,
                 'private owner error','2030-01-02 10:03:00');
            """
        )
        await conn.commit()
    page = await load_phone_history_page(
        factory,
        conversation_id=10,
        owner_user_id=1,
        message_ids=[20, 30],
        newest_page=True,
    )
    call = next(call for call in page.calls if call["id"] == "call-span")
    assert call["timeline"][-1] == {
        "event": "ended",
        "at": "2030-01-01 10:31:00",
    }
    assert call["cost_components"] == [
        {
            "provider": "twilio",
            "component_type": "pstn",
            "quantity": 700.0,
            "unit": "seconds",
            "customer_charge": 0.42,
            "currency": "USD",
            "state": "final",
            "occurred_at": "2030-01-01 10:31:00",
        }
    ]
    assert call["audio"] == {
        "tracks": [
            {
                "track": "mixed",
                "url": "/api/phone-calls/call-span/recording?track=mixed",
            },
            {
                "track": "participant",
                "url": "/api/phone-calls/call-span/recording?track=participant",
            },
        ]
    }
    assert call["recording"] == {
        "present": True,
        "status": "available",
        "audio_available": True,
    }
    assert call["purge"] == {
        "status": "needs_attention",
        "scope": "recording",
        "attempt": 3,
        "error": "Deletion requires administrator attention.",
    }
    empty_call = next(call for call in page.calls if call["id"] == "call-empty")
    assert empty_call["purge"] == {
        "status": "scheduled",
        "scope": "call",
        "attempt": 0,
        "error": None,
    }
    assert call["provenance_summary"] == {
        "total_messages": 2,
        "phone_messages": 2,
        "interrupted_messages": 1,
        "played_ms": 1300,
        "delivery_states": {"consumed": 2},
        "participants": {"caller": 1, "assistant": 1},
        "origin_channels": {"phone": 2},
        "turn_ids": ["turn-1"],
    }
    serialized = json.dumps(page.public_payload())
    assert "D:/private" not in serialized
    assert "provider_call_sid" not in serialized
    assert "provider_session_id" not in serialized
    assert "provider_cost" not in serialized
    assert "CA11111111111111111111111111111111" not in serialized
    assert "private owner error" not in serialized

    admin_page = await load_phone_history_page(
        factory,
        conversation_id=10,
        owner_user_id=999,
        message_ids=[20, 30],
        newest_page=True,
        allow_admin=True,
    )
    admin_call = next(call for call in admin_page.calls if call["id"] == "call-span")
    assert admin_call["audio"] is None
    assert admin_call["recording"] == {
        "present": True,
        "status": "available",
        "audio_available": False,
    }
    assert admin_call["cost_components"][0]["provider_cost"] == 0.3
    assert "/recording?track=" not in json.dumps(admin_page.public_payload())


@pytest.mark.asyncio
async def test_phone_history_is_owner_scoped_and_legacy_schema_is_compatible(
    tmp_path,
) -> None:
    factory = make_history_database(tmp_path / "scoped")
    denied = await load_phone_history_page(
        factory,
        conversation_id=10,
        owner_user_id=999,
        message_ids=[10, 20, 30, 40],
        newest_page=True,
    )
    assert denied == denied.__class__()

    legacy_path = tmp_path / "legacy.db"
    sqlite3.connect(legacy_path).close()

    @asynccontextmanager
    async def legacy_factory(readonly=False):
        mode = "ro" if readonly else "rw"
        conn = await aiosqlite.connect(f"file:{legacy_path}?mode={mode}", uri=True)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    legacy = await load_phone_history_page(
        legacy_factory,
        conversation_id=10,
        owner_user_id=1,
        message_ids=[1],
        newest_page=True,
    )
    assert legacy.public_payload() == {"calls": [], "markers": []}


@pytest.mark.asyncio
async def test_messages_route_preserves_non_phone_contract_and_owner_gate(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "route.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE USERS(id INTEGER PRIMARY KEY,username TEXT);
        CREATE TABLE PROMPTS(
            id INTEGER PRIMARY KEY,name TEXT,image TEXT,description TEXT,
            extensions_enabled INTEGER,extensions_free_selection INTEGER,
            is_paid INTEGER
        );
        CREATE TABLE LLM(id INTEGER PRIMARY KEY,machine TEXT,model TEXT);
        CREATE TABLE CONVERSATIONS(
            id INTEGER PRIMARY KEY,user_id INTEGER,role_id INTEGER,llm_id INTEGER,
            active_extension_id INTEGER,is_incognito INTEGER,hidden_from_history INTEGER,
            purge_on_close INTEGER,locked INTEGER,locked_reason TEXT
        );
        CREATE TABLE MESSAGES(
            id INTEGER PRIMARY KEY,conversation_id INTEGER,user_id INTEGER,
            message TEXT,type TEXT,date TEXT,is_bookmarked INTEGER,llm_id INTEGER,
            citations_json TEXT,input_tokens_used INTEGER DEFAULT 0,
            output_tokens_used INTEGER DEFAULT 0
        );
        CREATE TABLE PHONE_CALL_JOBS(id TEXT PRIMARY KEY,last_error_code TEXT);
        CREATE TABLE PHONE_CALLS(
            id TEXT PRIMARY KEY,job_id TEXT,owner_user_id INTEGER,conversation_id INTEGER,
            direction TEXT,status TEXT,answered_by TEXT,initiated_at TEXT,ringing_at TEXT,
            answered_at TEXT,ended_at TEXT,duration_seconds INTEGER,
            termination_reason TEXT,estimated_cost REAL,final_cost REAL,currency TEXT,
            recording_enabled INTEGER,amd_enabled INTEGER,created_at TEXT,updated_at TEXT,
            deleted_at TEXT
        );
        CREATE TABLE PHONE_CALL_MESSAGE_LINKS(
            id INTEGER PRIMARY KEY,call_id TEXT,message_id INTEGER,participant TEXT,
            turn_id TEXT,origin_channel TEXT,interrupted INTEGER,played_ms INTEGER,
            delivery_state TEXT,created_at TEXT
        );
        CREATE TABLE PHONE_CALL_COST_COMPONENTS(
            id INTEGER PRIMARY KEY,call_id TEXT,provider TEXT,component_type TEXT,
            quantity REAL,unit TEXT,provider_cost REAL,customer_charge REAL,
            currency TEXT,state TEXT,occurred_at TEXT
        );
        CREATE TABLE PHONE_RECORDINGS(
            id INTEGER PRIMARY KEY,call_id TEXT,status TEXT,participant_path TEXT,
            assistant_path TEXT,mixed_path TEXT
        );
        INSERT INTO USERS VALUES(1,'owner'),(2,'other');
        INSERT INTO PROMPTS VALUES(1,'Prompt',NULL,'Description',0,1,0);
        INSERT INTO LLM VALUES(1,'OpenAI','model');
        INSERT INTO CONVERSATIONS VALUES
            (10,1,1,1,NULL,0,0,0,0,NULL),
            (11,1,1,1,NULL,0,0,0,0,NULL),
            (20,2,1,1,NULL,0,0,0,0,NULL);
        INSERT INTO MESSAGES VALUES
            (100,10,1,'phone text','user','2030-01-01 10:00:00',0,1,NULL,0,0),
            (110,11,1,'web text','user','2030-01-01 10:01:00',0,1,NULL,0,0),
            (200,20,2,'private text','user','2030-01-01 10:02:00',0,1,NULL,0,0);
        INSERT INTO PHONE_CALLS VALUES(
            'call-route',NULL,1,10,'inbound','completed','human',
            '2030-01-01 09:59:00',NULL,'2030-01-01 09:59:05',
            '2030-01-01 10:05:00',300,'completed',0.1,0.1,'USD',0,0,
            '2030-01-01 09:59:00','2030-01-01 10:05:00',NULL
        );
        INSERT INTO PHONE_CALL_MESSAGE_LINKS VALUES(
            1,'call-route',100,'caller','turn-route','phone',0,NULL,
            'consumed','2030-01-01 10:00:01'
        );
        """
    )
    conn.commit()
    conn.close()

    @asynccontextmanager
    async def route_factory(readonly=False):
        mode = "ro" if readonly else "rw"
        db = await aiosqlite.connect(f"file:{db_path}?mode={mode}", uri=True)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()

    async def no_op(*_args, **_kwargs):
        return None

    async def no_attachments(*_args, **_kwargs):
        return {}

    async def render_message(message, *_args, **_kwargs):
        return message

    monkeypatch.setattr(messages_module, "get_db_connection", route_factory)
    monkeypatch.setattr(messages_module, "ensure_conversation_privacy_schema", no_op)
    monkeypatch.setattr(
        messages_module,
        "preload_attachment_records_for_messages",
        no_attachments,
    )
    monkeypatch.setattr(messages_module, "process_message", render_message)
    monkeypatch.setattr(
        messages_module,
        "get_signed_bot_avatar_urls",
        lambda *_args, **_kwargs: {
            "bot_profile_picture": "",
            "bot_profile_picture_128": "",
            "bot_profile_picture_fullsize": "",
        },
    )
    monkeypatch.setattr(messages_module, "touch_provider_activity", lambda *_args: None)

    class FakeUser:
        id = 1

        @property
        def is_admin(self):
            async def value():
                return False

            return value()

    async def current_user():
        return FakeUser()

    app = FastAPI()
    app.include_router(messages_module.router)
    app.dependency_overrides[get_current_user] = current_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        phone_response = await client.get("/api/conversations/10/messages")
        ordinary_response = await client.get("/api/conversations/11/messages")
        denied_response = await client.get("/api/conversations/20/messages")

    assert phone_response.status_code == 200
    phone_payload = phone_response.json()
    assert [message["id"] for message in phone_payload["messages"]] == [100]
    assert phone_payload["messages"][0]["phone_call_id"] == "call-route"
    assert phone_payload["messages"][0]["channel"] == "phone"
    assert phone_payload["messages"][0]["played_ms"] is None
    assert phone_payload["messages"][0]["delivery_state"] == "consumed"
    assert phone_payload["messages"][0]["provenance"]["turn_id"] == "turn-route"
    assert len(phone_payload["phone_history"]["calls"]) == 1

    assert ordinary_response.status_code == 200
    ordinary_payload = ordinary_response.json()
    assert "phone_history" not in ordinary_payload
    assert "phone_call_id" not in ordinary_payload["messages"][0]
    assert "channel" not in ordinary_payload["messages"][0]

    assert denied_response.status_code == 404
    assert "private text" not in denied_response.text
