from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.telephony.audio import PCMU_SILENCE_BYTE
from integrations.telephony.ffmpeg import FfmpegProcessError
from integrations.telephony.recording import (
    LocalCallRecorder,
    PhoneRecordingError,
    mix_pcmu_tracks_ffmpeg,
    mix_pcmu_tracks_ffmpeg_async,
)


def test_disabled_recorder_never_creates_audio(tmp_path):
    recorder = LocalCallRecorder("call-disabled", enabled=False, root=tmp_path)

    recorder.record_participant(b"\x01" * 160, start_ms=0)
    recorder.record_assistant(b"\x02" * 160, start_ms=0)
    asset = recorder.finalize()

    assert asset.participant_path is None
    assert asset.assistant_path is None
    assert asset.mixed_path is None
    assert asset.duration_ms == 0
    assert not tmp_path.joinpath("ca", "call-disabled").exists()


def test_enabled_recorder_keeps_aligned_private_raw_tracks(tmp_path):
    recorder = LocalCallRecorder("call-aligned", enabled=True, root=tmp_path)

    recorder.record_participant(b"\x01" * 160, start_ms=0)
    recorder.record_participant(b"\x03" * 160, start_ms=40)
    recorder.record_assistant(b"\x02" * 160, start_ms=20)
    asset = recorder.finalize(create_mix=False)

    assert asset.participant_path.read_bytes() == (
        b"\x01" * 160 + PCMU_SILENCE_BYTE * 160 + b"\x03" * 160
    )
    assert asset.assistant_path.read_bytes() == (
        PCMU_SILENCE_BYTE * 160 + b"\x02" * 160
    )
    assert asset.duration_ms == 60.0
    assert asset.mixed_path is None
    if os.name != "nt":
        assert asset.participant_path.stat().st_mode & 0o777 == 0o600


def test_raw_track_is_private_before_finalize_and_survives_reconnect(tmp_path):
    recorder = LocalCallRecorder("call-reconnect", enabled=True, root=tmp_path)
    recorder.record_participant(b"\x01" * 160, start_ms=0)
    path = tmp_path / "ca" / "call-reconnect" / "participant.mulaw"

    assert path.read_bytes() == b"\x01" * 160
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    # A new media-session object for the same durable call resumes the raw
    # track rather than truncating or exposing it.
    recorder.finalize(create_mix=False)
    resumed = LocalCallRecorder("call-reconnect", enabled=True, root=tmp_path)
    resumed.record_participant(b"\x02" * 160, start_ms=20)
    resumed.finalize(create_mix=False)
    assert path.read_bytes() == b"\x01" * 160 + b"\x02" * 160


def test_recording_timestamps_cannot_overlap(tmp_path):
    recorder = LocalCallRecorder("call-order", enabled=True, root=tmp_path)
    recorder.record_participant(b"\x01" * 160, start_ms=20)

    with pytest.raises(PhoneRecordingError, match="overlap"):
        recorder.record_participant(b"\x02" * 160, start_ms=10)

    recorder.finalize(create_mix=False)


def test_finalize_preserves_raw_tracks_when_mix_fails(tmp_path):
    recorder = LocalCallRecorder("call-mix-fail", enabled=True, root=tmp_path)
    recorder.record_participant(b"\x01" * 160)

    def fail_mix(**_kwargs):
        raise PhoneRecordingError("synthetic")

    asset = recorder.finalize(mixer=fail_mix)

    assert asset.participant_path.is_file()
    assert asset.mixed_path is None
    assert asset.mix_error == "mixed_audio_generation_failed"
    assert recorder.finalize() is asset


