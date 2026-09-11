"""Browser microphone transport for the existing application conversation runtime.

The browser supplies bounded PCM recordings and acknowledges complete audio
segments. Aurvek owns transcription, generation, tools, memory and billing.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import math
from pathlib import Path
import secrets
import wave
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ai_runtime.channel_turns import ChannelContext, TurnKey
from database import get_db_connection
from integrations.embed.context import get_embed_principal, get_embed_user, is_embed_request
from integrations.embed.models import EmbedError
from .runtime import authorize_application_read

router = APIRouter()
MAX_UTTERANCE_SECONDS = 60
MAX_AUDIO_BYTES = 16_000 * 2 * MAX_UTTERANCE_SECONDS + 256
MAX_SESSION_SECONDS = 600


def decode_utterance(encoded: str) -> tuple[bytes, float]:
    """Accept the client's mono 16 kHz PCM, deriving cost from actual samples."""
    if not isinstance(encoded, str) or len(encoded) > ((MAX_AUDIO_BYTES + 2) // 3) * 4:
        raise ValueError("Voice recording is too large")
    try:
        audio = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(audio), "rb") as recording:
            if (recording.getnchannels(), recording.getsampwidth(), recording.getframerate(),
                    recording.getcomptype()) != (1, 2, 16_000, "NONE"):
                raise ValueError("Expected mono 16 kHz PCM audio")
            frames = recording.getnframes()
            if not 1_600 <= frames <= 16_000 * MAX_UTTERANCE_SECONDS:
                raise ValueError("Voice recording must be between 0.1 and 60 seconds")
            if len(recording.readframes(frames)) != frames * 2:
                raise ValueError("Incomplete voice recording")
    except (wave.Error, EOFError, TypeError) as exc:
        raise ValueError("Invalid voice recording") from exc
    return audio, frames / 16_000


async def voice_scope(connection, conversation_id, user_id):
    scope = await authorize_application_read(connection, conversation_id, user_id)
    if scope is None:
        raise EmbedError("application_voice_required", 403)
    if not all(scope.capabilities.get(key) for key in ("text", "voice", "stt", "tts")):
        raise EmbedError("application_capability_denied", 403)
    cursor = await connection.execute(
        "SELECT locked,is_incognito,llm_id FROM CONVERSATIONS WHERE id=? AND user_id=?",
        (conversation_id, user_id))
    row = await cursor.fetchone()
    if not row or row["locked"] or row["is_incognito"]:
        raise EmbedError("conversation_unavailable", 403)
    return scope, int(row["llm_id"])


async def voice_configuration(connection, scope, model_id):
    from ai_runtime.messages import _load_phone_runtime_llm
    from ai_runtime.voice_resolution import resolve_prompt_voice
    model = await _load_phone_runtime_llm(connection, model_id)
    if (model is None or not model["enabled"] or model["machine"] in {"GPTSub", "GranSabio"}
            or (model["capabilities"].get("runtime") or {}).get("kind") != "standard"):
        raise EmbedError("browser_voice_model_unavailable", 409)
    return await resolve_prompt_voice(scope.prompt_id, conn=connection)


async def save_input_provenance(commit, connection):
    """Keep the heard user's origin with the canonical message transaction."""
    if commit.user_message_id is not None:
        await connection.execute(
            "INSERT INTO MESSAGE_INPUT_PROVENANCE(message_id,origin,perception) VALUES (?,?,?)",
            (commit.user_message_id, commit.context.input_origin, commit.context.input_perception))


class PreparedBrowserHandoff:
    def __init__(self, session, request, configuration):
        self.session, self.request, self.configuration = session, request, configuration

    async def close(self):
        pass  # Classical web voice has no destination provider session to close.

    def _check_input(self):
        if self.session.closed or self.session.interrupted.is_set() or self.session.pending is not None:
            raise EmbedError("handoff_source_interrupted", 409)

    async def commit(self, connection, source, target, *, channel_admission=None, replay=False):
        self._check_input()
        configuration = await self.session._prepare_destination(connection, source, target)
        if configuration != self.configuration:
            raise EmbedError("handoff_voice_configuration_changed", 409)
        # Input received while configuration/funding were being checked stays
        # with the source, including a barge-in after the last playback ack.
        self._check_input()
        return {}


