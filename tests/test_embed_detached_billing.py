"""Disconnecting the HTTP consumer must not hide continuing paid work."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from starlette.responses import StreamingResponse

from billing import usage_reservations as billing
from integrations.embed import activity
from integrations.embed.models import EmbedPrincipal


@pytest.mark.asyncio
async def test_http_disconnect_keeps_generation_until_native_billing_producer_finishes(monkeypatch):
    principal = EmbedPrincipal(app_id="test", issuer="https://identity.example", subject="one",
                              user_id=701, prompt_id=10, delegated_session_id="detached-billing-test",
                              session_version=1, membership_version=1, expires_at=9999999999,
                              conversation_id=701)
    monkeypatch.setattr(activity, "_revalidate", AsyncMock())
    stopped = AsyncMock()
    monkeypatch.setattr(activity, "_signal_stop", stopped)
    lease = type("Lease", (), {"release": AsyncMock()})()
    monkeypatch.setattr(billing, "_acquire_user_account_lease", AsyncMock(return_value=lease))
    consumer_got_chunk = asyncio.Event()
    finish_provider = asyncio.Event()
    provider_finished = asyncio.Event()
    response_returned = asyncio.Event()

    async def provider():
        yield b"data: first\n\n"
        await finish_provider.wait()
        provider_finished.set()
        yield b"data: done\n\n"

    async def response():
        return StreamingResponse(provider(), media_type="text/event-stream")

    async def receive():
        await consumer_got_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            consumer_got_chunk.set()

    async def request():
        async with activity.track_embed_generation(principal):
            wrapped = await billing.serialize_user_billing_response(701, response())
            await wrapped({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}}, receive, send)
            response_returned.set()

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(response_returned.wait(), timeout=2)
        await asyncio.wait_for(task, timeout=2)
        assert not provider_finished.is_set()
        assert await activity.activity_state(701) == "generating"
        active = activity._active[701]
        assert active.producers and not active.watcher.done()
        result = await activity.end_session_activity(principal.delegated_session_id, 701)
        assert result["closed"] is False
        stopped.assert_awaited_once_with(active)
    finally:
        finish_provider.set()
        await asyncio.wait_for(task, timeout=2)
        pending = list(billing._detached_billing_producers)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
        await asyncio.sleep(0)
        activity._ended_sessions.pop(principal.delegated_session_id, None)
    assert provider_finished.is_set()
    assert await activity.activity_state(701) == "idle"
    lease.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_standalone_billing_stream_retains_embed_activity_after_consumer_closes(monkeypatch):
    principal = EmbedPrincipal(app_id="test", issuer="https://identity.example", subject="two",
                              user_id=702, prompt_id=10, delegated_session_id="nested-billing-test",
                              session_version=1, membership_version=1, expires_at=9999999999,
                              conversation_id=702)
    monkeypatch.setattr(activity, "_revalidate", AsyncMock())
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def guarded(_user_id):
        yield

    monkeypatch.setattr(billing, "user_billing_guard", guarded)
    finish = asyncio.Event()

    async def provider():
        yield "first"
        await finish.wait()

    try:
        async with activity.track_embed_generation(principal):
            stream = billing.serialize_user_billing_stream(702, provider())
            assert await anext(stream) == "first"
            await stream.aclose()
        assert await activity.activity_state(702) == "generating"
    finally:
        finish.set()
        pending = list(billing._detached_billing_producers)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=2)
        await asyncio.sleep(0)
    assert await activity.activity_state(702) == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_kind", ["logout", "native_stop"])
async def test_revocation_while_waiting_for_real_payer_lock_never_starts_preflight(monkeypatch, stop_kind):
    principal = EmbedPrincipal(app_id="test", issuer="https://identity.example", subject="queued",
                              user_id=703, prompt_id=10, delegated_session_id="queued-billing-test",
                              session_version=1, membership_version=1, expires_at=9999999999,
                              conversation_id=703)
    monkeypatch.setattr(activity, "_revalidate", AsyncMock())
    waiting = asyncio.Event()
    started = False

    async def availability(_user_id):
        waiting.set()
        return {"billing_account_id": 703, "available": 1}

    async def stopped(active):
        active.ending = True

    async def native_preflight():
        nonlocal started
        started = True
        return StreamingResponse(iter(()))

    monkeypatch.setattr(billing, "get_user_billing_availability", availability)
    monkeypatch.setattr(activity, "_signal_stop", stopped)
    blocker = await billing._acquire_account_lease(703, object())

    async def request():
        async with activity.track_embed_generation(principal):
            return await billing.serialize_user_billing_response(703, native_preflight())

    task = asyncio.create_task(request())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert billing._account_locks[703].participants == 2
        assert not task.done() and not started
        if stop_kind == "logout":
            result = await activity.end_session_activity(principal.delegated_session_id, 703)
        else:
            activity.mark_embed_stop(principal)
            result = await activity.get_activity(principal)
        assert result == {"activity": "ending", "closed": False}
        await blocker.release()
        with pytest.raises(Exception) as rejected:
            await asyncio.wait_for(task, timeout=2)
        assert rejected.value.status_code == 401
        assert not started
        assert 703 not in billing._account_locks
        assert await activity.activity_state(703) == "idle"
    finally:
        await blocker.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        activity._ended_sessions.pop(principal.delegated_session_id, None)


@pytest.mark.asyncio
async def test_native_billing_preflight_has_no_embed_revalidation(monkeypatch):
    revalidate = AsyncMock(side_effect=AssertionError("Native request entered embed authentication"))
    monkeypatch.setattr(activity, "_revalidate", revalidate)
    monkeypatch.setattr(billing, "get_user_billing_availability",
                        AsyncMock(return_value={"billing_account_id": 704, "available": 1}))
    from starlette.responses import Response

    async def native_preflight():
        return Response("native")

    result = await billing.serialize_user_billing_response(704, native_preflight())
    assert result.body == b"native"
    revalidate.assert_not_awaited()
    assert 704 not in billing._account_locks


@pytest.mark.asyncio
async def test_revocation_watcher_reasserts_stop_until_producer_completion(monkeypatch):
    principal = EmbedPrincipal(app_id="test", issuer="https://identity.example", subject="watch",
                              user_id=705, prompt_id=10, delegated_session_id="watch-billing-test",
                              session_version=1, membership_version=1, expires_at=9999999999,
                              conversation_id=705)
    revalidate = AsyncMock()
    monkeypatch.setattr(activity, "_revalidate", revalidate)
    monkeypatch.setattr(activity, "_WATCH_ACCESS_INTERVAL", 0.001)
    signalled_twice = asyncio.Event()
    calls = 0

    async def stop(active):
        nonlocal calls
        active.ending = True
        calls += 1
        if calls >= 2:
            signalled_twice.set()

    monkeypatch.setattr(activity, "_signal_stop", stop)
    async with activity.track_embed_generation(principal):
        revalidate.side_effect = RuntimeError("revoked")
        await asyncio.wait_for(signalled_twice.wait(), timeout=2)
        assert not activity._active[705].watcher.done()
        assert await activity.activity_state(705) == "ending"
    assert await activity.activity_state(705) == "idle"
