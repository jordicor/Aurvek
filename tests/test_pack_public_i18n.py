"""Exercise the actual public pack route, fallback template and browser actions."""
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from markupsafe import escape
from starlette.requests import Request

from i18n import LANGUAGES, Translator, read_catalogs


def request(language='en', query=b''):
    return Request({'type':'http', 'method':'GET', 'path':'/pack/AbCd1234/fixture/',
                    'scheme':'https', 'server':('fixture.example',443), 'query_string':query,
                    'headers':[(b'cookie', ('ui_language=' + language).encode())]})


def render(context):
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(get_static_url=lambda value: value)
    # Use strict rendering to expose missing keys/parameters in real consumers.
    context = dict(context, t=Translator(context['ui_language']).render)
    return env.get_template('pack_landing_default.html').render(**context)


@pytest.fixture
def setup_route(monkeypatch):
    from marketplace.routes import packs as routes
    original = 'Original " \' <script> 日本語 {name}'
    cached = dict(pack_id=42, slug='fixture', pack_name=original, description=original,
                  cover_image='cover-wire', is_paid=True, price=1234.5, status='published',
                  is_public=True, has_custom_landing=False, created_by_user_id=7,
                  tags='["CreatorTag"]', username=original)
    monkeypatch.setattr(routes, 'require_public_landings_enabled', lambda *args: None)
    monkeypatch.setattr(routes, 'get_pack_landing_cached', AsyncMock(return_value=cached))
    monkeypatch.setattr(routes, 'get_current_user', AsyncMock(return_value=None))
    monkeypatch.setattr(routes, 'is_creator_content_request', lambda request: False)
    monkeypatch.setattr(routes, '_inject_pack_analytics', lambda html, pack_id: html + '<!-- analytics fixture -->')

    @asynccontextmanager
    async def connection(**kwargs):
        yield object()

    monkeypatch.setattr(routes, 'get_db_connection', connection)
    monkeypatch.setattr(routes, 'get_pack_items', AsyncMock(return_value=[dict(prompt_name=original, prompt_description=original)]))
    monkeypatch.setattr(routes, 'templates', SimpleNamespace(TemplateResponse=lambda name, context: SimpleNamespace(body=render(context).encode())))
    return routes, cached


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('paid', [True, False])
async def test_actual_route_renders_localized_fallback_and_browser_controls(language, paid, setup_route):
    routes, cached = setup_route
    cached['is_paid'] = paid
    response = await routes.pack_landing_page(request(language), 'AbCd1234', 'fixture')
    assert response.status_code == 200
    html = response.body.decode()
    tr = Translator(language)
    assert f'<html lang="{language}">' in html
    assert str(escape(cached['pack_name'])) in html
    assert 'value="42"' in html and 'value="AbCd1234"' in html
    price = tr.format_currency(1234.5) if paid else tr.render('marketplace.store.free')
    assert str(escape(price)) in html
    assert str(escape(tr.render('pack_public.inside', count=1, number=tr.format_number(1)))) in html
    assert '<!-- analytics fixture -->' in html
    assert response.headers['content-language'] == language
    assert response.headers['vary'] == 'Cookie, Accept-Language'
    assert 'price_display' not in cached and 'ui_language' not in cached
    payload = json.loads(re.search(r'id="aurvek-i18n">(.*?)</script>', html, re.S)[1])
    assert set(payload['resources'][language]) == {'common', 'navigation', 'pack_public', 'marketplace', 'auth', 'account'}
    script = next(source for source in re.findall(r'<script>(.*?)</script>', html, re.S) if 'purchasePack' in source)
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/pack_public_behavior.cjs'],
                            input=json.dumps(dict(script=script, payload=payload, paid=paid)),
                            text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_account_locale_wins_and_empty_free_pack_renders(setup_route):
    routes, cached = setup_route
    routes.get_current_user.return_value = SimpleNamespace(ui_language='pt')
    routes.get_pack_items.return_value = []
    cached.update(is_paid=False, description='', cover_image=None)
    response = await routes.pack_landing_page(request('de'), 'AbCd1234', 'fixture')
    html = response.body.decode()
    assert '<html lang="pt">' in html
    assert str(escape(Translator('pt').render('pack_public.description'))) in html
    assert str(escape(Translator('pt').render('marketplace.store.free'))) in html
    routes.get_current_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_custom_authored_content_is_unchanged(setup_route, monkeypatch, tmp_path):
    routes, cached = setup_route
    authored = '<html lang="fr"><p>Original creator HTML</p></html>'
    (tmp_path/'home.html').write_text(authored, encoding='utf-8')
    cached.update(has_custom_landing=True, path=tmp_path)
    monkeypatch.setattr(routes, 'is_creator_content_request', lambda request: True)
    response = await routes.pack_landing_page(request('ja'), 'AbCd1234', 'fixture')
    assert response.body.decode() == authored + '<!-- analytics fixture -->'
    routes.get_current_user.assert_not_awaited()
    routes.get_pack_items.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_public_not_found_is_localized(language, setup_route):
    routes, _ = setup_route
    response = await routes.pack_landing_page(request(language), 'invalid', 'fixture')
    assert response.status_code == 404
    assert str(escape(Translator(language).render('pack_public.not_found'))) in response.body.decode()
    routes.get_pack_landing_cached.assert_not_awaited()


def test_complete_pack_public_catalogs():
    catalogs = read_catalogs(Path('locales'))
    for language in LANGUAGES:
        assert catalogs[language]['pack_public'].keys() == catalogs['en']['pack_public'].keys()


@pytest.mark.asyncio
@pytest.mark.parametrize('user_id,admin,allowed', [(None,False,False), (99,False,False), (7,False,True), (99,True,True)])
async def test_fallback_preview_preserves_owner_and_admin_permissions(setup_route, user_id, admin, allowed):
    routes, _ = setup_route

    class Viewer:
        ui_language = 'ja'
        id = user_id

        @property
        async def is_admin(self):
            return admin

    routes.get_current_user.return_value = Viewer() if user_id is not None else None
    if allowed:
        response = await routes.pack_landing_page(request('ja', b'preview=1'), 'AbCd1234', 'fixture')
        assert response.status_code == 200
        assert b'analytics fixture' not in response.body
    else:
        with pytest.raises(HTTPException) as exc:
            await routes.pack_landing_page(request('ja', b'preview=1'), 'AbCd1234', 'fixture')
        assert exc.value.status_code == 403
        assert exc.value.detail == Translator('ja').render('pack_public.access_denied')
        routes.get_pack_items.assert_not_awaited()
    routes.get_current_user.assert_awaited_once()