@router.get("/api/conversations/{conversation_id}/voice/availability")
async def voice_availability(conversation_id: int, request: Request):
    from auth import get_current_user
    from .billing import application_funding_status
    user = await get_current_user(request)
    if user is None:
        return JSONResponse({"available": False, "error_code": "unauthenticated"}, status_code=401)
    async with get_db_connection(readonly=True) as connection:
        scope = await authorize_application_read(connection, conversation_id, user.id)
        if scope is None:
            # Ordinary native chat retains its existing hosted voice path.
            from integrations.elevenlabs.service import service
            result = await service.get_webrtc_availability(conversation_id, user.id, await user.is_admin)
            return JSONResponse(result or {"available": False}, status_code=200 if result else 404)
        funding = await application_funding_status(connection, scope)
        try:
            if funding is not None and not funding["available"]:
                raise EmbedError("application_funding_unavailable", 403)
            scope, model_id = await voice_scope(connection, conversation_id, user.id)
            voice = await voice_configuration(connection, scope, model_id)
        except (EmbedError, ValueError) as exc:
            return {"available": False, "transport": "aurvek", "reason": str(exc),
                    "application_funding": funding,
                    "error_code": getattr(exc, "code", "voice_unavailable")}
    return {"available": True, "transport": "aurvek", "provider": voice.provider,
            "application_funding": funding,
            "voice": voice.voice_code, "reason": "Voice uses this conversation's assistant."}


def _http_request(websocket: WebSocket, csrf_token: str) -> Request:
    """Reuse HTTP request guards without passing a WebSocket to form readers."""
    scope = dict(websocket.scope)
    scope.update(type="http", method="POST", scheme="https" if websocket.url.scheme == "wss" else "http")
    scope["headers"] = [(key, value) for key, value in scope["headers"]
                        if key.lower() not in {b"x-gptsub-csrf", b"content-type", b"content-length"}]
    scope["headers"].append((b"x-gptsub-csrf", csrf_token.encode("ascii")))
    async def empty_body():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request(scope, receive=empty_body)


@asynccontextmanager
async def _voice_operation(scope, principal):
    from billing.usage_reservations import user_billing_guard
    from integrations.embed.activity import track_embed_generation
    from .activity import begin_application_admission, bind_application_admission, end_application_operation
    from .billing import admit_application_billing, binding_contextmanager
    async with contextlib.AsyncExitStack() as stack:
        if principal is not None:
            await stack.enter_async_context(track_embed_generation(principal))
        active = begin_application_admission(scope)
        try:
            operation = await admit_application_billing(scope)
            bind_application_admission(active, operation)
            with binding_contextmanager(operation):
                async with user_billing_guard(scope.user_id):
                    yield operation
        finally:
            end_application_operation(active)


class VoiceTTSBilling:
    """Native media reservations, attributed to the current application turn."""
    def __init__(self, session):
        self.session = session

    async def cache_hit(self, **_kwargs):
        await self.session.check_access()

    async def reserve(self, *, provider, characters):
        from billing.usage_reservations import reserve_fixed_usage
        from common import Cost
        await self.session.check_access()
        rate, service_id = Cost.get_tts_service(provider)
        if service_id is None or rate <= 0:
            raise EmbedError("application_tts_billing_unavailable", 503)
        return await reserve_fixed_usage(user_id=self.session.user.id, purpose="tts",
            amount=rate * characters, service_id=service_id, usage_quantity=characters)

    async def provider_started(self, token):
        from billing.usage_reservations import claim_fixed_usage_provider
        await self.session.check_access()
        return await claim_fixed_usage_provider(token, purpose="tts", user_id=self.session.user.id)

    async def settle(self, token):
        from billing.usage_reservations import mark_fixed_usage_provider_succeeded, settle_fixed_usage
        if not await mark_fixed_usage_provider_succeeded(token, purpose="tts", user_id=self.session.user.id):
            raise EmbedError("application_tts_billing_unavailable", 503)
        await settle_fixed_usage(token)

    async def failed(self, token, *, provider_started, reason):
        # A dispatched provider request may be billable even if its audio failed.
        if not provider_started:
            from billing.usage_reservations import refund_fixed_usage
            await refund_fixed_usage(token)


