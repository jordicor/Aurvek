"""Storefront, analytics and creator billing presentation with real renderers/routes."""

import json
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from billing import connect, creator_payouts
from billing.routes import creator_payouts as billing_routes
from i18n import LANGUAGES, Translator, read_catalogs
from marketplace import config
from marketplace.routes import analytics, storefronts
from tests.test_marketplace_admin_i18n import Creator
from tests.test_marketplace_checkout_i18n import request


ROOT = Path(__file__).parents[1]
PAGES = ['storefront.html', 'my_storefront.html', 'creator_earnings.html', 'user_landing_analytics.html']


def render(page, language):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=select_autoescape())
    name = 'Creator " \' <script> & ${unchanged}'
    store = dict(
        profile=dict(display_name=name, slug='creator-slug', bio='Creator bio', is_verified=True,
                     social_links={'website': 'https://example.org', 'linkedin': 'https://linkedin.com'}),
        avatar_url=None, stats=dict(prompt_count=1200, pack_count=1),
        packs=[dict(id=20, name=name, description='Pack description', item_count=2, is_paid=True,
                    price=1234.50, public_id='pack-public', slug='pack-slug', cover_image=None)],
        prompts=[dict(id=10, name=name, description='Prompt description', is_paid=True,
                      purchase_price=5, public_id='prompt-public', slug='prompt-slug', image=None)],
        viewer_access=dict(pack_ids=[20], prompt_ids=[10]),
    )
    return env.get_template(page).render(
        t=tr.render, t_html=tr.html, format_number=tr.format_number, format_currency=tr.format_currency,
        ui_language=language, i18n_payload=tr.browser_payload,
        get_static_url=lambda url: url, get_static_theme_hashes=lambda: {},
        marketplace={'enabled': True, 'discovery_enabled': True},
        request=request(language=language), storefront=store,
    )


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('page', PAGES)
def test_pages_render_full_base_and_only_required_domains(page, language):
    html = render(page, language)
    domain = 'marketplace_storefront' if 'storefront' in page else 'marketplace_earnings'
    payload = json.loads(re.search(r'id="aurvek-i18n">(.*?)</script>', html, re.S)[1])
    assert set(payload['resources'][language]) == {'common', 'navigation', domain}
    body = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)
    assert domain + '.' not in body
    if page == 'storefront.html':
        assert str(escape('Creator " \' <script> & ${unchanged}')) in html
        assert '/pack/pack-public/pack-slug/' in html
        assert str(escape(Translator(language).format_currency(1234.50))) in html
    expected_ids = {
        'my_storefront.html': ['previewName', 'previewAvatar', 'displayName', 'slugInput'],
        'creator_earnings.html': ['totalEarned', 'thisMonth', 'withdrawBtn'],
        'user_landing_analytics.html': ['todayVisitors', 'tabPrompts', 'noPacksMessage', 'modalPromptName'],
    }
    for element_id in expected_ids.get(page, []):
        assert f'id="{element_id}"' in html
    # Every actual inline script is parsed in every locale, including French apostrophes.
    for attributes, source in re.findall(r'<script\b([^>]*)>(.*?)</script>', html, re.S):
        if 'application/json' in attributes or not source.strip():
            continue
        result = subprocess.run(['node', '--check'], input=source, text=True, capture_output=True)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('language', LANGUAGES)
def test_catalogs_have_complete_units_in_every_language(language):
    for domain in ['marketplace_storefront', 'marketplace_earnings']:
        english = json.loads((ROOT / 'locales/en' / f'{domain}.json').read_text(encoding='utf-8'))
        localized = json.loads((ROOT / 'locales' / language / f'{domain}.json').read_text(encoding='utf-8'))
        assert set(localized) == set(english)
        if language != 'en':
            assert localized['my_storefront' if domain.endswith('storefront') else 'creator_earnings'] != english['my_storefront' if domain.endswith('storefront') else 'creator_earnings']