def test_ffmpeg_mixer_uses_raw_mulaw_8khz_and_atomic_destination(tmp_path):
    participant = tmp_path / "participant.mulaw"
    assistant = tmp_path / "assistant.mulaw"
    participant.write_bytes(b"\x01" * 160)
    assistant.write_bytes(b"\x02" * 160)
    destination = tmp_path / "mixed.mp3"
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"synthetic-mp3")
        return SimpleNamespace(returncode=0)

    result = mix_pcmu_tracks_ffmpeg(
        participant_path=participant,
        assistant_path=assistant,
        destination_path=destination,
        command_runner=runner,
    )

    assert result == destination
    assert destination.read_bytes() == b"synthetic-mp3"
    command = calls[0][0]
    assert command.count("mulaw") == 2
    assert command.count("8000") == 2
    assert "amix=inputs=2:duration=longest:dropout_transition=0" in command
    assert calls[0][1]["shell"] is False


def test_call_id_and_public_static_destination_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        LocalCallRecorder("../escape", enabled=True, root=tmp_path)

    project_static = (
        Path(__file__).resolve().parents[1] / "data" / "static" / "recordings"
    )
    with pytest.raises(ValueError, match="data/static"):
        LocalCallRecorder("call-public", enabled=True, root=project_static)

    with pytest.raises(ValueError, match="boolean"):
        LocalCallRecorder("call-string-bool", enabled="false", root=tmp_path)


def test_recording_timestamp_has_a_hard_timeline_bound(tmp_path):
    recorder = LocalCallRecorder("call-gap-bound", enabled=True, root=tmp_path)
    with pytest.raises(PhoneRecordingError, match="outside its bounds"):
        recorder.record_participant(b"\x01", start_ms=86_400_001)


class _FakeAsyncProcess:
    def __init__(self, *, exit_on_terminate: bool) -> None:
        self.returncode = None
        self.exit_on_terminate = exit_on_terminate
        self.exited = asyncio.Event()
        self.terminated = 0
        self.killed = 0

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self.exit_on_terminate:
            self.returncode = -15
            self.exited.set()

    def kill(self):
        self.killed += 1
        self.returncode = -9
        self.exited.set()


@pytest.mark.asyncio
async def test_async_ffmpeg_timeout_terminates_then_kills_without_orphan(
    tmp_path,
) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)
    process = _FakeAsyncProcess(exit_on_terminate=False)

    async def process_factory(*_command, **_kwargs):
        return process

    with pytest.raises(PhoneRecordingError, match="timed out"):
        await mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            timeout_seconds=0.01,
            terminate_grace_seconds=0.01,
            kill_grace_seconds=0.1,
            process_factory=process_factory,
        )

    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert not (tmp_path / "mixed.mp3").exists()


