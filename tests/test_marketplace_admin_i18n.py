"""Pack administration localization through real templates and request handlers."""

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from i18n import LANGUAGES, Translator
from marketplace import config
from marketplace.routes import admin, packs
from marketplace.services.admin_localization import localized_config
from tests.test_marketplace_checkout_i18n import checkout_db, request


ROOT = Path(__file__).parents[1]


class Creator:
    id = 1
    username = 'Creator <name>'

    def __init__(self, language, admin=False):
        self.ui_language = language
        self.admin = admin

    @property
    async def is_admin(self):
        return self.admin

    @property
    async def is_user(self):
        return True


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('page', ['admin_packs.html', 'admin_packs_edit.html', 'marketplace/admin_marketplace.html'])
def test_admin_templates_full_base_payload_and_creator_escape(language, page):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=select_autoescape())
    pack = dict(id=20, name='Creator " \' <script> & pack', slug='creator-pack', tags=['Creator tag'],
                description='Creator description', status='published', item_count=1, max_items=10,
                is_paid=True, price=1234.50, created_at='2026-09-08 10:00:00', created_by_username='author',
                public_id='public-pack', landing_reg_config={})
    html = env.get_template(page).render(
        t=tr.render, format_number=tr.format_number, format_currency=tr.format_currency,
        ui_language=language, i18n_payload=tr.browser_payload,
        get_static_url=lambda url: url, get_static_theme_hashes=lambda: {},
        marketplace={'enabled': True, 'discovery_enabled': True},
        packs=[pack], pack=pack, pack_items=[dict(prompt_id=10, prompt_name=pack['name'], notice_period_snapshot=365)],
        welcome_message_content='<h1>Creator HTML</h1>', marketplace_admin_config=localized_config(tr),
    )
    payload = json.loads(re.search(r'id="aurvek-i18n">(.*?)</script>', html, re.S).group(1))
    assert set(payload['resources'][language]) == {'common', 'navigation', 'marketplace_admin'}
    assert 'marketplace_admin.' not in re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    if page != 'marketplace/admin_marketplace.html':
        assert str(escape(pack['name'])) in html
    if page == 'admin_packs_edit.html':
        assert 'value="1234.50"' in html  # The numeric input contract is not localized.
        assert 'value="modern" checked' in html and '<option value="es">' in html
        assert str(escape('<h1>Creator HTML</h1>')) in html
        assert 'onclick="removePackItem' not in html
    if language == 'en':
        for script in re.findall(r'<script\b([^>]*)>(.*?)</script>', html, re.S):
            if 'application/json' in script[0] or not script[1].strip():
                continue
            result = subprocess.run(['node', '--check'], input=script[1], text=True, capture_output=True)
            assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_pack_validation_and_owner_permissions_are_request_bound(checkout_db, monkeypatch, language):
    monkeypatch.setattr(config, 'marketplace_creator_tools_enabled', lambda: True)
    tr = Translator(language)
    with pytest.raises(HTTPException) as exc:
        await packs.api_create_pack(request({'name': ''}, language='de'), Creator(language))
    assert exc.value.status_code == 400
    assert exc.value.detail == tr.t('marketplace_admin.response.pack_name_is_required')
    with pytest.raises(HTTPException) as exc:
        packs._validate_tags(['a'] * 11, translator=tr)
    assert exc.value.detail == tr.t('marketplace_admin.response.maximum_value1_tags_allowed', value1=tr.format_number(10))
    outsider = Creator(language)
    outsider.id = 99
    with pytest.raises(HTTPException) as exc:
        await packs.api_delete_pack(request(language='de'), 20, outsider)
    assert exc.value.status_code == 403
    assert exc.value.detail == tr.t('marketplace_admin.response.access_denied')


