from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re

import pytest
from starlette.requests import Request


ROOT = Path(__file__).resolve().parents[1]


def _request(*, cookie: str = "", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    request_headers = list(headers or [])
    if cookie:
        request_headers.append((b"cookie", cookie.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": request_headers,
            "client": ("127.0.0.1", 12345),
            "query_string": b"",
            "scheme": "https",
            "server": ("testserver", 443),
        }
    )


def _requirement_map(filename: str) -> dict[str, str]:
    result = {}
    for raw_line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, version = line.split("==", 1)
        result[name.lower()] = version.strip()
    return result


@pytest.mark.parametrize("filename", ["aurvek-main.conf", "aurvek-cdn.conf"])
def test_nginx_private_trees_are_default_denied(filename):
    config = (ROOT / "nginx" / filename).read_text(encoding="utf-8")

    for private_prefix in (
        "file_blobs",
        "jobs",
        "cache",
        "archive",
        "config",
        "seed",
        "staging",
    ):
        assert re.search(
            rf"location \^~ /{private_prefix}/\s*{{\s*return 404;\s*}}",
            config,
        )

    user_default_deny = config.index("location ~ ^/users/ {")
    assert user_default_deny > config.index("video/(bot|user)")
    assert config.count("auth_request /auth-image;") == 3
    assert config.count("auth_request /auth-file;") == 2
    assert config.count("{") == config.count("}")


def test_nginx_public_static_serving_is_allowlisted():
    main = (ROOT / "nginx" / "aurvek-main.conf").read_text(encoding="utf-8")
    cdn = (ROOT / "nginx" / "aurvek-cdn.conf").read_text(encoding="utf-8")

    assert "if ($uri !~* \\." in main
    assert "try_files $uri $uri/ =404;" not in main
    assert "location / {\n        return 404;\n    }" in cdn
    assert cdn.index("location ~ ^/users/ {") < cdn.index(
        "location ~* \\.(html?"
    )
    for forbidden_extension in ("pem", "key", "env", "py", "sql", "bak", "db", "pkl"):
        assert f"|{forbidden_extension}|" not in main
        assert f"|{forbidden_extension}|" not in cdn


@pytest.mark.parametrize("filename", ["aurvek-main.conf", "aurvek-cdn.conf"])
def test_nginx_authenticated_prompt_avatar_accepts_sanitized_names(filename):
    config = (ROOT / "nginx" / filename).read_text(encoding="utf-8")
    location_line = next(
        line.strip()
        for line in config.splitlines()
        if line.lstrip().startswith("location ~ ^/users/")
        and "/prompts/" in line
        and "/static/img/" in line
    )
    pattern = location_line.removeprefix("location ~ ").removesuffix(" {")

    base = "/users/abc/defg/userhash/prompts/000/0063_nova_orion/static/img/"
    assert re.fullmatch(pattern, base + "63_nova-orion_32.webp")
    assert re.fullmatch(pattern, base + "29_escritor_pro_(life_coach)_128.webp")
    assert not re.fullmatch(pattern, base + "63_nova-orion_32.pem")


@pytest.mark.parametrize("filename", ["aurvek-main.conf", "aurvek-cdn.conf"])
def test_nginx_authenticated_audio_and_pdf_cover_legacy_names(filename):
    config = (ROOT / "nginx" / filename).read_text(encoding="utf-8")
    location_line = next(
        line.strip()
        for line in config.splitlines()
        if line.lstrip().startswith("location ~ ^/users/")
        and "/files/" in line
        and "mp3/" in line
        and "pdf/" in line
    )
    pattern = location_line.removeprefix("location ~ ").removesuffix(" {")

    base = "/users/abc/defg/userhash/files/123/2026/"
    assert re.fullmatch(pattern, base + "mp3/audio-versión_final.mp3")
    assert re.fullmatch(pattern, base + "pdf/informe-médico_(final).pdf")
    assert re.fullmatch(
        pattern,
        base + "pdf/uploads/sha256_informe-médico_(final).pdf",
    )
    assert not re.fullmatch(pattern, base + "mp3/audio-versión_final.pdf")
    assert not re.fullmatch(pattern, base + "pdf/uploads/nested/report.pdf")


def test_nginx_default_server_cannot_bypass_user_tree_deny():
    custom = (ROOT / "nginx" / "aurvek-custom-domains.conf").read_text(
        encoding="utf-8"
    )

    assert "listen 80 default_server;" in custom
    assert "listen 443 ssl default_server;" in custom
    deny_position = custom.index("location ^~ /users/")
    generic_proxy_position = custom.index("location / {")
    first_legacy_user_regex = custom.index("location ~ ^/users/")
    assert deny_position < generic_proxy_position
    assert deny_position < first_legacy_user_regex
    assert re.search(
        r"location \^~ /users/\s*\{\s*return 404;\s*\}",
        custom,
    )


def test_runtime_jobs_are_ignored_and_env_example_is_trackable():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/jobs/" in gitignore
    assert "!.env.example" in gitignore


def test_security_dependency_pins_are_aligned():
    windows = _requirement_map("requirements.txt")
    linux = _requirement_map("requirements-linux.txt")

    assert windows["python-multipart"] == "0.0.27"
    assert linux["python-multipart"] == "0.0.27"
    for package in (
        "fastapi",
        "pyjwt",
        "starlette",
    ):
        assert windows[package] == linux[package]


def test_python_312_ci_uses_linux_dependency_overlay():
    workflow = (ROOT / ".github" / "workflows" / "python-312.yml").read_text(
        encoding="utf-8"
    )

    assert 'python-version: "3.12"' in workflow
    assert "python -m pip install --requirement requirements-linux.txt" in workflow
    assert "python -m pytest -q" in workflow


def test_public_mark_conversion_endpoint_is_removed():
    source = (ROOT / "marketplace" / "routes" / "analytics.py").read_text(
        encoding="utf-8"
    )
    assert "/api/analytics/mark-conversion" not in source
    assert "mark_analytics_conversion" not in source


def test_category_counts_only_joined_visible_prompts():
    source = (ROOT / "marketplace" / "routes" / "discovery.py").read_text(
        encoding="utf-8"
    )
    assert "COUNT(p.id) as prompt_count" in source
    assert "COUNT(pc.prompt_id) as prompt_count" not in source


def test_session_reputation_uses_shared_cookie_name(monkeypatch):
    import common
    from auth_constants import SESSION_COOKIE_NAME
    from middleware import security

    assert SESSION_COOKIE_NAME == "session"
    monkeypatch.setattr(common, "decode_jwt_cached", lambda token, key: {"exp": 1})
    monkeypatch.setattr(common, "verify_token_expiration", lambda payload: True)

    assert security._has_valid_session(
        _request(cookie=f"{SESSION_COOKIE_NAME}=signed-token")
    )
    assert not security._has_valid_session(_request(cookie="access_token=old-name"))

    auth_source = (ROOT / "auth.py").read_text(encoding="utf-8")
    security_source = (ROOT / "middleware" / "security.py").read_text(
        encoding="utf-8"
    )
    assert auth_source.count("SESSION_COOKIE_NAME") >= 5
    assert 'cookies.get("session")' not in auth_source
    assert "request.cookies.get(SESSION_COOKIE_NAME)" in security_source

    raw_session_call = re.compile(
        r"(?:cookies\.get|set_cookie|delete_cookie)\(\s*"
        r"(?:key\s*=\s*)?[\"']session[\"']"
    )
    for relative_path in (
        "app.py",
        "auth.py",
        "chat/routes/warmup.py",
        "chat/services/message_requests.py",
        "middleware/security.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert not raw_session_call.search(source), relative_path


def test_rate_limiter_and_admin_audit_share_proxy_ip_helper():
    import admin_audit
    import rate_limiter
    from middleware.security import get_client_ip

    assert rate_limiter.get_client_ip is get_client_ip
    assert admin_audit.get_client_ip is get_client_ip

    request = _request(
        headers=[
            (b"cf-connecting-ip", b"203.0.113.9"),
            (b"x-forwarded-for", b"198.51.100.4, 127.0.0.1"),
            (b"x-real-ip", b"192.0.2.7"),
        ]
    )
    assert get_client_ip(request) == "203.0.113.9"


def test_tts_key_refresh_is_lazy_and_error_logs_are_redacted(monkeypatch):
    from tools import tts_load_balancer

    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "subscription": {
                    "character_limit": 100,
                    "character_count": 25,
                    "next_character_count_reset_unix": int(
                        (datetime.now() + timedelta(days=1)).timestamp()
                    ),
                }
            }

    monkeypatch.setattr(
        tts_load_balancer.requests,
        "get",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )
    manager = tts_load_balancer.APIKeyManager()
    manager.add_key("super-secret-elevenlabs-key-1234")
    assert calls == []
    assert manager.select_key() is not None
    assert len(calls) == 1

    logged = []
    monkeypatch.setattr(
        tts_load_balancer.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            tts_load_balancer.requests.Timeout("provider timeout")
        ),
    )
    monkeypatch.setattr(
        tts_load_balancer.logger,
        "error",
        lambda message, *args: logged.append(message % args),
    )
    manager.api_keys[0].update_info()
    assert "super-secret-elevenlabs-key-1234" not in logged[0]
    assert "1234" not in logged[0]


