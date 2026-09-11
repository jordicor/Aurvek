"""AI configuration localization keeps engine state and authored content intact."""
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
def test_ai_admin_rendered_flows(language, tmp_path):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    config = dict(enabled=True, transport='http', mode='personal_assistant', platform_id='aurvek',
                  has_service_api_key=True, service_api_key_masked='masked<key>',
                  has_admin_api_key=True, admin_api_key_masked='masked-admin',
                  generator_model='provider/model-id', qa_models=['authored<model>'],
                  max_iterations=3, min_global_score=8, cost_safety_multiplier=3)
    pages = {}
    for name in ('admin_atagia', 'admin_gransabio', 'admin_system_prompts'):
        pages[name] = env.get_template(name + '.html').render(ui_language=language,
            username='Fixture', is_admin=True, is_user=True, config=config,
            marketplace={'discovery_enabled': True, 'creator_tools_enabled': True})
    assert tr.render('ai_config.atagia_title') in pages['admin_atagia']
    assert 'masked&lt;key&gt;' in pages['admin_atagia']
    assert 'value="personal_assistant"' in pages['admin_atagia']
    assert 'provider/model-id' in pages['admin_gransabio']
    assert 'authored&lt;model&gt;' in pages['admin_gransabio']
    assert '{user_level}' in pages['admin_system_prompts']
    payload = tmp_path / 'pages.json'
    payload.write_text(json.dumps(pages), encoding='utf-8')
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/admin_ai_config_rendered.cjs', str(payload)],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


class Admin:
    ui_language = 'es'

    @property
    async def is_admin(self):
        return True


def request(data):
    req = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []})
    req.json = AsyncMock(return_value=data)
    return req


@pytest.mark.asyncio
async def test_atagia_admin_preserves_worker_actions_and_sanitizes_diagnostics(monkeypatch):
    import app
    import atagia_admin_status
    import atagia_bridge

    tr = Translator('es')
    bridge = SimpleNamespace(last_error={'message': 'engine diagnostic <internal>', 'operation': 'secret-operation'},
                             drain_and_pause=AsyncMock(return_value={'mode': 'drain_and_pause', 'reason': 'Authored reason'}))
    monkeypatch.setattr(atagia_bridge, 'get_atagia_bridge', lambda: bridge)
    response = await app.admin_atagia_worker_control_post(request({
        'action': 'drain_and_pause', 'reason': 'Authored reason', 'timeout_seconds': '30'}), Admin())
    bridge.drain_and_pause.assert_awaited_once_with(reason='Authored reason', timeout_seconds=30.0)
    assert json.loads(response.body)['state'] == {'mode': 'drain_and_pause', 'reason': 'Authored reason'}
    bridge.drain_and_pause.return_value = None
    response = await app.admin_atagia_worker_control_post(request({'action': 'drain_and_pause'}), Admin())
    body = json.loads(response.body)
    assert response.status_code == 502
    assert body['error'] == {'message': tr.render('ai_config.engine_error')}
    assert b'internal' not in response.body and b'secret-operation' not in response.body
    before = bridge.drain_and_pause.await_count
    response = await app.admin_atagia_worker_control_post(request({'action': 'drain_and_pause', 'timeout_seconds': 'NaN'}), Admin())
    assert response.status_code == 400
    assert bridge.drain_and_pause.await_count == before

    status = {'sync': {'latest_run': {'status': 'completed_with_errors', 'recent_errors': ['raw engine error']}},
              'atagia': {'available': True, 'active_jobs': [{'job_id': 'id', 'status': 'failed', 'error_message': 'raw failure'}],
                         'review': {'memory_items': [{'content': 'Authored memory text'}]}}}
    original = deepcopy(status)
    monkeypatch.setattr(atagia_admin_status, 'get_atagia_admin_status', AsyncMock(return_value=status))
    response = await app.admin_atagia_diagnostics(request({}), Admin())
    body = json.loads(response.body)['status']
    assert status == original  # Request language cannot mutate cached operational data.
    assert body['sync']['latest_run']['status'] == 'completed_with_errors'
    assert body['sync']['latest_run']['recent_errors'] == [tr.render('ai_config.engine_error')]
    assert body['atagia']['review'] == original['atagia']['review']
    assert body['atagia']['active_jobs'][0]['error_message'] == tr.render('ai_config.engine_error')


@pytest.mark.asyncio
async def test_gransabio_validation_and_engine_failure_are_localized(monkeypatch):
    import app
    import gransabio_config
    import gransabio_service

    tr = Translator('es')
    monkeypatch.setattr(gransabio_config, 'get_gransabio_config', AsyncMock(return_value={}))
    monkeypatch.setattr(gransabio_service, 'test_gransabio_connection', AsyncMock(return_value={
        'ok': False, 'version': None, 'model_count': 0, 'error': 'internal upstream traceback'}))
    response = await app.admin_gransabio_test(request({'url': 'http://127.0.0.1:8000'}), Admin())
    assert json.loads(response.body)['error'] == tr.render('ai_config.engine_error')
    response = await app.admin_gransabio_post(request({'enabled': True, 'generator_model': ''}), Admin())
    assert response.status_code == 400
    assert json.loads(response.body)['message'].startswith('Configuraci')
    assert b'Fix before enabling' not in response.body


@pytest.mark.asyncio
async def test_system_block_validation_retains_wire_values():
    import app
    tr = Translator('es')
    response = await app.api_create_system_prompt_block(request({'name': 'Authored name', 'content': 'Authored content',
                                                                'position': 'not-a-position'}), Admin())
    assert response.status_code == 422
    assert json.loads(response.body)['error'] == tr.render('ai_config.invalid_position', values='post_prompt, pre_prompt')
    response = await app.api_reorder_system_prompt_blocks(request({}), Admin())
    assert json.loads(response.body)['error'] == tr.render('ai_config.reorder_list')


def test_all_ai_config_catalogs_have_identical_contracts():
    from i18n import read_catalogs
    catalogs = read_catalogs(Path('locales'))
    expected = set(catalogs['en']['ai_config'])
    for language in LANGUAGES:
        assert set(catalogs[language]['ai_config']) == expected
