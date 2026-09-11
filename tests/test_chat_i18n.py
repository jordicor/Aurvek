import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, StreamingResponse

from i18n import LANGUAGES, Translator
from chat.services.localization import (
    chat_translator, localized_chat_stream, localize_chat_response,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
async def test_stream_translates_errors_preserving_content_protocol_and_retry(language):
    translator = Translator(language)
    content = b'data: {"content":"' + '日本語 {error} My Bookmarks'.encode() + b'"}\n\n'
    error = {
        "error": "private provider diagnostic", "error_code": "pdf_too_large",
        "provider_message": "private provider diagnostic", "retry_hint": "English hint",
        "filename": "<report>.pdf", "pages": 80, "retry_token": "opaque-token",
        "range_retry_available": True,
    }
    error_frame = b'id: 8\r\ndata: ' + json.dumps(error).encode() + b'\r\n\r\n'
    ending = b'data: {"message_ids":{"user":10,"bot":11}}\n\ndata: [DONE]\n\n'
    raw = b': keepalive\n\n' + content + error_frame + ending
    closed = []

    async def source():
        try:
            # Deliberately split UTF-8 and SSE boundaries, including CRLF.
            for start in range(0, len(raw), 7):
                yield raw[start:start + 7]
        finally:
            closed.append(True)

    result = b''.join([chunk async for chunk in localized_chat_stream(source(), translator)])
    assert b': keepalive\n\n' + content in result
    assert result.endswith(ending)
    assert b'id: 8\r\n' in result
    assert b'private provider' not in result
    error_data = result.split(b'id: 8\r\ndata: ', 1)[1].split(b'\r\n\r\n', 1)[0]
    localized = json.loads(error_data)
    assert localized['error'] == translator.render('chat_errors.pdf_too_large')
    assert localized['retry_hint'] == localized['error']
    for key in ('error_code', 'filename', 'pages', 'retry_token', 'range_retry_available'):
        assert localized[key] == error[key]
    assert closed == [True]


@pytest.mark.asyncio
async def test_concurrent_streams_capture_independent_languages_and_close_source():
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = []

    async def source(label):
        try:
            entered.set()
            await release.wait()
            yield 'data: {"error":"raw","error_code":"insufficient_balance"}\n\n'
            yield ': next\n\n'
        finally:
            finished.append(label)

    user = SimpleNamespace(ui_language='ja')
    japanese = localized_chat_stream(source('ja'), chat_translator(user))
    user.ui_language = 'de'
    german = localized_chat_stream(source('de'), chat_translator(user))
    pending = [asyncio.create_task(anext(stream)) for stream in (japanese, german)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*pending)
    for language, chunk in zip(('ja', 'de'), results):
        assert json.loads(chunk[6:])['error'] == Translator(language).render('chat_errors.insufficient_balance')
    await japanese.aclose()
    await german.aclose()
    assert sorted(finished) == ['de', 'ja']


def test_frame_preference_is_separate_from_loaded_account_and_responses_keep_headers():
    user = SimpleNamespace(ui_language='es', embed_principal=SimpleNamespace(ui_language='ja'))
    translator = chat_translator(user)
    assert translator.language == 'ja'
    assert user.ui_language == 'es'
    response = JSONResponse(
        {'error': 'secret SQL diagnostic', 'error_code': 'unknown_provider_code', 'action': 'retry'},
        status_code=503, headers={'Retry-After': '30'},
    )
    assert localize_chat_response(response, translator) is response
    assert response.status_code == 503
    assert response.headers['retry-after'] == '30'
    assert int(response.headers['content-length']) == len(response.body)
    assert json.loads(response.body) == {
        'error': translator.render('chat_errors.request_failed'),
        'error_code': 'unknown_provider_code', 'action': 'retry',
    }


@pytest.mark.asyncio
async def test_adapting_stream_preserves_response_lifecycle_and_nonerror_json():
    async def source():
        yield 'data: {"content":"Original response"}\n\n'
    background = object()
    response = StreamingResponse(source(), media_type='text/event-stream', background=background)
    adapted = localize_chat_response(response, Translator('es'))
    assert adapted is response
    assert adapted.background is background
    assert b''.join([chunk async for chunk in adapted.body_iterator]) == b'data: {"content":"Original response"}\n\n'
    content = JSONResponse({'content': 'Original response', 'message_ids': {'bot': 42}})
    original_body = content.body
    assert localize_chat_response(content, Translator('ja')).body == original_body
