"""Real admin renders keep identifiers, filtering and operator-authored data intact."""
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", ["admin_chat", "admin_watchdog", "admin_session_health", "task_manager", "admin/security_operations", "admin/security_guard_config"])
def test_operations_render_real_translations_and_preserve_control_values(language, name):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    content = "Original <operator>"
    html = env.get_template(name + ".html").render(
        ui_language=language, username="Fixture", is_admin=True, is_user=True,
        marketplace={"discovery_enabled": True, "creator_tools_enabled": True},
        filters=dict(search="", user_exact="", prompt_id=7, locked="", conversation_id="41", date_from="", date_to="", min_tokens="", event_type="role_breach_hard", severity="alert"),
        stats=dict(total=1, locked=1, hints=1, errors=0), page=1, total_pages=2, per_page=25,
        showing_start=1, showing_end=1, total_events=1, sort_by="id", sort_dir="desc",
        prompts=[dict(id=7, name=content)], conversations=[dict(id=41, locked=True, locked_reason=content, username="fixture", chat_name=content, prompt_name=content, message_count=2, total_tokens=12345, start_date="2026-09-01 23:55:00", last_activity="2026-09-02 00:10:00")],
        events=[dict(created_at="2026-09-01 23:55:00", event_type="role_breach_hard", severity="alert", action_taken="blocked", analysis=content, hint=content, prompt_name=content, conversation_id=41, chat_name=content)],
        event_types=["role_breach_hard", "drift"], severities=["alert", "info"],
        request=SimpleNamespace(query_params={}), current_llm_id=7, llms=[(7, "GPT", content)],
        pending_tasks=[dict(message_id="wire-id", options=dict(retries=1, max_retries=3)), dict(message_id="default-pending", options={})],
        active_tasks=[dict(message_id="default-active", options={})],
        failed_tasks=[dict(message_id="default-failed", options={})],
        delayed_tasks=[dict(message_id="default-delayed", options={})], dramatiq_queues=["wire-queue"], redis_keys=[],
    )
    payload = json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>', html, re.S)[1])
    assert set(payload["resources"][language]) == {"common", "navigation", "admin_operations"}
    if name == "admin_chat":
        assert 'value="41"' in html and 'data-locked="1"' in html
        assert "Original &lt;operator&gt;" in html
        assert tr.format_number(12345) in html
        assert tr.render("admin_operations.unlock") in html
    elif name == "admin_watchdog":
        assert 'value="role_breach_hard" selected' in html
        assert tr.render("admin_operations.role_breach_hard") in html
        assert "Original &lt;operator&gt;" in html
    elif name == "task_manager":
        assert "deleteTask('wire-id', 'pending')" in html
        assert tr.render("admin_operations.tasks.retries", current="1", maximum="3") in html
        assert html.count(tr.render("admin_operations.tasks.retries", current=tr.format_number(0), maximum=tr.t("admin_operations.unknown"))) == 4
    elif name == "admin/security_guard_config":
        assert 'name="llm_id" value="7" checked' in html
        assert "Original &lt;operator&gt;" in html
    elif name == "admin/security_operations":
        assert 'value="Manual block from admin panel"' not in html
        assert 'placeholder="' + tr.render("admin_operations.security.default_reason") + '"' in html


class Operator:
    id = 7
    ui_language = "ja"

    def __init__(self, admin=True):
        self.admin = admin

    @property
    async def is_admin(self):
        return self.admin


@pytest.mark.asyncio
async def test_security_denial_and_invalid_ip_do_not_call_operational_services(monkeypatch):
    import app as app_module
    from fastapi import HTTPException

    lookup = AsyncMock(side_effect=ValueError("untranslated address diagnostic"))
    monkeypatch.setattr(app_module, "is_ip_blocked_async", lookup)
    with pytest.raises(HTTPException) as denied:
        await app_module.admin_security_ip_status("invalid", Operator(admin=False))
    assert denied.value.status_code == 403
    assert denied.value.detail == Translator("ja").render("management_operations_errors.admin_required")
    lookup.assert_not_awaited()
    with pytest.raises(HTTPException) as invalid:
        await app_module.admin_security_ip_status("invalid", Operator())
    assert invalid.value.status_code == 400
    assert invalid.value.detail == Translator("ja").render("management_operations_errors.invalid_ip")


@pytest.mark.asyncio
async def test_task_cleanup_preserves_failure_and_never_returns_exception_details(monkeypatch):
    import app as app_module

    redis = SimpleNamespace(keys=AsyncMock(side_effect=RuntimeError("internal server detail")), delete=AsyncMock())
    monkeypatch.setattr(app_module, "redis_client", redis)
    response = await app_module.clear_dramatiq(Operator())
    assert json.loads(response.body) == {"success": False, "error": Translator("ja").render("common.error.generic")}
    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversation_partial_delete_keeps_failure_status_and_identifiers(monkeypatch):
    from chat.routes import admin
    from starlette.requests import Request

    monkeypatch.setattr(admin, "is_admin", AsyncMock(return_value=True))
    remove = AsyncMock(return_value=7)
    monkeypatch.setattr(admin, "delete_conversation_recursively", remove)
    monkeypatch.setattr(admin, "delete_conversation_folder", AsyncMock(return_value=False))

    async def receive():
        return {"type": "http.request", "body": b'{"conversation_ids":[41,73]}'}

    request = Request({"type": "http", "method": "POST", "path": "/admin/api/conversations/bulk_delete", "headers": []}, receive)
    response = await admin.delete_multiple_conversations(request, Operator())
    assert response.status_code == 500
    payload = json.loads(response.body)
    assert payload["failed_conversations"] == [41, 73]
    assert payload["message"] == Translator("ja").render("management_operations_errors.conversations_folder_failed")
    assert [call.args for call in remove.await_args_list] == [(41,), (73,)]
