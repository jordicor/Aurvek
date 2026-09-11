"""Render the five billing/usage pages with their real catalogs and base templates."""
import json
import re

import pytest
from jinja2 import Environment, FileSystemLoader

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('name', ['my_usage', 'user_team_consumption', 'admin_usage', 'admin_pricing', 'admin_storage_quotas'])
def test_usage_pages_render_real_templates_and_keep_input_values(language, name):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    html = env.get_template(name + '.html').render(
        ui_language=language, username='Fixture', is_admin=True, is_user=True,
        marketplace={'discovery_enabled': True, 'creator_tools_enabled': True},
        pricing_config={'pricing_margin_free': {'value': '12.345'}},
    )
    payload = json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>', html, re.S)[1])
    assert set(payload['resources'][language]) == {'common', 'navigation', 'admin_usage'}
    if name == 'admin_pricing':
        assert 'value="12.345"' in html
        assert tr.format_currency(10, 'USD', fraction_digits=0) in html
        assert 'name="pricing_commission"' in html
    if name == 'user_team_consumption':
        assert tr.render('admin_usage.team.estimated_cost') in html
        assert '<a href="/create-user">' in html
        assert tr.render('common.error.generic') not in html.split('<script')[0]
    if name == 'admin_storage_quotas':
        assert 'value="250"' in html
        assert 'id="defaultQuotaGb"' in html
