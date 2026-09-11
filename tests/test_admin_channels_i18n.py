"""Channel admin UI uses the operator locale without rewriting channel content."""
import importlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from markupsafe import escape
from starlette.datastructures import FormData
from starlette.requests import Request

from i18n import LANGUAGES, Translator, message_parameters, read_catalogs


class Operator:
    id = 7

    def __init__(self, language, admin=True):
        self.ui_language = language
        self.admin = admin

    @property
    async def is_admin(self):
        return self.admin


def request(channel='devices', form=None):
    result = Request({'type': 'http', 'method': 'POST' if form else 'GET',
                      'path': '/admin/' + channel, 'headers': [], 'query_string': b''})
    if form is not None:
        result._form = FormData(form)
    return result


def render_page(channel, language, populated=True):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda path: path, get_static_theme_hashes=lambda: {})
    original = 'Original <operator> 日本語 {username}'
    user = dict(id=7, username=original, conversation_id=42, answer_mode='voice',
                phone_display='+346***1234', chat_id_display='123***45', last_message=None)
    groups = [dict(id=8, name=original, username=original, slug='group-wire',
                   member_count=12345, member_names=original, conversation_id=42,
                   conversation_name=original)] if populated else []
    devices = [dict(id=9, display_name=original, username=original, slug='device-wire',
                    device_type='custom', enabled=True, last_seen_at=None,
                    memberships=[dict(id=8, name=original, is_primary_route_group=True)],
                    capabilities=dict(listen=True, speak=True, snapshot=True, ptz=True),
                    effective_binding=dict(status='bound', conversation_id=42,
                                           conversation_name=original, source='group', group_name=original),
                    direct_conversation_id=42, notes=original)] if populated else []
    events = [dict(device_name=original, device_slug='device-wire', created_at='2026-09-08 12:30:00',
                   direction='system', event_type='routing', status='routing_conflict',
                   conversation_id=42, latency_ms=12345)] if populated else []
    webhook = dict(match=False, current_url='', expected_url='https://fixture.example/whatsapp', service_name=original)
    return env.get_template(f'admin_{channel}.html').render(
        ui_language=language, username='Fixture', is_admin=True, is_user=True,
        marketplace=dict(discovery_enabled=True, creator_tools_enabled=True),
        request=request(channel), stats=dict.fromkeys(['total_devices', 'enabled_devices',
        'seen_recently', 'group_count', 'messages_today', 'active_users_today', 'text_count', 'voice_count'], 12345),
        message=original, error=original, token_block='AURVEK_DEVICE_TOKEN=wire-token',
        users=[user], current_user_id=7, groups=groups, devices=devices, conversations=[],
        basic_capabilities=('listen', 'speak', 'snapshot', 'ptz'), recent_events=events,
        telegram_configured=populated, twilio_configured=populated,
        telegram_users=[user] if populated else [], whatsapp_users=[user] if populated else [],
        unknown_user_message=original, welcome_message=original, bot_info=dict(username='wire_bot'),
        webhook_info=webhook, webhook_status=webhook, expected_webhook_url='https://fixture.example/telegram',
        admin_csrf_token='csrf-wire', require_phone_verification=True,
        telegram_retain_voice_notes=True, whatsapp_retain_voice_notes=True,
    )


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('channel', ['devices', 'telegram', 'whatsapp'])
def test_real_templates_preserve_wire_and_operator_content(channel, language):
    tr = Translator(language)
    html = render_page(channel, language)
    assert 'Original &lt;operator&gt; 日本語 {username}' in html
    assert tr.format_number(12345) in html
    assert str(escape(tr.render('admin_channels.messages_today'))) in html
    if channel == 'devices':
        assert 'value="listen"' in html and 'value="device-wire"' in html
        assert 'AURVEK_DEVICE_TOKEN=wire-token' in html
        assert str(escape(tr.render('admin_channels.capability.listen'))) in html
        assert str(escape(tr.render('admin_channels.event.routing_conflict'))) in html
        assert 'name="action" value="set_binding"' in html
        empty_key = 'no_devices_yet'
        empty_params = {}
    else:
        assert 'name="action" value="fix_webhook"' in html
        assert 'name="csrf_token" value="csrf-wire"' in html
        assert str(escape(tr.render('admin_channels.retain_voice_help'))) in html
        assert 'name="' + channel + '_retain_voice_notes" value="1" checked' in html
        empty_key = 'no_channel_users'
        empty_params = dict(channel='Telegram' if channel == 'telegram' else 'WhatsApp')
    assert str(escape(tr.render('admin_channels.' + empty_key, **empty_params))) in render_page(channel, language, False)


def test_complete_catalog_contracts():
    catalogs = read_catalogs(Path('locales'))
    source = catalogs['en']['admin_channels']
    for language in LANGUAGES:
        translated = catalogs[language]['admin_channels']
        assert translated.keys() == source.keys()
        for key, value in translated.items():
            params = {name: 'fixture' for name in message_parameters(value)}
            assert Translator(language).render('admin_channels.' + key, **params)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('channel', ['devices', 'telegram', 'whatsapp'])
