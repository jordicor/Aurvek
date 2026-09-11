"""Geo UI localization and honest Cloudflare outcomes without provider calls."""
from contextlib import asynccontextmanager
import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from cloudflare_geo import get_all_geo_data
from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
def test_geo_real_render_and_controls(language, tmp_path):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                      format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                      get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    original = json.dumps(get_all_geo_data(), sort_keys=True)
    raw = get_all_geo_data(language)
    grouped = {name: {'code': code, 'countries': [c for c in raw['countries'] if c['continent'] == code]}
               for code, name in raw['continents'].items()}
    page = env.get_template('admin_geo.html').render(ui_language=language, username='Fixture',
        is_admin=True, is_user=True, marketplace={'discovery_enabled': True}, geo_data=grouped)
    assert tr.render('admin_geo.mode.allow') in page
    assert json.dumps(get_all_geo_data(), sort_keys=True) == original
    assert [c['code'] for c in raw['countries']] == [c['code'] for c in get_all_geo_data()['countries']]
    path = tmp_path / 'geo.json'
    path.write_text(json.dumps({'html': page}), encoding='utf-8')
    result = subprocess.run([shutil.which('node') or 'C:/Program Files/nodejs/node.exe',
                             'tests/js/admin_geo_rendered.cjs', str(path)],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


class Admin:
    ui_language = 'es'
    @property
    async def is_admin(self):
        return True


@pytest.mark.asyncio
async def test_geo_invalid_input_never_writes_and_failed_sync_retains_saved_config(monkeypatch):
    from marketplace.routes import geo as routes
    conn = AsyncMock()
    @asynccontextmanager
    async def database(**kwargs):
        yield conn
    monkeypatch.setattr(routes, 'get_db_connection', database)
    sync = AsyncMock(return_value={'success': False, 'error': 'private diagnostic'})
    monkeypatch.setattr(routes.geo_sync_engine, 'sync_all', sync)
    request = Request({'type': 'http', 'headers': []})
    for invalid in [[], {'countries': 'ES'}, {'countries': [True]}, {'geo_enabled': 'false'}, {'response_html': {}}]:
        request.json = AsyncMock(return_value=invalid)
        response = await routes.update_geo_global(request, Admin())
        assert response.status_code == 400
    conn.execute.assert_not_awaited()
    sync.assert_not_awaited()
    request.json = AsyncMock(return_value={'geo_enabled': False, 'mode': 'allow', 'countries': ['es', 'JP'],
                                         'continents': ['eu'], 'response_html': '<h1>Authored notice</h1>'})
    response = await routes.update_geo_global(request, Admin())
    body = json.loads(response.body)
    assert response.status_code == 502 and body['saved'] and not body['success']
    assert body['message'] == Translator('es').t('admin_geo.saved_pending')
    assert 'private diagnostic' not in response.body.decode()
    assert conn.execute.await_args_list[0].args[1] == ('geo_enabled', '0')
    assert json.loads(conn.execute.await_args_list[2].args[1][1]) == ['ES', 'JP']
    assert conn.execute.await_args_list[4].args[1][1] == '<h1>Authored notice</h1>'
    conn.commit.assert_awaited_once()
    sync.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('handler,method,key', [('force_geo_sync','sync_all','error.sync'), ('remove_geo_rules','remove_all_rules','error.remove')])
async def test_geo_failed_provider_result_is_not_reported_as_success(monkeypatch, handler, method, key):
    from marketplace.routes import geo as routes
    monkeypatch.setattr(routes.geo_sync_engine, method, AsyncMock(return_value={'success': False, 'error': 'provider secret'}))
    response = await getattr(routes, handler)(Request({'type': 'http', 'headers': []}), Admin())
    assert response.status_code == 502
    assert json.loads(response.body)['message'] == Translator('es').t('admin_geo.' + key)
    assert b'provider secret' not in response.body


@pytest.mark.asyncio
async def test_geo_connection_failure_does_not_claim_connected_or_expose_exception(monkeypatch):
    from marketplace.routes import geo as routes
    class Client:
        def is_configured(self): return True
        get_zone_info = AsyncMock(side_effect=RuntimeError('private token detail'))
    class Cursor:
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration
    class Connection:
        @asynccontextmanager
        async def execute(self, *args): yield Cursor()
    @asynccontextmanager
    async def database(**kwargs): yield Connection()
    monkeypatch.setattr(routes, 'CloudflareGeoClient', Client)
    monkeypatch.setattr(routes, 'get_db_connection', database)
    response = await routes.get_geo_status(Request({'type': 'http', 'headers': []}), Admin())
    status = json.loads(response.body)['status']
    assert status['configured'] and not status['connected']
    assert status['rules_used'] is None and status['transforms_enabled'] is None
    assert status['connection_error'] == Translator('es').t('admin_geo.status.connection_error')
    assert b'private token' not in response.body
