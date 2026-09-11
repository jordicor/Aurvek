"""Exercise real admin templates, browser controls and localized API producers."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader
from markupsafe import escape
from starlette.requests import Request

from i18n import LANGUAGES, Translator, read_catalogs


def test_voice_admin_catalogs_are_complete_without_english_fallback():
    catalogs = read_catalogs(Path("locales"))
    for language in LANGUAGES:
        for domain in ("admin_voice_integrations", "admin_telephony", "admin_telephony_errors"):
            assert set(catalogs[language][domain]) == set(catalogs["en"][domain])
    for language in set(LANGUAGES) - {"en"}:
        assert catalogs[language]["admin_voice_integrations"]["ui.elevenlabs_agents"] != catalogs["en"]["admin_voice_integrations"]["ui.elevenlabs_agents"]
        assert catalogs[language]["admin_telephony"]["confirm.disable_assigned"] != catalogs["en"]["admin_telephony"]["confirm.disable_assigned"]


def render_voice_admin(language="en", name="admin_telephony", populated=True):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    operator_text = "Original <operator> 日本語"
    config = dict(enabled=False, recording_default=True, max_call_seconds=14000,
                  stt_language="multi", endpointing_ms=700, barge_in_confirmation_ms=350,
                  allowed_countries=["US", "ES"], silence_check_seconds=60,
                  silence_hangup_seconds=60, scheduler_jitter_seconds=10,
                  max_concurrent_dispatches=10, amd_default=False, reconnect_attempts=2)
    telephony = dict(
        config=config, readiness=dict(ready_for_enable=False, blocking_reasons=["twilio_credentials_missing"], missing_billing_rates=["pstn"], inbound_configuration_ready=True, canonical_voice_valid=True),
        credentials=dict(twilio_account_present=True, twilio_credential_present=False, elevenlabs_stt_present=True, canonical_tts_present=True),
        callbacks=dict(configured=True, voice_inbound="https://fixture.example/webhooks/voice", media_stream="wss://fixture.example/media"),
        billing=dict(rates=[dict(provider="twilio", component_type="pstn", direction="outbound", from_country="US", to_country="ES", unit="minute", provider_rate_per_unit=0.001234, customer_rate_per_unit=0.001567, currency="USD", service_name=operator_text, active=True)], stats_30_days=[dict(state="settled", items=12345, final_provider_cost=1.25, final_customer_charge=1.5, currency="USD", platform_absorbed_items=7)]),
        numbers=[dict(id=7, e164="+34999999999", friendly_name=operator_text, provider_present=True, voice_capable=True, iso_country="ES", region="", capabilities=dict(voice=True), enabled=True, inbound_enabled=True, is_outbound_default=True, webhooks_match=False, explicit_binding_count=12345)],
        audio_publisher_registered=False, voices=[dict(id=13, name=operator_text, provider="elevenlabs", available=True, is_default=True)],
        global_audio=dict(greetings={direction:[dict(literal_text=operator_text, fixed=True)] for direction in ["inbound", "outbound"]}, notices=dict(unknown_caller=operator_text), active_revision="revision-1", latest_definition_revision="revision-2"),
        diagnostics=dict(attention=dict(calls=1, jobs=2), last_webhooks=dict(valid=None, invalid=None), purge_jobs=[dict(id="purge-1", purge_scope="conversation", attempt_count=1)]),
        operation=dict(calls=[dict(id="call-1", conversation_id=41, contact_name=operator_text, direction="outbound", status="in_progress", duration_seconds=1234, final_cost=None, estimated_cost=0.012345, currency="USD", can_retry_hangup=False)], jobs=[dict(id="job-1", conversation_id=41, contact_name=operator_text, scheduled_at_utc="2026-09-08 14:30:00", timezone_name="Europe/Madrid", status="scheduled", last_error_code="fixture_code", last_error_detail="Requires operational attention")], costs_30_days=[dict(component_type="tts", items=11, provider_cost=0.123456, customer_charge=0.654321, currency="USD")]),
        paid_test_call=dict(ready=False, enabled=False, configured_destination_count=1, destinations_redacted=["+34******999"], configuration_error=None),
    )
    agents=[dict(agent_id="agent-wire-1", agent_name=operator_text, is_default=False)]
    mappings=[dict(prompt_id=41, prompt_name=operator_text, agent_id="agent-wire-1", canonical_voice_code="voice-wire-1", canonical_voice_provider="elevenlabs", canonical_voice_inherited=True, webrtc_compatible=False, compatibility_reason=tr.render("admin_voice_integrations.voice.canonical_voice_deprecated"))]
    if not populated:
        agents=[]; mappings=[]
        telephony["numbers"]=[]
        telephony["billing"]={"rates":[], "stats_30_days":[]}
        telephony["operation"]={"calls":[], "jobs":[], "costs_30_days":[]}
    return env.get_template(name+".html").render(
        ui_language=language, username="Fixture", is_admin=True, is_user=True,
        marketplace=dict(discovery_enabled=True, creator_tools_enabled=True),
        request=SimpleNamespace(query_params={}), telephony=telephony,
        telephony_csrf_token="csrf-fixture", agents=agents, mappings=mappings,
        prompts=[dict(id=41,name=operator_text)], enabled_voices=["voice-wire-1"], enabled_count=12345,
        voice_canonicalization_conflicts=[dict(prompt_name=operator_text,prompt_id_snapshot=41,legacy_voice_code="legacy-wire",reason_label=tr.render("admin_voice_integrations.voice.conflict"))],
    )


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", ["admin_elevenlabs", "admin_elevenlabs_voices", "admin_telephony"])
def test_real_voice_admin_renders_and_preserves_identifiers(language, name):
    html=render_voice_admin(language,name)
    tr=Translator(language)
    assert "Original &lt;operator&gt; 日本語" in html or name == "admin_elevenlabs_voices"
    payload=json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>',html,re.S)[1])
    domain="admin_telephony" if name=="admin_telephony" else "admin_voice_integrations"
    assert set(payload["resources"][language]) == {"common","navigation",domain}
    if name != "admin_elevenlabs":
        assert tr.format_number(12345) in html
    if name=="admin_telephony":
        assert 'data-binding-count="12345"' in html
        assert 'value="pstn"' in html and 'value="minute"' in html
        assert 'value="US,ES"' in html and 'value="multi"' in html
        assert 'value="13" selected' in html and 'data-phone-date="2026-09-08 14:30:00"' in html
        assert tr.format_currency(0.001234, "USD", fraction_digits=8) in html
        assert 'unknown_caller' in html and 'csrf-fixture' in html
    elif name=="admin_elevenlabs":
        assert 'value="agent-wire-1"' in html and '/agent-wire-1/delete' in html
        assert 'voice-wire-1' in html and tr.render("admin_voice_integrations.ui.webrtc_incompatible") in html
    else:
        assert 'value="premade"' in html and 'value="male"' in html
    empty=render_voice_admin(language,name,False)
    empty_key={"admin_telephony":"admin_telephony.ui.no_calls", "admin_elevenlabs":"admin_voice_integrations.ui.no_agents_configured_yet", "admin_elevenlabs_voices":"admin_voice_integrations.ui.click_sync_with_elevenlabs_to_load_available_voices"}[name]
    assert str(escape(tr.render(empty_key))) in empty


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", ["admin_elevenlabs", "admin_elevenlabs_voices", "admin_telephony"])
def test_real_rendered_voice_admin_browser_controls(language,name):
    html=render_voice_admin(language,name)
    payload=json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>',html,re.S)[1])
    scripts=re.findall(r'<script>(.*?)</script>',html,re.S)
    script=next(script for script in scripts if "voiceAdminT" in script)
    node=shutil.which("node") or "C:/Program Files/nodejs/node.exe"
    result=subprocess.run([node,"tests/js/voice_admin_controls.cjs"],input=json.dumps(dict(name=name,payload=payload,script=script)),text=True,encoding="utf-8",capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr


class Operator:
    id=7
    def __init__(self,language="ja",admin=True): self.ui_language=language; self.admin=admin
    @property
    async def is_admin(self): return self.admin


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.asyncio
async def test_voice_admin_producer_denials_and_compatibility(language,monkeypatch):
    from integrations.elevenlabs import admin_routes as routes
    from ai_runtime.voice_resolution import CanonicalVoiceResolutionError
    from fastapi import HTTPException
    request=Request(dict(type="http",method="GET",path="/admin/elevenlabs-agents",headers=[]))
    with pytest.raises(HTTPException) as denied:
        await routes.admin_elevenlabs_agents(request,Operator(language,False))
    assert denied.value.status_code==403
    assert denied.value.detail==Translator(language).render("management_operations_errors.admin_required")
    monkeypatch.setattr(routes,"resolve_prompt_voice",AsyncMock(side_effect=CanonicalVoiceResolutionError("canonical_voice_deprecated","internal diagnostic")))
    result=await routes._prompt_webrtc_voice_status(None,41,Translator(language).t)
    assert not result["webrtc_compatible"]
    assert result["compatibility_reason"]==Translator(language).render("admin_voice_integrations.voice.canonical_voice_deprecated")
    monkeypatch.setattr(routes,"get_elevenlabs_key",lambda: None)
    response=await routes.get_elevenlabs_voices(request,Operator(language))
    assert response.status_code==500
    assert json.loads(response.body)["error"]==Translator(language).render("admin_voice_integrations.error.api_key")


@pytest.mark.parametrize("language", LANGUAGES)
def test_telephony_typed_errors_preserve_status_and_materialized_state(language):
    from integrations.telephony.admin_routes import _error_response
    from integrations.telephony.admin_service import TelephonyAdminConflict,TelephonyAdminMaterializedError
    request=Request(dict(type="http",method="POST",path="/admin/telephony/config",headers=[]))
    key="admin_telephony_errors.error.the_outbound_default_must_remain_enabled"
    exc=TelephonyAdminConflict("The outbound default must remain enabled",message_key=key)
    response=_error_response(exc,request,Operator(language))
    assert response.status_code==409 and str(exc)=="The outbound default must remain enabled"
    assert json.loads(response.body)["detail"]==Translator(language).render(key)
    exc=TelephonyAdminMaterializedError("private provider diagnostic",materialized_state="synced")
    response=_error_response(exc,request,Operator(language))
    assert response.status_code==503 and exc.materialized_state=="synced"
    assert json.loads(response.body)["detail"]==Translator(language).render("admin_telephony.error.materialized")
