from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from ai_runtime.voice_resolution import CanonicalVoice
from tools import download_mp3


def _voice(provider: str, code: str) -> CanonicalVoice:
    return CanonicalVoice(
        id=1,
        voice_code=code,
        name=code,
        tts_service=1,
        service_name=f"TTS-{provider.upper()}",
        provider=provider,
        inherited_default=False,
    )


@pytest.mark.asyncio
async def test_export_resolves_prompt_and_owner_profile_voice(monkeypatch):
    bot = _voice("openai", "alloy")
    owner = _voice("elevenlabs", "owner-voice")
    prompt_resolver = AsyncMock(return_value=bot)
    catalog_resolver = AsyncMock(return_value=owner)
    default_resolver = AsyncMock(side_effect=AssertionError("profile voice must win"))
    monkeypatch.setattr(download_mp3, "resolve_prompt_voice", prompt_resolver)
    monkeypatch.setattr(download_mp3, "resolve_catalog_voice", catalog_resolver)
    monkeypatch.setattr(download_mp3, "resolve_default_voice", default_resolver)
    conn = object()

    result = await download_mp3._resolve_export_voices(
        {"role_id": 9, "user_voice_code": "owner-voice"}, conn
    )

    assert result == (bot, owner)
    prompt_resolver.assert_awaited_once_with(9, conn=conn)
    catalog_resolver.assert_awaited_once_with("owner-voice", conn=conn)


@pytest.mark.asyncio
async def test_export_owner_without_profile_voice_inherits_canonical_default(monkeypatch):
    bot = _voice("elevenlabs", "bot")
    inherited = _voice("openai", "nova")
    monkeypatch.setattr(download_mp3, "resolve_prompt_voice", AsyncMock(return_value=bot))
    monkeypatch.setattr(download_mp3, "resolve_default_voice", AsyncMock(return_value=inherited))
    catalog_resolver = AsyncMock(side_effect=AssertionError("no selected profile voice"))
    monkeypatch.setattr(download_mp3, "resolve_catalog_voice", catalog_resolver)

    result = await download_mp3._resolve_export_voices(
        {"role_id": 9, "user_voice_code": None}, object()
    )

    assert result == (bot, inherited)
    catalog_resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_provider_export_charges_each_provider_rate(monkeypatch):
    monkeypatch.setattr(
        download_mp3.Cost,
        "TTS_PROVIDER_SERVICES",
        {
            "elevenlabs": {"cost_per_character": 0.01, "service_id": 11},
            "openai": {"cost_per_character": 0.002, "service_id": 22},
        },
    )
    balance = AsyncMock(return_value=True)
    charge = AsyncMock(return_value=True)
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr(download_mp3, "has_sufficient_balance", balance)
    monkeypatch.setattr(download_mp3, "cost_tts", charge)
    monkeypatch.setattr(download_mp3, "refund_tts", refund)

    result = await download_mp3._charge_mp3_providers(
        7, {"openai": 100, "elevenlabs": 20}
    )

    assert result == {"openai": 100, "elevenlabs": 20}
    balance.assert_awaited_once_with(7, pytest.approx(0.4))
    assert charge.await_args_list[0].kwargs == {"provider": "openai"}
    assert charge.await_args_list[0].args == (7, 100)
    assert charge.await_args_list[1].kwargs == {"provider": "elevenlabs"}
    assert charge.await_args_list[1].args == (7, 20)
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_provider_charge_failure_refunds_prior_provider(monkeypatch):
    monkeypatch.setattr(
        download_mp3.Cost,
        "TTS_PROVIDER_SERVICES",
        {
            "openai": {"cost_per_character": 0.002, "service_id": 22},
            "elevenlabs": {"cost_per_character": 0.01, "service_id": 11},
        },
    )
    monkeypatch.setattr(download_mp3, "has_sufficient_balance", AsyncMock(return_value=True))
    monkeypatch.setattr(download_mp3, "cost_tts", AsyncMock(side_effect=[True, False]))
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr(download_mp3, "refund_tts", refund)

    result = await download_mp3._charge_mp3_providers(
        7, {"openai": 100, "elevenlabs": 20}
    )

    assert result is None
    refund.assert_awaited_once_with(7, 100, provider="openai")


