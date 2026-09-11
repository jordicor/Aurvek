"""Landing builders: real localized templates, request handlers, files and browser behavior."""
import base64
import io
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from httpx import ASGITransport, AsyncClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from i18n import LANGUAGES, Translator, get_catalogs, get_translator
from marketplace import config
from marketplace.routes import packs, prompt_landing_builder as builder, custom_domains as domains
from tests.test_marketplace_admin_i18n import Creator
from tests.test_marketplace_checkout_i18n import checkout_db, request

ROOT = Path(__file__).parents[1]
NODE = 'C:/Program Files/nodejs/node.exe'
PAGES = ['landing_config.html', 'pack_landing_config.html', 'user_branding.html',
         'web/component_edit.html', 'web/components_list.html', 'web/web_edit.html']


def render_page(page, language, **overrides):
    tr = Translator(language)
    context = dict(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                   format_currency=tr.format_currency, ui_language=language, i18n_payload=tr.browser_payload,
                   get_static_url=lambda x: x, get_static_theme_hashes=lambda: {},
                   marketplace={'enabled': True, 'discovery_enabled': True},
                   prompt={'id': 10, 'name': 'Creator <name>'}, pack={'id': 20, 'name': 'Creator <pack>'},
                   prompt_info={'name': 'Creator name', 'created_by_username': 'creator'},
                   prompt_id=10, prompt_name='Creator name', pages=[{'name': 'home', 'is_home': True, 'url_path': '/'}],
                   components={'html': ['header'], 'css': ['style'], 'js': ['app']},
                   components_by_type={'html': ['header']}, has_home_page=True, public_url='https://fixture.test/p/test/',
                   is_admin=False, user_slots={'used': 1, 'purchased': 2, 'available': 1},
                   domain_config={'domain': 'example.test', 'is_active': True, 'verification_status': 'verified',
                                  'activated_at': '2026-09-08'}, slot_price=25, cname_target='cname.test',
                   wizard_available=False, section='home', content='<p>Creator content & accents: café</p>',
                   component_type='html', component_name='header', is_pack=False)
    context.update(overrides)
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=select_autoescape())
    return env.get_template(page).render(**context)