@pytest.mark.asyncio
async def test_pack_update_retains_creator_content_and_price(checkout_db, monkeypatch):
    monkeypatch.setattr(config, 'marketplace_creator_tools_enabled', lambda: True)
    monkeypatch.setattr(packs, 'get_pricing_config', AsyncMock(return_value={'commission': 0.3}))
    content = 'Contenu du créateur <strong>unchanged</strong>'
    response = await packs.api_update_pack(20, request({'description': content, 'price': 12.34}), Creator('fr'))
    assert json.loads(response.body)['message'] == Translator('fr').t('marketplace_admin.response.pack_updated')
    _, connect = checkout_db
    async with connect() as conn:
        async with conn.execute('SELECT description, price, is_paid FROM PACKS WHERE id=20') as cursor:
            row = await cursor.fetchone()
    assert tuple(row) == (content, 12.34, 1)
    # Provision the unrelated minimum-item prerequisite directly in this temporary DB.
    async with connect() as conn:
        await conn.execute("INSERT INTO PROMPTS(id,name,created_by_user_id) VALUES(11,'Second creator prompt',1)")
        await conn.execute('INSERT INTO PACK_ITEMS(pack_id,prompt_id) VALUES(20,11)')
        await conn.commit()
    monkeypatch.setattr(packs, 'is_security_guard_enabled', AsyncMock(return_value=False))
    monkeypatch.setattr(packs, 'maybe_trigger_recalculation', AsyncMock())
    response = await packs.api_publish_pack(request(), 20, Creator('fr'))
    assert json.loads(response.body)['message'] == Translator('fr').t('marketplace_admin.response.pack_published')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_welcome_job_errors_do_not_publish_provider_details(checkout_db, monkeypatch, language):
    monkeypatch.setattr(packs, 'get_job', Mock(return_value={
        'pack_id': 20, 'status': 'failed', 'error': 'provider token SECRET', 'task_id': 'fixture',
    }))
    response = await packs.pack_welcome_wizard_status(request(language='de'), 20, 'fixture', Creator(language))
    body = json.loads(response.body)
    assert body['status'] == 'failed'
    assert body['error'] == Translator(language).t('marketplace_admin.response.job_failed')
    assert 'SECRET' not in response.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_controls_localize_validation_and_preserve_anonymous_response(monkeypatch, language):
    router = admin.create_router(AsyncMock())
    route = next(r.endpoint for r in router.routes if r.path == '/api/admin/marketplace-config' and 'POST' in r.methods)
    response = await route({'flags': {'unknown': True}}, request(language='de'), Creator(language, admin=True))
    assert response.status_code == 400
    assert json.loads(response.body)['message'] == Translator(language).t('marketplace_admin.config.unknown_flag')
    response = await route({}, request(language=language), None)
    assert response.status_code == 401
    assert json.loads(response.body) == {'error': 'unauthenticated', 'redirect': '/login'}
    assert 'set-cookie' in response.headers
    monkeypatch.setenv('MARKETPLACE_CHECKOUT_ENABLED', 'false')
    response = await route({'flags': {'marketplace_checkout_enabled': True}}, request(), Creator(language, admin=True))
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body['message'] == Translator(language).t('marketplace_admin.config.locked')
    assert body['locked_flags'] == [{
        'key': 'marketplace_checkout_enabled', 'env_var': 'MARKETPLACE_CHECKOUT_ENABLED',
        'label': Translator(language).t('marketplace_admin.flags.checkout.label'),
    }]


def test_admin_catalog_references_and_stable_flag_values():
    catalog = json.loads((ROOT / 'locales/en/marketplace_admin.json').read_text(encoding='utf-8'))
    for name in ['templates/admin_packs.html', 'templates/admin_packs_edit.html',
                 'templates/marketplace/admin_marketplace.html', 'data/static/js/admin-packs.js',
                 'marketplace/routes/packs.py', 'marketplace/routes/admin.py']:
        source = (ROOT / name).read_text(encoding='utf-8')
        refs = set(re.findall(r"['\"]marketplace_admin\.([^'\"]+)['\"]", source))
        refs.discard('flags.')  # Explicit fixed flag keys in the owning presenter.
        assert refs <= catalog.keys(), (name, refs - catalog.keys())
    before = config.get_marketplace_config_state()
    translated = localized_config(Translator('ja'))
    assert translated['status'] == before['status']
    for raw, shown in zip(before['flags'], translated['flags']):
        for key in ['key', 'env_var', 'desired', 'effective', 'env_override', 'source']:
            assert raw[key] == shown[key]