@pytest.mark.asyncio
async def test_async_ffmpeg_cancellation_reaps_process_before_propagating(
    tmp_path,
) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)
    process = _FakeAsyncProcess(exit_on_terminate=True)

    async def process_factory(*_command, **_kwargs):
        return process

    mixing = asyncio.create_task(
        mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            process_factory=process_factory,
        )
    )
    await asyncio.sleep(0)
    mixing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await mixing

    assert process.terminated == 1
    assert process.killed == 0
    assert process.returncode == -15
    assert not (tmp_path / "mixed.mp3").exists()
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_repeated_cancellation_cannot_abandon_process(
    tmp_path,
) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)

    class Process(_FakeAsyncProcess):
        def __init__(self) -> None:
            super().__init__(exit_on_terminate=False)
            self.wait_calls = 0
            self.initial_wait_started = asyncio.Event()
            self.terminate_reap_started = asyncio.Event()

        async def wait(self):
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.initial_wait_started.set()
            elif self.wait_calls == 2:
                self.terminate_reap_started.set()
            return await super().wait()

    process = Process()

    async def process_factory(*_command, **_kwargs):
        return process

    mixing = asyncio.create_task(
        mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            timeout_seconds=10.0,
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.1,
            process_factory=process_factory,
        )
    )
    await process.initial_wait_started.wait()
    mixing.cancel()
    await process.terminate_reap_started.wait()
    mixing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await mixing
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert process.wait_calls == 3
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_repeated_cancellation_during_spawn_recovers_process(
    tmp_path,
) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)
    factory_started = asyncio.Event()
    first_cancel_received = asyncio.Event()
    release_factory = asyncio.Event()
    process = _FakeAsyncProcess(exit_on_terminate=False)

    async def process_factory(*_command, **_kwargs):
        factory_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancel_received.set()
            await release_factory.wait()
            return process

    mixing = asyncio.create_task(
        mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            timeout_seconds=10.0,
            terminate_grace_seconds=0.001,
            kill_grace_seconds=0.1,
            process_factory=process_factory,
        )
    )
    await factory_started.wait()
    mixing.cancel()
    await first_cancel_received.wait()
    mixing.cancel()
    release_factory.set()

    with pytest.raises(asyncio.CancelledError):
        await mixing
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_spawn_timeout_joins_factory_without_task_leak(
    tmp_path,
) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)
    factory_finished = asyncio.Event()

    async def process_factory(*_command, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            factory_finished.set()

    with pytest.raises(PhoneRecordingError, match="timed out"):
        await mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            timeout_seconds=0.001,
            process_factory=process_factory,
        )
    assert factory_finished.is_set()
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_factory_runtime_error_is_normalized(tmp_path) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)

    async def process_factory(*_command, **_kwargs):
        raise RuntimeError("synthetic factory failure")

    with pytest.raises(PhoneRecordingError, match="could not mix") as failure:
        await mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            process_factory=process_factory,
        )
    assert isinstance(failure.value.__cause__, FfmpegProcessError)
    assert isinstance(failure.value.__cause__.__cause__, RuntimeError)
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_wait_runtime_error_is_normalized(tmp_path) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)

    class Process(_FakeAsyncProcess):
        def __init__(self) -> None:
            super().__init__(exit_on_terminate=True)
            self.wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise RuntimeError("synthetic wait failure")
            return await super().wait()

    process = Process()

    async def process_factory(*_command, **_kwargs):
        return process

    with pytest.raises(PhoneRecordingError, match="could not mix") as failure:
        await mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            process_factory=process_factory,
        )
    assert isinstance(failure.value.__cause__, FfmpegProcessError)
    assert isinstance(failure.value.__cause__.__cause__, RuntimeError)
    assert process.terminated == 1
    assert process.killed == 0
    assert process.returncode == -15
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_reports_unreaped_process_explicitly(tmp_path) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)

    class Process(_FakeAsyncProcess):
        def kill(self):
            self.killed += 1

    process = Process(exit_on_terminate=False)

    async def process_factory(*_command, **_kwargs):
        return process

    with pytest.raises(PhoneRecordingError, match="could not be stopped"):
        await mix_pcmu_tracks_ffmpeg_async(
            participant_path=participant,
            assistant_path=None,
            destination_path=tmp_path / "mixed.mp3",
            timeout_seconds=0.001,
            terminate_grace_seconds=0.001,
            kill_grace_seconds=0.001,
            process_factory=process_factory,
        )
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode is None
    assert participant.read_bytes() == b"\x01" * 160
    assert not _recording_ffmpeg_tasks()


@pytest.mark.asyncio
async def test_async_ffmpeg_publishes_only_successful_nonempty_output(
    tmp_path,
) -> None:
    participant = tmp_path / "participant.mulaw"
    participant.write_bytes(b"\x01" * 160)
    process = _FakeAsyncProcess(exit_on_terminate=False)
    process.returncode = 0
    process.exited.set()
    calls = []

    async def process_factory(*command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"async-synthetic-mp3")
        return process

    destination = tmp_path / "mixed.mp3"
    result = await mix_pcmu_tracks_ffmpeg_async(
        participant_path=participant,
        assistant_path=None,
        destination_path=destination,
        process_factory=process_factory,
    )

    assert result == destination
    assert destination.read_bytes() == b"async-synthetic-mp3"
    assert calls[0][0][0] == "ffmpeg"
    assert "shell" not in calls[0][1]
    assert not _recording_ffmpeg_tasks()


def _recording_ffmpeg_tasks():
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and task.get_name().startswith("aurvek-ffmpeg-recording-")
    ]
