"""Service/voice forms keep technical choices, precision and provider identity."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", ["services/services_list", "services/create_service", "services/edit_service", "voices/voices_list", "voices/create_voice", "voices/edit_voice"])
def test_service_voice_forms_preserve_configured_data_and_input_precision(language, name):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    cost = 0.000123456789
    html = env.get_template(name + ".html").render(
        ui_language=language, username="Fixture", is_admin=True, is_user=True,
        marketplace={"discovery_enabled": True, "creator_tools_enabled": True},
        services=[(7, "Original <service>", "character", cost, "Images")],
        service_types=["TTS", "STT", "Phone", "Images", "Video", "Music"],
        service_id=7, service_name="Original <service>", service_unit="character", service_cost_per_unit=cost, service_type="Images",
        voices=[(41, "Original <voice>", "wire-voice", "TTS-ELEVENLABS", 1, 0)], voice_id=41, voice_name="Original <voice>", voice_code="wire-voice",
    )
    if name == "services/services_list":
        assert tr.format_number(cost, maximum_fraction_digits=20) in html
        assert "Original &lt;service&gt;" in html
        assert tr.render("admin_services.type.images") in html
    elif name == "services/edit_service":
        assert 'value="0.000123456789"' in html
        assert 'value="Images" selected' in html
        assert tr.render("admin_services.type.images") in html
    elif name == "services/create_service":
        assert 'value="TTS"' in html and 'value="Images"' in html
    elif name == "voices/voices_list":
        assert "Original &lt;voice&gt;" in html and "wire-voice" in html
        assert tr.render("admin_voices.voice_count", count=1) in html
        assert 'onclick="setDefault(41)"' in html
    else:
        assert 'name="tts_service" value="5"' in html


class Admin:
    ui_language = "es"

    @property
    async def is_admin(self):
        return True


@pytest.mark.asyncio
async def test_service_cost_validation_precedes_writes_and_preserves_valid_precision(monkeypatch):
    import app as app_module

    connection = AsyncMock()
    cursor = AsyncMock()
    connection.cursor.return_value = cursor

    @asynccontextmanager
    async def database():
        yield connection

    monkeypatch.setattr(app_module, "get_db_connection", database)
    request = Request({"type": "http", "method": "POST", "path": "/admin/services/new", "headers": []})
    for value in (float("nan"), float("inf")):
        with pytest.raises(HTTPException) as invalid:
            await app_module.create_service_post(request, Admin(), "wire-service", "character", value, "TTS")
        assert invalid.value.status_code == 400
        assert invalid.value.detail == Translator("es").render("admin_services.error.invalid_cost")
    connection.cursor.assert_not_awaited()
    cost = 0.000123456789
    response = await app_module.create_service_post(request, Admin(), "wire-service", "character", cost, "TTS")
    assert response.status_code == 303
    assert cursor.execute.await_args.args[1] == ("wire-service", "character", cost, "TTS")
