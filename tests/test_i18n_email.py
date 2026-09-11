"""Render actual transactional mail at the mocked Postmark boundary; never send mail."""

import ast
import asyncio
from contextlib import asynccontextmanager
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import email_service
from i18n import LANGUAGES, Translator, get_catalogs, get_translator


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setenv("USE_EMAIL_SERVICE", "true")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "test-postmark-token")
    service = email_service.EmailService()
    captured = []

    def post(url, **kwargs):
        assert url == "https://api.postmarkapp.com/email"
        assert kwargs["timeout"] == 10
        captured.append(kwargs["json"])
        return SimpleNamespace(status_code=200, text="OK")

    monkeypatch.setattr(email_service.requests, "post", post)
    return service, captured


@pytest.mark.parametrize("language", LANGUAGES)
def test_all_email_families_render_recipient_language_and_safe_branding(delivery, language):
    service, sent = delivery
    name = 'Studio <&> "Original"'
    product = 'Product <script>alert(1)</script>'
    username = '<img src=x onerror=alert(1)>'
    link = 'https://example.test/token/abc?next=%2Fchat&source="test"'
    branding = {"company_name": name, "footer_text": 'Custom footer <&>',
                "email_signature": 'Signature "<&>"', "brand_color_primary": "#123456",
                "logo_url": 'https://example.test/logo?x=1&y=2', "hide_platform_branding": True}
    assert service.send_magic_link_email("test@example.test", link, username, branding, language)
    assert service.send_verification_email("test@example.test", link, True, None, branding, language)
    assert service.send_verification_email("test@example.test", link, False, product, branding, language)
    assert service.send_claim_entitlement_email("test@example.test", link, product, branding, language)
    assert service.send_ultra_admin_code("test@example.test", "123456", username, language)
    t = Translator(language).render
    expected_subjects = [t("email.magic.subject", company_name=name),
                         t("email.verification.subject_creator", company_name=name),
                         t("email.verification.subject_customer", display_name=product),
                         t("email.claim.subject", display_name=product), t("email.admin.subject")]
    for payload, subject in zip(sent, expected_subjects):
        assert payload["Subject"] == subject
        html = payload["HtmlBody"]
        assert f'lang="{language}"' in html
        assert '<script>' not in html and '<img src=x' not in html
        assert payload["To"] == "test@example.test" and payload["MessageStream"] == "outbound"
    for payload in sent[:4]:
        html = payload["HtmlBody"]
        assert 'href="https://example.test/token/abc?next=%2Fchat&amp;source=&quot;test&quot;"' in html
        assert 'src="https://example.test/logo?x=1&amp;y=2"' in html
        assert 'Custom footer &lt;&amp;&gt;' in html and 'Signature &quot;&lt;&amp;&gt;&quot;' in html
        assert '#123456' in html and t("email.footer.powered_by") not in unescape(html)
        assert t("email.footer.automated") in unescape(html)
    assert t("email.magic.greeting", username=username) in unescape(sent[0]["HtmlBody"])
    assert t("email.verification.intro_creator", company_name=name) in unescape(sent[1]["HtmlBody"])
    assert t("email.verification.intro_customer", product_name=product) in unescape(sent[2]["HtmlBody"])
    assert t("email.claim.intro", display_name=product) in unescape(sent[3]["HtmlBody"])
    assert sent[4]["TextBody"] == t("email.admin.text", code="123456")
    assert t("email.admin.warning") in unescape(sent[4]["HtmlBody"])


def test_email_catalogs_are_complete_and_unknown_locale_uses_english(delivery):
    catalogs = get_catalogs()
    english = catalogs["en"]["email"]
    for language in LANGUAGES:
        assert set(catalogs[language]["email"]) == set(english)
    service, sent = delivery
    assert service.send_verification_email("test@example.test", "https://example.test/token", ui_language="unknown")
    t = Translator("en").render
    assert sent[0]["Subject"] == "Verify your account for Aurvek"
    assert t("email.verification.intro_customer_generic") in sent[0]["HtmlBody"]
    assert t("email.footer.experiences", company_name="Aurvek") in sent[0]["HtmlBody"]
    assert t("email.footer.powered_by") in sent[0]["HtmlBody"]


