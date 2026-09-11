"""Render actual model pages and exercise their emitted JavaScript without providers."""
import json
import re
import shutil
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
def test_model_pages_render_and_run_localized_controls(language, tmp_path):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {}, url_for=lambda name, **kwargs: '/admin/llms')
    model = dict(id=77, machine='OpenRouter', model='vendor/model:1', display_name='Fixture <model>',
                 context_window_tokens=123456, max_output_tokens=5432, input_token_cost=0.1234,
                 output_token_cost=1.5678, enabled=True, vision=True, sync_managed=True, sync_status='synced')
    fixture = dict(ui_language=language, username='Fixture', is_admin=True, is_user=True,
                   marketplace={'discovery_enabled': True, 'creator_tools_enabled': True},
                   llms=[model], providers=['OpenRouter'], provider_tabs=[dict(key='openrouter', label='OpenRouter', icon='fa-cloud')],
                   provider_counts={'openrouter': {'enabled': 1, 'total': 1}}, active_provider='openrouter',
                   catalog_count=1234, enabled_count=1, needs_review_count=0,
                   llm_id=77, llm_machine='OpenRouter', llm_model='vendor/model:1', llm_input_cost=0.1234,
                   llm_output_cost=1.5678, llm_display_name='Fixture <model>', llm_sync_managed=True, llm_vision=True)
    rendered = {}
    for name in ['admin_models', 'llms/llm_list', 'llms/create_llm', 'llms/edit_llm']:
        html = env.get_template(name + '.html').render(**fixture)
        rendered[name] = html
        assert tr.render('common.error.generic') not in html.split('<script>')[-1]
        if name == 'llms/llm_list':
            assert 'data-input-price="0.1234"' in html
            assert 'data-enabled="yes"' in html
            assert tr.format_currency(0.1234, 'USD', 4) in html
            assert 'aria-label="' + tr.render('admin_models.action.disable_model', model='Fixture &lt;model&gt;') + '"' in html
        if name == 'llms/edit_llm':
            assert 'name="machine" value="OpenRouter"' in html
            assert 'name="model" value="vendor/model:1"' in html
            assert 'name="input_token_cost" value="0.1234"' in html
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    fixture_path = tmp_path / 'models.json'
    fixture_path.write_text(json.dumps(rendered), encoding='utf-8')
    result = subprocess.run([node, 'tests/js/model_management_rendered.cjs', str(fixture_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

@pytest.mark.asyncio
async def test_model_admin_access_and_invalid_request_are_localized(monkeypatch):
    from unittest.mock import AsyncMock
    from starlette.requests import Request
    import app as app_module

    class User:
        ui_language = 'es'
        def __init__(self, admin):
            self.admin = admin
        @property
        async def is_admin(self):
            return self.admin

    def no_database(*args, **kwargs):
        raise AssertionError('Access/input validation must not access the database')

    monkeypatch.setattr(app_module, 'get_db_connection', no_database)
    denied = await app_module.api_admin_llm_catalog(current_user=User(False))
    assert denied.status_code == 403
    assert json.loads(denied.body)['error'] == Translator('es').render('admin_models.error.access_denied')
    request = Request({'type': 'http', 'headers': []})
    monkeypatch.setattr(request, 'json', AsyncMock(return_value=[]))
    invalid = await app_module.api_admin_llms_enabled(request=request, current_user=User(True))
    assert invalid.status_code == 400
    assert json.loads(invalid.body)['error'] == Translator('es').render('admin_models.error.model_state_update_requires_a_json_object')
    result = app_module._localize_model_sync_result({'status': 'error', 'provider': 'openai', 'error': 'private provider diagnostics', 'error_key': 'error.provider_http', 'error_params': {'status': 429}}, Translator('es').render)
    assert 'private provider diagnostics' not in result['error']
    assert result['status'] == 'error'
    assert result['provider'] == 'openai'
    assert '429' in result['error']


@pytest.mark.asyncio
async def test_model_selection_validation_keeps_managed_models_unchanged():
    import aiosqlite
    from llm_catalog import LlmCatalogError, set_models_enabled
    async with aiosqlite.connect(':memory:') as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute('CREATE TABLE LLM (id INTEGER, machine TEXT, model TEXT, enabled INTEGER)')
        await conn.executemany('INSERT INTO LLM VALUES (?, ?, ?, ?)', [(1, 'GPT', 'vendor/id', 1), (2, 'GPTSub', 'account/model', 1)])
        with pytest.raises(LlmCatalogError) as caught:
            await set_models_enabled(conn, [1, 2], False)
        assert caught.value.message_key == 'error.gptsub_model_state_is_managed_by_linked_account_catalog_synchronization'
        assert caught.value.localized(Translator('es').render) == Translator('es').render('admin_models.' + caught.value.message_key)
        rows = await (await conn.execute('SELECT enabled FROM LLM ORDER BY id')).fetchall()
        assert [row['enabled'] for row in rows] == [1, 1]
        assert await set_models_enabled(conn, [1], False) == [{'id': 1, 'enabled': False}]