def test_actual_catalog_loader_and_literal_consumers_agree():
    # The normal loader rejects duplicate keys, invalid placeholders and incompatible units.
    catalogs = read_catalogs(ROOT / 'locales')
    consumers = [ROOT / 'templates' / page for page in PAGES] + [ROOT / path for path in (
        'marketplace/routes/storefronts.py', 'marketplace/routes/analytics.py',
        'billing/routes/creator_payouts.py', 'billing/connect.py', 'billing/creator_payouts.py',
    )]
    for path in consumers:
        for domain, key in re.findall(r"['\"](marketplace_(?:storefront|earnings))\.([^'\"]+)['\"]", path.read_text(encoding='utf-8')):
            assert key in catalogs['en'][domain], (path.name, key)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_storefront_request_bound_validation_without_database(monkeypatch, language):
    monkeypatch.setattr(config, 'marketplace_storefronts_enabled', lambda: True)
    db = Mock(side_effect=AssertionError('Validation must not query the DB'))
    monkeypatch.setattr(storefronts, 'get_db_connection', db)
    tr = Translator(language)
    response = await storefronts.update_creator_profile(request({'display_name': ''}, language='en'), Creator(language))
    assert response.status_code == 400
    assert json.loads(response.body) == {'error': tr.t('marketplace_storefront.display_name_is_required_max_200_characters')}
    with pytest.raises(HTTPException) as exc:
        await storefronts.get_creator_profile_api(request(language=language), None)
    assert exc.value.status_code == 401
    assert exc.value.detail == tr.t('marketplace_storefront.authentication_required')
    monkeypatch.setattr(storefronts, 'get_creator_profile_by_slug', AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await storefronts.creator_storefront(request(language=language), 'missing', None)
    assert exc.value.status_code == 404
    assert exc.value.detail == tr.t('marketplace_storefront.creator_not_found')
    db.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
async def test_analytics_and_connect_producer_errors_preserve_contracts(monkeypatch, language):
    monkeypatch.setattr(config, 'marketplace_creator_tools_enabled', lambda: True)
    monkeypatch.setattr(config, 'marketplace_public_landings_enabled', lambda: True)
    tr = Translator(language)
    response = await analytics.track_landing_visit(request({'prompt_id': 10, 'pack_id': 20}, language=language))
    assert response.status_code == 400
    assert json.loads(response.body)['error'] == tr.t('marketplace_earnings.cannot_track_both_prompt_id_and_pack_id_in_a_single_visit')
    response = await analytics.get_pack_landing_analytics(request(language=language), None)
    assert response.status_code == 401
    assert json.loads(response.body) == {'error': 'unauthenticated', 'redirect': '/login'}
    monkeypatch.setattr(connect, 'STRIPE_SECRET_KEY', None)
    monkeypatch.setattr(creator_payouts, 'STRIPE_SECRET_KEY', None)
    response = await billing_routes.stripe_connect_onboard(request(language='en'), Creator(language))
    assert response.status_code == 503
    assert json.loads(response.body) == {'success': False, 'message': tr.t('marketplace_earnings.stripe_not_configured')}
    response = await billing_routes.request_creator_payout(request(language='en'), Creator(language))
    assert response.status_code == 503
    assert json.loads(response.body) == {'success': False, 'message': tr.t('marketplace_earnings.payment_system_not_configured')}


@pytest.mark.asyncio
@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('outcome', ['paid', 'failed', 'ambiguous'])
async def test_reserved_payout_keeps_amount_idempotency_and_failure_state(monkeypatch, language, outcome):
    queries = []

    class Connection:
        async def execute(self, query, params=()):
            queries.append((query, params))
            return SimpleNamespace(rowcount=1)

        async def commit(self):
            pass

    @asynccontextmanager
    async def connection():
        yield Connection()

    transfer = Mock(return_value=SimpleNamespace(id='tr_fixture'))
    if outcome == 'failed':
        transfer.side_effect = creator_payouts.stripe.error.InvalidRequestError('PROVIDER_SECRET', 'amount')
    elif outcome == 'ambiguous':
        transfer.side_effect = creator_payouts.stripe.error.APIConnectionError('PROVIDER_SECRET')
    monkeypatch.setattr(creator_payouts, 'get_db_connection', connection)
    monkeypatch.setattr(creator_payouts.stripe.Transfer, 'create', transfer)
    tr = Translator(language)
    response = await creator_payouts._complete_reserved_payout(
        current_user=Creator(language), pending=1234.50, connect_account_id='acct_fixture',
        payout_tx_id=9, idempotency_key='same-idempotency-key', translator=tr,
    )
    body = json.loads(response.body)
    assert transfer.call_args.kwargs['amount'] == 123450
    assert transfer.call_args.kwargs['currency'] == 'usd'
    assert transfer.call_args.kwargs['idempotency_key'] == 'same-idempotency-key'
    assert 'PROVIDER_SECRET' not in body['message']
    if outcome == 'paid':
        assert response.status_code == 200
        assert body == {'success': True, 'amount': 1234.50, 'transfer_id': 'tr_fixture',
                        'message': tr.t('marketplace_earnings.payout_delivered', amount=tr.format_currency(1234.50))}
        assert any("type = 'payout_completed'" in sql and params == ('tr_fixture', 9) for sql, params in queries)
    elif outcome == 'failed':
        assert response.status_code == 400 and body['success'] is False
        assert body['message'] == tr.t('marketplace_earnings.payout_preserved')
        assert any('pending_earnings = pending_earnings + ?' in sql and params == (1234.50, 1) for sql, params in queries)
    else:
        assert response.status_code == 503 and body['success'] is False
        assert body['message'] == tr.t('marketplace_earnings.payout_confirming')
        assert any("type = 'payout_pending'" in sql for sql, params in queries)
        assert not any('pending_earnings = pending_earnings + ?' in sql for sql, params in queries)


@pytest.mark.parametrize('language', LANGUAGES)
def test_actual_analytics_and_earnings_javascript(language):
    scripts = []
    for page in ['user_landing_analytics.html', 'creator_earnings.html']:
        html = render(page, language)
        scripts.append(re.findall(r'<script>(.*?)</script>', html, re.S)[-1])
    payload = Translator(language).browser_payload(['marketplace_earnings'])
    runner = r'''
const vm = require('vm');
const assert = require('assert');
const {create} = require('./data/static/js/common/i18n.js');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function harness(source) {
    const elements = new Map();
    function node() { return {textContent:'', innerHTML:'', disabled:false, style:{}}; }
    const document = {
        addEventListener(){},
        getElementById(id) { if (!elements.has(id)) elements.set(id,node()); return elements.get(id); },
        createElement() { return {set textContent(value) {this.innerHTML=String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}; }
    };
    const context = {document, AurvekI18n:create(input.payload), console, window:{}, URLSearchParams, Date};
    vm.createContext(context); vm.runInContext(source, context);
    return {context,elements};
}
const {context:a,elements} = harness(input.scripts[0]);
const name = 'Creator " \' <img src=x onerror=bad()> ${untouched}';
a.updatePromptsTable([{id:10,name,today_visitors:1234,week_visitors:20,month_visitors:40,conversions:2,conversion_rate:2.5}]);
const row = elements.get('promptsTableBody').innerHTML;
assert(row.includes(a.AurvekI18n.formatNumber(1234)));
assert(row.includes('&quot;'));
assert(!row.includes('<img src=x'));
assert(row.includes(a.AurvekI18n.formatNumber(.025,{style:'percent',maximumFractionDigits:1})));
const entities = {quot:'"','#39':"'",lt:'<',gt:'>',amp:'&'};
const onclick = row.match(/onclick="([^"]+)"/)[1].replace(/&(quot|#39|lt|gt|amp);/g,(_,key)=>entities[key]);
let selected;
a.showPromptDetail = (id, name) => {selected={id,name};};
vm.runInContext('(function(){'+onclick+'})()',a);
assert.deepEqual(selected,{id:10,name});
a.updateChart([{date:'2026-09-08',visits:1234}]);
assert(elements.get('visitsChart').innerHTML.includes(a.formatDay('2026-09-08')));
a.updateReferrers([{referrer:'direct',is_direct:true,count:12},{referrer:'direct',is_direct:false,count:1}]);
assert(elements.get('referrersList').innerHTML.includes(a.AurvekI18n.t('marketplace_earnings.direct')));
a.updatePromptsTable([]);
assert.equal(elements.get('noPromptsMessage').style.display,'block');
const {context:e,elements:ee} = harness(input.scripts[1]);
vm.runInContext('connectState={connected:true,payouts_enabled:true}; pendingEarningsAmount=12.34; updateWithdrawButton();',e);
assert(ee.get('withdrawBtn').disabled);
assert.equal(ee.get('withdrawNote').textContent,e.AurvekI18n.t('marketplace_earnings.remaining',{remaining:e.formatCurrency(37.66),minimum:e.formatCurrency(50)}));
vm.runInContext('pendingEarningsAmount=50; updateWithdrawButton();',e);
assert(!ee.get('withdrawBtn').disabled);
assert.equal(e.formatCurrency(1234.50),e.AurvekI18n.formatCurrency(1234.50,'USD',2));
assert.equal(e.formatTokens(1234567),e.AurvekI18n.formatNumber(1234567,{notation:'compact',maximumFractionDigits:1}));
(async () => {
    a.fetch = async () => ({ok:false});
    await a.loadPackAnalytics();
    assert(!a.window._packAnalyticsLoaded);
    assert(elements.get('packsTableBody').innerHTML.includes(a.AurvekI18n.t('marketplace_earnings.failed_to_load_pack_analytics')));
    assert.notEqual(a.document.getElementById('noPacksMessage').style.display,'block');
    a.fetch = async () => ({ok:true,json:async()=>({packs:[]})});
    await a.loadPackAnalytics();
    assert(a.window._packAnalyticsLoaded);
    assert.equal(elements.get('noPacksMessage').style.display,'block');
    e.fetch = async () => ({ok:true,json:async()=>({connected:true,payouts_enabled:true})});
    await e.loadConnectStatus();
    assert(ee.get('connectStatus').innerHTML.includes(e.escapeHtml(e.AurvekI18n.t('marketplace_earnings.connected_ready'))));
})().catch(error => {console.error(error);process.exitCode=1;});
'''
    result = subprocess.run(['node', '-e', runner], input=json.dumps({'scripts': scripts, 'payload': payload}),
                            text=True, capture_output=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr
