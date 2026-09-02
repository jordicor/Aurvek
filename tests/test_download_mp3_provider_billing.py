from types import SimpleNamespace
from unittest.mock import AsyncMock

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
