from __future__ import annotations

import pytest

import integrations.media as media


def _stub_audio_decode(monkeypatch) -> None:
    monkeypatch.setattr(media, "_probe_audio_duration_seconds", lambda _audio: 1.0)


@pytest.mark.asyncio
async def test_external_audio_defaults_to_elevenlabs_when_engine_is_unset(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)
    reservations = []

    async def reserve_stt_attempt(**values):
        reservations.append(values)
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    async def transcribe_with_elevenlabs(**_kwargs):
        return "scribe transcript"

    async def must_not_use_deepgram(**_kwargs):
        raise AssertionError("Deepgram must require an explicit selection")

    monkeypatch.setattr(media, "stt_engine", None)
    monkeypatch.setattr(media, "stt_fallback_enabled", True)
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)
    monkeypatch.setattr(
        media,
        "transcribe_with_elevenlabs",
        transcribe_with_elevenlabs,
    )
    monkeypatch.setattr(
        media,
        "transcribe_with_deepgram",
        must_not_use_deepgram,
    )

    result = await media.transcribe_external_audio(
        user_id=31,
        audio_content=b"synthetic audio",
    )

    assert result == "scribe transcript"
    assert [item["engine"] for item in reservations] == ["elevenlabs"]


@pytest.mark.asyncio
async def test_detailed_external_audio_reports_actual_provider_model_and_duration(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)

    async def reserve_stt_attempt(**_values):
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    monkeypatch.setattr(media, "stt_engine", "elevenlabs")
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)
    async def deepgram(**_kwargs):
        return "new transcript"

    monkeypatch.setattr(media, "transcribe_with_deepgram", deepgram)

    result = await media.transcribe_external_audio_detailed(
        user_id=31,
        audio_content=b"synthetic audio",
        preferred_engine="deepgram",
    )

    assert result.text == "new transcript"
    assert result.provider == "deepgram"
    assert result.model == "nova-2"
    assert result.duration_seconds == 1.0


@pytest.mark.asyncio
async def test_known_retained_duration_avoids_redecoding_long_audio(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_probe_audio_duration_seconds",
        lambda _audio: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    async def reserve_stt_attempt(**values):
        assert values["duration_min"] == pytest.approx(90.0)
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    async def deepgram(**_kwargs):
        return "long transcript"

    monkeypatch.setattr(media, "stt_engine", "deepgram")
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)
    monkeypatch.setattr(media, "transcribe_with_deepgram", deepgram)

    result = await media.transcribe_external_audio_detailed(
        user_id=31,
        audio_content=b"retained compressed audio",
        duration_seconds=5_400,
    )

    assert result.duration_seconds == 5_400


@pytest.mark.asyncio
async def test_external_elevenlabs_failure_never_falls_back_to_deepgram(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)
    reservations = []
    failed = []
    deepgram_called = False

    async def reserve_stt_attempt(**values):
        reservations.append(values)
        return "reservation"

    async def finalize_failed_stt_attempt(*args, **kwargs):
        failed.append((args, kwargs))

    async def fail_elevenlabs(**_kwargs):
        raise RuntimeError("synthetic Scribe failure")

    async def track_deepgram(**_kwargs):
        nonlocal deepgram_called
        deepgram_called = True
        return "must not be returned"

    monkeypatch.setattr(media, "stt_engine", "elevenlabs")
    monkeypatch.setattr(media, "stt_fallback_enabled", True)
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(
        media,
        "finalize_failed_stt_attempt",
        finalize_failed_stt_attempt,
    )
    monkeypatch.setattr(media, "transcribe_with_elevenlabs", fail_elevenlabs)
    monkeypatch.setattr(media, "transcribe_with_deepgram", track_deepgram)

    with pytest.raises(RuntimeError, match="synthetic Scribe failure"):
        await media.transcribe_external_audio(
            user_id=31,
            audio_content=b"synthetic audio",
        )

    assert deepgram_called is False
    assert [item["engine"] for item in reservations] == ["elevenlabs"]
    assert len(failed) == 1