def _handler(name, namespace):
    # Execute the real handler without importing app.py's startup services.
    tree = ast.parse((Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == name)
    node.decorator_list = []
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    node.args.defaults = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), namespace)
    return namespace[name]


@pytest.mark.asyncio
async def test_magic_recovery_uses_recipient_preference_not_request(delivery, monkeypatch):
    service, sent = delivery
    import common
    monkeypatch.setattr(common, "get_branding_for_user", AsyncMock(return_value=None))
    cursor = SimpleNamespace(execute=AsyncMock(), fetchone=AsyncMock(return_value=(7, "recipient")))

    @asynccontextmanager
    async def connect(readonly=False):
        yield SimpleNamespace(cursor=AsyncMock(return_value=cursor))

    ns = {"get_translator": get_translator, "get_db_connection": connect,
          "check_rate_limits": lambda *a, **kw: None,
          "RLC": SimpleNamespace(RECOVERY_BY_IP=1, RECOVERY_BY_EMAIL=1),
          "get_user_by_id": AsyncMock(return_value=SimpleNamespace(is_enabled=True, ui_language="ja")),
          "generate_magic_link": AsyncMock(return_value="https://example.test/magic/fixed-token"),
          "email_service": service, "asyncio": asyncio,
          "templates": SimpleNamespace(TemplateResponse=lambda *a: a),
          "logger": SimpleNamespace(error=lambda *a: None)}
    request = Request({"type": "http", "method": "POST", "query_string": b"",
                       "headers": [(b"accept-language", b"de")]})
    request.form = AsyncMock(return_value={"email": "recipient@example.test"})
    await _handler("magic_link_recovery", ns)(request)
    assert len(sent) == 1
    assert sent[0]["Subject"] == "Aurvekへのログインリンク"
    assert 'lang="ja"' in sent[0]["HtmlBody"]


@pytest.mark.asyncio
async def test_ultra_admin_uses_loaded_account_language(delivery):
    service, sent = delivery

    @asynccontextmanager
    async def connect(readonly=False):
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            fetchone=AsyncMock(return_value=("admin@example.test",)))))

    ns = {"get_translator": get_translator, "get_db_connection": connect,
          "get_active_lock_owner": AsyncMock(return_value=None),
          "generate_elevation_code": AsyncMock(return_value="123456"),
          "email_service": service, "asyncio": asyncio, "JSONResponse": JSONResponse,
          "log_admin_action": AsyncMock(), "get_client_ip": lambda request: "127.0.0.1"}
    request = Request({"type": "http", "method": "POST", "query_string": b"",
                       "headers": [(b"accept-language", b"es")]})
    user = SimpleNamespace(id=7, username="admin", ui_language="pt", is_admin=AsyncMock(return_value=True)())
    await _handler("ultra_admin_request_code", ns)(request, user)
    assert sent[0]["Subject"] == "Aurvek - Código de verificação Ultra Admin+"
    assert 'lang="pt"' in sent[0]["HtmlBody"]


@pytest.mark.asyncio
async def test_claim_worker_uses_explicit_recipient_language(delivery, monkeypatch):
    from marketplace.services import pending_entitlements

    service, sent = delivery
    monkeypatch.setattr(pending_entitlements, "email_service", service)
    monkeypatch.setattr(pending_entitlements, "create_pending_entitlement", AsyncMock(return_value="claim-token"))
    monkeypatch.setattr(pending_entitlements, "get_auth_base_url", lambda request: "https://example.test")

    @asynccontextmanager
    async def connect(readonly=False):
        yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
            fetchone=AsyncMock(return_value=("Original product", None)))))

    monkeypatch.setattr(pending_entitlements, "get_db_connection", connect)
    request = Request({"type": "http", "method": "POST", "query_string": b"",
                       "headers": [(b"accept-language", b"de")]})
    assert await pending_entitlements.send_entitlement_claim_email(
        request, "recipient@example.test", 7, prompt_id=10, ui_language="fr")
    assert sent[0]["Subject"] == "Activez votre accès à Original product"
    assert 'href="https://example.test/claim-entitlement/claim-token"' in sent[0]["HtmlBody"]