@pytest.mark.asyncio
async def test_worker_lazy_initializes_missing_tts_service_ids(monkeypatch):
    monkeypatch.setattr(
        download_mp3.Cost,
        "TTS_PROVIDER_SERVICES",
        {
            "elevenlabs": {
                "cost_per_character": 0.01,
                "service_id": None,
            },
        },
    )

    async def initialize_costs():
        download_mp3.Cost.TTS_PROVIDER_SERVICES = {
            "elevenlabs": {
                "cost_per_character": 0.01,
                "service_id": 11,
            },
        }

    initialize = AsyncMock(side_effect=initialize_costs)
    monkeypatch.setattr(download_mp3.Cost, "initialize", initialize)
    monkeypatch.setattr(
        download_mp3,
        "has_sufficient_balance",
        AsyncMock(return_value=True),
    )
    charge = AsyncMock(return_value=True)
    monkeypatch.setattr(download_mp3, "cost_tts", charge)

    result = await download_mp3._charge_mp3_providers(
        7,
        {"elevenlabs": 25},
    )

    assert result == {"elevenlabs": 25}
    initialize.assert_awaited_once_with()
    charge.assert_awaited_once_with(7, 25, provider="elevenlabs")


@pytest.mark.asyncio
async def test_preparation_prefers_originals_and_bills_only_tts_fallback(
    monkeypatch,
):
    original = download_mp3.AudioSegment.silent(duration=1_000)
    decode = AsyncMock(side_effect=[original, None])
    monkeypatch.setattr(download_mp3, "_decode_original_audio", decode)
    bot = _voice("openai", "bot")
    user = _voice("elevenlabs", "user")
    messages = [
        {"id": 1, "message": "original words", "type": "user"},
        {"id": 2, "message": "fallback", "type": "user"},
        {"id": 3, "message": "bot", "type": "bot"},
    ]
    sources = {
        1: download_mp3._OriginalAudioSource(
            "attachment", 1, "audio-1", duration_seconds=1
        ),
        2: download_mp3._OriginalAudioSource(
            "phone", 2, duration_seconds=1
        ),
    }

    prepared, characters = await download_mp3._prepare_message_audio(
        messages,
        sources,
        requester_user_id=7,
        conversation_id=9,
        bot_voice=bot,
        user_voice=user,
    )

    assert prepared[0].original_segment is original
    assert [item.original_segment for item in prepared[1:]] == [None, None]
    assert [item.voice for item in prepared] == [None, user, bot]
    assert characters == {"elevenlabs": len("fallback"), "openai": len("bot")}
    assert decode.await_args_list[0].kwargs == {
        "requester_user_id": 7,
        "conversation_id": 9,
    }


@pytest.mark.asyncio
async def test_oversized_original_falls_back_before_decoding(monkeypatch):
    decode = AsyncMock(side_effect=AssertionError("oversized audio must not decode"))
    monkeypatch.setattr(download_mp3, "_decode_original_audio", decode)
    user = _voice("elevenlabs", "user")
    text = "safe transcript fallback"

    prepared, characters = await download_mp3._prepare_message_audio(
        [{"id": 1, "message": text, "type": "user"}],
        {
            1: download_mp3._OriginalAudioSource(
                "phone",
                1,
                duration_seconds=(
                    download_mp3._MAX_REUSED_ORIGINAL_MESSAGE_SECONDS + 1
                ),
            )
        },
        requester_user_id=7,
        conversation_id=9,
        bot_voice=_voice("openai", "bot"),
        user_voice=user,
    )

    decode.assert_not_awaited()
    assert prepared[0].voice is user
    assert characters == {"elevenlabs": len(text)}


