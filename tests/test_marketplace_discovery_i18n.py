"""Home/Explore render their actual base and only the requested locale catalogs."""

import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import escape

from i18n import LANGUAGES, Translator


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("page", ["home", "explore"])
def test_discovery_template_locale_payload_and_accessible_controls(language, page):
    root = Path(__file__).parents[1]
    environment = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape())
    translator = Translator(language)
    html = environment.get_template(f"{page}.html").render(
        t=translator.render, ui_language=language, i18n_payload=translator.browser_payload,
        get_static_url=lambda url: url, get_static_theme_hashes=lambda: {},
        marketplace={"enabled": True, "discovery_enabled": True},
    )
    assert f'<html lang="{language}"' in html
    assert f'<title>{escape(translator.t(f"marketplace.{page}.title"))} - AURVEK</title>' in html
    payload = json.loads(re.search(r'<script type="application/json" id="aurvek-i18n">(.*?)</script>', html, re.S).group(1))
    assert payload["language"] == language
    assert set(payload["resources"]) == {"en", language}
    assert set(payload["resources"][language]) == {"common", "navigation", "marketplace"}
    assert html.index('/static/js/common/i18n.js') < html.index(f'/static/js/{page}.js')
    accessible_key = "home.select_prompt" if page == "home" else "explore.landing_preview"
    assert str(escape(translator.t(f"marketplace.{accessible_key}"))) in html


def test_marketplace_literal_browser_references_exist_in_every_catalog():
    root = Path(__file__).parents[1]
    references = set()
    for page in ("home", "explore"):
        source = (root / f"data/static/js/{page}.js").read_text(encoding="utf-8")
        references.update(re.findall(r"\b(?:mt|mh)\(['\"]([^'\"]+)['\"]", source))
    for language in LANGUAGES:
        messages = json.loads((root / f"locales/{language}/marketplace.json").read_text(encoding="utf-8"))
        assert references <= messages.keys()
