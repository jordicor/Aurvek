"""Regression coverage for global maintenance authorization and responses."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app as app_module
import request_security
from maintenance_tasks import (
    MaintenanceTaskBusy,
    MaintenanceTaskTimedOut,
    validate_audio_cache_age,
)
from request_security import CSRF_HEADER


_VALID_CSRF_TOKEN = "test-admin-maintenance-csrf-token-1234567890"


class DummyUser:
    def __init__(self, user_id: int, *, token_claims_admin: bool):
        self.id = user_id
        self.username = f"user-{user_id}"
        self._token_claims_admin = token_claims_admin

    @property
    async def is_admin(self):
        return self._token_claims_admin


def _request(
    *,
    forwarded_for: str | None = None,
    csrf_token: str | None = _VALID_CSRF_TOKEN,
    origin: str = "https://example.test",
) -> Request:
    headers = [
        (b"host", b"example.test"),
        (b"origin", origin.encode("ascii")),
    ]
    if csrf_token is not None:
        headers.append((CSRF_HEADER.lower().encode("ascii"), csrf_token.encode("ascii")))
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/disable-cloudflare-cache",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
            "scheme": "https",
            "session": {"gptsub_csrf_token": _VALID_CSRF_TOKEN},
        }
    )


def _endpoint_cases():
    return [
        (
            app_module.disable_cloudflare_cache,
            "run_cloudflare_cache_disable",
            {"request": _request()},
        ),
        (
            app_module.clear_audio_cache,
            "run_audio_cache_cleanup",
            {"request": _request(), "time_arg": {"time_arg": "24h"}},
        ),
    ]


@pytest.fixture()
def live_admin_mocks(monkeypatch):
    live_role = AsyncMock(side_effect=lambda user_id: user_id == 1)
    elevation = AsyncMock(return_value=True)
    monkeypatch.setattr(app_module, "is_admin", live_role)
    monkeypatch.setattr(app_module, "is_elevated", elevation)
    monkeypatch.setattr(app_module, "log_admin_action", AsyncMock())
    monkeypatch.setattr(request_security, "PRIMARY_APP_DOMAIN", "")
    return live_role, elevation


@pytest.mark.asyncio
@pytest.mark.parametrize(("endpoint", "runner_name", "arguments"), _endpoint_cases())
async def test_token_admin_claim_does_not_bypass_live_role(
    monkeypatch, live_admin_mocks, endpoint, runner_name, arguments
):
    runner = AsyncMock()
    monkeypatch.setattr(app_module, runner_name, runner)

    response = await endpoint(
        current_user=DummyUser(10, token_claims_admin=True),
        **arguments,
    )

    assert response.status_code == 403
    runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("endpoint", "runner_name", "arguments"), _endpoint_cases())
async def test_live_admin_is_allowed_even_with_stale_token_claim(
    monkeypatch, live_admin_mocks, endpoint, runner_name, arguments
):
    runner = AsyncMock()
    monkeypatch.setattr(app_module, runner_name, runner)

    response = await endpoint(
        current_user=DummyUser(1, token_claims_admin=False),
        **arguments,
    )

    assert "message" in response
    assert response["success"] is True
    runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_cloudflare_maintenance_does_not_require_current_elevation(
    monkeypatch, live_admin_mocks
):
    _, elevation = live_admin_mocks
    elevation.return_value = False
    runner = AsyncMock()
    monkeypatch.setattr(app_module, "run_cloudflare_cache_disable", runner)
    request = _request(forwarded_for="198.51.100.9, 10.0.0.2")

    response = await app_module.disable_cloudflare_cache(
        request=request,
        current_user=DummyUser(1, token_claims_admin=True),
    )

    assert response["success"] is True
    elevation.assert_not_awaited()
    runner.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_case",
    [
        _request(csrf_token=None),
        _request(csrf_token="incorrect-token"),
        _request(origin="https://attacker.example"),
    ],
)
async def test_cloudflare_maintenance_rejects_invalid_csrf(
    monkeypatch, live_admin_mocks, request_case
):
    runner = AsyncMock()
    monkeypatch.setattr(app_module, "run_cloudflare_cache_disable", runner)

    response = await app_module.disable_cloudflare_cache(
        request=request_case,
        current_user=DummyUser(1, token_claims_admin=True),
    )

    assert response.status_code == 403
    runner.assert_not_awaited()


def test_dashboard_cloudflare_request_includes_csrf_token():
    template = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'meta name="aurvek-admin-csrf-token"' in template
    assert "headers: {'X-GPTSub-CSRF': csrfToken}" in template


@pytest.mark.asyncio
@pytest.mark.parametrize("age", ["0h", "24h"])
async def test_audio_cache_accepts_valid_age(
    monkeypatch, live_admin_mocks, age
):
    async def _validate_without_running(value):
        validate_audio_cache_age(value)

    runner = AsyncMock(side_effect=_validate_without_running)
    monkeypatch.setattr(app_module, "run_audio_cache_cleanup", runner)

    response = await app_module.clear_audio_cache(
        request=_request(),
        time_arg={"time_arg": age},
        current_user=DummyUser(1, token_claims_admin=False),
    )

    assert "message" in response
    assert response["success"] is True
    runner.assert_awaited_once_with(age)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"time_arg": "-1h"},
        {"time_arg": "24"},
        {"time_arg": "1hour"},
        {"time_arg": " 24h"},
        {"time_arg": ""},
        {"time_arg": None},
        {},
        None,
    ],
)
async def test_audio_cache_rejects_negative_or_malformed_age(
    monkeypatch, live_admin_mocks, payload
):
    async def _validate_without_running(value):
        validate_audio_cache_age(value)

    runner = AsyncMock(side_effect=_validate_without_running)
    monkeypatch.setattr(app_module, "run_audio_cache_cleanup", runner)

    with pytest.raises(HTTPException) as exc_info:
        await app_module.clear_audio_cache(
            request=_request(),
            time_arg=payload,
            current_user=DummyUser(1, token_claims_admin=True),
        )

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(("endpoint", "runner_name", "arguments"), _endpoint_cases())
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (MaintenanceTaskBusy("already running"), 409),
        (MaintenanceTaskTimedOut("timed out"), 504),
        (subprocess.CalledProcessError(1, ["maintenance-script"]), 500),
    ],
)
async def test_maintenance_failures_have_stable_http_status(
    monkeypatch,
    live_admin_mocks,
    endpoint,
    runner_name,
    arguments,
    error,
    expected_status,
):
    monkeypatch.setattr(app_module, runner_name, AsyncMock(side_effect=error))

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(
            current_user=DummyUser(1, token_claims_admin=True),
            **arguments,
        )

    assert exc_info.value.status_code == expected_status
