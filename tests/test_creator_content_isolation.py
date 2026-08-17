from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from starlette.responses import Response


def _request(
    host: str,
    path: str,
    *,
    method: str = "GET",
    query: str = "",
    cookie: str = "",
) -> Request:
    headers = [(b"host", host.encode("ascii"))]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": headers,
            "client": ("203.0.113.10", 1234),
            "server": (host, 443),
        }
    )


@pytest.fixture
def isolated_origin_env(monkeypatch):
    monkeypatch.setenv("PRIMARY_APP_DOMAIN", "aurvek.example")
    monkeypatch.delenv("CLOUDFLARE_DOMAIN", raising=False)
    monkeypatch.delenv("AURVEK_PRIMARY_DOMAINS", raising=False)
    monkeypatch.setenv("CREATOR_CONTENT_ORIGIN", "https://pages.aurvek-content.example")
    monkeypatch.setenv("APP_SECRET_KEY", "test-creator-isolation-secret")


def test_creator_origin_must_be_https_and_on_a_separate_site(monkeypatch):
    from marketplace.landing.isolation import get_creator_content_config

    monkeypatch.setenv("PRIMARY_APP_DOMAIN", "example.com")
    monkeypatch.delenv("CLOUDFLARE_DOMAIN", raising=False)
    monkeypatch.delenv("AURVEK_PRIMARY_DOMAINS", raising=False)

    monkeypatch.setenv("CREATOR_CONTENT_ORIGIN", "https://pages.example.com")
    assert get_creator_content_config() is None

    monkeypatch.setenv("CREATOR_CONTENT_ORIGIN", "http://aurvek-pages.net")
    assert get_creator_content_config() is None

    monkeypatch.setenv("CREATOR_CONTENT_ORIGIN", "https://aurvek-pages.net")
    config = get_creator_content_config()
    assert config is not None
    assert config.host == "aurvek-pages.net"


def test_content_tokens_are_short_lived_bound_and_tamper_evident(isolated_origin_env):
    from marketplace.landing.isolation import sign_content_token, verify_content_token

    token = sign_content_token(
        {"purpose": "prompt_preview", "prompt_id": 7, "page": "home"},
        ttl_seconds=60,
    )
    assert token
    assert verify_content_token(
        token,
        expected={"purpose": "prompt_preview", "prompt_id": 7, "page": "home"},
    )
    assert verify_content_token(token, expected={"prompt_id": 8}) is None
    assert verify_content_token(token[:-1] + ("A" if token[-1] != "A" else "B")) is None


def test_welcome_tokens_support_a_work_session_without_exposing_user_id(
    isolated_origin_env,
):
    from marketplace.landing.isolation import sign_content_token, verify_content_token

    token = sign_content_token(
        {"purpose": "welcome", "entity_type": "prompt", "entity_id": 7},
        ttl_seconds=8 * 60 * 60,
    )
    payload = verify_content_token(
        token,
        expected={"purpose": "welcome", "entity_type": "prompt", "entity_id": 7},
    )
    assert payload is not None
    assert payload["exp"] - payload["iat"] == 8 * 60 * 60
    assert "user_id" not in payload


def test_injected_landing_helpers_work_from_the_isolated_origin(isolated_origin_env):
    from marketplace.landing.rendering import (
        inject_custom_domain_analytics,
        inject_prompt_landing_analytics,
    )

    for rendered in (
        inject_prompt_landing_analytics("<html><body></body></html>", 17),
        inject_custom_domain_analytics("<html><body></body></html>", 17),
    ):
        assert "mode: 'no-cors'" in rendered
        assert "text/plain;charset=UTF-8" in rendered
        assert "https://aurvek.example/purchase/prompt/17" in rendered
        assert "aurvek-purchase-request" in rendered
        assert "fetch('/api/prompts/'" not in rendered


@pytest.mark.asyncio
async def test_purchase_bridge_returns_to_trusted_login_with_safe_next(monkeypatch):
    from marketplace.routes import checkout

    monkeypatch.setattr(checkout, "require_checkout_enabled", lambda: None)
    response = await checkout.prompt_purchase_bridge(
        _request("aurvek.example", "/purchase/prompt/17"),
        17,
        None,
    )
    assert response.status_code == 302
    assert response.headers["location"] == (
        "/login?next=%2Fpurchase%2Fprompt%2F17"
    )


