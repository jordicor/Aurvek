import sqlite3

import aiosqlite
import pytest

from integrations.telephony.config import TelephonyConfig
from integrations.telephony.settings import (
    DEFAULT_WARNING_MILESTONES_SECONDS,
    TelephonySettingsError,
    resolve_effective_phone_settings,
    resolve_phone_settings,
)


def test_absent_prompt_settings_inherit_global_defaults():
    global_config = TelephonyConfig(
        stt_language="multi",
        max_call_seconds=14_400,
        recording_default=True,
        amd_default=True,
        silence_check_seconds=60,
        silence_hangup_seconds=60,
    )

    effective = resolve_phone_settings(global_config, None)

    assert effective.stt_locale == "multi"
    assert effective.endpointing_ms == 700
    assert effective.barge_in_confirmation_ms == 350
    assert effective.interruptible is True
    assert effective.ignore_backchannels is True
    assert effective.max_duration_seconds == 14_400
    assert effective.warning_milestones_seconds == DEFAULT_WARNING_MILESTONES_SECONDS
    assert effective.silence_prompt_seconds == 60
    assert effective.silence_hangup_seconds == 60
    assert effective.recording_default is True
    assert effective.amd_default is True


def test_prompt_duration_silence_and_milestones_resolve_below_global():
    global_config = TelephonyConfig(
        max_call_seconds=600,
        silence_check_seconds=45,
        silence_hangup_seconds=50,
    )
    prompt = {
        "stt_locale": "es",
        "endpointing_ms": 1_400,
        "interruptible": 1,
        "interrupt_sensitivity": "low",
        "ignore_backchannels": 1,
        "max_duration_seconds": 540,
        "warning_milestones_json": "[900,300,180,60,60]",
        "silence_prompt_seconds": 30,
        "silence_hangup_seconds": 40,
        "ai_initiation_mode": "proactive",
        "inbound_greeting_mode": "random",
        "outbound_greeting_mode": "fixed",
        "recording_default": 1,
        "amd_default": 0,
    }

    effective = resolve_phone_settings(global_config, prompt)

    assert effective.max_duration_seconds == 540
    assert effective.warning_milestones_seconds == (300, 180, 60)
    assert effective.silence_prompt_seconds == 30
    assert effective.silence_hangup_seconds == 40
    assert effective.ai_initiation_mode == "proactive"
    assert effective.stt_locale == "es"
    assert effective.endpointing_ms == 1_400
    assert effective.interrupt_sensitivity == "low"
    assert effective.barge_in_confirmation_ms == 612


def test_prompt_turn_taking_settings_validate_and_support_slow_speech():
    effective = resolve_phone_settings(
        TelephonyConfig(endpointing_ms=700, barge_in_confirmation_ms=400),
        {
            "endpointing_ms": 2_000,
            "interruptible": 0,
            "interrupt_sensitivity": "high",
            "ignore_backchannels": 0,
            "warning_milestones_json": "[]",
            "silence_prompt_seconds": None,
            "silence_hangup_seconds": None,
        },
    )
    assert effective.endpointing_ms == 2_000
    assert effective.interruptible is False
    assert effective.barge_in_confirmation_ms == 200
    assert effective.ignore_backchannels is False

    for invalid_endpointing_ms in (299, 3_001):
        with pytest.raises(TelephonySettingsError, match="endpointing_ms"):
            resolve_phone_settings(
                TelephonyConfig(),
                {"endpointing_ms": invalid_endpointing_ms},
            )


def test_existing_prompt_row_can_disable_warnings_and_silence():
    effective = resolve_phone_settings(
        TelephonyConfig(),
        {
            "stt_locale": "auto",
            "max_duration_seconds": None,
            "warning_milestones_json": "[]",
            "silence_prompt_seconds": None,
            "silence_hangup_seconds": None,
            "ai_initiation_mode": "on_request",
            "inbound_greeting_mode": "inherit",
            "outbound_greeting_mode": "inherit",
            "recording_default": 0,
            "amd_default": 0,
        },
    )

    assert effective.warning_milestones_seconds == ()
    assert effective.silence_enabled is False


