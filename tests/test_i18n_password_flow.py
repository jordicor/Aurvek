"""End-to-end coverage for the phase-one password localization pilot."""

import gzip
import html
import json
import os
import re
import sqlite3
import time
from collections import Counter
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("SECURITY_REDIS_MODE", "off")
os.environ.setdefault("USE_EMAIL_SERVICE", "false")

import aiosqlite
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app as app_module
import auth
import common
import models
from models import User


USER_ID = 41


def _user(*, language="en", authentication_mode="password_only", can_change=True):
    user = User(
        id=USER_ID,
        username="i18n-fixture",
        password=None,
        role_id=3,
        is_enabled=True,
        can_send_files=False,
        can_generate_images=False,
        current_prompt_id=None,
        authentication_mode=authentication_mode,
        can_change_password=can_change,
        session_version=1,
    )
    user.ui_language = language
    user.auth_time = int(time.time())
    return user


@pytest_asyncio.fixture
async def password_flow(tmp_path, monkeypatch):
    database_path = tmp_path / "password-flow.sqlite"
    old_hash = app_module.hash_password("old-password")
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE USER_ROLES (
                id INTEGER PRIMARY KEY,
                role_name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE USERS (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password TEXT,
                role_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                auth_provider TEXT NOT NULL DEFAULT 'local',
                profile_picture TEXT,
                session_version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE USER_DETAILS (
                user_id INTEGER PRIMARY KEY,
                current_alter_ego_id INTEGER
            );
            CREATE TABLE USER_ALTER_EGOS (
                id INTEGER PRIMARY KEY,
                name TEXT,
                profile_picture TEXT
            );
            INSERT INTO USER_ROLES (id, role_name) VALUES
                (1, 'admin'), (2, 'user'), (3, 'customer');
            INSERT INTO USER_DETAILS (user_id, current_alter_ego_id)
                VALUES (41, NULL);
            """
        )
        connection.execute(
            """INSERT INTO USERS
                   (id, username, password, role_id, auth_provider, session_version)
               VALUES (?, ?, ?, 3, 'local', 1)""",
            (USER_ID, "i18n-fixture", old_hash),
        )

    query_counts = Counter()

    def connection_factory(consumer):
        @asynccontextmanager
        async def connect(readonly=False):
            connection = await aiosqlite.connect(database_path)
            connection.row_factory = aiosqlite.Row

            def count(statement):
                if statement.lstrip().upper().startswith("SELECT"):
                    query_counts[consumer] += 1

            await connection.set_trace_callback(count)
            try:
                yield connection
            finally:
                await connection.close()

        return connect

    monkeypatch.setattr(app_module, "get_db_connection", connection_factory("app"))
    monkeypatch.setattr(common, "get_db_connection", connection_factory("common"))
    monkeypatch.setattr(models, "get_db_connection", connection_factory("models"))
    monkeypatch.setattr(auth, "get_db_connection", connection_factory("auth"))

    state = SimpleNamespace(user=_user())

    async def current_user():
        return state.user

    application = FastAPI()
    application.add_api_route(
        "/change-password", app_module.show_change_password_form, methods=["GET"]
    )
    application.add_api_route(
        "/api/change-password", app_module.change_password, methods=["POST"]
    )
    application.add_api_route(
        "/api/set-password", app_module.set_initial_password, methods=["POST"]
    )
    application.dependency_overrides[app_module.get_current_user] = current_user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://i18n-fixture.test",
    ) as client:
        yield SimpleNamespace(
            path=database_path,
            client=client,
            state=state,
            query_counts=query_counts,
        )


def _password_row(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT password, session_version FROM USERS WHERE id = ?", (USER_ID,)
        ).fetchone()


def _payload(rendered):
    match = re.search(
        r'<script type="application/json" id="aurvek-i18n">(.*?)</script>',
        rendered,
        re.DOTALL,
    )
    assert match
    source = html.unescape(match.group(1))
    return source, json.loads(source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "provider", "password", "expected_form", "expected_title"),
    [
        ("es", "local", "hashed", "change-password-form", "Cambiar contraseña"),
        ("ja", "google", None, "set-password-form", "パスワードを設定"),
    ],
)
async def test_password_page_real_route_renders_locale_payload_without_extra_queries(
    password_flow, language, provider, password, expected_form, expected_title
):
    with closing(sqlite3.connect(password_flow.path)) as connection, connection:
        connection.execute(
            "UPDATE USERS SET password = ?, auth_provider = ? WHERE id = ?",
            (password, provider, USER_ID),
        )
    password_flow.state.user = _user(language=language)
    password_flow.query_counts.clear()

    response = await password_flow.client.get("/change-password")

    assert response.status_code == 200
    assert f'<html lang="{language}"' in response.text
    assert f'id="{expected_form}"' in response.text
    assert expected_title in response.text
    assert password_flow.query_counts == {"app": 1, "models": 1, "common": 1}

    payload_source, payload = _payload(response.text)
    assert payload["language"] == language
    assert set(payload["resources"]) == {"en", language}
    assert set(payload["resources"][language]) == {"common", "account", "navigation"}
    assert len(payload_source.encode("utf-8")) > len(gzip.compress(payload_source.encode("utf-8")))


@pytest.mark.asyncio
async def test_change_password_localizes_failures_and_preserves_security_flow(password_flow):
    password_flow.state.user = _user(language="es", can_change=False)
    response = await password_flow.client.post(
        "/api/change-password",
        data={"old_password": "old-password", "new_password": "new-password"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "No tienes permiso para cambiar tu contraseña"

    password_flow.state.user = _user(language="es")
    response = await password_flow.client.post(
        "/api/change-password",
        data={"old_password": "old-password", "new_password": "short"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "La nueva contraseña debe tener al menos 8 caracteres"

    response = await password_flow.client.post(
        "/api/change-password",
        data={"old_password": "incorrect", "new_password": "new-password"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "La contraseña actual es incorrecta"

    response = await password_flow.client.post(
        "/api/change-password",
        data={"old_password": "old-password", "new_password": "new-password"},
    )
    stored_password, session_version = _password_row(password_flow.path)
    assert response.status_code == 200
    assert response.json() == {
        "detail": "La contraseña se ha cambiado correctamente. Vuelve a iniciar sesión.",
        "reauthenticate": True,
    }
    assert app_module.verify_password(stored_password, "new-password")
    assert session_version == 2
    assert "session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_set_password_localizes_checks_and_preserves_security_flow(password_flow):
    with closing(sqlite3.connect(password_flow.path)) as connection, connection:
        connection.execute(
            "UPDATE USERS SET password = NULL, auth_provider = 'google' WHERE id = ?",
            (USER_ID,),
        )
    password_flow.state.user = _user(language="ja", can_change=False)
    response = await password_flow.client.post(
        "/api/set-password", data={"new_password": "new-password"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "パスワードを設定する権限がありません"

    user = _user(language="ja")
    user.auth_time = int(time.time()) - 601
    password_flow.state.user = user

    response = await password_flow.client.post(
        "/api/set-password", data={"new_password": "new-password"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "パスワードを設定する前に、もう一度ログインしてください"

    user.auth_time = int(time.time())
    response = await password_flow.client.post(
        "/api/set-password", data={"new_password": "short"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "パスワードは 8 文字以上である必要があります"

    response = await password_flow.client.post(
        "/api/set-password", data={"new_password": "new-password"}
    )
    stored_password, session_version = _password_row(password_flow.path)
    assert response.status_code == 200
    assert response.json() == {
        "detail": "パスワードが設定されました。もう一度ログインしてください。",
        "reauthenticate": True,
    }
    assert app_module.verify_password(stored_password, "new-password")
    assert session_version == 2
    assert "session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_password_template_uses_catalog_messages_as_text():
    source = (common.SCRIPT_DIR / "templates" / "admin_profile.html").read_text(
        encoding="utf-8"
    )

    assert 'data.detail === "Current password is incorrect"' not in source
    assert "statusDiv.innerHTML" not in source
    assert "submitBtn.innerHTML" not in source
    assert "AurvekI18n.t(key, params)" in source
    assert "document.createTextNode" in source
