"""Native signup captures UI language without borrowing an embedded frame locale."""

import json
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from starlette.requests import Request

from marketplace.services import pending_registrations


@pytest.fixture
def pending_db(tmp_path, monkeypatch):
    path = tmp_path / "pending.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((Path(__file__).parents[1] / "aurvek_schema.sql").read_text(encoding="utf-8"))

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(path) as connection:
            yield connection

    monkeypatch.setattr(pending_registrations, "get_db_connection", connect)
    return path


@pytest.mark.asyncio
async def test_pending_locale_roundtrip_default_and_invalid_does_not_replace(pending_db):
    values = dict(email="signup@example.test", username="signup", password_hash=b"test-hash", token="test-token",
                  target_role="user", prompt_id=None, expires_at=datetime.now() + timedelta(hours=1))
    assert await pending_registrations.create_pending_registration(**values)
    assert (await pending_registrations.get_pending_registration("test-token"))["ui_language"] == "en"
    assert await pending_registrations.create_pending_registration(**values, ui_language="ja-JP")
    assert (await pending_registrations.get_pending_registration("test-token"))["ui_language"] == "ja"
    with pytest.raises(ValueError, match="Unsupported interface language"):
        await pending_registrations.create_pending_registration(**values, ui_language="unsupported")
    assert (await pending_registrations.get_pending_registration("test-token"))["ui_language"] == "ja"


def _request(payload, language="de"):
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/register-pack", "query_string": b"",
                    "headers": [(b"content-type", b"application/json"), (b"accept-language", language.encode())],
                    "scheme": "https", "server": ("example.test", 443), "client": ("127.0.0.1", 1)}, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection,expected", [(None, "de"), ("pt-BR", "pt")])
async def test_pack_signup_captures_explicit_or_negotiated_language(monkeypatch, selection, expected):
    from marketplace.routes import acquisition

    monkeypatch.setattr(acquisition, "marketplace_public_landings_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "cleanup_expired_registrations", AsyncMock())
    monkeypatch.setattr(acquisition, "check_rate_limits", lambda *a, **kw: None)
    monkeypatch.setattr(acquisition, "check_failure_limit", lambda *a, **kw: None)
    monkeypatch.setattr(acquisition, "validate_email_robust", lambda email: (True, None))
    monkeypatch.setattr(acquisition, "get_user_by_email_record", AsyncMock(return_value=None))
    monkeypatch.setattr(acquisition, "generate_unique_username", AsyncMock(return_value="signup"))
    monkeypatch.setattr(acquisition, "get_auth_base_url", lambda request: "https://example.test")
    import email_service
    monkeypatch.setenv("USE_EMAIL_SERVICE", "true")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "test-postmark-token")
    monkeypatch.setattr(acquisition, "email_service", email_service.EmailService())
    sent = []
    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(email_service.requests, "post", post)
    create = AsyncMock(return_value=True)
    monkeypatch.setattr(acquisition, "create_pending_registration", create)
    rows = iter([(7, "public-pack", "published", 1, None, 0), (10,)])

    @asynccontextmanager
    async def connect(readonly=False):
        async def execute(*args):
            return SimpleNamespace(fetchone=AsyncMock(return_value=next(rows)))
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(acquisition, "get_db_connection", connect)
    payload = {"email": "signup@example.test", "password": "test-password", "password_confirm": "test-password",
               "pack_id": 7, "public_id": "public-pack"}
    if selection is not None:
        payload["ui_language"] = selection
    response = await acquisition.register_pack_submit(_request(payload))
    assert response.status_code == 200
    assert create.await_args.kwargs["ui_language"] == expected
    assert len(sent) == 1 and f'lang="{expected}"' in sent[0]["HtmlBody"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"ui_language": "unsupported"}, {"ui_language": None}, []])
async def test_pack_signup_rejects_invalid_language_before_account_access(monkeypatch, payload):
    from marketplace.routes import acquisition

    monkeypatch.setattr(acquisition, "marketplace_public_landings_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "cleanup_expired_registrations", AsyncMock())
    lookup = AsyncMock()
    monkeypatch.setattr(acquisition, "get_user_by_email_record", lookup)
    response = await acquisition.register_pack_submit(_request(payload))
    assert response.status_code == 400
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_pack_account_claim_uses_saved_language(monkeypatch):
    from marketplace.routes import acquisition

    monkeypatch.setattr(acquisition, "marketplace_public_landings_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "marketplace_checkout_enabled", lambda: True)
    monkeypatch.setattr(acquisition, "cleanup_expired_registrations", AsyncMock())
    monkeypatch.setattr(acquisition, "check_rate_limits", lambda *a, **kw: None)
    monkeypatch.setattr(acquisition, "check_failure_limit", lambda *a, **kw: None)
    monkeypatch.setattr(acquisition, "validate_email_robust", lambda email: (True, None))
    monkeypatch.setattr(acquisition, "get_user_by_email_record", AsyncMock(
        return_value={"id": 7, "username": "existing", "ui_language": "it"}))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(acquisition, "send_entitlement_claim_email", send)
    payload = {"email": "existing@example.test", "password": "test-password", "password_confirm": "test-password",
               "pack_id": 7, "public_id": "public-pack", "ui_language": "ja"}
    response = await acquisition.register_pack_submit(_request(payload, language="de"))
    assert response.status_code == 200
    assert send.await_args.kwargs["ui_language"] == "it"
