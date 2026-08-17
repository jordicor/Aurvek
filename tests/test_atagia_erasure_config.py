"""Erasure bridge config: custody-gated, independent of the runtime provider.

Account erasure must keep working while the active memory provider is switched
away from Atagia, because the Atagia store still holds user data in custody.
"""

import pytest

import atagia_config
import memory.config as memory_config


_BASE_CONFIG = {
    "atagia_enabled": "true",
    "atagia_transport": "local",
    "atagia_db_path": "db/atagia.db",
}


@pytest.mark.asyncio
async def test_erasure_config_enabled_while_active_provider_is_none(monkeypatch):
    async def fake_get_atagia_config():
        return dict(_BASE_CONFIG)

    async def fake_active_provider():
        return "none"

    monkeypatch.setattr(atagia_config, "get_atagia_config", fake_get_atagia_config)
    monkeypatch.setattr(memory_config, "get_active_memory_provider", fake_active_provider)

    runtime = await atagia_config.get_atagia_bridge_config()
    erasure = await atagia_config.get_atagia_erasure_bridge_config()

    # Runtime ops are gated on the active provider; erasure is not.
    assert runtime.enabled is False
    assert erasure.enabled is True
    assert erasure.transport == "local"
    assert erasure.db_path == "db/atagia.db"


@pytest.mark.asyncio
async def test_erasure_config_off_when_atagia_disabled(monkeypatch):
    async def fake_get_atagia_config():
        cfg = dict(_BASE_CONFIG)
        cfg["atagia_enabled"] = "false"
        return cfg

    monkeypatch.setattr(atagia_config, "get_atagia_config", fake_get_atagia_config)

    erasure = await atagia_config.get_atagia_erasure_bridge_config()
    assert erasure.enabled is False
