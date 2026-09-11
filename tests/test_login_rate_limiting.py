from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import auth_flows
import rate_limiter as login_limits
from models import User


class DummyRequest:
    def __init__(self, ip: str):
        self.headers = {}
        self.cookies = {}
        self.state = SimpleNamespace()
        self.client = SimpleNamespace(host=ip)


class TemplateResult:
    def __init__(self, name, context):
        self.name = name
        self.context = context


def _post_login_request(
    *,
    username: str = "victim",
    password: str = "secret",
    captcha_token: str = "captcha",
) -> Request:
    body = (
        f"username={username}&password={password}&captcha_token={captcha_token}"
    ).encode("utf-8")

    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/x-www-form-urlencoded"),
                (b"cf-connecting-ip", b"203.0.113.10"),
            ],
            "session": {},
        },
        receive,
    )


@pytest.fixture
def isolated_limiter(monkeypatch):
    instance = login_limits.RateLimiter()
    monkeypatch.setattr(login_limits, "rate_limiter", instance)
    return instance


def _configure_login_limits(
    monkeypatch,
    *,
    pair=(3, 15),
    ip_failures=(100, 60),
):
    monkeypatch.setattr(
        login_limits.RateLimitConfig,
        "LOGIN_BY_ACCOUNT_IP_FAILURES",
        pair,
        raising=False,
    )
    monkeypatch.setattr(
        login_limits.RateLimitConfig,
        "LOGIN_BY_IP_FAILURES",
        ip_failures,
    )


def test_failures_only_block_the_same_account_ip_pair(
    monkeypatch,
    isolated_limiter,
):
    _configure_login_limits(monkeypatch, pair=(3, 15))
    ip_a = DummyRequest("203.0.113.10")
    ip_b = DummyRequest("203.0.113.11")

    for expected_count in range(1, 4):
        assert login_limits.record_login_failure(ip_a, "victim") == expected_count

    assert login_limits.check_login_failure_limits(ip_a, "victim") is not None
    assert login_limits.check_login_failure_limits(ip_b, "victim") is None


def test_success_clears_pair_and_account_but_not_ip_failure_bucket(
    monkeypatch,
    isolated_limiter,
):
    _configure_login_limits(monkeypatch)
    request = DummyRequest("203.0.113.10")

    login_limits.record_login_failure(request, "victim")
    login_limits.record_login_failure(request, "victim")

    assert len(isolated_limiter._attempts["ip_fail:login:203.0.113.10"]) == 2
    assert len(
        isolated_limiter._attempts[
            "pair_fail:login:203.0.113.10:victim"
        ]
    ) == 2
    assert len(isolated_limiter._attempts["id_fail:login:victim"]) == 2

    login_limits.clear_login_failures(request, "victim")

    assert len(isolated_limiter._attempts["ip_fail:login:203.0.113.10"]) == 2
    assert not isolated_limiter._attempts.get(
        "pair_fail:login:203.0.113.10:victim"
    )
    assert not isolated_limiter._attempts.get("id_fail:login:victim")


def test_login_failure_identifiers_are_normalized(
    monkeypatch,
    isolated_limiter,
):
    _configure_login_limits(monkeypatch, pair=(1, 15))
    request = DummyRequest("203.0.113.10")

    login_limits.record_login_failure(request, "  ViCtIm  ")

    assert login_limits.check_login_failure_limits(request, "victim") is not None
    assert "pair_fail:login:203.0.113.10:victim" in isolated_limiter._attempts
    assert not any("ViCtIm" in key for key in isolated_limiter._attempts)


def test_login_backoff_progresses_and_is_capped():
    assert login_limits.get_login_backoff_seconds(0) == 0
    assert login_limits.get_login_backoff_seconds(1) == 2
    assert login_limits.get_login_backoff_seconds(2) == 4
    assert login_limits.get_login_backoff_seconds(3) == 8
    assert login_limits.get_login_backoff_seconds(4) == 8
    assert login_limits.get_login_backoff_seconds(100) == 8


def test_ip_failure_limit_stops_password_spraying(
    monkeypatch,
    isolated_limiter,
):
    _configure_login_limits(
        monkeypatch,
        pair=(100, 15),
        ip_failures=(3, 60),
    )
    attacking_ip = DummyRequest("203.0.113.10")
    other_ip = DummyRequest("203.0.113.11")

    for username in ("alice", "bob", "carol"):
        login_limits.record_login_failure(attacking_ip, username)

    assert (
        login_limits.check_login_failure_limits(attacking_ip, "another-user")
        is not None
    )
    assert login_limits.check_login_failure_limits(other_ip, "another-user") is None


@pytest.mark.asyncio
async def test_invalid_captcha_does_not_poison_account_buckets(
    monkeypatch,
    isolated_limiter,
):
    _configure_login_limits(monkeypatch)
    request = _post_login_request(username="ViCtIm")

    monkeypatch.setattr(auth_flows, "check_rate_limits", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        auth_flows,
        "verify_captcha",
        AsyncMock(return_value=(False, "Invalid CAPTCHA.")),
    )
    monkeypatch.setattr(
        auth_flows.templates,
        "TemplateResponse",
        lambda name, context: TemplateResult(name, context),
    )

    response = await auth_flows.handle_login_request(request)

    assert response.name == "login.html"
    assert response.context["error"] == "Please complete the security verification and try again."
    account_keys = {
        key
        for key in isolated_limiter._attempts
        if key.startswith(("pair_fail:login:", "id_fail:login:"))
    }
    assert account_keys == set()


@pytest.mark.asyncio
async def test_successful_password_login_clears_login_failure_state(
    monkeypatch,
    isolated_limiter,
):
    _configure_login_limits(monkeypatch)
    seed_request = DummyRequest("203.0.113.10")
    login_limits.record_login_failure(seed_request, "victim")
    login_limits.record_login_failure(seed_request, "victim")

    request = _post_login_request(username="victim")
    user = User(
        id=42,
        username="victim",
        password=b"unused",
        role_id=3,
        is_enabled=True,
        can_send_files=False,
        can_generate_images=False,
        current_prompt_id=None,
        authentication_mode="password_only",
        is_admin=False,
        is_user=False,
    )
    login_response = object()

    monkeypatch.setattr(auth_flows, "check_rate_limits", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        auth_flows,
        "verify_captcha",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        auth_flows,
        "get_user_by_username",
        AsyncMock(return_value=user),
    )
    monkeypatch.setattr(auth_flows, "verify_password", lambda *args: True)
    monkeypatch.setattr(
        auth_flows,
        "create_user_info",
        AsyncMock(return_value={"id": user.id}),
    )
    monkeypatch.setattr(
        auth_flows,
        "get_after_login_redirect",
        AsyncMock(return_value="/home"),
    )
    monkeypatch.setattr(
        auth_flows,
        "create_login_response",
        lambda *args, **kwargs: login_response,
    )

    response = await auth_flows.handle_login_request(request)

    assert response is login_response
    assert len(isolated_limiter._attempts["ip_fail:login:203.0.113.10"]) == 2
    assert not isolated_limiter._attempts.get(
        "pair_fail:login:203.0.113.10:victim"
    )
    assert not isolated_limiter._attempts.get("id_fail:login:victim")
