"""Category controls preserve authored data and stable mutation contracts."""
from contextlib import asynccontextmanager
import html
import json
import re
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
def test_category_page_executes_with_quoted_authored_content(language, tmp_path):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    category = dict(id=41, name='Owner\'s "category" <img>', description='Original <description>',
                    icon='fa-tag', is_age_restricted=True, display_order=1234, prompt_count=2345)
    page = env.get_template('admin_categories.html').render(ui_language=language,
        username='Fixture', is_admin=True, is_user=True, categories=[category],
        marketplace={'discovery_enabled': True, 'creator_tools_enabled': True})
    data = html.unescape(re.search(r'data-category="([^"]+)"', page)[1])
    assert json.loads(data) == category
    assert tr.format_number(category['prompt_count']) in page
    path = tmp_path / 'categories.json'
    path.write_text(json.dumps({'html': page, 'data': data}), encoding='utf-8')
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/admin_categories_rendered.cjs', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_category_authorization_validation_and_writes(monkeypatch):
    import app as app_module

    class User:
        ui_language = 'es'
        def __init__(self, allowed):
            self.allowed = allowed
        @property
        async def is_admin(self):
            return self.allowed

    cursor = AsyncMock()
    cursor.fetchone.return_value = (41,)
    connection = AsyncMock()
    # aiosqlite execute supports both await and async-with.
    class Execution:
        def __await__(self):
            async def result():
                return cursor
            return result().__await__()
        async def __aenter__(self):
            return cursor
        async def __aexit__(self, *args):
            pass
    connection.execute = MagicMock(side_effect=lambda *args: Execution())
    @asynccontextmanager
    async def database(*args, **kwargs):
        yield connection
    monkeypatch.setattr(app_module, 'get_db_connection', database)
    request = Request({'type': 'http', 'headers': []})
    request.json = AsyncMock(return_value={'name': ''})
    for user, status, key in [(User(False), 403, 'error.admin_required'), (User(True), 400, 'error.name_required')]:
        with pytest.raises(HTTPException) as caught:
            await app_module.create_category(request, user)
        assert caught.value.status_code == status
        assert caught.value.detail == Translator('es').render('admin_categories.' + key)
    connection.execute.assert_not_called()
    request.json = AsyncMock(return_value=dict(name='Original name', description='Original text', icon='fa-tag', is_age_restricted=True, display_order=8))
    response = await app_module.create_category(request, User(True))
    assert response == {'success': True, 'id': 41, 'message': Translator('es').render('admin_categories.notice.created')}
    assert connection.execute.call_args_list[0].args[1] == ('Original name', 'Original text', 'fa-tag', 1, 8)
