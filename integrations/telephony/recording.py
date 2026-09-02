"""Optional private audio retention for native telephone calls.

Recording is disabled by default and this module performs no provider-side
recording.  When a call snapshot enables retention, the media session can keep
the exact raw PCMU participant and assistant tracks.  Gaps are represented as
PCMU silence so both files share a call timeline and can later be replayed,
mixed or re-transcribed without touching the canonical transcript.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Callable

from integrations.telephony.audio import (
    PCMU_SAMPLE_RATE_HZ,
    PCMU_SILENCE_BYTE,
    TelephonyAudioError,
    pcmu_duration_ms,
)
from integrations.telephony.ffmpeg import (
    FfmpegProcessError,
    FfmpegProcessOwnershipError,
    FfmpegProcessTimeout,
    run_owned_ffmpeg,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATIC_ROOT = _PROJECT_ROOT / "data" / "static"
DEFAULT_RECORDING_ROOT = _PROJECT_ROOT / "data" / "phone_recordings"
_CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
MAX_RECORDING_TIMELINE_MS = 86_400_000
_SILENCE_WRITE_CHUNK_BYTES = 64 * 1024


class PhoneRecordingError(RuntimeError):
    """A local recording could not be written or materialized safely."""


@dataclass(frozen=True, slots=True)
class LocalRecordingAsset:
    participant_path: Path | None
    assistant_path: Path | None
    mixed_path: Path | None
    duration_ms: float
    mix_error: str | None = None


class _PcmuTimelineTrack:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None
        self._bytes_written = (
            path.stat().st_size if path.is_file() else 0
        )
        self._closed = False

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def append(self, audio: bytes, *, start_ms: int | None = None) -> None:
        if self._closed:
            raise PhoneRecordingError("recording track is already closed")
        if not isinstance(audio, (bytes, bytearray, memoryview)):
            raise PhoneRecordingError("recording audio must be bytes-like")
        raw = bytes(audio)
        if not raw:
            return
        target = self._bytes_written
        if start_ms is not None:
            if (
                isinstance(start_ms, bool)
                or not isinstance(start_ms, int)
                or not 0 <= start_ms <= MAX_RECORDING_TIMELINE_MS
            ):
                raise PhoneRecordingError("recording timestamp is outside its bounds")
            target = start_ms * PCMU_SAMPLE_RATE_HZ // 1_000
            if target < self._bytes_written:
                raise PhoneRecordingError("recording timestamps cannot overlap or move backwards")
        self._ensure_open()
        gap = target - self._bytes_written
        while gap:
            write_size = min(gap, _SILENCE_WRITE_CHUNK_BYTES)
            self._file.write(PCMU_SILENCE_BYTE * write_size)
            self._bytes_written += write_size
            gap -= write_size
        self._file.write(raw)
        self._bytes_written += len(raw)

    def close(self) -> Path | None:
        if self._closed:
            return self.path if self._bytes_written else None
        self._closed = True
        if self._file is not None:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            self._file = None
            os.chmod(self.path, 0o600)
        return self.path if self._bytes_written else None

    def _ensure_open(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        if self.path.exists():
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND)
        else:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        try:
            os.chmod(self.path, 0o600)
            self._file = os.fdopen(descriptor, "ab", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise


class LocalCallRecorder:
    """Write optional raw tracks for one call and finalize them once."""

    def __init__(
        self,
        call_id: str,
        *,
        enabled: bool,
        root: str | os.PathLike[str] = DEFAULT_RECORDING_ROOT,
    ) -> None:
        normalized_call_id = str(call_id or "").strip()
        if _CALL_ID_PATTERN.fullmatch(normalized_call_id) is None:
            raise ValueError("call_id is not safe for private recording storage")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self.call_id = normalized_call_id
        self.enabled = enabled
        self.root = Path(root).resolve()
        _assert_private_root(self.root)
        shard = normalized_call_id[:2].lower()
        self.directory = self.root / shard / normalized_call_id
        self._participant = _PcmuTimelineTrack(self.directory / "participant.mulaw")
        self._assistant = _PcmuTimelineTrack(self.directory / "assistant.mulaw")
        self._raw_finalized: LocalRecordingAsset | None = None
        self._finalized: LocalRecordingAsset | None = None

    def record_participant(self, audio: bytes, *, start_ms: int | None = None) -> None:
        if self.enabled:
            self._participant.append(audio, start_ms=start_ms)

    def record_assistant(self, audio: bytes, *, start_ms: int | None = None) -> None:
        if self.enabled:
            self._assistant.append(audio, start_ms=start_ms)

    def finalize(
        self,
        *,
        create_mix: bool = True,
        mixer: Callable[..., Path] | None = None,
    ) -> LocalRecordingAsset:
        if self._finalized is not None:
            return self._finalized
        raw = self.finalize_raw()
        mixed = None
        mix_error = None
        sources = tuple(
            path
            for path in (raw.participant_path, raw.assistant_path)
            if path is not None
        )
        if create_mix and sources:
            try:
                active_mixer = mixer or mix_pcmu_tracks_ffmpeg
                mixed = active_mixer(
                    participant_path=raw.participant_path,
                    assistant_path=raw.assistant_path,
                    destination_path=self.directory / "mixed.mp3",
                )
            except Exception:
                # Raw tracks are the canonical retained assets.  A mix can be
                # regenerated later, so keep the failure visible but do not
                # discard re-transcribable audio.
                mix_error = "mixed_audio_generation_failed"
        self._finalized = LocalRecordingAsset(
            participant_path=raw.participant_path,
            assistant_path=raw.assistant_path,
            mixed_path=mixed,
            duration_ms=raw.duration_ms,
            mix_error=mix_error,
        )
        return self._finalized

    async def finalize_async(
        self,
        *,
        create_mix: bool = True,
        mixer: Callable[..., Awaitable[Path]] | None = None,
    ) -> LocalRecordingAsset:
        """Finalize without placing an unowned ffmpeg process in a thread."""

        if self._finalized is not None:
            return self._finalized
        raw = self.finalize_raw()
        mixed = None
        mix_error = None
        sources = tuple(
            path
            for path in (raw.participant_path, raw.assistant_path)
            if path is not None
        )
        if create_mix and sources:
            try:
                active_mixer = mixer or mix_pcmu_tracks_ffmpeg_async
                mixed = await active_mixer(
                    participant_path=raw.participant_path,
                    assistant_path=raw.assistant_path,
                    destination_path=self.directory / "mixed.mp3",
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                mix_error = "mixed_audio_generation_failed"
        self._finalized = LocalRecordingAsset(
            participant_path=raw.participant_path,
            assistant_path=raw.assistant_path,
            mixed_path=mixed,
            duration_ms=raw.duration_ms,
            mix_error=mix_error,
        )
        return self._finalized

    def finalize_raw(self) -> LocalRecordingAsset:
        """Close and fsync raw tracks without starting an optional mixer."""

        if self._raw_finalized is not None:
            return self._raw_finalized
        if not self.enabled:
            self._raw_finalized = LocalRecordingAsset(None, None, None, 0.0)
            return self._raw_finalized
        participant = self._participant.close()
        assistant = self._assistant.close()
        self._raw_finalized = LocalRecordingAsset(
            participant_path=participant,
            assistant_path=assistant,
            mixed_path=None,
            duration_ms=max(
                pcmu_duration_ms(self._participant.bytes_written),
                pcmu_duration_ms(self._assistant.bytes_written),
            ),
        )
        return self._raw_finalized


def mix_pcmu_tracks_ffmpeg(
    *,
    participant_path: Path | None,
    assistant_path: Path | None,
    destination_path: Path,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: float = 120.0,
    command_runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """Atomically render one or two aligned raw PCMU tracks as private MP3."""

    sources = [
        Path(path)
        for path in (participant_path, assistant_path)
        if path is not None and Path(path).is_file() and Path(path).stat().st_size > 0
    ]
    if not sources:
        raise PhoneRecordingError("at least one non-empty PCMU track is required")
    destination = Path(destination_path)
    _assert_private_root(destination.parent.resolve())
    if not ffmpeg_binary.strip() or timeout_seconds <= 0:
        raise PhoneRecordingError("invalid ffmpeg recording configuration")
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp.mp3",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        command: list[str] = [ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        for source in sources:
            command.extend(
                [
                    "-f",
                    "mulaw",
                    "-ar",
                    str(PCMU_SAMPLE_RATE_HZ),
                    "-ac",
                    "1",
                    "-i",
                    str(source),
                ]
            )
        if len(sources) == 2:
            command.extend(
                [
                    "-filter_complex",
                    "amix=inputs=2:duration=longest:dropout_transition=0",
                ]
            )
        command.extend(["-codec:a", "libmp3lame", str(temporary_path)])
        result = command_runner(
            tuple(command),
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        if getattr(result, "returncode", 1) != 0:
            raise PhoneRecordingError("ffmpeg could not mix the call recording")
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise PhoneRecordingError("ffmpeg produced no mixed recording")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
        temporary_path = None
        return destination
    except (OSError, subprocess.SubprocessError, TelephonyAudioError) as exc:
        raise PhoneRecordingError("could not mix the call recording") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def mix_pcmu_tracks_ffmpeg_async(
    *,
    participant_path: Path | None,
    assistant_path: Path | None,
    destination_path: Path,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: float = 120.0,
    terminate_grace_seconds: float = 2.0,
    kill_grace_seconds: float = 2.0,
    process_factory: Callable[..., Awaitable[Any]] = asyncio.create_subprocess_exec,
) -> Path:
    """Render aligned PCMU tracks with one owned, cancelable ffmpeg process."""

    sources = [
        Path(path)
        for path in (participant_path, assistant_path)
        if path is not None and Path(path).is_file() and Path(path).stat().st_size > 0
    ]
    if not sources:
        raise PhoneRecordingError("at least one non-empty PCMU track is required")
    destination = Path(destination_path)
    _assert_private_root(destination.parent.resolve())
    if (
        not ffmpeg_binary.strip()
        or timeout_seconds <= 0
        or terminate_grace_seconds <= 0
        or kill_grace_seconds <= 0
    ):
        raise PhoneRecordingError("invalid ffmpeg recording configuration")
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp.mp3",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        command: list[str] = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
        ]
        for source in sources:
            command.extend(
                [
                    "-f",
                    "mulaw",
                    "-ar",
                    str(PCMU_SAMPLE_RATE_HZ),
                    "-ac",
                    "1",
                    "-i",
                    str(source),
                ]
            )
        if len(sources) == 2:
            command.extend(
                [
                    "-filter_complex",
                    "amix=inputs=2:duration=longest:dropout_transition=0",
                ]
            )
        command.extend(["-codec:a", "libmp3lame", str(temporary_path)])
        try:
            returncode = await run_owned_ffmpeg(
                command,
                timeout_seconds=timeout_seconds,
                terminate_grace_seconds=terminate_grace_seconds,
                kill_grace_seconds=kill_grace_seconds,
                process_factory=process_factory,
                process_kwargs={
                    "stdin": asyncio.subprocess.DEVNULL,
                    "stdout": asyncio.subprocess.DEVNULL,
                    "stderr": asyncio.subprocess.DEVNULL,
                },
                task_name_prefix="aurvek-ffmpeg-recording",
            )
        except FfmpegProcessTimeout as exc:
            raise PhoneRecordingError("ffmpeg recording mix timed out") from exc
        except FfmpegProcessOwnershipError as exc:
            raise PhoneRecordingError(
                "ffmpeg recording process could not be stopped"
            ) from exc
        except FfmpegProcessError as exc:
            raise PhoneRecordingError("could not mix the call recording") from exc
        if returncode != 0:
            raise PhoneRecordingError("ffmpeg could not mix the call recording")
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise PhoneRecordingError("ffmpeg produced no mixed recording")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
        temporary_path = None
        return destination
    except asyncio.CancelledError:
        raise
    except (OSError, TelephonyAudioError) as exc:
        raise PhoneRecordingError("could not mix the call recording") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _assert_private_root(path: Path) -> None:
    static_root = _STATIC_ROOT.resolve()
    resolved = path.resolve()
    if resolved == static_root or static_root in resolved.parents:
        raise ValueError("phone recordings cannot be stored under data/static")


__all__ = [
    "DEFAULT_RECORDING_ROOT",
    "LocalCallRecorder",
    "LocalRecordingAsset",
    "PhoneRecordingError",
    "mix_pcmu_tracks_ffmpeg",
    "mix_pcmu_tracks_ffmpeg_async",
]
