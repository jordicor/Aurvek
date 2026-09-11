"""Prompt editors render real catalogs and exercise their rendered JavaScript without providers."""
import json
import re
import shutil
import subprocess
import inspect
import io
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from fastapi import HTTPException, Request, UploadFile
from jinja2 import Environment, FileSystemLoader
from i18n import LANGUAGES, Translator


def render_editor(name, language):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {},
                       url_for=lambda name: '/prompts')
    return env.get_template('prompts/' + name + '_prompt.html').render(
        ui_language=language, username='Creator', is_admin=True, is_user=True, is_owner=True,
        marketplace={'discovery_enabled': True, 'creator_tools_enabled': True},
        prompt_id=27, prompt_name='Original <name>', prompt_text='Untranslated <instructions>',
        description='Original description', welcome_message_content='<h1>Creator HTML</h1>',
        editor_ids=[9], editors=[(9, 'Editor')], users=[(9, 'Editor')],
        voice_code='voice-wire', voice_catalog_id=3, forced_reasoning_selection={'mode': 'default'},
        allowed_llms_json='[5]', phone_settings_csrf_token='fixture-csrf',
        markup_per_mtokens=12.345, purchase_price=23.45, is_paid=True,
        is_public=True, allow_in_packs=True, pack_notice_period_days=90,
        watchdog_config={'pre_watchdog': {'objectives': [], 'llm_id': None},
                         'post_watchdog': {'objectives': [], 'thresholds': {}, 'llm_id': None}},
        gransabio_config={'qa_layers': [{'name': 'Creator QA', 'min_score': 7.25}]},
    )


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('name', ['create', 'edit'])
def test_prompt_editor_real_render_and_runtime(name, language):
    html = render_editor(name, language)
    tr = Translator(language)
    assert tr.render('prompt_editor.runtime.' + ('create_new_prompt' if name == 'create' else 'edit_prompt')) in html
    assert 'value="restricted"' in html and 'value="forced"' in html
    assert 'name="markup_per_mtokens"' in html
    if name == 'create':
        from markupsafe import escape
        assert 'title="' + str(escape(tr.render('prompt_editor.attribute.monetization_help'))) + '"' in html
    if name == 'edit':
        assert 'Untranslated &lt;instructions&gt;' in html
        assert '&lt;h1&gt;Creator HTML&lt;/h1&gt;' in html
        assert 'value="12.345"' in html and 'value="23.45"' in html
        assert 'fixture-csrf' in html
        assert tr.render('prompt_editor.notice.current', period=tr.render('prompt_editor.notice.days', count=90, number=tr.format_number(90))) in html
    payload = json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>', html, re.S)[1])
    assert 'prompt_editor' in payload['resources'][language]
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/prompt_editor_rendered.cjs', name], input=html,
                            text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr

@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_prompt_editor_producer_validation_and_unchanged_watchdog_values(language):
    from fastapi import HTTPException
    import prompts

    tr = Translator(language)
    with pytest.raises(HTTPException) as restricted:
        await prompts._validate_prompt_llm_selection('restricted', None, '[]', translator=tr)
    assert restricted.value.status_code == 400
    assert restricted.value.detail == tr.render('prompt_editor_errors.restricted_mode_requires_valid_model_ids')
    with pytest.raises(HTTPException) as reasoning:
        await prompts._validated_reasoning_json(llm_id=None, mode='high', budget_tokens=None,
            inherit_value='default', field_label=tr.render('prompt_editor_errors.prompt_thinking'), translator=tr)
    assert reasoning.value.detail == tr.render('prompt_editor_errors.requires_a_specific_ai_model', field=tr.render('prompt_editor_errors.prompt_thinking'))
    value = {'post_watchdog': {'mode':'interview', 'objectives':['Creator objective'], 'steering_prompt':'Creator instruction', 'frequency':2}}
    saved = prompts.validate_watchdog_config(value, translator=tr)
    assert saved['post_watchdog']['mode'] == 'interview'
    assert saved['post_watchdog']['objectives'] == ['Creator objective']
    assert saved['post_watchdog']['steering_prompt'] == 'Creator instruction'
    assert saved['post_watchdog']['frequency'] == 2
    with pytest.raises(ValueError) as invalid:
        prompts.validate_watchdog_config({'post_watchdog': {'frequency':200}}, translator=tr)
    assert str(invalid.value) == tr.render('prompt_editor_errors.post_watchdog_frequency_must_be_between_and',
        watchdog_freq_min=tr.format_number(prompts.WATCHDOG_FREQ_MIN), watchdog_freq_max=tr.format_number(prompts.WATCHDOG_FREQ_MAX))