@pytest.mark.asyncio
async def test_all_original_preparation_does_not_resolve_tts_voices(monkeypatch):
    original = download_mp3.AudioSegment.silent(duration=1_000)
    monkeypatch.setattr(
        download_mp3,
        "_decode_original_audio",
        AsyncMock(return_value=original),
    )
    resolver = AsyncMock(side_effect=AssertionError("TTS is not needed"))

    prepared, characters = await download_mp3._prepare_message_audio(
        [{"id": 1, "message": "retained", "type": "user"}],
        {
            1: download_mp3._OriginalAudioSource(
                "phone",
                1,
                duration_seconds=1,
            )
        },
        requester_user_id=7,
        conversation_id=9,
        bot_voice=None,
        user_voice=None,
        voice_resolver=resolver,
    )

    assert prepared[0].original_segment is original
    assert characters == {}
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_owner_admin_export_does_not_discover_private_audio(
    monkeypatch,
):
    discover = AsyncMock(side_effect=AssertionError("must remain owner-only"))
    monkeypatch.setattr(download_mp3, "_discover_original_audio_sources", discover)

    result = await download_mp3._discover_requester_original_audio_sources(
        object(),
        conversation_id=9,
        owner_user_id=8,
        requester_user_id=1,
    )

    assert result == {}
    discover.assert_not_awaited()


def test_attachment_audio_format_uses_mime_then_storage_suffix():
    assert download_mp3._attachment_audio_format(
        {"mime_detected": "audio/ogg; codecs=opus", "storage_key": "x.bin"}
    ) == "ogg"
    assert download_mp3._attachment_audio_format(
        {"mime_detected": "application/octet-stream", "storage_key": "x.m4a"}
    ) == "mp4"


def test_attachment_decode_is_duration_and_format_bounded(monkeypatch):
    run = Mock(
        return_value=Mock(
            returncode=0,
            stdout=b"\x00\x00" * download_mp3._ATTACHMENT_DECODE_SAMPLE_RATE,
        )
    )
    monkeypatch.setattr(download_mp3.subprocess, "run", run)

    result = download_mp3._decode_bounded_attachment_audio(b"opus", "ogg")

    assert len(result) == 1_000
    command = run.call_args.args[0]
    assert command[command.index("-t") + 1] == str(
        download_mp3._ATTACHMENT_DECODE_MAX_SECONDS
    )
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"
    assert command[-3:] == ["-f", "s16le", "pipe:1"]
    assert run.call_args.kwargs["input"] == b"opus"
    assert run.call_args.kwargs["timeout"] == (
        download_mp3._ATTACHMENT_DECODE_TIMEOUT_SECONDS
    )


def test_attachment_decode_rejects_output_beyond_hard_cap(monkeypatch):
    monkeypatch.setattr(download_mp3, "_MAX_ATTACHMENT_DECODED_BYTES", 3)
    monkeypatch.setattr(
        download_mp3.subprocess,
        "run",
        Mock(return_value=Mock(returncode=0, stdout=b"1234")),
    )

    with pytest.raises(RuntimeError, match="exceeded its limit"):
        download_mp3._decode_bounded_attachment_audio(b"audio", "ogg")


@pytest.mark.asyncio
async def test_retained_attachment_is_read_with_owner_scope(monkeypatch):
    import file_storage

    read = AsyncMock(
        return_value=(
            b"original-ogg",
            {"mime_detected": "audio/ogg", "storage_key": "audio.ogg"},
        )
    )
    monkeypatch.setattr(file_storage, "read_attachment_bytes", read)
    original = object()
    decode = Mock(return_value=original)
    monkeypatch.setattr(
        download_mp3,
        "_decode_bounded_attachment_audio",
        decode,
    )
    source = download_mp3._OriginalAudioSource(
        kind="attachment",
        message_id=12,
        attachment_ref="audio-ref",
    )

    result = await download_mp3._decode_original_audio(
        source,
        requester_user_id=7,
        conversation_id=9,
    )

    assert result is original
    decode.assert_called_once_with(b"original-ogg", "ogg")
    read.assert_awaited_once_with(
        "audio-ref",
        user_id=7,
        conversation_id=9,
        message_id=12,
        require_kind="audio",
    )


