"""Continuous Gemini audio and video, using Aurvek for delegated actions."""

from __future__ import annotations

import asyncio
import audioop
import base64
import inspect
from uuid import uuid4

from google import genai
from google.genai import types

from integrations.telephony.gemini_live_billing import parse_usage, require_supported_model


class GeminiLiveClient:
    """The socket and its multimodal context survive individual spoken turns.

    Events use the same voice/delegation vocabulary as the GPT Live transport.
    Audio is PCM16/24 kHz for apps, or PCMU/8 kHz at the telephone boundary.
    """

    _aurvek_internal_realtime_bridge = True
    input_perception = "audio_native"

    def __init__(self, *, api_key_provider, session_loader, model="gemini-3.8-live",
                 pcmu=False, client_factory=genai.Client):
        require_supported_model(model)
        self.api_key_provider = api_key_provider
        self.session_loader = session_loader
        self.model, self.pcmu = model, pcmu
        self.client_factory = client_factory
        self.options = None
        self.session_id = uuid4().hex
        self.usage_seconds = 0.0
        self.final_usage_confirmed = False
        self.close_reason = None
        self._client = self._connection = self._session = self._receiver = None
        self._queue = asyncio.Queue(maxsize=512)
        self._closed = asyncio.Event()
        self._closing = False
        self._error = None
        self._input_samples = self._output_samples = 0
        self._input_state = self._output_state = None
        self._pending = {}
        self._usage = None
        self._turn = 0
        self._close_lock = asyncio.Lock()
        self._close_task = None
        self._flushing = False
        self._flush_done = asyncio.Event()
        self._response_open = False

    async def connect(self):
        configuration = await self.session_loader()
        key = self.api_key_provider()
        if inspect.isawaitable(key):
            key = await key
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Gemini Live credentials unavailable")
        voice = configuration["audio"]["output"]["voice"]
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            input_audio_transcription={}, output_audio_transcription={},
            system_instruction=configuration["instructions"] + (
                "\nUse one consistent speaker identity and the configured voice throughout the session. "
                "Keep the same vocal timbre across turns and languages. Do not imitate the user's voice "
                "or introduce another speaker when quoting text or reporting tool results."),
            max_output_tokens=2048,
            speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": voice}}},
            context_window_compression={"trigger_tokens": 24000, "sliding_window": {"target_tokens": 12000}},
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
                name="ask_aurvek", behavior="BLOCKING",
                description="Ask the Aurvek backend to use tools, search memory or current information, or perform an action. Include the complete current request and relevant visual facts.",
                parameters={"type": "OBJECT", "properties": {"request": {"type": "STRING"}}, "required": ["request"]},
            )])],
        )
        try:
            async with asyncio.timeout(20):
                self._client = self.client_factory(api_key=key)
                self._connection = self._client.aio.live.connect(model=self.model, config=config)
                self._session = await self._connection.__aenter__()
                history = [types.Content(role="model" if item["role"] == "assistant" else "user",
                                         parts=[types.Part(text=part["text"]) for part in item["content"]])
                           for item in configuration.get("input", [])]
                if history:
                    await self._session.send_client_content(turns=history, turn_complete=False)
            self._receiver = asyncio.create_task(self._receive(), name="gemini-live-provider")
            return self
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise RuntimeError("Gemini Live connection failed: " + type(exc).__name__) from None

    def _emit(self, kind, **data):
        self._queue.put_nowait({"type": kind, **data})

    async def _receive(self):
        try:
            while not self._closing:
                # The SDK receive iterator ends at turn_complete, not at socket close.
                async for event in self._session.receive():
                    if event.go_away:
                        raise RuntimeError("Gemini Live session limit reached; start a new session")
                    content = event.server_content
                    if content:
                        if content.model_turn or content.input_transcription or content.output_transcription:
                            self._response_open = True
                        if content.interrupted:
                            self._emit("session.output_audio.interrupted")
                        for part in (content.model_turn.parts or ()) if content.model_turn else ():
                            if part.inline_data:
                                blob = part.inline_data
                                if blob.mime_type != "audio/pcm;rate=24000" or len(blob.data) % 2:
                                    raise ValueError("Unexpected Gemini Live audio format")
                                audio = blob.data
                                self._output_samples += len(audio) // 2
                                if self.pcmu:
                                    audio, self._output_state = audioop.ratecv(audio, 2, 1, 24000, 8000, self._output_state)
                                    audio = audioop.lin2ulaw(audio, 2)
                                if audio:
                                    self._emit("session.output_audio.delta", delta=base64.b64encode(audio).decode("ascii"))
                        for field, kind, samples, rate in (
                            (content.input_transcription, "input", self._input_samples, 16000),
                            (content.output_transcription, "output", self._output_samples, 24000),
                        ):
                            if field and field.text:
                                end = samples * 1000 // rate
                                self._emit(f"session.{kind}_transcript.delta", delta=field.text,
                                           start_ms=end, end_ms=end)
                    if event.tool_call_cancellation:
                        for call_id in event.tool_call_cancellation.ids or []:
                            self._pending.pop(call_id, None)
                            self._emit("session.delegation.canceled", delegation_id=call_id)
                    if event.tool_call:
                        self._response_open = True
                        for call in event.tool_call.function_calls or []:
                            request = (call.args or {}).get("request")
                            if call.name != "ask_aurvek" or not call.id or not isinstance(request, str) or not request.strip():
                                raise ValueError("Invalid Gemini Live delegation")
                            self._pending[call.id] = call.name
                            self._emit("session.delegation.created", delegation={"id": call.id, "target": "client", "request": request[:16000]})
                    if event.usage_metadata:
                        self._usage = parse_usage(event.usage_metadata)
                    if content and content.turn_complete:
                        if self._usage is None:
                            raise RuntimeError("Gemini Live turn completed without usage")
                        self._turn += 1
                        self._emit("session.usage.updated", usage=self._usage,
                                   usage_id=f"{self.session_id}:{self._turn}")
                        self._usage = None
                        self._response_open = False
                        self._emit("session.turn.completed")
                        if self._flushing:
                            self.final_usage_confirmed = True
                            self._flush_done.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never expose SDK exception text, which can contain credentials.
            self._error = RuntimeError("Gemini Live stream failed: " + type(exc).__name__)
        finally:
            self._closed.set()

    async def events(self):
        while not self._closed.is_set() or not self._queue.empty():
            try:
                yield await asyncio.wait_for(self._queue.get(), .2)
            except TimeoutError:
                pass
        if self._error:
            raise self._error
        if not self._closing:
            raise RuntimeError("Gemini Live session ended unexpectedly")

    def drain_events(self):
        while not self._queue.empty():
            yield self._queue.get_nowait()

    async def send_audio(self, audio):
        if self._closing or self._session is None:
            raise RuntimeError("Gemini Live transport unavailable")
        if not audio or len(audio) > 32000 or (not self.pcmu and len(audio) % 2):
            raise ValueError("Invalid Gemini Live input audio")
        if self.pcmu:
            audio, self._input_state = audioop.ratecv(audioop.ulaw2lin(audio, 2), 2, 1, 8000, 16000, self._input_state)
        self._input_samples += len(audio) // 2
        self.usage_seconds = self._input_samples / 16000
        await self._session.send_realtime_input(audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000"))

    async def send_video(self, jpeg):
        if self.pcmu:
            raise ValueError("Telephone audio does not support camera frames")
        await self._session.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))

    async def commentary(self, content, *, delegation_id=None):
        name = self._pending.pop(delegation_id, None)
        if name is None:
            return False  # An interrupted tool result must not restart speech.
        await self._session.send_tool_response(function_responses=[types.FunctionResponse(
            id=delegation_id, name=name, response={"result": content},
        )])
        return True

    async def finalize(self):
        await self.close()

    async def close(self):
        # Media consumers can be canceled during phone or browser shutdown.
        # Preserve the final provider usage drain independently of that waiter.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="gemini-live-close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        async with self._close_lock:
            if self._closing:
                return
            try:
                if self._session and self._receiver and not self._receiver.done():
                    self._flushing = True
                    await self._session.send_realtime_input(audio_stream_end=True)
                    try:
                        await asyncio.wait_for(self._flush_done.wait(), 3)
                    except TimeoutError:
                        pass  # Leave an unconfirmed boundary reserved for reconciliation.
                    # Live has response usage, not a session.closed usage event.
                    # A drained automatic-VAD stream with no unfinished response
                    # can settle its reported turns, including a silent session.
                    self.final_usage_confirmed = (self._error is None and not self._response_open
                                                  and not self._pending and self._usage is None)
                self._closing = True
                if self._receiver and not self._receiver.done():
                    self._receiver.cancel()
                    await asyncio.gather(self._receiver, return_exceptions=True)
                if self._connection and self._session:
                    await self._connection.__aexit__(None, None, None)
            finally:
                self._closing = True
                if self._client:
                    await self._client.aio.aclose()
                self._closed.set()