@pytest.mark.asyncio
async def test_shared_content_host_blocks_app_routes_and_strips_session_cookies(
    isolated_origin_env,
):
    from marketplace.middleware.custom_domains import (
        CustomDomainMiddleware,
        set_primary_domains,
    )

    set_primary_domains(["aurvek.example"])
    middleware = CustomDomainMiddleware(lambda scope, receive, send: None)
    called = False

    async def should_not_run(request):
        nonlocal called
        called = True
        return Response("unexpected")

    blocked = await middleware.dispatch(
        _request(
            "pages.aurvek-content.example",
            "/api/users/1",
            cookie="session=secret; _aurvek_visitor=visitor",
        ),
        should_not_run,
    )
    assert blocked.status_code == 404
    assert called is False

    seen_cookies = None

    async def allowed_route(request):
        nonlocal seen_cookies
        seen_cookies = dict(request.cookies)
        response = Response("ok")
        response.set_cookie("session", "must-be-removed")
        response.set_cookie("_aurvek_visitor", "visitor")
        return response

    allowed = await middleware.dispatch(
        _request(
            "pages.aurvek-content.example",
            "/p/AbCd1234/demo/",
            cookie="session=secret; _aurvek_visitor=visitor",
        ),
        allowed_route,
    )
    assert allowed.status_code == 200
    assert allowed.headers["strict-transport-security"] == "max-age=31536000"
    assert seen_cookies == {"_aurvek_visitor": "visitor"}
    cookies = allowed.headers.getlist("set-cookie")
    assert any(value.startswith("_aurvek_visitor=") for value in cookies)
    assert not any(value.startswith("session=") for value in cookies)
    assert "frame-ancestors 'none'" in allowed.headers["content-security-policy"]

    preview = await middleware.dispatch(
        _request(
            "pages.aurvek-content.example",
            "/p/AbCd1234/demo/",
            query="preview=1&preview_token=signed",
        ),
        allowed_route,
    )
    assert preview.headers["cache-control"] == "no-store"
    assert preview.headers["referrer-policy"] == "no-referrer"

    embedded = await middleware.dispatch(
        _request(
            "pages.aurvek-content.example",
            "/p/AbCd1234/demo/",
            query="embed=1",
        ),
        allowed_route,
    )
    assert "frame-ancestors https://aurvek.example" in embedded.headers[
        "content-security-policy"
    ]
    assert embedded.headers["cross-origin-resource-policy"] == "cross-origin"
    assert "x-frame-options" not in embedded.headers

    static_asset = await middleware.dispatch(
        _request(
            "pages.aurvek-content.example",
            "/p/AbCd1234/demo/static/site.css",
        ),
        allowed_route,
    )
    assert static_asset.headers["cross-origin-resource-policy"] == "cross-origin"
    assert static_asset.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_legacy_welcome_assets_are_not_served_on_primary_origin(
    isolated_origin_env,
):
    from marketplace.middleware.custom_domains import (
        CustomDomainMiddleware,
        set_primary_domains,
    )

    set_primary_domains(["aurvek.example"])
    middleware = CustomDomainMiddleware(lambda scope, receive, send: None)
    called = False

    async def call_next(request):
        nonlocal called
        called = True
        return Response("creator javascript")

    response = await middleware.dispatch(
        _request("aurvek.example", "/home/static/p7/js/custom.js"),
        call_next,
    )
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert called is False


@pytest.mark.asyncio
async def test_custom_domain_has_landing_only_allowlist_and_primary_auth_redirect(
    isolated_origin_env,
    monkeypatch,
):
    from marketplace.middleware.custom_domains import (
        CustomDomainMiddleware,
        set_primary_domains,
    )

    set_primary_domains(["aurvek.example"])
    middleware = CustomDomainMiddleware(lambda scope, receive, send: None)

    async def domain_data(host):
        assert host == "creator-site.net"
        return {
            "prompt_id": 9,
            "prompt_name": "Demo Prompt",
            "username": "creator",
            "public_id": "AbCd1234",
        }

    monkeypatch.setattr(middleware, "_get_domain_data", domain_data)

    called = False

    async def call_next(request):
        nonlocal called
        called = True
        return Response("unexpected")

    blocked = await middleware.dispatch(
        _request("creator-site.net", "/settings"),
        call_next,
    )
    assert blocked.status_code == 404
    assert called is False

    login = await middleware.dispatch(
        _request("creator-site.net", "/login"),
        call_next,
    )
    assert login.status_code == 302
    assert login.headers["location"] == (
        "https://aurvek.example/p/AbCd1234/demo-prompt/login"
    )
    assert called is False