@pytest.mark.asyncio
async def test_retained_phone_range_is_resolved_and_closed(monkeypatch):
    from integrations.telephony import message_audio, purge, recording_storage

    get_audio = AsyncMock(
        return_value={
            "call_id": "call_12345678",
            "path": "private/participant.mulaw",
            "start_byte": 10,
            "end_byte": 20,
            "conversation_id": 9,
        }
    )

    class Repository:
        get_owned_message_audio = get_audio

    monkeypatch.setattr(purge, "PhoneDataPurgeRepository", Repository)
    resolved_path = Path("safe/participant.mulaw")
    resolve = Mock(return_value=resolved_path)
    monkeypatch.setattr(
        recording_storage,
        "resolve_private_recording_path",
        resolve,
    )

    class Chunks:
        closed = False

        def __iter__(self):
            return iter((b"wav-", b"bytes"))

        def close(self):
            self.closed = True

    chunks = Chunks()
    open_chunks = Mock(return_value=chunks)
    monkeypatch.setattr(
        message_audio,
        "open_pcmu_range_as_wav_chunks",
        open_chunks,
    )
    original = object()

    def decode(file_object, *, format):
        assert file_object.read() == b"wav-bytes"
        assert format == "wav"
        return original

    monkeypatch.setattr(download_mp3.AudioSegment, "from_file", decode)

    result = await download_mp3._decode_original_audio(
        download_mp3._OriginalAudioSource(kind="phone", message_id=12),
        requester_user_id=7,
        conversation_id=9,
    )

    assert result is original
    get_audio.assert_awaited_once_with(owner_user_id=7, message_id=12)
    resolve.assert_called_once_with("call_12345678", "private/participant.mulaw")
    audio_range = open_chunks.call_args.args[1]
    assert (audio_range.start_byte, audio_range.end_byte) == (10, 20)
    assert chunks.closed


@pytest.mark.asyncio
async def test_export_inserts_whole_calls_once_between_text_and_voice_notes(monkeypatch):
    call_a = download_mp3.AudioSegment.silent(duration=2000)
    call_b = download_mp3.AudioSegment.silent(duration=3000)
    note = download_mp3.AudioSegment.silent(duration=500)
    decoded = {"call-a": call_a, "call-b": call_b, "note": note}
    decode = AsyncMock(side_effect=lambda source, **kwargs: decoded[source.call_id or source.attachment_ref])
    monkeypatch.setattr(download_mp3, "_decode_original_audio", decode)
    messages = [{"id": n, "message": "written" if n in (1, 7) else "recorded", "type": "user"} for n in range(1, 8)]
    # The individual caller/assistant clips overlap. None should be decoded
    # after the complete mixed recording has been inserted at their first id.
    sources = {n: download_mp3._OriginalAudioSource("phone", n, duration_seconds=1.5) for n in (2, 3, 5, 6)}
    sources[4] = download_mp3._OriginalAudioSource("attachment", 4, "note", 0.5)
    recordings = {
        2: download_mp3._OriginalAudioSource("phone_call", 2, duration_seconds=2, call_id="call-a", covered_message_ids=(2, 3)),
        5: download_mp3._OriginalAudioSource("phone_call", 5, duration_seconds=3, call_id="call-b", covered_message_ids=(5, 6)),
    }
    prepared, characters = await download_mp3._prepare_message_audio(
        messages, sources, requester_user_id=7, conversation_id=9,
        bot_voice=_voice("openai", "bot"), user_voice=_voice("elevenlabs", "user"),
        phone_recordings=recordings,
    )
    assert [item.original_segment for item in prepared] == [None, call_a, note, call_b, None]
    assert characters == {"elevenlabs": 2 * len("written")}
    assert decode.await_count == 3
    assert sum(len(item.original_segment) for item in prepared if item.original_segment is not None) == 5500


