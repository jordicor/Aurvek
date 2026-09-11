"""Managed-account presentation using rendered templates and isolated fixtures."""
import json
import re
import sqlite3

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

from i18n import LANGUAGES, Translator
from tests.test_user_management_security import DummyUser, _update_kwargs, user_management_db


def render_users_page(name, language):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
        format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
        get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    user = dict(id=7, username='OriginalUser', role='user', role_name='user', role_id=2,
        phone='+34931234567', phone_number='+34931234567', email='original@example.test',
        is_enabled=True, is_expired='Active', authentication_mode='magic_link_password',
        magic_link='https://example.test/login?token=original', auth_provider='google_linked',
        prompt_name='Original <prompt>', llm_model='Original model', current_prompt_id=5, llm_id=8,
        tokens=12345, total_cost=.012345, balance=12.345, conversation_count=2,
        billing_account_id=1,billing_limit=0,billing_limit_action='auto_refill',
        billing_auto_refill_amount=1.5,billing_max_limit=None,storage_quota_bytes=1610612736)
    return env.get_template(name+'.html').render(ui_language=language,username='Admin',
        is_admin=True,is_user=True,marketplace={'discovery_enabled':True,'creator_tools_enabled':True},
        users=[user],user_data=user,user_roles=[{'id':2,'name':'user'}],
        prompts=[{'id':5,'name':'Original <prompt>','text':'Original <prompt>'}],
        llm_models=[(8,'Original provider','Original model',True)],categories=[],
        storage_quota_default_bytes=1610612736,ultra_admin_elevated=False,ultra_admin_ttl=0)


@pytest.mark.parametrize('language',LANGUAGES)
@pytest.mark.parametrize('name',['create_user','edit_user','users_list'])
def test_management_forms_keep_numeric_inputs_and_wire_enums(language,name):
    html=render_users_page(name,language)
    tr=Translator(language)
    payload=json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>',html,re.S)[1])
    assert set(payload['resources'][language])=={'common','navigation','admin_users'}
    assert set(payload['resources'][language]['admin_users'])==set(payload['resources']['en']['admin_users'])
    if name=='users_list':
        assert 'data-role="user"' in html and 'data-cost="0.012345"' in html
        assert tr.format_currency(.012345,'USD',fraction_digits=4) in html
        assert tr.format_currency(12.345,'USD') in html
        assert 'Original &lt;prompt&gt;' in html
    elif name=='edit_user':
        assert 'value="12.345"' in html and 'value="1.5"' in html
        assert 'id="billingLimitInput" name="billing_limit" value="0"' in html
        assert tr.render('admin_users.flow.storage_hint',quota=tr.render('admin_users.flow.storage_gb',amount=tr.format_number(1.5,1,1))) in html
    else:
        assert 'value="magic_link_only"' in html and 'value="user_pays"' in html
        assert 'localizedCountries' in html


@pytest.mark.asyncio
@pytest.mark.parametrize('amount',['NaN','Infinity'])
async def test_billing_nonfinite_rejected_before_account_mutation(user_management_db,amount):
    import app
    kwargs=_update_kwargs('assigned-customer',10)
    actor=DummyUser(1,'admin',admin=True);actor.ui_language='es'
    kwargs.update(current_user=actor,billing_mode='user_pays',billing_limit=amount)
    with pytest.raises(HTTPException) as caught:
        await app.update_user(**kwargs)
    assert caught.value.status_code==400
    assert caught.value.detail==Translator('es').render('admin_users.response.invalid_billing_amount')
    with sqlite3.connect(user_management_db) as conn:
        assert conn.execute('SELECT billing_account_id,billing_limit FROM USER_DETAILS WHERE user_id=20').fetchone()==(None,None)


@pytest.mark.asyncio
async def test_creator_permission_response_localized_with_same_live_authorization(user_management_db):
    import app
    kwargs=_update_kwargs('unassigned-customer',10)
    kwargs['current_user'].ui_language='es'
    with pytest.raises(HTTPException) as caught:
        await app.update_user(**kwargs)
    assert caught.value.status_code==403
    assert caught.value.detail==Translator('es').render('admin_users.response.you_do_not_have_permission_to_manage_this_user')