class BrowserVoiceSession:
    def __init__(self, websocket, conversation_id, user, request):
        self.websocket = websocket
        self.conversation_id = conversation_id
        self.user = user
        self.request = request
        self.session_id = secrets.token_urlsafe(18)
        self.generation = 0
        self.turn_number = 0
        self.turn_task = None
        self.current_runtime = None
        self.pending = None
        self.interrupted = asyncio.Event()
        self.ack = None
        self.segment_id = None
        self.closed = False
        self.recording = None

    async def send(self, event, **data):
        if not self.closed:
            await self.websocket.send_json({"event": event, "generation": self.generation, **data})

    async def check_access(self):
        from auth import get_current_user_from_websocket
        principal = get_embed_principal(self.websocket)
        if principal is not None:
            from integrations.embed.identity import get_embed_store
            principal = await get_embed_store().revalidate_principal(principal)
            self.websocket.scope["state"]["embed_principal"] = principal
            user = await get_embed_user(self.websocket)
            if principal.conversation_id != self.conversation_id:
                raise EmbedError("frame_destination_changed", 409)
        else:
            user = await get_current_user_from_websocket(self.websocket)
        if user is None or user.id != self.user.id:
            raise EmbedError("unauthenticated", 401)
        async with get_db_connection(readonly=True) as connection:
            scope, model_id = await voice_scope(connection, self.conversation_id, user.id)
        self.user = user
        return scope, model_id, principal

    async def run(self):
        await self.send("ready", conversation_id=self.conversation_id)
        try:
            async with asyncio.timeout(MAX_SESSION_SECONDS):
                while True:
                    raw = await self.websocket.receive_text()
                    if len(raw) > ((MAX_AUDIO_BYTES + 2) // 3) * 4 + 512:
                        raise ValueError("Voice message is too large")
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("Invalid voice message")
                    event = message.get("event")
                    if event == "stop":
                        break
                    if event == "interrupt":
                        self._interrupt()
                    elif event == "played":
                        played_ms = message.get("played_ms")
                        if (self.ack is not None and not self.ack.done()
                                and message.get("id") == self.segment_id
                                and type(played_ms) in {int, float}
                                and math.isfinite(played_ms) and 0 < played_ms <= 60_000):
                            self.ack.set_result(int(played_ms))
                    elif event == "utterance":
                        if message.get("generation") != self.generation:
                            await self.send("ready", message="The assistant changed. Please repeat your message.")
                            continue
                        recording = decode_utterance(message.get("audio"))
                        if self.turn_task is not None and not self.turn_task.done():
                            if self.pending is not None:
                                raise ValueError("A voice message is already waiting")
                            self.pending = (self.generation, recording)
                            self._interrupt()
                        else:
                            self._start(recording)
        finally:
            self.closed = True
            self.pending = None
            if self.turn_task is not None:
                self.turn_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.turn_task

    def _start(self, recording):
        self.interrupted = asyncio.Event()
        self.turn_task = asyncio.create_task(self._run_turn(recording))

    def _interrupt(self):
        self.interrupted.set()
        # Once the transcript has entered the canonical runtime, its existing
        # interruption path can save the caller and heard prefix immediately.
        if self.current_runtime is not None and self.turn_task is not None:
            self.turn_task.cancel()

    async def _prepare_destination(self, connection, source, target):
        from .billing import admit_handoff_funding
        _, model_id = await voice_scope(connection, target.conversation_id, target.user_id)
        voice = await voice_configuration(connection, target, model_id)
        await admit_handoff_funding(connection, target, model_id)
        return model_id, voice.provider, voice.voice_code

    async def prepare_handoff(self, context, request, *, service):
        from .handoff import prepare_handoff_destination
        resolved, configuration = await prepare_handoff_destination(service, context, request,
            prepare_in_transaction=self._prepare_destination)
        return PreparedBrowserHandoff(self, resolved, configuration)

    async def _run_turn(self, recording):
        from integrations.media import transcribe_external_audio_detailed
        from integrations.telephony.speech import PhoneTextFragmenter
        from integrations.telephony.transport import start_canonical_voice_turn
        runtime = None
        heard = ""
        played_ms = 0
        from .web_audio import VoiceRecording
        recording_state = VoiceRecording(self.conversation_id, self.user.id)
        self.recording = recording_state
        async def finish_runtime():
            if runtime is not None and not runtime.handle.committed:
                try:
                    await recording_state.retain_reply()
                    await runtime.interrupt(heard, played_ms=played_ms, reason="browser_interrupted")
                except BaseException:
                    await runtime.abort("browser_cleanup_failed")
                    raise
        try:
            scope, model_id, principal = await self.check_access()
            async with get_db_connection(readonly=True) as connection:
                voice = await voice_configuration(connection, scope, model_id)
            async with contextlib.AsyncExitStack() as stack:
                operation = await stack.enter_async_context(_voice_operation(scope, principal))
                # Cleanup precedes releasing the source's admission and billing
                # context, including when the microphone socket disappears.
                stack.push_async_callback(finish_runtime)
                await self.send("state", state="thinking")
                audio, duration = recording
                transcription = await transcribe_external_audio_detailed(
                    user_id=self.user.id, audio_content=audio, duration_seconds=duration,
                    user_agent=self.websocket.headers.get("user-agent"))
                text = str(transcription.text or "").strip()
                if not text:
                    await self.send("ready", message="I could not hear speech. Please try again.")
                    return
                await self.check_access()
                await recording_state.retain_input(audio)
                self.turn_number += 1
                async def commit_guard(_context, connection):
                    await voice_scope(connection, scope.conversation_id, scope.user_id)
                    return True
                async def commit_recording(commit, connection):
                    await save_input_provenance(commit, connection)
                    await recording_state.commit(commit, connection)
                context = ChannelContext(channel="web", persistence="deferred",
                    turn_key=TurnKey(self.session_id, str(self.turn_number)),
                    application=operation.scope, input_origin="web.live_voice",
                    input_perception="transcript_only", commit_guard=commit_guard,
                    on_commit_in_transaction=commit_recording,
                    provenance={"prepare_voice_handoff": self.prepare_handoff})
                await self.send("transcript", text=text)
                runtime = await start_canonical_voice_turn(conversation_id=self.conversation_id,
                    current_user=self.user, user_text=text, channel_context=context,
                    request=self.request, expected_llm_id=model_id)
                self.current_runtime = runtime
                fragmenter = PhoneTextFragmenter(min_chars=24, max_chars=120)
                async for event in runtime.events_until_draft():
                    if self.interrupted.is_set():
                        break
                    if event.persistence_error:
                        raise RuntimeError("Voice response could not be saved")
                    if event.content:
                        for segment in fragmenter.feed(event.content):
                            elapsed = await self._speak(segment, voice)
                            if elapsed is None:
                                break
                            heard += segment
                            played_ms += elapsed
                if not self.interrupted.is_set():
                    for segment in fragmenter.finish():
                        elapsed = await self._speak(segment, voice)
                        if elapsed is None:
                            break
                        heard += segment
                        played_ms += elapsed
                await recording_state.retain_reply()
                if self.interrupted.is_set():
                    await runtime.interrupt(heard, played_ms=played_ms, reason="browser_barge_in")
                else:
                    await runtime.confirm_audible(heard, played_ms=played_ms)
                result = runtime.handoff_result
                if result:
                    self.conversation_id = int(result["conversation_id"])
                    self.generation += 1
                    if principal is not None:
                        from integrations.embed.identity import get_embed_store
                        store = get_embed_store()
                        from integrations.embed.models import EMBED_COOKIE_NAME
                        destination = await store.resolve_frame(self.websocket.cookies.get(EMBED_COOKIE_NAME, ""),
                            self.websocket.headers["host"], principal.app_id, principal.frame_instance_id)
                        self.websocket.scope["state"]["embed_principal"] = destination
                        self.websocket.scope["state"]["application_context"] = destination.application
                    await self.send("handoff", **result)
                elif runtime.handoff_failure:
                    await self.send("notice", message="transfer_failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from log_config import logger
            logger.warning("Browser voice failed (%s)", type(exc).__name__)
            await self.send("error", message=getattr(exc, "code", "request_failed"))
        finally:
            await recording_state.discard_pending()
            if recording_state.retention_failed:
                await self.send("notice", message="voice_recording_unavailable")
            self.recording = None
            self.current_runtime = None
            self.segment_id = None
            self.ack = None
            waiting, self.pending = self.pending, None
            if not self.closed:
                if waiting is not None and waiting[0] == self.generation:
                    self._start(waiting[1])
                else:
                    await self.send("ready", conversation_id=self.conversation_id,
                        **({"message": "The assistant changed while you were speaking. Please repeat your message."}
                           if waiting is not None else {}))

    async def _speak(self, text, voice):
        from tools.tts import handle_tts_request
        if self.interrupted.is_set():
            return None
        await self.check_access()
        path, error = await handle_tts_request(None,
            {"text": text, "author": "bot", "conversationId": self.conversation_id},
            self.user, resolved_voice_override=voice, billing_adapter=VoiceTTSBilling(self))
        if error or not path:
            raise RuntimeError("Voice synthesis failed")
        if self.interrupted.is_set():
            return None
        audio = await asyncio.to_thread(Path(path).read_bytes)
        self.segment_id = secrets.token_urlsafe(12)
        self.ack = asyncio.get_running_loop().create_future()
        await self.send("audio", id=self.segment_id, text=text, audio=base64.b64encode(audio).decode("ascii"))
        interrupted = asyncio.create_task(self.interrupted.wait())
        try:
            done, _ = await asyncio.wait([self.ack, interrupted], timeout=45,
                return_when=asyncio.FIRST_COMPLETED)
            if self.ack in done:
                if self.recording is not None:
                    self.recording.played_segments.append(audio)
                return self.ack.result()
            if interrupted in done:
                return None
            raise TimeoutError("Voice playback was not acknowledged")
        finally:
            interrupted.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await interrupted


@router.websocket("/ws/conversations/{conversation_id}/voice")
async def voice_stream(websocket: WebSocket, conversation_id: int):
    from auth import get_current_user_from_websocket
    from common import READONLY_MODE
    from request_security import validate_mutation_request
    user = (await get_embed_user(websocket) if is_embed_request(websocket)
            else await get_current_user_from_websocket(websocket))
    if user is None or READONLY_MODE:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    try:
        async with asyncio.timeout(10):
            hello = await websocket.receive_json()
        token = hello.get("csrf_token") if isinstance(hello, dict) else None
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii():
            raise EmbedError("unauthenticated", 401)
        request = _http_request(websocket, token)
        if validate_mutation_request(request) is not None:
            raise EmbedError("unauthenticated", 401)
        session = BrowserVoiceSession(websocket, conversation_id, user, request)
        scope, model_id, _ = await session.check_access()
        async with get_db_connection(readonly=True) as connection:
            await voice_configuration(connection, scope, model_id)
        await session.run()
    except WebSocketDisconnect:
        return
    except (EmbedError, ValueError, HTTPException, TimeoutError) as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json({"event": "error", "message": getattr(exc, "code", "Voice session closed.")})
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


# Shared application media is HTTP scoped; ordinary native chat retains its
# existing endpoints and WebSocket protocol.
from .web_audio import router as web_audio_router
router.include_router(web_audio_router)
