from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from integrations.telephony.cached_playback import (
    CachedAudioPlayback,
    PhoneCachedAudioBackend,
)
from integrations.telephony.greetings import CachedPhoneAudio, PhoneTextAlignment
from integrations.telephony.recording import LocalCallRecorder


def asset(tmp_path: Path) -> CachedPhoneAudio:
    mp3 = tmp_path / "private.mp3"
    pcmu = tmp_path / "private.pcmu"
    mp3.write_bytes(b"not-used-by-media-streams")
    pcmu.write_bytes(b"\xff" * 160)
    return CachedPhoneAudio(
        cache_id=1,
        cache_key="phone:prompt:2:r1:greeting:1:abcdef",
        prompt_id=2,
        greeting_id=3,
        asset_kind="greeting",
        technical_notice_key=None,
        direction="inbound",
        literal_text="Hello again.",
        audio_revision=1,
        voice_id=4,
        provider_key="elevenlabs",
        provider_voice_id="voice-4",
        tts_profile_json="{}",
        content_hash="a" * 64,
        mp3_path=mp3,
        pcmu_path=pcmu,
        duration_ms=20,
        alignment_json="{}",
        alignment=PhoneTextAlignment(
            text="Hello again.",
            character_start_ms=tuple(range(12)),
            character_end_ms=tuple(range(1, 13)),
        ),
    )


@pytest.mark.asyncio
async def test_cached_audio_waits_for_mark_before_persisting(tmp_path) -> None:
    sent = []
    persisted = []

    async def send(message):
        sent.append(message)

    async def persist(text, played_ms, interrupted):
        persisted.append((text, played_ms, interrupted))
        return 99

    playback = CachedAudioPlayback(
        asset(tmp_path), persist_audible_prefix=persist
    )
    task = asyncio.create_task(
        playback.run(
            stream_sid="MZ" + "1" * 32,
            send_message=send,
            recorder=LocalCallRecorder("cached", enabled=False),
            call_started_monotonic=0.0,
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
        marks = [item for item in sent if item["event"] == "mark"]
        if marks:
            break

    assert task.done() is False
    assert persisted == []
    await playback.acknowledge_mark(marks[0]["mark"]["name"])
    result = await task

    assert persisted == [("Hello again.", 20, False)]
    assert result.message_id == 99
    assert result.confirmed_text == "Hello again."


@pytest.mark.asyncio
async def test_cached_audio_barge_is_conservative_and_sends_clear(tmp_path) -> None:
    sent = []
    persisted = []

    async def send(message):
        sent.append(message)

    async def persist(text, played_ms, interrupted):
        persisted.append((text, played_ms, interrupted))
        return 99

    playback = CachedAudioPlayback(
        asset(tmp_path), persist_audible_prefix=persist
    )
    task = asyncio.create_task(
        playback.run(
            stream_sid="MZ" + "1" * 32,
            send_message=send,
            recorder=LocalCallRecorder("cached", enabled=False),
            call_started_monotonic=0.0,
        )
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if any(item["event"] == "mark" for item in sent):
            break

    result = await playback.barge_in()
    completed = await task

    assert result == completed
    assert result.interrupted is True
    assert result.confirmed_text == ""
    assert persisted == []
    assert sent[-1]["event"] == "clear"


@pytest.mark.asyncio
async def test_unknown_notice_uses_captured_global_revision(monkeypatch) -> None:
    captured = {}
    expected = object()

    class Cursor:
        async def fetchone(self):
            return (7,)

    class Connection:
        async def execute(self, sql, values):
            assert "SYSTEM_CONFIG" in sql
            assert values == ("telephony_global_audio_revision",)
            return Cursor()

    @asynccontextmanager
    async def factory(readonly=False):
        assert readonly is True
        yield Connection()

    async def default_voice():
        return object()

    async def profile(_context):
        return object()

    async def load_notice(_conn, **values):
        captured.update(values)
        if values["notice_key"] == "inbound_unavailable":
            raise RuntimeError("revision 7 predates this notice")
        return expected

    monkeypatch.setattr(
        "integrations.telephony.cached_playback.resolve_default_voice",
        default_voice,
    )
    monkeypatch.setattr(
        "integrations.telephony.cached_playback.get_tts_profile", profile
    )
    monkeypatch.setattr(
        "integrations.telephony.cached_playback.load_cached_technical_notice",
        load_notice,
    )
    backend = PhoneCachedAudioBackend(object(), connection_factory=factory)

    result = await backend.load_unknown_notice()

    assert result is expected
    assert captured["prompt_id"] is None
    assert captured["notice_key"] == "unknown_caller"
    assert captured["revision"] == 7
    assert await backend.global_ready() is True
    with pytest.raises(RuntimeError, match="predates"):
        await backend.load_inbound_unavailable_notice()