@pytest.mark.asyncio
async def test_prompt_landing_redirects_before_creator_html_and_preview_is_owner_only(
    isolated_origin_env,
    tmp_path,
    monkeypatch,
):
    from marketplace.routes import prompt_landings

    (tmp_path / "home.html").write_text(
        "<html><body><script>window.creatorCode = true</script></body></html>",
        encoding="utf-8",
    )

    async def landing_data(public_id):
        return {
            "prompt_id": 17,
            "prompt_name": "Demo",
            "is_unlisted": 0,
            "public": 1,
            "has_landing_page": 1,
            "landing_trusted": 0,
            "username": "creator",
            "path": tmp_path,
        }

    async def no_custom_domain(prompt_id):
        return None

    monkeypatch.setattr(prompt_landings, "require_public_landings_enabled", lambda: None)
    monkeypatch.setattr(prompt_landings, "get_landing_path_cached", landing_data)
    monkeypatch.setattr(prompt_landings, "get_active_custom_domain", no_custom_domain)

    primary_request = _request("aurvek.example", "/p/AbCd1234/demo/")
    redirect = await prompt_landings.public_landing_page(
        primary_request,
        "AbCd1234",
        "demo",
    )
    assert redirect.status_code == 302
    assert redirect.headers["location"].startswith(
        "https://pages.aurvek-content.example/p/AbCd1234/demo/"
    )
    assert b"creatorCode" not in redirect.body

    async def denied_preview(request, prompt_id):
        return False

    monkeypatch.setattr(prompt_landings, "_can_preview_prompt", denied_preview)
    with pytest.raises(HTTPException) as exc_info:
        await prompt_landings.public_landing_page(
            _request(
                "aurvek.example",
                "/p/AbCd1234/demo/",
                query="preview=1",
            ),
            "AbCd1234",
            "demo",
        )
    assert exc_info.value.status_code == 403

    async def allowed_preview(request, prompt_id):
        return True

    monkeypatch.setattr(prompt_landings, "_can_preview_prompt", allowed_preview)
    preview_redirect = await prompt_landings.public_landing_page(
        _request(
            "aurvek.example",
            "/p/AbCd1234/demo/",
            query="preview=1",
        ),
        "AbCd1234",
        "demo",
    )
    location = preview_redirect.headers["location"]
    query = parse_qs(urlsplit(location).query)
    assert query["preview"] == ["1"]
    assert query["preview_token"]

    isolated_request = _request(
        "pages.aurvek-content.example",
        "/p/AbCd1234/demo/",
        query=urlsplit(location).query,
    )
    isolated_request.state.creator_content_origin = True
    isolated_response = await prompt_landings.public_landing_page(
        isolated_request,
        "AbCd1234",
        "demo",
    )
    assert isolated_response.status_code == 200
    assert b"creatorCode" in isolated_response.body


@pytest.mark.asyncio
async def test_custom_pack_html_is_served_only_on_isolated_origin(
    isolated_origin_env,
    tmp_path,
    monkeypatch,
):
    from marketplace.routes import packs

    (tmp_path / "home.html").write_text(
        "<html><body><script>window.packCreatorCode = true</script></body></html>",
        encoding="utf-8",
    )
    cached = {
        "pack_id": 4,
        "pack_name": "Pack",
        "slug": "pack",
        "description": "",
        "cover_image": None,
        "is_paid": 0,
        "price": 0,
        "status": "published",
        "is_public": 1,
        "has_custom_landing": 1,
        "created_by_user_id": 5,
        "tags": None,
        "username": "creator",
        "path": tmp_path,
    }

    async def pack_data(public_id):
        return cached

    monkeypatch.setattr(packs, "require_public_landings_enabled", lambda: None)
    monkeypatch.setattr(packs, "get_pack_landing_cached", pack_data)

    primary = await packs.pack_landing_page(
        _request("aurvek.example", "/pack/AbCd1234/pack/"),
        "AbCd1234",
        "pack",
    )
    assert primary.status_code == 302
    assert primary.headers["location"].startswith("https://pages.aurvek-content.example/")
    assert b"packCreatorCode" not in primary.body

    isolated_request = _request(
        "pages.aurvek-content.example",
        "/pack/AbCd1234/pack/",
    )
    isolated_request.state.creator_content_origin = True
    isolated = await packs.pack_landing_page(
        isolated_request,
        "AbCd1234",
        "pack",
    )
    assert isolated.status_code == 200
    assert b"packCreatorCode" in isolated.body


@pytest.mark.asyncio
async def test_welcome_creator_html_is_wrapped_in_cross_origin_sandbox(
    isolated_origin_env,
    tmp_path,
    monkeypatch,
):
    import welcome_service

    welcome_dir = tmp_path / "welcome"
    welcome_dir.mkdir()
    (welcome_dir / "index.html").write_text(
        "<html><script>window.untrustedWelcome = true</script></html>",
        encoding="utf-8",
    )

    async def switcher_data(user, world):
        return {"current": world, "products": []}

    monkeypatch.setattr(welcome_service, "get_world_switcher_data", switcher_data)
    monkeypatch.setattr(welcome_service, "render_aurvek_world_navbar", lambda world: "<nav>safe</nav>")

    response = await welcome_service.serve_welcome_world(
        _request("aurvek.example", "/welcome/prompt/7"),
        SimpleNamespace(id=3),
        {"type": "prompt", "id": 7, "name": "Demo", "path": str(tmp_path)},
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "window.untrustedWelcome" not in body
    assert "https://pages.aurvek-content.example/_aurvek/welcome/prompt/7/" in body
    assert (
        "sandbox=\"allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox "
        "allow-top-navigation-by-user-activation\""
    ) in body
    assert "allow-same-origin" not in body
    token_match = re.search(r"/_aurvek/welcome/prompt/7/([^/]+)/", body)
    assert token_match is not None
    from marketplace.landing.isolation import verify_content_token

    payload = verify_content_token(token_match.group(1))
    assert payload is not None
    assert "user_id" not in payload
