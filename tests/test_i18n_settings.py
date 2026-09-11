"""Render the actual settings/profile handlers with isolated account data."""

import json
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

import app as app_module
import common
import models
import timezone_cities
from i18n import LANGUAGES, Translator
from user_languages import language_display_name
from tests.test_i18n_password_flow import _user, USER_ID, _payload


@pytest.fixture
def settings_db(tmp_path, monkeypatch):
    path = tmp_path / "settings.sqlite"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript((Path(__file__).parents[1] / "aurvek_schema.sql").read_text(encoding="utf-8"))
        connection.execute("INSERT INTO USER_ROLES(id,role_name) VALUES(3,'customer')")
        connection.execute("INSERT INTO USERS(id,username,role_id,email,preferred_languages_json) VALUES(?,?,?,?,?)",
                           (USER_ID, "i18n-fixture", 3, "fixture@example.test", '["es","en"]'))
        connection.execute("INSERT INTO USER_DETAILS(user_id,balance) VALUES(?,?)", (USER_ID, 10.5))

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(path) as connection:
            connection.row_factory = aiosqlite.Row
            yield connection

    for module in (app_module, common, models):
        monkeypatch.setattr(module, "get_db_connection", connect)
    monkeypatch.setattr(common, "get_user_api_key_mode", AsyncMock(return_value="both_prefer_own"))
    monkeypatch.setattr(common, "user_requires_own_keys", AsyncMock(return_value=False))
    monkeypatch.setattr(app_module, "get_balance", AsyncMock(return_value=10.5))
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("handler_name", ["settings_page", "show_edit_profile_form"])
async def test_settings_handlers_render_interface_choice_and_preserve_conversation_languages(
    settings_db, language, handler_name, caplog,
):
    request = Request({"type": "http", "method": "GET", "path": "/settings", "query_string": b"",
                       "scheme": "https", "server": ("fixture.test", 443), "session": {},
                       "headers": [(b"accept-language", b"de"), (b"cookie", b"ui_language=it")]})
    response = await getattr(app_module, handler_name)(request, _user(language=language))
    html = response.body.decode()
    assert response.status_code == 200
    assert f'<html lang="{language}"' in html
    assert re.search(fr'<option value="{language}"\s+selected>', html)
    assert Translator(language).t("profile.language.interface_label") in html
    assert 'name="preferred_languages_json"' in html
    assert f'value="{language_display_name("es", language)}"' in html
    _, payload = _payload(html)
    assert payload["language"] == language
    assert set(payload["resources"]) == {"en", language}
    assert set(payload["resources"][language]) == {"common", "navigation", "account", "profile"}
    assert html.index("/static/js/common/i18n.js") < html.index("/static/js/common/notification-modal.js")
    assert "Invalid UI message" not in caplog.text
    with closing(sqlite3.connect(settings_db)) as connection:
        assert connection.execute("SELECT preferred_languages_json FROM USERS WHERE id=?", (USER_ID,)).fetchone()[0] == '["es","en"]'


@pytest.mark.asyncio
async def test_city_timezone_endpoint_requires_login_and_bounds_queries(tmp_path, monkeypatch):
    catalog_path = tmp_path / "cities.sqlite"
    timezone_cities.build_timezone_city_catalog(catalog_path)
    monkeypatch.setattr(timezone_cities, "CITY_CATALOG_PATH", catalog_path)
    application = FastAPI()
    application.add_api_route(
        "/api/profile/timezone-cities", app_module.profile_timezone_cities, methods=["GET"]
    )
    signed_in_user = None

    async def current_user():
        return signed_in_user

    application.dependency_overrides[app_module.get_current_user] = current_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://fixture.test"
    ) as client:
        response = await client.get("/api/profile/timezone-cities", params={"q": "Córdoba"})
        assert response.status_code == 401
        signed_in_user = _user(language="es")
        response = await client.get("/api/profile/timezone-cities", params={"q": "Córdoba, España"})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        results = response.json()["results"]
        assert results[0]["timezone"] == "Europe/Madrid"
        assert results[0]["country"] == "España"
        response = await client.get("/api/profile/timezone-cities", params={"q": "x" * 101})
        assert response.status_code == 422
