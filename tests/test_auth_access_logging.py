"""Real Uvicorn access records must not expose browser authorization secrets."""
import logging

import pytest

from log_config import AuthAccessLogFilter, CustomColourizedFormatter, sanitize_auth_request_target


@pytest.mark.parametrize("target, expected", [
    ("/login?token=secret&next=secret", "/login"),
    ("/register?next=secret", "/register"),
    ("/magic-link-recovery?next=secret", "/magic-link-recovery"),
    ("/auth/google?state=secret", "/auth/google"),
    ("/auth/google/callback?code=secret&state=secret", "/auth/google/callback"),
    ("/verify-email/secret?next=secret", "/verify-email/[redacted]"),
    ("/%76erify-email/secret", "/verify-email/[redacted]"),
    ("/embed/authorize?transaction=secret", "/embed/authorize"),
    ("/embed/authorize/complete?transaction=secret", "/embed/authorize/complete"),
    ("/embed/bootstrap?bootstrap=secret", "/embed/bootstrap"),
    ("/api/embed/v1/session?token=secret", "/api/embed/v1/session"),
    ("https://identity.example/auth/google/callback?code=secret", "/auth/google/callback"),
])
def test_access_formatter_never_receives_auth_secret(target, expected):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:4567", "GET", target, "1.1", 302), None)
    assert AuthAccessLogFilter().filter(record)
    rendered = CustomColourizedFormatter("%(message)s", use_colors=False).format(record)
    assert "secret" not in rendered
    assert f'GET {expected} HTTP/1.1" 302' in rendered
    assert "127.0.0.1:4567" in rendered


def test_non_auth_native_access_diagnostics_preserved():
    assert sanitize_auth_request_target("/api/conversations/3/messages?offset=20") == "/api/conversations/3/messages?offset=20"
    assert sanitize_auth_request_target("/static/js/chat.js?v=123") == "/static/js/chat.js?v=123"


def test_filter_installed_on_access_logger_once():
    access = logging.getLogger("uvicorn.access")
    assert len([item for item in access.filters if isinstance(item, AuthAccessLogFilter)]) == 1