async def test_access_denials_are_localized(channel, language):
    routes = importlib.import_module(f'integrations.{channel}.admin_routes')
    for name in (f'admin_{channel}', f'admin_{channel}_action' if channel == 'devices' else f'admin_{channel}_save'):
        with pytest.raises(HTTPException) as exc:
            await getattr(routes, name)(request(channel), Operator(language, False))
        assert exc.value.status_code == 403
        assert exc.value.detail == Translator(language).render('admin_channels.access_denied')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_device_success_and_safe_validation(monkeypatch, language):
    from integrations.devices import admin_routes as routes
    tr = Translator(language)
    monkeypatch.setattr(routes, 'update_device', AsyncMock())
    response = await routes.admin_devices_action(request(form=dict(action='update_device', device_id='42', display_name='Original', slug='wire')), Operator(language))
    assert parse_qs(urlsplit(response.headers['location']).query)['message'] == [tr.render('admin_channels.device_updated')]
    assert routes.update_device.await_args.kwargs['slug'] == 'wire'
    render = AsyncMock(return_value='rendered')
    monkeypatch.setattr(routes, '_render_admin_devices', render)
    for service_message, expected in (
        ('Device not found', tr.render('admin_channels.device_not_found')),
        ('Slug must use lowercase letters, numbers, and hyphens.', tr.render('admin_channels.field_slug', field=tr.render('admin_channels.slug'))),
        ('SECRET credential provider exception', tr.render('admin_channels.validation_failed')),
    ):
        routes.update_device.side_effect = routes.DeviceValidationError(service_message)
        await routes.admin_devices_action(request(form=dict(action='update_device', device_id='42')), Operator(language))
        assert render.await_args.kwargs['error'] == expected
    routes.update_device.reset_mock()
    await routes.admin_devices_action(request(form=dict(action='update_device', device_id='invalid')), Operator(language))
    assert render.await_args.kwargs['error'] == tr.render('admin_channels.field_number', field=tr.render('admin_channels.device'))
    routes.update_device.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('channel', ['telegram', 'whatsapp'])
async def test_channel_save_and_webhook_failure(monkeypatch, language, channel):
    routes = importlib.import_module(f'integrations.{channel}.admin_routes')
    tr = Translator(language)
    conn = AsyncMock()

    @asynccontextmanager
    async def connection(**kwargs):
        yield conn

    monkeypatch.setattr(routes, 'get_db_connection', connection)
    monkeypatch.setattr(routes, 'validate_mutation_request', lambda *args, **kwargs: None)
    provider = SimpleNamespace(set_webhook=AsyncMock(side_effect=RuntimeError('SECRET provider token')),
                               update_messaging_service=AsyncMock(side_effect=RuntimeError('SECRET provider token')))
    monkeypatch.setattr(routes, 'async_telegram' if channel == 'telegram' else 'async_twilio', provider)
    if channel == 'whatsapp':
        monkeypatch.setattr(routes, 'twilio_messaging_service_sid', 'sid-wire')
        monkeypatch.setattr(routes, 'set_phone_user_not_found', lambda value: None)
    save = getattr(routes, f'admin_{channel}_save')
    response = await save(request(channel, dict(action='save_config', unknown_user_message='Original 日本語', welcome_message='Hello {username}', **{channel + '_retain_voice_notes': '1'})), Operator(language))
    assert parse_qs(urlsplit(response.headers['location']).query)['message'] == [tr.render('admin_channels.configuration_saved')]
    assert conn.execute.await_args_list[0].args[1] == ('Original 日本語',)
    assert conn.execute.await_args_list[1].args[1] == ('Hello {username}',)
    assert conn.execute.await_args_list[3].args[1] == ('1',)
    response = await save(request(channel, dict(action='fix_webhook')), Operator(language))
    assert parse_qs(urlsplit(response.headers['location']).query)['error'] == [tr.render('admin_channels.webhook_failed')]
    assert 'SECRET' not in response.headers['location']


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['telegram', 'whatsapp'])
async def test_dashboard_hides_provider_details_and_skips_malformed_telegram_config(monkeypatch, channel):
    routes = importlib.import_module(f'integrations.{channel}.admin_routes')
    cursor = AsyncMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = (0,)
    users_cursor = AsyncMock()
    users_cursor.fetchall.return_value = [('bad-list', 1, '[]'), ('bad-nested', 2, '{"telegram":"bad"}'),
                                          ('valid', 123456, '{"telegram":{"conversation_id":42}}')]
    conn = AsyncMock()

    async def execute(sql, *params):
        return users_cursor if 'SELECT u.username, u.telegram_chat_id' in sql else cursor

    conn.execute.side_effect = execute

    @asynccontextmanager
    async def connection(**kwargs):
        yield conn

    monkeypatch.setattr(routes, 'get_db_connection', connection)
    monkeypatch.setattr(routes, 'get_template_context', AsyncMock(return_value={}))
    monkeypatch.setattr(routes, 'templates', SimpleNamespace(TemplateResponse=lambda name, context: context))
    monkeypatch.setattr(routes, 'ensure_csrf_token', lambda request: 'csrf-fixture')
    provider = SimpleNamespace(get_me=AsyncMock(side_effect=RuntimeError('SECRET provider token')),
                               get_messaging_service=AsyncMock(side_effect=RuntimeError('SECRET provider token')))
    monkeypatch.setattr(routes, 'async_telegram' if channel == 'telegram' else 'async_twilio', provider)
    if channel == 'whatsapp':
        monkeypatch.setattr(routes, 'twilio_messaging_service_sid', 'sid-wire')
        monkeypatch.setattr(routes, 'get_phone_user_not_found', lambda: 'Original operator message')
    result = await getattr(routes, 'admin_' + channel)(request(channel), Operator('ja'))
    status = result['webhook_info' if channel == 'telegram' else 'webhook_status']
    assert status['error'] == Translator('ja').render('admin_channels.webhook_check_error', provider='Telegram' if channel == 'telegram' else 'Twilio')
    assert 'SECRET' not in json.dumps(result)
    if channel == 'telegram':
        assert [user['username'] for user in result['telegram_users']] == ['valid']