def test_partial_silence_override_fails_visibly():
    with pytest.raises(TelephonySettingsError, match="must both be set"):
        resolve_phone_settings(
            TelephonyConfig(),
            {
                "warning_milestones_json": "[60]",
                "silence_prompt_seconds": 30,
                "silence_hangup_seconds": None,
            },
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_duration_seconds", 601, "max_duration_seconds cannot exceed"),
        ("silence_prompt_seconds", 46, "silence_prompt_seconds cannot exceed"),
        ("silence_hangup_seconds", 51, "silence_hangup_seconds cannot exceed"),
    ],
)
def test_prompt_limits_above_admin_fail_visibly(field, value, message):
    prompt = {
        "max_duration_seconds": 500,
        "warning_milestones_json": "[300,180,60]",
        "silence_prompt_seconds": 30,
        "silence_hangup_seconds": 40,
    }
    prompt[field] = value

    with pytest.raises(TelephonySettingsError, match=message):
        resolve_phone_settings(
            TelephonyConfig(
                max_call_seconds=600,
                silence_check_seconds=45,
                silence_hangup_seconds=50,
            ),
            prompt,
        )


def test_prompt_cannot_enable_silence_when_admin_maximum_is_disabled():
    with pytest.raises(
        TelephonySettingsError,
        match="silence_prompt_seconds cannot exceed",
    ):
        resolve_phone_settings(
            TelephonyConfig(
                silence_check_seconds=0,
                silence_hangup_seconds=0,
            ),
            {
                "warning_milestones_json": "[60]",
                "silence_prompt_seconds": 30,
                "silence_hangup_seconds": 30,
            },
        )


def test_mapping_booleans_and_enums_are_validated_robustly():
    prompt = {
        "warning_milestones_json": "[60]",
        "silence_prompt_seconds": None,
        "silence_hangup_seconds": None,
        "recording_default": "0",
        "amd_default": "false",
        "ai_initiation_mode": "PROACTIVE",
        "inbound_greeting_mode": "RANDOM",
        "outbound_greeting_mode": "fixed",
    }
    effective = resolve_phone_settings(TelephonyConfig(), prompt)
    assert effective.recording_default is False
    assert effective.amd_default is False
    assert effective.ai_initiation_mode == "proactive"
    assert effective.inbound_greeting_mode == "random"

    prompt["ai_initiation_mode"] = "surprise"
    with pytest.raises(TelephonySettingsError, match="ai_initiation_mode"):
        resolve_phone_settings(TelephonyConfig(), prompt)

    prompt["ai_initiation_mode"] = "on_request"
    prompt["recording_default"] = "sometimes"
    with pytest.raises(TelephonySettingsError, match="recording_default"):
        resolve_phone_settings(TelephonyConfig(), prompt)


@pytest.mark.asyncio
async def test_effective_settings_load_real_prompt_phone_row(tmp_path):
    path = tmp_path / "phone-settings.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
                CREATE TABLE PROMPT_PHONE_SETTINGS (
                    prompt_id INTEGER PRIMARY KEY,
                    stt_locale TEXT,
                    endpointing_ms INTEGER,
                    interruptible INTEGER,
                    interrupt_sensitivity TEXT,
                    ignore_backchannels INTEGER,
                    max_duration_seconds INTEGER,
                warning_milestones_json TEXT,
                silence_prompt_seconds INTEGER,
                silence_hangup_seconds INTEGER,
                ai_initiation_mode TEXT,
                inbound_greeting_mode TEXT,
                outbound_greeting_mode TEXT,
                recording_default INTEGER,
                amd_default INTEGER
            );
                INSERT INTO PROMPT_PHONE_SETTINGS VALUES
                    (7, 'auto', 1200, 1, 'low', 1, 300, '[180,60]', 30, 40,
                     'on_request', 'inherit', 'inherit', 0, 1);
            """
        )
        conn.commit()

    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        effective = await resolve_effective_phone_settings(
            7,
            global_config=TelephonyConfig(max_call_seconds=600),
            conn=conn,
        )
    finally:
        await conn.close()

    assert effective.max_duration_seconds == 300
    assert effective.endpointing_ms == 1200
    assert effective.interrupt_sensitivity == "low"
    assert effective.barge_in_confirmation_ms == 612
    assert effective.warning_milestones_seconds == (180, 60)
    assert effective.amd_default is True
