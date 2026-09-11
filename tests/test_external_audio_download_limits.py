"""Mechanical download limits using an in-process HTTP transport, without providers."""

import asyncio
import gzip

import httpx
import pytest
from fastapi import HTTPException

from integrations import media


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, stall=False):
        self.chunks = chunks
        self.stall = stall
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def serve(monkeypatch):
    client_class = httpx.AsyncClient

    def install(handler):
        def create_client(**kwargs):
            return client_class(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(media.httpx, "AsyncClient", create_client)

    return install


@pytest.mark.asyncio
async def test_exact_limit_accepts_stream_and_requests_no_compression(serve):
    stream = TrackedStream([b"1234", b"5678"])

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Length": "8"}, stream=stream)

    serve(handler)
    assert await media.download_external_audio("https://media.test/audio", max_bytes=8) == b"12345678"
    assert stream.closed


@pytest.mark.asyncio
async def test_declared_oversize_rejected_without_reading_body(serve):
    stream = TrackedStream([b"oversized"])
    serve(lambda request: httpx.Response(200, headers={"Content-Length": "100"}, stream=stream))
    with pytest.raises(HTTPException) as caught:
        await media.download_external_media("https://media.test/image", max_bytes=8)
    assert caught.value.status_code == 413
    assert stream.consumed == 0
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "3"}])
async def test_actual_stream_limit_stops_missing_or_false_content_length(serve, headers):
    stream = TrackedStream([b"1234", b"56789", b"must not be consumed"])
    serve(lambda request: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(HTTPException) as caught:
        await media.download_external_media("https://media.test/audio", max_bytes=8)
    assert caught.value.status_code == 413
    assert stream.consumed == 2
    assert stream.closed


@pytest.mark.asyncio
async def test_bounded_download_rejects_compression_before_reading(serve):
    stream = TrackedStream([gzip.compress(b"audio" * 100)])
    serve(lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream))
    with pytest.raises(HTTPException) as caught:
        await media.download_external_media("https://media.test/audio", max_bytes=8)
    assert caught.value.status_code == 400
    assert stream.consumed == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_redirect_body_is_not_read_and_default_audio_still_decodes(serve):
    redirect = TrackedStream([b"must not be consumed"])
    audio = TrackedStream([gzip.compress(b"audio")])

    def handler(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"Location": "/audio"}, stream=redirect)
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=audio)

    serve(handler)
    assert await media.download_external_audio("https://media.test/redirect") == b"audio"
    assert redirect.consumed == 0
    assert redirect.closed and audio.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,content,detail", [(404, b"missing", "Error downloading audio file"), (200, b"", "No audio")])
async def test_audio_error_contract_is_preserved(serve, status, content, detail):
    stream = TrackedStream([content])
    serve(lambda request: httpx.Response(status, stream=stream))
    with pytest.raises(HTTPException) as caught:
        await media.download_external_audio("https://media.test/audio")
    assert caught.value.status_code == 400
    assert caught.value.detail == detail
    assert stream.closed


@pytest.mark.asyncio
async def test_total_deadline_closes_stalled_response(serve):
    stream = TrackedStream([b"start"], stall=True)
    serve(lambda request: httpx.Response(200, stream=stream))
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            media.download_external_media("https://media.test/audio", max_bytes=1024, total_timeout=0.03),
            timeout=1,
        )
    assert asyncio.get_running_loop().time() - started < 0.7
    assert stream.closed