def test_email_configuration_is_explicit_and_fallback_logs_no_secrets(
    monkeypatch, caplog
):
    import email_service

    monkeypatch.delenv("USE_EMAIL_SERVICE", raising=False)
    with pytest.raises(RuntimeError, match="explicitly set"):
        email_service.EmailService()

    monkeypatch.setenv("USE_EMAIL_SERVICE", "sometimes")
    with pytest.raises(RuntimeError, match="explicitly set"):
        email_service.EmailService()

    monkeypatch.setenv("USE_EMAIL_SERVICE", "false")
    service = email_service.EmailService()
    caplog.clear()
    assert not service.send_magic_link_email(
        "person@example.com", "https://secret.example/magic/token-ghi", "person"
    )
    assert not service.send_ultra_admin_code("admin@example.com", "917204", "admin")
    assert not service.send_verification_email(
        "person@example.com", "https://secret.example/verify/token-abc"
    )
    assert not service.send_claim_entitlement_email(
        "person@example.com", "https://secret.example/claim/token-def"
    )
    assert "917204" not in caplog.text
    assert "token-abc" not in caplog.text
    assert "token-def" not in caplog.text
    assert "token-ghi" not in caplog.text


def test_ultra_admin_email_has_bounded_timeout(monkeypatch):
    import email_service

    monkeypatch.setenv("USE_EMAIL_SERVICE", "true")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "test-token")
    service = email_service.EmailService()
    captured = {}

    class Response:
        status_code = 200
        text = "OK"

    def fake_post(*args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(email_service.requests, "post", fake_post)
    assert service.send_ultra_admin_code("admin@example.com", "123456", "admin")
    assert captured["timeout"] == 10


def test_email_example_documents_runtime_names():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE=Aurvek.db" in example
    assert "DATABASE=data/Aurvek.db" not in example
    assert "USE_EMAIL_SERVICE=" in example
    assert "POSTMARK_SERVER_TOKEN=" in example
    assert "FROM_EMAIL=" in example
    assert "POSTMARK_API_TOKEN=" not in example