@pytest.mark.asyncio
@pytest.mark.parametrize("mix_available", [False, True])
async def test_whole_call_skip_is_committed_only_after_successful_decode(monkeypatch, mix_available):
    recording = download_mp3.AudioSegment.silent(duration=2000) if mix_available else None
    individual = download_mp3.AudioSegment.silent(duration=1000)
    decode = AsyncMock(side_effect=lambda source, **kwargs: recording if source.kind == "phone_call" else individual)
    monkeypatch.setattr(download_mp3, "_decode_original_audio", decode)
    resolver = AsyncMock(side_effect=AssertionError("All messages have original audio"))
    prepared, characters = await download_mp3._prepare_message_audio(
        [{"id": n, "message": "recorded", "type": "user"} for n in (1, 2)],
        {n: download_mp3._OriginalAudioSource("phone", n, duration_seconds=1) for n in (1, 2)},
        requester_user_id=7, conversation_id=9, bot_voice=None, user_voice=None, voice_resolver=resolver,
        phone_recordings={1: download_mp3._OriginalAudioSource("phone_call", 1, duration_seconds=2,
                                                            call_id="call-a", covered_message_ids=(1, 2))},
    )
    assert len(prepared) == (1 if mix_available else 2)
    assert decode.await_count == (1 if mix_available else 3)
    assert characters == {}
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_discovery_preserves_conversation_scope_and_intervening_messages():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript("""
            CREATE TABLE MESSAGES(id INTEGER PRIMARY KEY,conversation_id INTEGER,user_id INTEGER);
            CREATE TABLE PHONE_CALLS(id TEXT PRIMARY KEY,conversation_id INTEGER,owner_user_id INTEGER,
                active_conversation_id INTEGER,ended_at TEXT,deleted_at TEXT);
            CREATE TABLE PHONE_RECORDINGS(id INTEGER PRIMARY KEY,call_id TEXT,status TEXT,
                local_deleted_at TEXT,mixed_path TEXT,duration_seconds REAL);
            CREATE TABLE PHONE_CALL_MESSAGE_LINKS(call_id TEXT,message_id INTEGER,origin_channel TEXT);
        """)
        await conn.executemany("INSERT INTO MESSAGES VALUES(?,?,7)",
                              [(mid, 10 if mid == 12 else 9) for mid in range(1, 15)])
        for index, (cid, ids, active) in enumerate([
            ("complete-a", (2, 3), None), ("complete-b", (5, 6), None),
            ("interleaved", (8, 10), None), ("shared", (11, 12), None),
            ("transferred", (13,), 9), ("no-mix", (14,), None),
        ], 1):
            await conn.execute("INSERT INTO PHONE_CALLS VALUES(?,9,7,?,'ended',NULL)", (cid, active))
            await conn.execute("INSERT INTO PHONE_RECORDINGS VALUES(?,?,'available',NULL,?,2)",
                               (index, cid, None if cid == "no-mix" else "private/mixed.mp3"))
            await conn.executemany("INSERT INTO PHONE_CALL_MESSAGE_LINKS VALUES(?,?,'phone')", [(cid, mid) for mid in ids])
        messages = [{"id": mid} for mid in range(1, 15) if mid != 12]
        blocks = await download_mp3._discover_phone_recording_blocks(
            conn, conversation_id=9, requester_user_id=7, messages=messages,
        )
        assert set(blocks) == {2, 5}
        assert blocks[2].covered_message_ids == (2, 3)
        assert blocks[5].covered_message_ids == (5, 6)
        assert await download_mp3._discover_phone_recording_blocks(
            conn, conversation_id=9, requester_user_id=8, messages=messages,
        ) == {}
        # A snapshot omitting one call message must not import the whole call.
        assert await download_mp3._discover_phone_recording_blocks(
            conn, conversation_id=9, requester_user_id=7, messages=[{"id": 2}],
        ) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_id", [9, 10])
async def test_whole_call_decode_uses_owned_mix_and_checks_conversation(monkeypatch, tmp_path, conversation_id):
    from integrations.telephony import purge, recording_storage

    path = tmp_path / "mixed.mp3"
    path.write_bytes(b"retained-duplex-mix")
    get_recording = AsyncMock(return_value={"call_id": "call-a", "path": str(path), "conversation_id": conversation_id})
    monkeypatch.setattr(purge, "PhoneDataPurgeRepository", lambda: Mock(get_owned_recording=get_recording))
    monkeypatch.setattr(recording_storage, "resolve_private_recording_path", Mock(return_value=path))
    original = object()
    decode = Mock(return_value=original)
    monkeypatch.setattr(download_mp3, "_decode_bounded_attachment_audio", decode)
    result = await download_mp3._decode_original_audio(
        download_mp3._OriginalAudioSource("phone_call", 1, call_id="call-a"),
        requester_user_id=7, conversation_id=9,
    )
    get_recording.assert_awaited_once_with(owner_user_id=7, call_id="call-a", track="mixed")
    if conversation_id == 9:
        assert result is original
        decode.assert_called_once_with(b"retained-duplex-mix", "mp3")
    else:
        assert result is None
        decode.assert_not_called()
