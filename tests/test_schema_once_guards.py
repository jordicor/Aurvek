from __future__ import annotations

from contextlib import asynccontextmanager
import sqlite3

import aiosqlite
import pytest

import file_storage
from chat.services import privacy


def _create_privacy_db(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE CONVERSATIONS (
                id INTEGER PRIMARY KEY,
                user_id INTEGER,
                folder_id INTEGER,
                last_activity TEXT
            )
            """
        )


@pytest.mark.asyncio
async def test_privacy_schema_runs_once_per_resolved_database(tmp_path, monkeypatch):
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    _create_privacy_db(first_path)
    _create_privacy_db(second_path)
    privacy.reset_conversation_privacy_schema_guard()

    original = privacy._ensure_schema_on_connection
    calls = []

    async def counted(conn):
        calls.append(await privacy._resolved_database_identity(conn))
        await original(conn)

    monkeypatch.setattr(privacy, "_ensure_schema_on_connection", counted)
    try:
        conn = await aiosqlite.connect(first_path)
        await privacy.ensure_conversation_privacy_schema(conn)
        await privacy.ensure_conversation_privacy_schema(conn)
        await conn.close()

        conn = await aiosqlite.connect(first_path)
        await privacy.ensure_conversation_privacy_schema(conn)
        await conn.close()

        conn = await aiosqlite.connect(second_path)
        await privacy.ensure_conversation_privacy_schema(conn)
        await conn.close()

        assert calls == [str(first_path.resolve()), str(second_path.resolve())]
    finally:
        privacy.reset_conversation_privacy_schema_guard()


@pytest.mark.asyncio
async def test_file_storage_schema_runs_once_per_resolved_database(
    tmp_path, monkeypatch
):
    first_path = tmp_path / "storage-first.db"
    second_path = tmp_path / "storage-second.db"
    file_storage.reset_file_storage_schema_guard()

    original = file_storage._ensure_schema_on_connection
    calls = []

    async def counted(conn):
        calls.append(await file_storage._resolved_database_identity(conn))
        await original(conn)

    monkeypatch.setattr(file_storage, "_ensure_schema_on_connection", counted)
    try:
        conn = await aiosqlite.connect(first_path)
        await file_storage.ensure_file_storage_schema(conn)
        await file_storage.ensure_file_storage_schema(conn)
        await conn.close()

        conn = await aiosqlite.connect(first_path)
        await file_storage.ensure_file_storage_schema(conn)
        await conn.close()

        conn = await aiosqlite.connect(second_path)
        await file_storage.ensure_file_storage_schema(conn)
        await conn.close()

        assert calls == [str(first_path.resolve()), str(second_path.resolve())]
    finally:
        file_storage.reset_file_storage_schema_guard()


@pytest.mark.asyncio
async def test_schema_guard_retries_after_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "retry.db"
    _create_privacy_db(db_path)
    privacy.reset_conversation_privacy_schema_guard()
    original = privacy._ensure_schema_on_connection
    attempts = 0

    async def fail_once(conn):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic DDL failure")
        await original(conn)

    monkeypatch.setattr(privacy, "_ensure_schema_on_connection", fail_once)
    conn = await aiosqlite.connect(db_path)
    try:
        with pytest.raises(RuntimeError, match="synthetic DDL failure"):
            await privacy.ensure_conversation_privacy_schema(conn)
        await privacy.ensure_conversation_privacy_schema(conn)
        assert attempts == 2
    finally:
        await conn.close()
        privacy.reset_conversation_privacy_schema_guard()


@pytest.mark.asyncio
async def test_schema_guard_does_not_commit_callers_transaction(tmp_path):
    db_path = tmp_path / "transaction.db"
    _create_privacy_db(db_path)
    privacy.reset_conversation_privacy_schema_guard()
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "INSERT INTO CONVERSATIONS (id, user_id) VALUES (1, 7)"
        )
        await privacy.ensure_conversation_privacy_schema(conn)
        assert conn.in_transaction
        await conn.rollback()
        row = await (
            await conn.execute("SELECT 1 FROM CONVERSATIONS WHERE id = 1")
        ).fetchone()
        assert row is None
    finally:
        await conn.close()
        privacy.reset_conversation_privacy_schema_guard()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["privacy", "file_storage"])
async def test_default_schema_guard_does_not_reopen_ready_database(
    tmp_path,
    monkeypatch,
    target,
):
    db_path = tmp_path / f"{target}.db"
    if target == "privacy":
        _create_privacy_db(db_path)
        module = privacy
        ensure = privacy.ensure_conversation_privacy_schema
        reset = privacy.reset_conversation_privacy_schema_guard
    else:
        module = file_storage
        ensure = file_storage.ensure_file_storage_schema
        reset = file_storage.reset_file_storage_schema_guard

    opens = 0

    @asynccontextmanager
    async def default_connection(readonly=False):
        nonlocal opens
        opens += 1
        conn = await aiosqlite.connect(db_path)
        conn._aurvek_database_identity = str(db_path.resolve())
        try:
            yield conn
        finally:
            await conn.close()

    monkeypatch.setattr(module.database, "get_db_connection", default_connection)
    monkeypatch.setattr(
        module,
        "_DEFAULT_DATABASE_CONNECTION_FACTORY",
        default_connection,
    )
    monkeypatch.setattr(
        module.database,
        "get_database_identity",
        lambda: str(db_path.resolve()),
    )

    reset()
    try:
        await ensure()
        await ensure()
        assert opens == 1
    finally:
        reset()