class EditorUser:
    id = 17
    username = 'creator'

    def __init__(self, language):
        self.ui_language = language

    @property
    async def is_admin(self):
        return True

    @property
    async def is_user(self):
        return True


def editor_request(language):
    return Request({'type': 'http', 'headers': [(b'accept-language', language.encode())]})


def create_form(language):
    import prompts
    values = {name: parameter.default.default
              for name, parameter in inspect.signature(prompts.create_prompt_post).parameters.items()
              if hasattr(parameter.default, 'default')}
    values.update(request=editor_request(language), current_user=EditorUser(language),
                  name='Example', prompt='Creator instructions', description='Description')
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_reasoning_failure_does_not_expose_engine_diagnostics(monkeypatch, language):
    import prompts
    tr = Translator(language)
    monkeypatch.setattr(prompts, '_shared_llm_configuration', AsyncMock(return_value=({}, {})))
    # The real validator rejects an unknown mode; its English diagnostic stays internal.
    with pytest.raises(HTTPException) as error:
        await prompts._validated_reasoning_json(llm_id=1, mode='private-unknown-mode',
            budget_tokens=None, inherit_value='default', field_label=tr.t('prompt_editor_errors.prompt_thinking'), translator=tr)
    assert error.value.status_code == 400
    assert error.value.detail == tr.t('prompt_editor_errors.field_error',
        field=tr.t('prompt_editor_errors.prompt_thinking'), error=tr.t('chat_errors.invalid_reasoning_selection'))
    assert 'private-unknown-mode' not in error.value.detail