def browser_parts(html):
    payload = json.loads(re.search(r'id="aurvek-i18n">(.*?)</script>', html, re.S).group(1))
    scripts = [body for attrs, body in re.findall(r'<script\b([^>]*)>(.*?)</script>', html, re.S)
               if body.strip() and 'application/json' not in attrs]
    return payload, scripts[-1]


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('page', PAGES)
def test_real_templates_have_complete_locale_and_preserve_creator_data(language, page):
    html = render_page(page, language)
    payload, script = browser_parts(html)
    resources = payload['resources']
    assert set(resources[language]['landing_builder']) == set(get_catalogs()['en']['landing_builder'])
    assert 'landing_builder.' not in re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    if page.startswith('web/') and page != 'web/components_list.html':
        assert str(escape('<p>Creator content & accents: café</p>')) in html
    if page in ['landing_config.html', 'pack_landing_config.html']:
        assert 'value="modern" checked' in html and '<option value="es">' in html
        assert 'value="3" min="1" max="60"' in html
    result = subprocess.run([NODE, '--check'], input=script, text=True, capture_output=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('language', LANGUAGES)
def test_prompt_domain_variants_and_money(language):
    tr = Translator(language)
    html = render_page('landing_config.html', language, has_home_page=False,
                       domain_config={'domain': 'example.test', 'is_active': False, 'verification_status': 'verified'},
                       user_slots={'used': 2, 'purchased': 2, 'available': 0})
    assert str(escape(tr.t('landing_builder.ui.buy_slot_activate_amount', amount=tr.format_currency(25, 'USD', 2)))) in html
    assert str(escape(tr.t('landing_builder.ui.no_home_html_page_exists_yet_create_one_to_make'))) in html
    assert 'onclick="purchaseAndActivate()"' in html


@pytest.fixture
def landing_db(checkout_db, monkeypatch, tmp_path):
    path, connect = checkout_db
    monkeypatch.setattr(config, 'marketplace_creator_tools_enabled', lambda: True)
    monkeypatch.setattr(config, 'marketplace_public_landings_enabled', lambda: True)
    monkeypatch.setattr(builder, 'get_db_connection', connect)
    monkeypatch.setattr(domains, 'get_db_connection', connect)
    async def context(req, current_user):
        get_translator(req, current_user)
        return {'request': req}
    monkeypatch.setattr(packs, 'get_template_context', context)
    async def pack_dir(*args, **kwargs):
        return tmp_path / 'pack', 'Creator <name>'
    monkeypatch.setattr(packs, '_get_pack_dir_and_info', pack_dir)
    monkeypatch.setattr(packs, 'invalidate_pack_landing_cache', Mock())
    return path, connect, tmp_path / 'pack'


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_real_sqlite_row_pack_save_status_and_owner_checks(landing_db, monkeypatch, language):
    _, connect, pack_dir = landing_db
    tr = Translator(language)
    pack_dir.mkdir()
    content = '<h1>Creator content: café</h1>'
    response = await packs.pack_landing_save_page(request(language='de'), 20, 'home',
                                                base64.b64encode(content.encode()).decode(), Creator(language))
    assert json.loads(response.body)['message'] == tr.t('landing_builder.response.changes_saved_successfully')
    assert content in (pack_dir / 'home.html').read_text(encoding='utf8')
    async with connect() as conn:
        row = await (await conn.execute('SELECT has_custom_landing FROM PACKS WHERE id=20')).fetchone()
    assert row[0] == 1
    monkeypatch.setattr(packs, 'get_job', lambda task_id: {'task_id': task_id, 'pack_id': 20,
                                                        'status': 'completed', 'files_created': ['home.html']})
    status = await packs.pack_ai_wizard_status(request(), 20, 'test-job', Creator(language))
    assert json.loads(status.body)['files_created'] == ['home.html']
    outsider = Creator(language)
    outsider.id = 99
    with pytest.raises(HTTPException) as exc:
        await packs.pack_landing_save_page(request(), 20, 'home', base64.b64encode(b'overwrite').decode(), outsider)
    assert exc.value.status_code == 403
    assert content in (pack_dir / 'home.html').read_text(encoding='utf8')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_request_validation_has_locale_and_keeps_paths_and_images(landing_db, monkeypatch, language):
    tr = Translator(language)
    with pytest.raises(HTTPException) as exc:
        await packs.pack_landing_create_page(request({'page_name': '../escape'}, 'de'), 20, Creator(language))
    assert exc.value.detail == tr.t('landing_builder.response.invalid_page_name')
    with pytest.raises(HTTPException) as exc:
        await packs.pack_landing_delete_page(request(), 20, 'home', Creator(language))
    assert exc.value.detail == tr.t('landing_builder.response.cannot_delete_the_home_page')
    with pytest.raises(HTTPException) as exc:
        await packs.pack_landing_upload_images(request(), 20,
              images=[UploadFile(filename='broken.png', file=io.BytesIO(b'not an image'))], names=['broken'], current_user=Creator(language))
    assert exc.value.detail == tr.t('landing_builder.response.invalid_image_file_value1', value1='broken.png')
    monkeypatch.setattr(builder, 'can_manage_prompt', AsyncMock(return_value=False))
    response = await builder.delete_landing_page(request(), 10, 'info', Creator(language))
    assert response.status_code == 403
    assert json.loads(response.body)['message'] == tr.t('landing_builder.response.access_denied')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_disabled_creator_features_return_localized_not_found_before_work(monkeypatch, language):
    monkeypatch.setattr(config, 'marketplace_creator_tools_enabled', lambda: False)
    for handler, args in [(builder.create_landing_page, (request(), 10, Creator(language))),
                          (domains.get_slots_info, (request(), Creator(language)))]:
        with pytest.raises(HTTPException) as exc:
            await handler(*args)
        assert exc.value.status_code == 404
        assert exc.value.detail == Translator(language).t('marketplace_admin.response.not_found')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_real_domain_http_validation_localized_without_dns_or_db(landing_db, monkeypatch, language):
    from auth import get_current_user
    app = FastAPI()
    app.include_router(domains.router)
    app.dependency_overrides[get_current_user] = lambda: Creator(language, admin=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='https://fixture.test') as client:
        response = await client.post('/api/domains/10/configure', json={'domain': '<invalid>'}, headers={'accept-language': 'de'})
    assert response.status_code == 422
    assert response.json()['detail'][0]['msg'] == Translator(language).t('landing_builder.response.enter_a_valid_domain_name_on_a_separate_site_from')
    assert response.json()['detail'][0]['loc'] == ['body', 'domain']


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_geo_display_data_localized_without_mutating_ids_or_shared_data(landing_db, monkeypatch, language):
    _, connect, _ = landing_db
    monkeypatch.setattr(builder, 'can_manage_prompt', AsyncMock(return_value=True))
    monkeypatch.setattr(builder.CloudflareGeoClient, 'is_configured', lambda self: True)
    async with connect() as conn:
        await conn.execute("INSERT OR REPLACE INTO SYSTEM_CONFIG(key,value) VALUES('geo_enabled','0')")
        await conn.commit()
    original = json.dumps(builder.get_all_geo_data(), sort_keys=True)
    response = await builder.get_landing_geo(request(), 10, Creator(language))
    data = json.loads(response.body)
    assert response.status_code == 200, data
    assert {c['code'] for c in data['geo_data']['countries']} == {c['code'] for c in builder.get_all_geo_data()['countries']}
    assert json.dumps(builder.get_all_geo_data(), sort_keys=True) == original
    if language != 'en':
        assert data['geo_data']['continents']['EU'] != 'Europe' or language in ['fr']


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('page', PAGES)
def test_rendered_browser_behavior_uses_locale_and_keeps_wire_values(language, page):
    payload, script = browser_parts(render_page(page, language))
    result = subprocess.run([NODE, str(ROOT / 'tests/js/landing_builder_behavior.cjs')],
                            input=json.dumps({'payload': payload, 'script': script, 'page': page}),
                            text=True, capture_output=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('language', LANGUAGES)
def test_publication_checks_localize_guidance_and_preserve_matching(language):
    from tools.prompt_pipeline import verifier
    tr = Translator(language)
    scenarios = [
        ('<p>Get your dashboard and unlimited exports.</p>', {}),
        ('<section class="testimonials">Excellent service</section>', {}),
        ('<p>Upload your files. Use web search.</p>', {'file_upload': False, 'web_search': 'disabled'}),
        ('<a href="register">Continue</a>', {'access': 'paid', 'price': 1234.50}),
        ('<p>Buy now</p>', {'access': 'free'}),
        ('<nav>Nav</nav><header>hero</header><div>features grid cols</div><section>testimonials</section><footer>Footer</footer>', {}),
    ]
    for html, truth in scenarios:
        original = verifier.check_publication_readiness(html, truth)
        localized = verifier.check_publication_readiness(html, truth, translator=tr)
        assert original and len(localized) == len(original)
        assert not any(message.startswith(('TRUTH:', 'HALLUCINATION:', 'TESTIMONIAL:', 'FREE CLAIM:', 'STRUCTURE:')) for message in localized)
    paid = verifier.check_publication_readiness('<a href="register">Continue</a>', {'access': 'paid', 'price': 1234.50}, translator=tr)
    assert tr.t('landing_publication.paid_price', amount=tr.format_currency(1234.50, 'USD', 2)) in paid
    assert set(get_catalogs()[language]['landing_publication']) == set(get_catalogs()['en']['landing_publication'])


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_branding_real_handlers_validate_and_preserve_authored_fields(landing_db, monkeypatch, language):
    import app as application
    _, connect, _ = landing_db
    monkeypatch.setattr(application, 'get_db_connection', connect)
    tr = Translator(language)
    response = await application.update_my_branding(request({'brand_color_primary': []}), Creator(language))
    assert response.status_code == 400
    assert json.loads(response.body)['error'] == tr.t('landing_builder.response.invalid_primary_color_format_use_hex_format_rrggbb')
    response = await application.update_my_branding(request({'forced_theme': 'nonexistent'}), Creator(language))
    assert response.status_code == 400
    assert json.loads(response.body)['error'] == tr.t('landing_builder.response.select_a_valid_theme')
    data = {'company_name': 'Créateur <company>', 'footer_text': 'Authored footer',
            'email_signature': 'Authored signature', 'brand_color_primary': '#123456',
            'brand_color_secondary': '#654321', 'forced_theme': 'dark', 'disable_theme_selector': True}
    response = await application.update_my_branding(request(data, language='de'), Creator(language))
    assert json.loads(response.body)['message'] == tr.t('landing_builder.response.branding_settings_saved_successfully')
    async with connect() as conn:
        row = await (await conn.execute('SELECT company_name,footer_text,email_signature,brand_color_primary,forced_theme FROM USER_BRANDING WHERE user_id=1')).fetchone()
    assert tuple(row) == ('Créateur <company>', 'Authored footer', 'Authored signature', '#123456', 'dark')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_publication_unavailable_is_localized_and_fail_closed(tmp_path, monkeypatch, language):
    monkeypatch.setattr(builder, '_PROMPT_PIPELINE_DIR', tmp_path / 'missing')
    errors = await builder._get_landing_publication_errors(10, '<p>Creator page</p>', translator=Translator(language))
    assert errors == [Translator(language).t('landing_publication.unavailable')]
