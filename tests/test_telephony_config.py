import aiosqlite
import pytest

from integrations.telephony.config import (
    TelephonyConfigError,
    load_telephony_config,
    parse_telephony_config,
    serialize_config_updates,
)


def test_defaults_lock_the_validated_product_path():
    config = parse_telephony_config(None)

    assert config.enabled is False
    assert config.transport == "media_streams"
    assert config.stt_provider == "elevenlabs"
    assert config.stt_model == "scribe_v2_realtime"
    assert config.stt_language == "multi"
    assert config.endpointing_ms == 700
    assert config.barge_in_confirmation_ms == 350
    assert config.max_call_seconds == 4 * 60 * 60
    assert config.allowed_countries == ("US", "ES")
    assert config.recording_default is False
    assert config.amd_default is False
    assert config.reconnect_attempts == 2
    assert config.silence_check_seconds == 60
    assert config.silence_hangup_seconds == 60
    assert config.scheduler_jitter_seconds == 10
    assert config.max_concurrent_dispatches == 10


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("telephony_transport", "conversation_relay"),
        ("telephony_stt_provider", "deepgram"),
        ("telephony_stt_provider", "other"),
        ("telephony_stt_model", "nova-3"),
        ("telephony_stt_model", "flux-general-multi"),
        ("telephony_max_call_seconds", "59"),
        ("telephony_reconnect_attempts", "3"),
        ("telephony_allowed_countries", '["USA"]'),
        ("telephony_endpointing_ms", "299"),
        ("telephony_endpointing_ms", "3001"),
        ("telephony_barge_in_confirmation_ms", "2001"),
    ],
)
def test_invalid_or_duplicate_product_paths_fail_closed(key, value):
    with pytest.raises(TelephonyConfigError):
        parse_telephony_config({key: value})


def test_config_serialization_is_canonical_and_round_trips():
    stored = serialize_config_updates(
        {
            "telephony_enabled": True,
            "telephony_allowed_countries": ["es", "US", "es"],
            "telephony_max_call_seconds": 3600,
            "telephony_recording_default": False,
        }
    )

    assert stored["telephony_enabled"] == "1"
    assert stored["telephony_allowed_countries"] == '["ES","US"]'
    assert "telephony_stt_model" not in stored
    assert "telephony_silence_check_seconds" not in stored
    assert parse_telephony_config(stored).max_call_seconds == 3600


def test_dispatch_concurrency_is_bounded_and_serialized():
    config = parse_telephony_config({"telephony_max_concurrent_dispatches": "37"})
    assert config.max_concurrent_dispatches == 37
    assert serialize_config_updates(
        {"telephony_max_concurrent_dispatches": 37}
    ) == {"telephony_max_concurrent_dispatches": "37"}
    for value in (0, 101):
        with pytest.raises(TelephonyConfigError):
            parse_telephony_config(
                {"telephony_max_concurrent_dispatches": str(value)}
            )


def test_turn_taking_timings_are_bounded_and_serialized():
    stored = serialize_config_updates(
        {
            "telephony_endpointing_ms": 1_400,
            "telephony_barge_in_confirmation_ms": 500,
        }
    )
    assert stored == {
        "telephony_endpointing_ms": "1400",
        "telephony_barge_in_confirmation_ms": "500",
    }
    config = parse_telephony_config(stored)
    assert config.endpointing_ms == 1_400
    assert config.barge_in_confirmation_ms == 500


@pytest.mark.parametrize("endpointing_ms", [300, 3_000])
def test_scribe_vad_boundaries_round_trip_without_clamping(endpointing_ms):
    stored = serialize_config_updates(
        {"telephony_endpointing_ms": endpointing_ms}
    )

    assert stored == {"telephony_endpointing_ms": str(endpointing_ms)}
    assert parse_telephony_config(stored).endpointing_ms == endpointing_ms


@pytest.mark.asyncio
async def test_load_uses_only_telephony_system_config_values():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute(
            "CREATE TABLE SYSTEM_CONFIG (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await conn.executemany(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            [
                ("telephony_enabled", "1"),
                ("telephony_max_call_seconds", "1800"),
                ("telephony_global_audio_revision", "27"),
                ("telegram_enabled", "1"),
            ],
        )
        config = await load_telephony_config(conn=conn)
    finally:
        await conn.close()

    assert config.enabled is True
    assert config.max_call_seconds == 1800
    assert config.transport == "media_streams"


@pytest.mark.asyncio
async def test_load_still_rejects_unregistered_telephony_keys():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute(
            "CREATE TABLE SYSTEM_CONFIG (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO SYSTEM_CONFIG (key, value) VALUES (?, ?)",
            ("telephony_unregistered_state", "1"),
        )
        with pytest.raises(TelephonyConfigError, match="Unsupported"):
            await load_telephony_config(conn=conn)
    finally:
        await conn.close()
