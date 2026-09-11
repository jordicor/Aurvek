"""Ranking localization preserves scoring inputs and reports asynchronous work."""
from contextlib import asynccontextmanager
import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
def test_ranking_render_and_controls_keep_scoring_payload(language, tmp_path):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    weights = dict(users_with_access=3.125, purchases=5, unique_chatters=4, landing_conversions=6, favorites=2, has_landing_boost=15, recency_max_bonus=30)
    page = env.get_template('admin_ranking.html').render(ui_language=language,
        username='Fixture', is_admin=True, is_user=True,
        marketplace={'discovery_enabled': True, 'creator_tools_enabled': True},
        ranking_config={'mode': 'scheduled', 'interval_hours': 6, 'weights': weights, 'last_updated': 1700000000})
    assert 'value="scheduled"' in page and 'value="3.125"' in page
    assert tr.format_number(3.125) in page
    assert tr.render('admin_ranking.mode.scheduled') in page
    path = tmp_path / 'ranking.json'
    path.write_text(json.dumps({'html': page, 'weights': weights}), encoding='utf-8')
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/admin_ranking_rendered.cjs', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_ranking_invalid_values_do_not_write_and_valid_precision_is_preserved(monkeypatch):
    from marketplace.routes import ranking as routes
    class Admin:
        ui_language = 'es'
        @property
        async def is_admin(self):
            return True
    conn = AsyncMock()
    cursor = AsyncMock()
    conn.cursor.return_value = cursor
    @asynccontextmanager
    async def database():
        yield conn
    monkeypatch.setattr(routes, 'get_db_connection', database)
    monkeypatch.setattr(routes, 'invalidate_ranking_config_cache', lambda: None)
    request = Request({'type': 'http', 'headers': []})
    for value in [float('nan'), float('inf'), True, 'invalid']:
        request.json = AsyncMock(return_value={'mode': 'scheduled', 'weights': {'purchases': value}})
        response = await routes.api_update_ranking_config(request, Admin())
        assert response.status_code == 400
        assert json.loads(response.body)['message'] == Translator('es').render('admin_ranking.error.invalid_weight', metric='Compras')
    cursor.execute.assert_not_awaited()
    request.json = AsyncMock(return_value={'mode': 'scheduled', 'interval_hours': 6, 'weights': {'purchases': 0.012345}})
    response = await routes.api_update_ranking_config(request, Admin())
    assert response.status_code == 200
    assert cursor.execute.await_args_list[0].args[1] == ('scheduled',)
    assert cursor.execute.await_args_list[1].args[1] == ('6',)
    assert json.loads(cursor.execute.await_args_list[2].args[1][0]) == {'purchases': 0.012345}
    conn.commit.assert_awaited_once()
