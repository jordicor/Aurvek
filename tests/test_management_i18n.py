"""Real management template rendering, with no accounts or operational changes."""

import json
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from i18n import LANGUAGES, Translator


def render_management(template_name, language, **values):
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    translator = Translator(language)
    environment.globals.update(
        get_static_url=lambda value: value,
        get_static_theme_hashes=lambda: {},
        t=translator.t, t_html=translator.html,
        format_number=translator.format_number,
        format_currency=translator.format_currency,
        i18n_payload=translator.browser_payload,
    )
    return environment.get_template(template_name).render(
        ui_language=language, username="Original <creator>",
        marketplace=SimpleNamespace(discovery_enabled=True, creator_tools_enabled=True),
        **values,
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_dashboard_preserves_prompt_data_and_maintenance_form_values(language):
    html = render_management(
        "index.html", language, is_admin=True, is_user=True,
        manageable_prompts=[SimpleNamespace(id=7, name="Original <prompt>", text="Original <prompt>", public_id="public-7", custom_domain=None)],
        admin_csrf_token="fixture-csrf", captcha_enabled=True,
    )
    assert Translator(language).render("dashboard.business_tools") in html
    assert 'value="older" checked' in html
    assert 'value="h"' in html and 'value="d"' in html
    assert 'content="fixture-csrf"' in html
    assert "Original &lt;prompt&gt;" in html
    payload = json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>', html, re.S)[1])
    assert set(payload["resources"][language]) == {"common", "navigation", "dashboard"}
    assert payload["language"] == language
    assert "devMode=true; path=/; max-age=86400" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("markup", ["NaN", "Infinity", None])
async def test_curation_rejects_invalid_amount_before_database_write(monkeypatch, markup):
    import app as app_module

    class User:
        id = 7
        ui_language = "es"

        @property
        async def is_user(self):
            return True

    async def receive():
        return {"type": "http.request", "body": json.dumps({"referral_markup_per_mtokens": markup}).encode()}

    request = Request({"type": "http", "method": "PUT", "path": "/api/user/curation-settings", "headers": []}, receive)
    monkeypatch.setattr(app_module, "require_creator_tools_enabled", lambda: None)

    def unexpected_database(*args, **kwargs):
        pytest.fail("Invalid markup must not reach the database")

    monkeypatch.setattr(app_module, "get_db_connection", unexpected_database)
    response = await app_module.update_curation_settings(request, User())
    assert response.status_code == 400
    assert json.loads(response.body)["message"] == Translator("es").render("management_errors.curation.invalid")


@pytest.mark.asyncio
async def test_curation_localizes_response_without_changing_stored_amount(monkeypatch):
    import app as app_module

    class User:
        id = 7
        ui_language = "ja"

        @property
        async def is_user(self):
            return True

    amount = 0.012345

    async def receive():
        return {"type": "http.request", "body": json.dumps({"referral_markup_per_mtokens": amount}).encode()}

    request = Request({"type": "http", "method": "PUT", "path": "/api/user/curation-settings", "headers": []}, receive)
    cursor = AsyncMock()
    connection = AsyncMock()
    connection.cursor.return_value = cursor

    @asynccontextmanager
    async def database():
        yield connection

    monkeypatch.setattr(app_module, "require_creator_tools_enabled", lambda: None)
    monkeypatch.setattr(app_module, "get_db_connection", database)
    response = await app_module.update_curation_settings(request, User())
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["message"] == Translator("ja").render("management_errors.curation.saved")
    assert body["referral_markup_per_mtokens"] == amount
    assert cursor.execute.call_args.args[1] == (amount, 7)
    connection.commit.assert_awaited_once()