@pytest.mark.parametrize('language', LANGUAGES)
def test_gransabio_validation_preserves_actionable_fields(language):
    from gransabio_service import validate_merged_config
    tr = Translator(language)
    valid_layer = {'name': 'Creator QA', 'description': 'Creator description', 'criteria': 'Creator criteria'}
    cases = [
        ({}, 'gransabio_generator_required', {}),
        ({'generator_model': 'wire-model', 'qa_layers': [{}]}, 'gransabio_layer_name_required', {'layer': tr.format_number(1)}),
        ({'generator_model': 'wire-model', 'qa_layers': [dict(valid_layer, min_score=11)]}, 'gransabio_layer_score_range', {'layer': tr.format_number(1)}),
        ({'generator_model': 'wire-model', 'qa_layers': [valid_layer]}, 'gransabio_models_required', {}),
    ]
    for config, key, values in cases:
        valid, message = validate_merged_config(config, translator=tr)
        assert not valid
        assert message == tr.t('prompt_editor_errors.' + key, **values)
        assert 'wire-model' not in message and 'Creator' not in message
    assert validate_merged_config({}) == (False, 'generator_model is required')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_create_gransabio_errors_are_localized_and_safe(monkeypatch, language):
    import prompts
    import gransabio_config
    tr = Translator(language)
    log = Mock()
    monkeypatch.setattr(prompts, 'logger', log)
    load = AsyncMock(return_value={})
    monkeypatch.setattr(gransabio_config, 'get_gransabio_config', load)
    values = create_form(language)
    values.update(gransabio_enabled=True)
    response = await prompts.create_prompt_post(**values)
    assert response.status_code == 400
    message = json.loads(response.body)['message']
    assert tr.t('prompt_editor_errors.gransabio_generator_required') in message
    load.side_effect = RuntimeError('private database diagnostic')
    response = await prompts.create_prompt_post(**values)
    assert response.status_code == 400
    assert json.loads(response.body)['message'] == tr.t('prompt_editor_errors.gransabio_config_validation_error')
    log.exception.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_prompt_image_failures_preserve_validation_status_and_hide_diagnostics(monkeypatch, tmp_path, language):
    import prompts
    tr = Translator(language)
    db_path = tmp_path / 'image.db'
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript("""
            CREATE TABLE Users (id INTEGER, username TEXT);
            CREATE TABLE PROMPT_PERMISSIONS (prompt_id INTEGER, user_id INTEGER, permission_level TEXT);
            INSERT INTO Users VALUES (17, 'creator');
            INSERT INTO PROMPT_PERMISSIONS VALUES (27, 17, 'owner');
        """)

    @asynccontextmanager
    async def connection(**kwargs):
        async with aiosqlite.connect(db_path) as conn:
            yield conn

    monkeypatch.setattr(prompts, 'get_db_connection', connection)
    monkeypatch.setattr(prompts, 'users_directory', str(tmp_path))
    monkeypatch.setattr(prompts, 'MAX_IMAGE_UPLOAD_SIZE', 2)
    with pytest.raises(HTTPException) as too_large:
        await prompts.process_prompt_image_upload(27, UploadFile(file=io.BytesIO(b'large')),
            {'name': 'Example'}, EditorUser(language))
    assert too_large.value.status_code == 400
    assert too_large.value.detail == tr.t('prompt_editor_errors.image_too_large_maximum_size_is_mb', megabytes=tr.format_number(0))
    monkeypatch.setattr(prompts, 'MAX_IMAGE_UPLOAD_SIZE', 1024)
    with pytest.raises(HTTPException) as invalid:
        await prompts.process_prompt_image_upload(27, UploadFile(file=io.BytesIO(b'not an image')),
            {'name': 'Example'}, EditorUser(language))
    assert invalid.value.status_code == 500
    assert invalid.value.detail == tr.t('prompt_editor_errors.error_processing_image')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_delete_failure_is_safe_and_rolls_back(monkeypatch, tmp_path, language):
    import prompts
    tr = Translator(language)
    db_path = tmp_path / 'delete.db'
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript("""
            CREATE TABLE Prompts (id INTEGER, name TEXT);
            CREATE TABLE Users (id INTEGER, username TEXT);
            CREATE TABLE PROMPT_PERMISSIONS (prompt_id INTEGER, user_id INTEGER, permission_level TEXT);
            INSERT INTO Prompts VALUES (27, 'Example');
            INSERT INTO Users VALUES (17, 'creator');
            INSERT INTO PROMPT_PERMISSIONS VALUES (27, 17, 'owner');
        """)

    @asynccontextmanager
    async def connection(**kwargs):
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            yield conn

    monkeypatch.setattr(prompts, 'get_db_connection', connection)
    with pytest.raises(HTTPException) as failure:
        await prompts.delete_prompt(27, EditorUser(language))
    assert failure.value.status_code == 500
    assert failure.value.detail == tr.t('prompt_editor_errors.error_deleting_prompt')
    async with connection() as conn:
        assert (await (await conn.execute('SELECT count(*) FROM Prompts')).fetchone())[0] == 1
    with pytest.raises(HTTPException) as missing:
        await prompts.delete_prompt(28, EditorUser(language))
    assert missing.value.status_code == 404
    assert missing.value.detail == tr.t('prompt_editor_errors.prompt_not_found')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_create_database_failure_is_safe(monkeypatch, language):
    import prompts
    tr = Translator(language)

    @asynccontextmanager
    async def connection(**kwargs):
        # A fresh database produces a genuine missing-table error on insert.
        async with aiosqlite.connect(':memory:') as conn:
            yield conn

    monkeypatch.setattr(prompts, 'get_db_connection', connection)
    monkeypatch.setattr(prompts, '_resolve_submitted_prompt_voice', AsyncMock(return_value=3))
    log = Mock()
    monkeypatch.setattr(prompts, 'logger', log)
    with pytest.raises(HTTPException) as failure:
        await prompts.create_prompt_post(**create_form(language))
    assert failure.value.status_code == 500
    assert failure.value.detail == tr.t('prompt_editor_errors.error_creating_prompt')
    log.exception.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_watchdog_billing_guard_is_safe_and_stops_before_reservation(monkeypatch, language):
    import prompts
    import ai_runtime.billing
    tr = Translator(language)

    @asynccontextmanager
    async def connection(**kwargs):
        cursor = AsyncMock()
        cursor.fetchone.return_value = None
        conn = AsyncMock()
        conn.execute.return_value = cursor
        yield conn

    request = editor_request(language)
    request._json = {'llm_id': 1, 'prompt_text': 'Creator instructions'}
    monkeypatch.setattr(prompts, 'get_db_connection', connection)
    monkeypatch.setattr(prompts, 'get_llm_info', AsyncMock(return_value={'machine': 'Claude', 'model': 'wire-model'}))
    monkeypatch.setattr(prompts, 'get_user_api_key_mode', AsyncMock(return_value='system'))
    monkeypatch.setattr(prompts, 'resolve_api_key_for_provider', Mock(return_value=(None, True)))
    monkeypatch.setattr(ai_runtime.billing, 'assert_billable_claude_system_key', Mock(return_value='private billing configuration diagnostic'))
    reserve = AsyncMock()
    monkeypatch.setattr(prompts, 'reserve_ai_provider_call', reserve)
    with pytest.raises(HTTPException) as failure:
        await prompts.watchdog_suggest_config(request, EditorUser(language))
    assert failure.value.status_code == 500
    assert failure.value.detail == tr.t('prompt_editor_errors.billing_is_temporarily_unavailable_please_try_again')
    reserve.assert_not_awaited()
