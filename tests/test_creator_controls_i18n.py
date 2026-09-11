"""Creator controls render translated units without changing input or content."""
import json
import shutil
import subprocess

import pytest
from jinja2 import Environment, FileSystemLoader

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize('language', LANGUAGES)
def test_creator_controls_render_and_keep_payloads(language, tmp_path):
    tr = Translator(language)
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    env.globals.update(t=tr.render, t_html=tr.html, format_number=tr.format_number,
                       format_currency=tr.format_currency, i18n_payload=tr.browser_payload,
                       get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    fixture = dict(ui_language=language, username='Fixture', is_admin=True, is_user=True,
                   marketplace={'discovery_enabled': True, 'creator_tools_enabled': True},
                   curation_data=dict(pending_earnings=12.34, referral_markup_per_mtokens=0.012345),
                   prompts=[dict(id=41, name='Original <prompt>', description='Creator text',
                                 voice_name='Original voice', owner_username='Owner', public=True, can_edit=True)])
    pages = {name: env.get_template(name + '.html').render(**fixture)
             for name in ['curation_settings', 'prompts/prompt_list']}
    assert 'value="0.012345"' in pages['curation_settings']
    assert 'Original &lt;prompt&gt;' in pages['prompts/prompt_list']
    assert 'data-public="public"' in pages['prompts/prompt_list']
    assert 'name="selected_prompts" value="41"' in pages['prompts/prompt_list']
    path = tmp_path / 'creator.json'
    path.write_text(json.dumps(pages), encoding='utf-8')
    node = shutil.which('node') or 'C:/Program Files/nodejs/node.exe'
    result = subprocess.run([node, 'tests/js/creator_controls_rendered.cjs', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
