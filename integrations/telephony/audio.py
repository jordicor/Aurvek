"""Audio primitives for Twilio bidirectional Media Streams.

Twilio's phone transport uses headerless 8 kHz, mono mu-law (PCMU) audio.
This module deliberately does not know anything about prompts, voices or TTS
providers: callers must provide audio rendered with the already-resolved
canonical voice.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


PCMU_SAMPLE_RATE_HZ = 8_000
PCMU_CHANNELS = 1
PCMU_BYTES_PER_SAMPLE = 1
_PCMU_BYTES_PER_SECOND = (
    PCMU_SAMPLE_RATE_HZ * PCMU_CHANNELS * PCMU_BYTES_PER_SAMPLE
)
MEDIA_FRAME_DURATION_MS = 20
MEDIA_FRAME_BYTES = (
    PCMU_SAMPLE_RATE_HZ
    * PCMU_CHANNELS
    * PCMU_BYTES_PER_SAMPLE
    * MEDIA_FRAME_DURATION_MS
    // 1_000
)
PCMU_SILENCE_BYTE = b"\xff"

_MAX_TWILIO_ENCODED_PAYLOAD_CHARS = 131_072
_PROJECT_STATIC_ROOT = Path(__file__).resolve().parents[2] / "data" / "static"


class TelephonyAudioError(ValueError):
    """Base error for malformed or unusable phone audio."""


class AudioConversionError(TelephonyAudioError):
    """Raised when a cached asset cannot be converted to raw PCMU."""


@dataclass(frozen=True, slots=True)
class PcmuFrame:
    """One exact 20 ms outbound Media Streams frame."""

    sequence: int
    payload: bytes
    source_bytes: int

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise TelephonyAudioError("frame sequence cannot be negative")
        if len(self.payload) != MEDIA_FRAME_BYTES:
            raise TelephonyAudioError(
                f"PCMU frames must contain exactly {MEDIA_FRAME_BYTES} bytes"
            )
        if not 1 <= self.source_bytes <= MEDIA_FRAME_BYTES:
            raise TelephonyAudioError("source_bytes is outside the frame")

    @property
    def start_ms(self) -> int:
        return self.sequence * MEDIA_FRAME_DURATION_MS

    @property
    def duration_ms(self) -> int:
        return MEDIA_FRAME_DURATION_MS

    @property
    def twilio_payload(self) -> str:
        return encode_twilio_media_payload(self.payload)


@dataclass(frozen=True, slots=True)
class PcmuCacheAsset:
    """Metadata derived from a private, headerless PCMU cache file."""

    path: Path
    byte_length: int
    duration_ms: float
    sha256: str


class PcmuFrameBuffer:
    """Frame arbitrarily-sized streaming PCMU chunks without audio gaps."""

    def __init__(self, *, start_sequence: int = 0) -> None:
        if start_sequence < 0:
            raise TelephonyAudioError("start_sequence cannot be negative")
        self._next_sequence = start_sequence
        self._pending = bytearray()
        self._finished = False

    @property
    def pending_bytes(self) -> int:
        return len(self._pending)

    @property
    def finished(self) -> bool:
        return self._finished

    def feed(
        self,
        audio: bytes | bytearray | memoryview,
    ) -> tuple[PcmuFrame, ...]:
        """Accept a provider chunk and return all newly-complete frames."""

        if self._finished:
            raise TelephonyAudioError("cannot feed a finished PCMU frame buffer")
        raw = _coerce_audio_bytes(audio)
        if not raw:
            return ()
        self._pending.extend(raw)
        complete_bytes = len(self._pending) // MEDIA_FRAME_BYTES * MEDIA_FRAME_BYTES
        frames: list[PcmuFrame] = []
        for offset in range(0, complete_bytes, MEDIA_FRAME_BYTES):
            frames.append(
                PcmuFrame(
                    sequence=self._next_sequence,
                    payload=bytes(
                        self._pending[offset : offset + MEDIA_FRAME_BYTES]
                    ),
                    source_bytes=MEDIA_FRAME_BYTES,
                )
            )
            self._next_sequence += 1
        if complete_bytes:
            del self._pending[:complete_bytes]
        return tuple(frames)

    def finish(self) -> tuple[PcmuFrame, ...]:
        """Pad and return the single remaining frame, if any, exactly once."""

        if self._finished:
            return ()
        self._finished = True
        if not self._pending:
            return ()
        source_bytes = len(self._pending)
        payload = bytes(self._pending) + (
            PCMU_SILENCE_BYTE * (MEDIA_FRAME_BYTES - source_bytes)
        )
        self._pending.clear()
        frame = PcmuFrame(
            sequence=self._next_sequence,
            payload=payload,
            source_bytes=source_bytes,
        )
        self._next_sequence += 1
        return (frame,)


def decode_twilio_media_payload(
    payload: str,
    *,
    max_encoded_chars: int = _MAX_TWILIO_ENCODED_PAYLOAD_CHARS,
) -> bytes:
    """Strictly decode one base64 Media Streams payload.

    Incoming Twilio chunks are not required to be 20 ms long, so this helper
    validates only transport encoding and a defensive size bound. Outbound
    framing is handled by :func:`iter_pcmu_frames`.
    """

    if not isinstance(payload, str) or not payload:
        raise TelephonyAudioError("Twilio media payload must be a non-empty string")
    if max_encoded_chars <= 0:
        raise TelephonyAudioError("max_encoded_chars must be positive")
    if len(payload) > max_encoded_chars:
        raise TelephonyAudioError("Twilio media payload exceeds the size limit")
    try:
        encoded = payload.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise TelephonyAudioError("Twilio media payload is not valid base64") from exc
    if not decoded:
        raise TelephonyAudioError("Twilio media payload decoded to no audio")
    return decoded


def encode_twilio_media_payload(audio: bytes | bytearray | memoryview) -> str:
    """Encode raw PCMU bytes for a Twilio ``media.payload`` field."""

    raw = _coerce_audio_bytes(audio)
    if not raw:
        raise TelephonyAudioError("audio cannot be empty")
    return base64.b64encode(raw).decode("ascii")


def iter_pcmu_frames(
    audio: bytes | bytearray | memoryview,
) -> Iterator[PcmuFrame]:
    """Yield exact 160-byte/20-ms PCMU frames, padding only the last frame.

    ``source_bytes`` records how many bytes in the final frame came from the
    source. This lets the later playback ledger avoid treating padding as
    spoken audio without coupling framing to that ledger.
    """

    raw = _coerce_audio_bytes(audio)

    buffer = PcmuFrameBuffer()
    yield from buffer.feed(raw)
    yield from buffer.finish()


def pcmu_duration_ms(byte_length: int) -> float:
    """Return the exact duration represented by headerless PCMU bytes."""

    if byte_length < 0:
        raise TelephonyAudioError("PCMU byte length cannot be negative")
    return byte_length * 1_000 / _PCMU_BYTES_PER_SECOND


def pcmu_duration_ceiling_ms(byte_length: int) -> int:
    """Return the smallest whole millisecond containing all PCMU samples.

    Cached character alignments use conservative integer-millisecond end
    boundaries.  Rounding an exact PCMU duration down would make a valid final
    boundary appear to extend beyond its audio.  Integer ceiling arithmetic
    preserves that boundary without changing the exact float timeline used by
    playback.
    """

    if byte_length < 0:
        raise TelephonyAudioError("PCMU byte length cannot be negative")
    duration_numerator = byte_length * 1_000
    return (
        duration_numerator + _PCMU_BYTES_PER_SECOND - 1
    ) // _PCMU_BYTES_PER_SECOND


def convert_mp3_to_pcmu_ffmpeg(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: float = 60.0,
    command_runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Convert an MP3 asset to headerless mono 8 kHz PCMU using ffmpeg.

    The command is always passed as an argument sequence with ``shell=False``.
    ``command_runner`` is injectable so tests and callers never need network
    access and can validate the exact codec contract without invoking ffmpeg.
    """

    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise AudioConversionError("source MP3 does not exist or is empty")
    if source.resolve() == destination.resolve():
        raise AudioConversionError("source and destination paths must differ")
    if not ffmpeg_binary.strip():
        raise AudioConversionError("ffmpeg binary cannot be empty")
    if timeout_seconds <= 0:
        raise AudioConversionError("ffmpeg timeout must be positive")

    destination.parent.mkdir(parents=True, exist_ok=True)
    command: Sequence[str] = (
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(PCMU_SAMPLE_RATE_HZ),
        "-acodec",
        "pcm_mulaw",
        "-f",
        "mulaw",
        str(destination),
    )
    try:
        result = command_runner(
            command,
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioConversionError("ffmpeg could not convert the audio") from exc

    if getattr(result, "returncode", 1) != 0:
        raise AudioConversionError("ffmpeg rejected the source audio")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise AudioConversionError("ffmpeg produced no PCMU audio")


def materialize_pcmu_cache(
    source_mp3_path: str | os.PathLike[str],
    cache_path: str | os.PathLike[str],
    *,
    converter: Callable[[Path, Path], None] = convert_mp3_to_pcmu_ffmpeg,
) -> PcmuCacheAsset:
    """Atomically create a private raw-PCMU derivative of a cached MP3.

    The completed file replaces the destination only after conversion and
    validation succeed. Public ``data/static`` paths are rejected because
    phone audio cache files must be served only by authorized endpoints.
    """

    source = Path(source_mp3_path)
    destination = Path(cache_path)
    _assert_private_cache_destination(destination)
    if not source.is_file() or source.stat().st_size <= 0:
        raise AudioConversionError("source MP3 does not exist or is empty")
    if source.resolve() == destination.resolve():
        raise AudioConversionError("source and cache paths must differ")

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
        converter(source, temporary_path)
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise AudioConversionError("audio converter produced no PCMU audio")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
        temporary_path = None
        return describe_pcmu_cache(destination)
    except AudioConversionError:
        raise
    except Exception as exc:
        raise AudioConversionError("could not materialize the PCMU cache") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def describe_pcmu_cache(
    cache_path: str | os.PathLike[str],
) -> PcmuCacheAsset:
    """Validate and describe an existing private raw-PCMU cache file."""

    path = Path(cache_path)
    _assert_private_cache_destination(path)
    if not path.is_file():
        raise TelephonyAudioError("PCMU cache file does not exist")
    byte_length = path.stat().st_size
    if byte_length <= 0:
        raise TelephonyAudioError("PCMU cache file is empty")

    digest = hashlib.sha256()
    with path.open("rb") as cached_audio:
        for block in iter(lambda: cached_audio.read(64 * 1024), b""):
            digest.update(block)
    return PcmuCacheAsset(
        path=path,
        byte_length=byte_length,
        duration_ms=pcmu_duration_ms(byte_length),
        sha256=digest.hexdigest(),
    )


def _assert_private_cache_destination(path: Path) -> None:
    resolved = path.resolve()
    static_root = _PROJECT_STATIC_ROOT.resolve()
    if resolved == static_root or static_root in resolved.parents:
        raise TelephonyAudioError(
            "phone audio cache cannot be stored under data/static"
        )


def _coerce_audio_bytes(audio: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(audio, (bytes, bytearray, memoryview)):
        raise TelephonyAudioError("audio must be bytes-like")
    return bytes(audio)


__all__ = [
    "AudioConversionError",
    "MEDIA_FRAME_BYTES",
    "MEDIA_FRAME_DURATION_MS",
    "PCMU_CHANNELS",
    "PCMU_SAMPLE_RATE_HZ",
    "PcmuCacheAsset",
    "PcmuFrame",
    "PcmuFrameBuffer",
    "TelephonyAudioError",
    "convert_mp3_to_pcmu_ffmpeg",
    "decode_twilio_media_payload",
    "describe_pcmu_cache",
    "encode_twilio_media_payload",
    "iter_pcmu_frames",
    "materialize_pcmu_cache",
    "pcmu_duration_ceiling_ms",
    "pcmu_duration_ms",
]
