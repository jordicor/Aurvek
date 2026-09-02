from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from integrations.telephony.audio import (
    AudioConversionError,
    MEDIA_FRAME_BYTES,
    MEDIA_FRAME_DURATION_MS,
    TelephonyAudioError,
    PcmuFrameBuffer,
    convert_mp3_to_pcmu_ffmpeg,
    decode_twilio_media_payload,
    describe_pcmu_cache,
    encode_twilio_media_payload,
    iter_pcmu_frames,
    materialize_pcmu_cache,
    pcmu_duration_ceiling_ms,
    pcmu_duration_ms,
)


def test_twilio_payload_round_trip_is_strict_base64():
    audio = bytes(range(160))
    payload = encode_twilio_media_payload(audio)

    assert payload == base64.b64encode(audio).decode("ascii")
    assert decode_twilio_media_payload(payload) == audio

    for invalid in ("", "not base64!", "YWJj\n", "áudio"):
        with pytest.raises(TelephonyAudioError):
            decode_twilio_media_payload(invalid)


def test_twilio_payload_has_a_defensive_size_bound():
    with pytest.raises(TelephonyAudioError, match="size limit"):
        decode_twilio_media_payload("AAAA", max_encoded_chars=3)


def test_pcmu_chunking_emits_exact_20ms_frames_and_pads_only_the_tail():
    source = bytes(range(160)) + b"\x07"

    frames = list(iter_pcmu_frames(source))

    assert MEDIA_FRAME_BYTES == 160
    assert MEDIA_FRAME_DURATION_MS == 20
    assert len(frames) == 2
    assert frames[0].sequence == 0
    assert frames[0].source_bytes == 160
    assert frames[0].payload == source[:160]
    assert frames[1].sequence == 1
    assert frames[1].start_ms == 20
    assert frames[1].duration_ms == 20
    assert frames[1].source_bytes == 1
    assert frames[1].payload == b"\x07" + (b"\xff" * 159)
    assert decode_twilio_media_payload(frames[1].twilio_payload) == frames[1].payload


def test_empty_pcmu_has_no_frames_and_duration_uses_raw_8khz_bytes():
    assert list(iter_pcmu_frames(b"")) == []
    assert pcmu_duration_ms(160) == 20.0
    assert pcmu_duration_ms(8_000) == 1_000.0
    with pytest.raises(TelephonyAudioError):
        pcmu_duration_ms(-1)
    with pytest.raises(TelephonyAudioError, match="bytes-like"):
        list(iter_pcmu_frames(160))


def test_pcmu_integer_duration_ceil_never_understates_fractional_sample_span():
    assert pcmu_duration_ms(16_347) == 2_043.375
    assert pcmu_duration_ceiling_ms(16_347) == 2_044
    assert pcmu_duration_ceiling_ms(16_344) == 2_043
    assert pcmu_duration_ceiling_ms(0) == 0
    with pytest.raises(TelephonyAudioError):
        pcmu_duration_ceiling_ms(-1)


def test_streaming_frame_buffer_carries_remainder_between_provider_chunks():
    buffer = PcmuFrameBuffer(start_sequence=4)

    assert buffer.feed(b"a" * 80) == ()
    frames = buffer.feed((b"b" * 80) + b"c")

    assert len(frames) == 1
    assert frames[0].sequence == 4
    assert frames[0].payload == (b"a" * 80) + (b"b" * 80)
    assert buffer.pending_bytes == 1

    tail = buffer.finish()
    assert len(tail) == 1
    assert tail[0].sequence == 5
    assert tail[0].source_bytes == 1
    assert tail[0].payload == b"c" + (b"\xff" * 159)
    assert buffer.finish() == ()
    with pytest.raises(TelephonyAudioError, match="finished"):
        buffer.feed(b"late")


def test_ffmpeg_conversion_locks_headerless_mono_8khz_mulaw_contract(tmp_path):
    source = tmp_path / "greeting.mp3"
    destination = tmp_path / "greeting.pcmu"
    source.write_bytes(b"synthetic-mp3")
    captured = {}

    def fake_runner(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"\xff" * 160)
        return SimpleNamespace(returncode=0, stderr=b"")

    convert_mp3_to_pcmu_ffmpeg(
        source,
        destination,
        ffmpeg_binary="test-ffmpeg",
        command_runner=fake_runner,
    )

    command = captured["command"]
    assert command[0] == "test-ffmpeg"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "8000"
    assert command[command.index("-acodec") + 1] == "pcm_mulaw"
    assert command[command.index("-f") + 1] == "mulaw"
    assert captured["kwargs"]["shell"] is False
    assert destination.read_bytes() == b"\xff" * 160


def test_ffmpeg_failure_is_generic_and_does_not_claim_an_asset(tmp_path):
    source = tmp_path / "greeting.mp3"
    destination = tmp_path / "greeting.pcmu"
    source.write_bytes(b"synthetic-mp3")

    def failed_runner(_command, **_kwargs):
        return SimpleNamespace(returncode=1, stderr=b"provider detail")

    with pytest.raises(AudioConversionError, match="rejected"):
        convert_mp3_to_pcmu_ffmpeg(
            source,
            destination,
            command_runner=failed_runner,
        )
    assert not destination.exists()


def test_cache_materialization_is_atomic_and_returns_verified_metadata(tmp_path):
    source = tmp_path / "greeting.mp3"
    destination = tmp_path / "private" / "greeting.pcmu"
    source.write_bytes(b"synthetic-mp3")
    raw_pcmu = (b"\xff\x7f" * 100)

    def converter(received_source: Path, temporary: Path):
        assert received_source == source
        assert temporary.parent == destination.parent
        temporary.write_bytes(raw_pcmu)

    asset = materialize_pcmu_cache(source, destination, converter=converter)

    assert destination.read_bytes() == raw_pcmu
    assert asset.path == destination
    assert asset.byte_length == len(raw_pcmu)
    assert asset.duration_ms == 25.0
    assert asset.sha256 == hashlib.sha256(raw_pcmu).hexdigest()
    assert describe_pcmu_cache(destination) == asset
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []


def test_failed_cache_refresh_keeps_previous_complete_asset(tmp_path):
    source = tmp_path / "greeting.mp3"
    destination = tmp_path / "private" / "greeting.pcmu"
    source.write_bytes(b"synthetic-mp3")
    destination.parent.mkdir()
    destination.write_bytes(b"previous-complete-asset")

    def failing_converter(_source: Path, temporary: Path):
        temporary.write_bytes(b"partial")
        raise AudioConversionError("synthetic failure")

    with pytest.raises(AudioConversionError, match="synthetic failure"):
        materialize_pcmu_cache(source, destination, converter=failing_converter)

    assert destination.read_bytes() == b"previous-complete-asset"
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []


def test_phone_cache_refuses_public_static_destination(tmp_path):
    source = tmp_path / "greeting.mp3"
    source.write_bytes(b"synthetic-mp3")
    public_destination = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "static"
        / "phone-audio-should-not-exist.pcmu"
    )

    with pytest.raises(TelephonyAudioError, match="data/static"):
        materialize_pcmu_cache(source, public_destination)
    assert not public_destination.exists()
