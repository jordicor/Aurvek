"""TTS admin labels follow the request locale while provider values stay fixed."""
from contextlib import asynccontextmanager
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from i18n import LANGUAGES, Translator


class Admin:
    def __init__(self, language='es'):
        self.ui_language = language
    @property
    async def is_admin(self):
        return True


def request(query=b''):
    return Request({'type': 'http', 'method': 'GET', 'path': '/admin/elevenlabs-tts', 'headers': [], 'query_string': query})


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_tts_route_renders_choices_and_localized_redirect_messages(monkeypatch, language, tmp_path):
    from integrations.elevenlabs import admin_routes as routes
    from tools.tts_config import _DEFAULTS
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    context = dict(ui_language=language, username='Fixture', is_admin=True, is_user=True,
                   marketplace={'discovery_enabled': True, 'creator_tools_enabled': True})
    monkeypatch.setattr(routes, 'get_template_context', AsyncMock(side_effect=lambda *args: dict(context)))
    monkeypatch.setattr(routes, 'get_tts_config', AsyncMock(return_value=dict(_DEFAULTS)))
    monkeypatch.setattr(routes, 'templates', SimpleNamespace(TemplateResponse=lambda name, data: env.get_template(name).render(**data)))
    page = await routes.admin_elevenlabs_tts(request(b'error=ws_model'), Admin(language))
    assert tr.render('admin_elevenlabs.tts.warning_v3') in page
    assert 'value="eleven_turbo_v2_5" selected' in page
    assert 'value="mp3_44100_128" selected' in page
    assert 'step="0.01" value="0.45"' in page
    assert tr.render('admin_elevenlabs.model.eleven_flash_v2_5') in page
    assert tr.format_number(0.45) in page
    saved = await routes.admin_elevenlabs_tts(request(b'saved=1'), Admin(language))
    assert tr.render('admin_elevenlabs.notice.saved') in saved
    unknown = await routes.admin_elevenlabs_tts(request(b'error=raw-provider-diagnostic'), Admin(language))
    assert 'raw-provider-diagnostic' not in unknown
    assert tr.render('admin_elevenlabs.error.save_failed') in unknown
    path = tmp_path / 'tts.json'
    path.write_text(json.dumps(page), encoding='utf-8')
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/admin_tts_rendered.cjs', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_tts_save_preserves_wire_settings_and_validation(monkeypatch):
    from integrations.elevenlabs import admin_routes as routes
    connection = AsyncMock()
    @asynccontextmanager
    async def database():
        yield connection
    monkeypatch.setattr(routes, 'get_db_connection', database)
    monkeypatch.setattr(routes, 'invalidate_tts_config_cache', lambda: None)
    req = request()
    req.form = AsyncMock(return_value={'action': 'save_config', 'tts_webchat_model': 'eleven_v3', 'tts_webchat_ws_enabled': '1'})
    response = await routes.admin_elevenlabs_tts_save(req, Admin())
    assert response.headers['location'] == '/admin/elevenlabs-tts?error=ws_model'
    connection.execute.assert_not_awaited()
    req.form = AsyncMock(return_value={'action': 'save_config', 'tts_webchat_model': 'eleven_flash_v2_5', 'tts_webchat_format': 'mp3_44100_128', 'tts_webchat_stability': '0.45', 'tts_webchat_similarity': 'nan', 'tts_webchat_chunk_schedule': '[true]', 'tts_webchat_ws_enabled': '1'})
    response = await routes.admin_elevenlabs_tts_save(req, Admin())
    assert response.headers['location'] == '/admin/elevenlabs-tts?saved=1'
    params = [call.args[1] for call in connection.execute.await_args_list]
    assert ('tts_webchat_model', 'eleven_flash_v2_5') in params
    assert ('tts_webchat_stability', '0.45') in params
    assert ('1',) in params
    assert not any('tts_webchat_similarity' in values or 'tts_webchat_chunk_schedule' in values for values in params)
    connection.commit.assert_awaited_once()
