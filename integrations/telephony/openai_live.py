"""GPT-Live's continuous PCMU transport (not the Realtime turn protocol)."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
from typing import Any

import aiohttp


LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODEL = "gpt-live-1"


class OpenAILiveError(RuntimeError):
    pass


class OpenAILiveClient:
    """One provider session; the receiver survives the media consumer's close."""

    def __init__(self, *, api_key_provider, session_loader, http_factory=None):
        self.api_key_provider = api_key_provider
        self.session_loader = session_loader
        self.http_factory = http_factory or aiohttp.ClientSession
        self.options = None
        self.session_id: str | None = None
        self.usage_seconds = 0.0
        self.final_usage_confirmed = False
        self.close_reason: str | None = None
        self._http = None
        self._ws = None
        self._receiver = None
        self._close_task = None
        self._queue = asyncio.Queue(maxsize=512)
        self._closed = asyncio.Event()
        self._closing = False
        self._send_lock = asyncio.Lock()
        self._error: Exception | None = None

    async def connect(self):
        if self._ws is not None:
            return self
        configuration = await self.session_loader()
        key = self.api_key_provider()
        if inspect.isawaitable(key):
            key = await key
        if not isinstance(key, str) or not key.strip():
            raise OpenAILiveError("GPT-Live credentials unavailable")
        self._http = self.http_factory(timeout=aiohttp.ClientTimeout(total=None, connect=15))
        try:
            self._ws = await self._http.ws_connect(
                LIVE_URL, headers={"Authorization": "Bearer " + key},
                max_msg_size=4 * 1024 * 1024, heartbeat=20,
            )
            await self.send({"type": "session.start", "session": configuration})
            async with asyncio.timeout(15):
                message = await self._ws.receive()
            event = self._decode(message)
            if event.get("type") != "session.started":
                raise OpenAILiveError("GPT-Live session startup rejected")
            self.session_id = str(event["session"]["id"])
            self._receiver = asyncio.create_task(self._receive(), name="phone-live-provider")
            return self
        except BaseException:
            await self._release()
            raise

    @staticmethod
    def _decode(message):
        if message.type != aiohttp.WSMsgType.TEXT:
            raise OpenAILiveError("GPT-Live connection closed without finalization")
        event = json.loads(message.data)
        if not isinstance(event, dict):
            raise OpenAILiveError("Invalid GPT-Live event")
        return event

    async def _receive(self):
        try:
            async for message in self._ws:
                event = self._decode(message)
                kind = event.get("type")
                if kind in {"session.usage.updated", "session.closed"}:
                    seconds = event.get("usage", {}).get("seconds")
                    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                            or not math.isfinite(seconds) or seconds < self.usage_seconds):
                        raise OpenAILiveError("Invalid GPT-Live cumulative usage")
                    self.usage_seconds = float(seconds)
                if kind == "session.closed":
                    self.final_usage_confirmed = True
                    self.close_reason = str(event.get("reason") or "unknown")
                    return
                if kind == "error":
                    # Provider text can contain request data: retain only its type/code.
                    error = event.get("error") or {}
                    raise OpenAILiveError("GPT-Live provider error: " + str(error.get("code") or error.get("type"))[:100])
                if kind in {
                    "session.output_audio.delta", "session.input_transcript.delta",
                    "session.output_transcript.delta", "session.delegation.created",
                }:
                    self._queue.put_nowait(event)
            if not self.final_usage_confirmed:
                raise OpenAILiveError("GPT-Live connection lost")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
        finally:
            self._closed.set()

    async def events(self):
        while not self._closed.is_set() or not self._queue.empty():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except TimeoutError:
                pass
        if self._error is not None:
            raise self._error
        if not self._closing:
            raise OpenAILiveError("GPT-Live session ended unexpectedly")

    def drain_events(self):
        while not self._queue.empty():
            yield self._queue.get_nowait()

    async def send(self, event):
        if self._ws is None or self._ws.closed:
            raise OpenAILiveError("GPT-Live transport unavailable")
        async with self._send_lock:
            await self._ws.send_json(event)

    async def send_audio(self, audio: bytes):
        if self._closing:
            return
        if not audio or len(audio) > 256 * 1024:
            raise OpenAILiveError("Invalid GPT-Live input audio")
        await self.send({"type": "session.input_audio.append", "audio": base64.b64encode(audio).decode("ascii")})

    async def commentary(self, content: str, *, delegation_id: str | None = None):
        await self.send({"type": "session.commentary.append", "delegation_id": delegation_id, "content": content})

    async def finalize(self):
        await self.close()

    async def close(self):
        # Twilio Stop and session cleanup can close concurrently. Canceling
        # either waiter must not kill the receiver before final usage arrives.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="phone-live-close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._http is None:
            return
        self._closing = True
        try:
            if not self._closed.is_set() and self._ws is not None and not self._ws.closed:
                await self.send({"type": "session.close"})
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=3)
                except TimeoutError:
                    pass  # The caller retains unconfirmed usage for reconciliation.
        finally:
            await self._release()

    async def _release(self):
        if self._receiver is not None and not self._receiver.done():
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        if self._http is not None:
            await self._http.close()
        self._http = None
        self._closed.set()


def live_session_configuration(*, instructions: str, history: list, voice: str = "marin") -> dict[str, Any]:
    return {
        "model": LIVE_MODEL,
        "instructions": instructions,
        "input": history,
        "audio": {"format": {"type": "audio/pcmu", "rate": 8000}, "output": {"voice": voice}},
        "delegation": {"type": "client"},
        "store": False,
    }
