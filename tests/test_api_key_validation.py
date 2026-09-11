from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import pytest
from starlette.requests import Request

import app as app_module


class _JsonRequest(Request):
    def __init__(self):
        super().__init__({"type": "http", "headers": []})

    async def json(self) -> dict[str, str]:
        return {
            "provider": "anthropic",
            "key": "test-anthropic-key",
        }


@pytest.mark.asyncio
async def test_anthropic_api_key_validation_uses_models_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class _Models:
        def list(self, *, limit: int) -> list[object]:
            calls.append(("models.list", limit))
            return []

    class _Anthropic:
        def __init__(self, *, api_key: str) -> None:
            calls.append(("api_key", api_key))
            self.models = _Models()

    monkeypatch.setattr(anthropic, "Anthropic", _Anthropic)

    response = await app_module.test_api_key(
        _JsonRequest(),
        current_user=SimpleNamespace(id=7),
    )

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "success": True,
        "message": "Anthropic API key is valid",
    }
    assert calls == [
        ("api_key", "test-anthropic-key"),
        ("models.list", 1),
    ]
