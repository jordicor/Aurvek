"""Canonical TTS rendering and safe text segmentation for phone playback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Mapping
from urllib.parse import quote

import aiohttp

from integrations.telephony.audio import (
    PcmuCacheAsset,
    describe_pcmu_cache,
    materialize_pcmu_cache,
)
from integrations.telephony.billing import (
    PhoneBillingError,
    PhoneBillingExhausted,
    PhoneBillingService,
    PhoneCostComponent,
)
from integrations.telephony.snapshot import (
    ELEVENLABS_PHONE_TTS_MODEL_ID,
    ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
    PhoneSnapshotError,
    canonical_voice_from_snapshot,
    live_tts_profile_from_snapshot,
)
from tools.tts import (
    TTSBillingAdapter,
    TTSBillingError,
    TTSBillingInsufficientBalance,
    get_tts_cache_digest,
    handle_tts_request,
    process_text_for_tts,
)
from tools.tts_load_balancer import get_elevenlabs_key


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PHONE_SPEECH_CACHE = _PROJECT_ROOT / "data" / "phone_audio_cache"
_PROJECT_STATIC_ROOT = _PROJECT_ROOT / "data" / "static"
_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+(?:[\"')\]]+)?\s+")
ELEVENLABS_PHONE_TTS_ENDPOINT = (
    "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
)
ELEVENLABS_PHONE_TTS_TIMEOUT_SECONDS = 30.0
ELEVENLABS_PHONE_TTS_CONNECT_TIMEOUT_SECONDS = 5.0
ELEVENLABS_PHONE_TTS_READ_TIMEOUT_SECONDS = 15.0
ELEVENLABS_PHONE_KEY_SELECTION_TIMEOUT_SECONDS = 5.0
MAX_ELEVENLABS_PHONE_AUDIO_BYTES = 4 * 1024 * 1024
_ELEVENLABS_READ_CHUNK_BYTES = 16 * 1024
_ELEVENLABS_KEY_PROBE_CAPACITY = 4
_ELEVENLABS_KEY_PROBE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_ELEVENLABS_KEY_PROBE_CAPACITY,
    thread_name_prefix="elevenlabs-phone-key",
)
_ELEVENLABS_KEY_PROBE_FLIGHTS: dict[str, Future[str | None]] = {}
_ELEVENLABS_KEY_PROBE_GUARD = threading.Lock()
_ELEVENLABS_KEY_PROBE_POLL_SECONDS = 0.01


PcmuChunkConsumer = Callable[[bytes], Awaitable[None]]
PcmuCompleteConsumer = Callable[[bytes], Awaitable[None]]


class PhoneSpeechError(RuntimeError):
    """Canonical phone audio could not be rendered safely."""


class PhoneSpeechBillingExhausted(PhoneSpeechError):
    """The next live TTS fragment cannot be reserved."""


@dataclass(slots=True)
class _PcmuRenderFlight:
    lock: asyncio.Lock
    users: int = 0


_PCMU_RENDER_FLIGHTS: dict[str, _PcmuRenderFlight] = {}
_PCMU_RENDER_FLIGHTS_GUARD = threading.Lock()


@asynccontextmanager
async def _single_pcmu_render(cache_key: str):
    """Serialize only one cache digest and remove idle locks deterministically."""

    with _PCMU_RENDER_FLIGHTS_GUARD:
        flight = _PCMU_RENDER_FLIGHTS.get(cache_key)
        if flight is None:
            flight = _PcmuRenderFlight(lock=asyncio.Lock())
            _PCMU_RENDER_FLIGHTS[cache_key] = flight
        flight.users += 1
    try:
        async with flight.lock:
            yield
    finally:
        with _PCMU_RENDER_FLIGHTS_GUARD:
            flight.users -= 1
            if flight.users == 0 and _PCMU_RENDER_FLIGHTS.get(cache_key) is flight:
                del _PCMU_RENDER_FLIGHTS[cache_key]


class _PhoneTTSBillingAdapter(TTSBillingAdapter):
    def __init__(
        self,
        *,
        call_id: str,
        dedupe_key: str,
        service: PhoneBillingService,
    ) -> None:
        self.call_id = str(call_id)
        self.dedupe_key = str(dedupe_key)
        self.service = service

    async def cache_hit(
        self, *, provider: str, characters: int, cache_key: str
    ) -> None:
        await self.service.record_cache_hit(
            call_id=self.call_id,
            provider=provider,
            component_type="tts",
            quantity=characters,
            dedupe_key=f"{self.dedupe_key}:cache:{cache_key}",
        )

    async def reserve(self, *, provider: str, characters: int) -> PhoneCostComponent:
        try:
            component = await self.service.reserve_component(
                call_id=self.call_id,
                provider=provider,
                component_type="tts",
                quantity=characters,
                dedupe_key=self.dedupe_key,
            )
            if component.state != "reserved":
                raise TTSBillingError(
                    "Telephone TTS fragment was already processed"
                )
            return component
        except PhoneBillingExhausted as exc:
            raise TTSBillingInsufficientBalance(
                "Insufficient telephone balance"
            ) from exc
        except PhoneBillingError as exc:
            raise TTSBillingError("Telephone TTS billing is unavailable") from exc

    async def provider_started(self, token: PhoneCostComponent) -> bool:
        return await self.service.claim_provider_start(token.id) is not None

    async def settle(self, token: PhoneCostComponent) -> None:
        await self.service.settle_component(token.id)

    async def failed(
        self,
        token: PhoneCostComponent,
        *,
        provider_started: bool,
        reason: str,
    ) -> None:
        if provider_started:
            await self.service.mark_ambiguous(token.id, reason=reason)
        else:
            await self.service.refund_component(token.id, reason=reason)


@dataclass(frozen=True, slots=True)
class PhoneSpeechAsset:
    text: str
    pcmu: bytes
    cache: PcmuCacheAsset


class PhoneTextFragmenter:
    """Produce exact-prefix fragments at sentence or whitespace boundaries."""

    def __init__(self, *, min_chars: int = 24, max_chars: int = 240) -> None:
        if not 1 <= min_chars <= max_chars <= 1_000:
            raise ValueError("invalid phone text fragment bounds")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self._buffer = ""

    def feed(self, text: str) -> tuple[str, ...]:
        if not isinstance(text, str):
            raise TypeError("phone text fragments must be strings")
        if "\x00" in text or "\r" in text:
            raise ValueError("phone text contains unsupported controls")
        self._buffer += text
        return tuple(self._drain(final=False))

    def finish(self) -> tuple[str, ...]:
        return tuple(self._drain(final=True))

    def _drain(self, *, final: bool) -> list[str]:
        fragments: list[str] = []
        while self._buffer:
            boundary = self._sentence_cut()
            if boundary is None and len(self._buffer) > self.max_chars:
                boundary = self._whitespace_cut()
            if boundary is None:
                if final:
                    boundary = len(self._buffer)
                else:
                    break
            fragment = self._buffer[:boundary]
            self._buffer = self._buffer[boundary:]
            if fragment.strip():
                fragments.append(fragment)
            elif fragments:
                fragments[-1] += fragment
        return fragments

    def _sentence_cut(self) -> int | None:
        for match in _SENTENCE_BOUNDARY.finditer(self._buffer):
            if match.end() >= self.min_chars:
                return match.end()
        return None

    def _whitespace_cut(self) -> int | None:
        window = self._buffer[: self.max_chars + 1]
        lower_bound = min(self.min_chars, len(window))
        for index in range(len(window) - 1, lower_bound - 1, -1):
            if window[index].isspace():
                return index + 1
        # A single overlong token is not safe to split into a claimed audible
        # word. Keep it buffered until a boundary or finalization arrives.
        if len(self._buffer) <= self.max_chars * 4:
            return None
        raise PhoneSpeechError("phone response contains an unbounded token")


def _finish_elevenlabs_key_probe(
    voice_id: str,
    future: Future[str | None],
) -> None:
    with _ELEVENLABS_KEY_PROBE_GUARD:
        if _ELEVENLABS_KEY_PROBE_FLIGHTS.get(voice_id) is future:
            del _ELEVENLABS_KEY_PROBE_FLIGHTS[voice_id]


def _elevenlabs_key_probe(voice_id: str) -> Future[str | None]:
    """Return one shared bounded synchronous probe for an exact voice."""

    created = False
    with _ELEVENLABS_KEY_PROBE_GUARD:
        future = _ELEVENLABS_KEY_PROBE_FLIGHTS.get(voice_id)
        if future is None:
            if (
                len(_ELEVENLABS_KEY_PROBE_FLIGHTS)
                >= _ELEVENLABS_KEY_PROBE_CAPACITY
            ):
                raise PhoneSpeechError(
                    "ElevenLabs credential selection capacity is unavailable"
                )
            # Capture the callable so a test/runtime replacement cannot alter
            # an already-submitted probe while it is executing.
            key_getter = get_elevenlabs_key
            future = _ELEVENLABS_KEY_PROBE_EXECUTOR.submit(
                key_getter,
                voice_id=voice_id,
            )
            _ELEVENLABS_KEY_PROBE_FLIGHTS[voice_id] = future
            created = True
    if created:
        # Register outside the non-reentrant guard: add_done_callback invokes
        # immediately when a very fast probe completed before registration.
        future.add_done_callback(
            lambda completed: _finish_elevenlabs_key_probe(voice_id, completed)
        )
    return future


async def _await_elevenlabs_key_probe(
    future: Future[str | None],
) -> str | None:
    # Avoid one retained concurrent-future callback per timed-out waiter.  The
    # shared probe has exactly one cleanup callback until its thread exits.
    while not future.done():
        await asyncio.sleep(_ELEVENLABS_KEY_PROBE_POLL_SECONDS)
    return future.result()


async def _select_elevenlabs_phone_key(voice_id: str) -> str | None:
    """Resolve an exact-voice key inside a hard outer deadline."""

    future = _elevenlabs_key_probe(voice_id)
    try:
        return await asyncio.wait_for(
            _await_elevenlabs_key_probe(future),
            timeout=ELEVENLABS_PHONE_KEY_SELECTION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # A running synchronous probe cannot be killed safely.  It remains in
        # the shared flight map and consumes bounded capacity until it exits.
        raise PhoneSpeechError(
            "ElevenLabs credential selection timed out"
        ) from None


async def _request_elevenlabs_phone_pcmu(
    *,
    voice_id: str,
    text: str,
    api_key: str,
    model_id: str,
    output_format: str,
    stability: float,
    similarity_boost: float,
    on_pcmu_chunk: PcmuChunkConsumer | None = None,
) -> bytes:
    """Fetch one bounded raw-PCMU fragment from ElevenLabs' stream endpoint."""

    if model_id != ELEVENLABS_PHONE_TTS_MODEL_ID:
        raise PhoneSpeechError("ElevenLabs phone TTS model is unsupported")
    if output_format != ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT:
        raise PhoneSpeechError("ElevenLabs phone TTS format is unsupported")
    if not api_key:
        raise PhoneSpeechError("ElevenLabs phone TTS credentials are unavailable")

    timeout = aiohttp.ClientTimeout(
        total=ELEVENLABS_PHONE_TTS_TIMEOUT_SECONDS,
        connect=ELEVENLABS_PHONE_TTS_CONNECT_TIMEOUT_SECONDS,
        sock_connect=ELEVENLABS_PHONE_TTS_CONNECT_TIMEOUT_SECONDS,
        sock_read=ELEVENLABS_PHONE_TTS_READ_TIMEOUT_SECONDS,
    )
    url = ELEVENLABS_PHONE_TTS_ENDPOINT.format(
        voice_id=quote(voice_id, safe="")
    )
    headers = {
        "Accept": "application/octet-stream",
        "Content-Type": "application/json",
        "xi-api-key": api_key,
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
        },
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                params={"output_format": output_format},
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise PhoneSpeechError(
                        f"ElevenLabs phone TTS returned HTTP {response.status}"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except (TypeError, ValueError) as exc:
                        raise PhoneSpeechError(
                            "ElevenLabs phone TTS returned an invalid size"
                        ) from exc
                    if not 0 < declared_length <= MAX_ELEVENLABS_PHONE_AUDIO_BYTES:
                        raise PhoneSpeechError(
                            "ElevenLabs phone TTS audio exceeds the size limit"
                        )
                audio = bytearray()
                async for chunk in response.content.iter_chunked(
                    _ELEVENLABS_READ_CHUNK_BYTES
                ):
                    audio.extend(chunk)
                    if len(audio) > MAX_ELEVENLABS_PHONE_AUDIO_BYTES:
                        raise PhoneSpeechError(
                            "ElevenLabs phone TTS audio exceeds the size limit"
                        )
                    if on_pcmu_chunk is not None and chunk:
                        # The provider already returns the exact Twilio codec.
                        # Forward each validated chunk immediately instead of
                        # waiting for the complete response body.
                        await on_pcmu_chunk(bytes(chunk))
    except PhoneSpeechError:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise PhoneSpeechError("ElevenLabs phone TTS request failed") from exc

    if not audio:
        raise PhoneSpeechError("ElevenLabs phone TTS returned no audio")
    return bytes(audio)


def _phone_cache_destination(cache_root: str | Path, cache_key: str) -> Path:
    root = Path(cache_root).resolve()
    static_root = _PROJECT_STATIC_ROOT.resolve()
    if root == static_root or static_root in root.parents:
        raise PhoneSpeechError("phone speech cache must be private")
    return root / cache_key[:2] / cache_key[2:4] / f"{cache_key}.mulaw"


def _read_cached_pcmu(destination: Path) -> tuple[bytes, PcmuCacheAsset] | None:
    if not destination.is_file():
        return None
    byte_length = destination.stat().st_size
    if not 0 < byte_length <= MAX_ELEVENLABS_PHONE_AUDIO_BYTES:
        raise PhoneSpeechError("cached phone audio has an invalid size")
    cache = describe_pcmu_cache(destination)
    pcmu = destination.read_bytes()
    if len(pcmu) != cache.byte_length:
        raise PhoneSpeechError("cached phone audio changed while it was read")
    try:
        os.utime(destination, None)
    except OSError:
        pass
    return pcmu, cache


def _write_pcmu_cache(destination: Path, pcmu: bytes) -> PcmuCacheAsset:
    if not 0 < len(pcmu) <= MAX_ELEVENLABS_PHONE_AUDIO_BYTES:
        raise PhoneSpeechError("phone audio has an invalid size")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(pcmu)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
        temporary_path = None
        return describe_pcmu_cache(destination)
    except PhoneSpeechError:
        raise
    except Exception as exc:
        raise PhoneSpeechError("phone audio could not be cached") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def _fail_billing_after_provider_boundary(
    adapter: _PhoneTTSBillingAdapter,
    token: PhoneCostComponent,
    *,
    provider_started: bool,
    reason: str,
) -> None:
    await adapter.failed(
        token,
        provider_started=provider_started,
        reason=reason,
    )


async def _load_native_phone_cache(
    *,
    destination: Path,
    literal_text: str,
    characters: int,
    cache_key: str,
    billing_adapter: _PhoneTTSBillingAdapter | None,
) -> PhoneSpeechAsset | None:
    try:
        cached = await asyncio.to_thread(_read_cached_pcmu, destination)
    except PhoneSpeechError:
        raise
    except Exception as exc:
        raise PhoneSpeechError("cached phone audio could not be read") from exc
    if cached is not None:
        pcmu, cache = cached
        if billing_adapter is not None:
            try:
                await billing_adapter.cache_hit(
                    provider="elevenlabs",
                    characters=characters,
                    cache_key=cache_key,
                )
            except Exception as exc:
                raise PhoneSpeechError(
                    "Telephone TTS cache usage could not be recorded"
                ) from exc
        return PhoneSpeechAsset(text=literal_text, pcmu=pcmu, cache=cache)
    return None


async def _render_native_elevenlabs_phone_speech(
    *,
    literal_text: str,
    voice: Any,
    profile: Any,
    cache_root: str | Path,
    billing_adapter: _PhoneTTSBillingAdapter | None,
    on_pcmu_chunk: PcmuChunkConsumer | None = None,
    on_pcmu_complete: PcmuCompleteConsumer | None = None,
) -> PhoneSpeechAsset:
    synthesized_text = process_text_for_tts(literal_text)
    if not synthesized_text.strip():
        raise PhoneSpeechError("phone speech text has no audible content")
    characters = len(synthesized_text)
    cache_key = get_tts_cache_digest(synthesized_text, voice, profile)
    destination = _phone_cache_destination(cache_root, cache_key)
    cached = await _load_native_phone_cache(
        destination=destination,
        literal_text=literal_text,
        characters=characters,
        cache_key=cache_key,
        billing_adapter=billing_adapter,
    )
    if cached is not None:
        return cached
    if billing_adapter is None:
        raise PhoneSpeechError(
            "Telephone TTS billing identity is unavailable for provider work"
        )

    async with _single_pcmu_render(cache_key):
        # A concurrent renderer may have populated this digest while this
        # caller waited.  Re-check before selecting a key or reserving usage.
        cached = await _load_native_phone_cache(
            destination=destination,
            literal_text=literal_text,
            characters=characters,
            cache_key=cache_key,
            billing_adapter=billing_adapter,
        )
        if cached is not None:
            return cached
        return await _render_elevenlabs_cache_miss(
            literal_text=literal_text,
            synthesized_text=synthesized_text,
            characters=characters,
            voice=voice,
            profile=profile,
            destination=destination,
            billing_adapter=billing_adapter,
            on_pcmu_chunk=on_pcmu_chunk,
            on_pcmu_complete=on_pcmu_complete,
        )


async def _render_elevenlabs_cache_miss(
    *,
    literal_text: str,
    synthesized_text: str,
    characters: int,
    voice: Any,
    profile: Any,
    destination: Path,
    billing_adapter: _PhoneTTSBillingAdapter | None,
    on_pcmu_chunk: PcmuChunkConsumer | None = None,
    on_pcmu_complete: PcmuCompleteConsumer | None = None,
) -> PhoneSpeechAsset:

    try:
        api_key = await _select_elevenlabs_phone_key(voice.voice_code)
    except Exception as exc:
        raise PhoneSpeechError(
            "ElevenLabs credentials could not be selected for the canonical voice"
        ) from exc
    if not api_key:
        raise PhoneSpeechError(
            "No ElevenLabs credential can access the canonical voice"
        )

    billing_token: PhoneCostComponent | None = None
    provider_started = False
    try:
        if billing_adapter is not None:
            try:
                billing_token = await billing_adapter.reserve(
                    provider="elevenlabs",
                    characters=characters,
                )
            except TTSBillingInsufficientBalance as exc:
                raise PhoneSpeechBillingExhausted(
                    "Insufficient telephone balance"
                ) from exc
            claimed = await billing_adapter.provider_started(billing_token)
            if not claimed:
                billing_token = None
                raise PhoneSpeechError(
                    "Telephone TTS provider work was already claimed"
                )
            provider_started = True

        pcmu = await _request_elevenlabs_phone_pcmu(
            voice_id=voice.voice_code,
            text=synthesized_text,
            api_key=api_key,
            model_id=profile.model_id,
            output_format=profile.output_format,
            stability=profile.stability,
            similarity_boost=profile.similarity_boost,
            on_pcmu_chunk=on_pcmu_chunk,
        )
        if billing_token is not None:
            await billing_adapter.settle(billing_token)
            billing_token = None
    except asyncio.CancelledError:
        if billing_token is not None:
            cleanup = asyncio.create_task(
                _fail_billing_after_provider_boundary(
                    billing_adapter,
                    billing_token,
                    provider_started=provider_started,
                    reason="tts_generation_cancelled",
                )
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        raise
    except PhoneSpeechBillingExhausted:
        raise
    except Exception as exc:
        if billing_token is not None:
            await billing_adapter.failed(
                billing_token,
                provider_started=provider_started,
                reason=f"elevenlabs_phone_tts_error:{type(exc).__name__}",
            )
        if isinstance(exc, PhoneSpeechError):
            raise
        raise PhoneSpeechError("ElevenLabs phone TTS could not be rendered") from exc

    cache_task = asyncio.create_task(
        asyncio.to_thread(_write_pcmu_cache, destination, pcmu),
        name=f"phone-pcmu-cache-{destination.stem[:12]}",
    )
    if on_pcmu_complete is not None:
        try:
            # Publish the final alignment frontier as soon as the complete
            # provider body is known.  The atomic cache write proceeds in
            # parallel and is still joined before this render resolves.
            await on_pcmu_complete(pcmu)
        except BaseException:
            # Provider work has already succeeded and been settled. Preserve
            # its reusable cache even if the phone transport disappeared.
            try:
                await asyncio.shield(cache_task)
            except BaseException:
                pass
            raise
    try:
        cache = await asyncio.shield(cache_task)
    except asyncio.CancelledError:
        # Provider usage is already settled.  Keep the digest flight until the
        # atomic cache publication finishes so a retry cannot call it twice.
        try:
            await cache_task
        except Exception:
            pass
        raise
    except PhoneSpeechError:
        raise
    except Exception as exc:
        raise PhoneSpeechError("ElevenLabs phone audio could not be cached") from exc
    return PhoneSpeechAsset(text=literal_text, pcmu=pcmu, cache=cache)


async def render_phone_speech(
    *,
    text: str,
    conversation_id: int,
    current_user: Any,
    call_snapshot: Mapping[str, Any],
    cache_root: str | Path = DEFAULT_PHONE_SPEECH_CACHE,
    call_id: str | None = None,
    billing_dedupe_key: str | None = None,
    billing_service: PhoneBillingService | None = None,
    on_pcmu_chunk: PcmuChunkConsumer | None = None,
    on_pcmu_complete: PcmuCompleteConsumer | None = None,
) -> PhoneSpeechAsset:
    """Render one bounded fragment with the call's exact voice/profile snapshot."""

    literal_text = str(text or "")
    if not literal_text.strip():
        raise PhoneSpeechError("phone speech text is empty")
    if len(literal_text) > 2_000:
        raise PhoneSpeechError("phone speech fragment is too long")
    try:
        snapshot_conversation_id = int(call_snapshot.get("conversation_id"))
    except (TypeError, ValueError) as exc:
        raise PhoneSpeechError("call snapshot conversation is invalid") from exc
    if snapshot_conversation_id != int(conversation_id):
        raise PhoneSpeechError("call snapshot belongs to another conversation")
    try:
        voice = canonical_voice_from_snapshot(call_snapshot)
        profile = live_tts_profile_from_snapshot(call_snapshot)
    except PhoneSnapshotError as exc:
        raise PhoneSpeechError(str(exc)) from exc

    billing_adapter = None
    if billing_service is not None:
        if not call_id or not billing_dedupe_key:
            raise PhoneSpeechError("Telephone TTS billing identity is unavailable")
        billing_adapter = _PhoneTTSBillingAdapter(
            call_id=call_id,
            dedupe_key=billing_dedupe_key,
            service=billing_service,
        )
    if (
        voice.provider == "elevenlabs"
        and profile.model_id == ELEVENLABS_PHONE_TTS_MODEL_ID
        and profile.output_format == ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT
    ):
        return await _render_native_elevenlabs_phone_speech(
            literal_text=literal_text,
            voice=voice,
            profile=profile,
            cache_root=cache_root,
            billing_adapter=billing_adapter,
            on_pcmu_chunk=on_pcmu_chunk,
            on_pcmu_complete=on_pcmu_complete,
        )

    # OpenAI and durable legacy ElevenLabs call snapshots retain the existing
    # provider path.  Only newly captured canonical ElevenLabs calls use the
    # direct raw-PCMU path above.
    audio_path, error = await handle_tts_request(
        None,
        {
            "text": literal_text,
            "author": "bot",
            "conversationId": int(conversation_id),
        },
        current_user,
        is_whatsapp=True,
        tts_context="external",
        resolved_voice_override=voice,
        tts_profile_override=profile,
        billing_adapter=billing_adapter,
    )
    if not audio_path:
        if error == "insufficient-balance":
            raise PhoneSpeechBillingExhausted("Insufficient telephone balance")
        raise PhoneSpeechError(str(error or "canonical TTS returned no audio"))

    source = Path(audio_path)
    cache_key = source.stem
    if not cache_key or len(cache_key) > 128:
        raise PhoneSpeechError("canonical TTS returned an invalid cache identity")
    destination = _phone_cache_destination(cache_root, cache_key)
    try:
        if destination.is_file() and destination.stat().st_size > 0:
            from integrations.telephony.audio import describe_pcmu_cache

            cache = await asyncio.to_thread(describe_pcmu_cache, destination)
        else:
            cache = await asyncio.to_thread(
                materialize_pcmu_cache,
                source,
                destination,
            )
        pcmu = await asyncio.to_thread(cache.path.read_bytes)
    except Exception as exc:
        raise PhoneSpeechError("canonical TTS audio could not be converted to PCMU") from exc
    if not pcmu:
        raise PhoneSpeechError("canonical TTS produced empty phone audio")
    return PhoneSpeechAsset(text=literal_text, pcmu=pcmu, cache=cache)


__all__ = [
    "DEFAULT_PHONE_SPEECH_CACHE",
    "PcmuChunkConsumer",
    "PcmuCompleteConsumer",
    "PhoneSpeechAsset",
    "PhoneSpeechBillingExhausted",
    "PhoneSpeechError",
    "PhoneTextFragmenter",
    "render_phone_speech",
]
