import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment
from markupsafe import Markup
from starlette.requests import Request

import i18n


def test_display_formatting_preserves_currency_precision_and_numeric_values():
    amount = Decimal("0.00001234")
    en, es, ja = (i18n.Translator(language) for language in ("en", "es", "ja"))
    assert en.format_currency(amount, "USD", 6) == "$0.000012"
    assert es.format_currency(amount, "USD", 6).startswith("0,000012")
    assert ja.format_currency(Decimal("12.34"), "USD", 2) == "$12.34"
    assert en.format_currency(Decimal("12.34"), "JPY", 2) == "¥12.34"
    assert en.format_number(12345.5, 2, 2) == "12,345.50"
    assert es.format_number(12345.5, 2, 2) == "12.345,50"
    assert en.format_currency(-2, "USD", 2) == "-$2.00"
    assert amount == Decimal("0.00001234")
    with pytest.raises(ValueError):
        en.format_currency(amount, "USD", -1)


CASES = json.loads((Path(__file__).parent / "fixtures/i18n_cases.json").read_text(encoding="utf-8"))


@pytest.fixture
def catalogs(tmp_path, monkeypatch):
    for language, domains in CASES["catalogs"].items():
        (tmp_path / language).mkdir()
        for domain, messages in domains.items():
            (tmp_path / language / f"{domain}.json").write_text(json.dumps(messages), encoding="utf-8")
    result = i18n.read_catalogs(tmp_path)
    monkeypatch.setattr(i18n, "get_catalogs", lambda: result)
    return result


@pytest.mark.parametrize("case", CASES["cases"])
def test_shared_python_browser_contract(catalogs, case):
    assert i18n.Translator(case["language"]).render(case["key"], **case["params"]) == case["expected"]


@pytest.mark.parametrize(("cookie", "header", "expected"), [
    ("ja-JP", "es", "ja"), ("unsupported", "de-DE;q=0.8,fr;q=0.9", "fr"),
    (None, "en;q=0,es;q=0.5", "es"), (None, "es;q=0,*;q=0.8", "en"),
    (None, "es;q=0,es-ES;q=1", "es"), (None, "fr;q=oops,de;q=0.2", "de"),
    (None, "pt-BR", "pt"), ("../../es", "ja", "ja"), (None, "", "en"),
    (None, "es-MX;q=0.1,es-AR;q=0.9,de;q=0.5", "es"),
    (None, "es-AR;q=0.9,es-MX;q=0.1,de;q=0.5", "es"),
    (None, "es-MX;q=0,es-AR;q=1,de;q=0.5", "es"),
    (None, "es-AR;q=1,es-MX;q=0,de;q=0.5", "es"),
    (None, "es-ES;q=0,es-MX;q=1,de;q=0.5", "de"),
])
def test_language_negotiation(cookie, header, expected):
    assert i18n.negotiate_language(cookie, header) == expected


@pytest.mark.parametrize(("key", "params"), [
    ("sample.items", {}), ("sample.items", {"count": True}),
    ("sample.items", {"count": float("nan")}), ("sample.hello", {}),
    ("sample.hello", {"name": "private", "extra": "private"}),
    ("sample.hello", {"name": {"private": "value"}}), ("missing.key", {}),
])
def test_usage_errors_are_explicit_and_ui_fallback_does_not_log_values(catalogs, caplog, key, params):
    translator = i18n.Translator("es")
    with pytest.raises(ValueError):
        translator.render(key, **params)
    assert translator.t(key, **params) == "Se produjo un error."
    assert "private" not in caplog.text


def test_autoescape_and_payload_are_safe_without_interpreting_catalog_html(catalogs):
    translator = i18n.Translator("en")
    value = '</script><img src=x onerror="alert(1)">'
    template = Environment(autoescape=True).from_string(
        '<p>{{ t("sample.hello", name=value) }}</p>'
        '<script type="application/json">{{ payload|tojson }}</script>'
    )
    result = template.render(t=translator.t, value=value, payload={"value": value})
    assert "<img" not in result
    assert result.count("</script>") == 1
    assert "&lt;img" in result and "\\u003cimg" in result


def test_html_messages_escape_catalog_and_values_but_keep_template_links(monkeypatch):
    messages = {"common": {"error.generic": "Unable to display this message."}, "auth": {
        "link": '<b>{name}</b>: {link} {{literal}}',
    }}
    monkeypatch.setattr(i18n, "get_catalogs", lambda: {"en": messages, "ja": {
        "auth": {"link": '{link} — <b>{name}</b> {{literal}}'},
    }})
    translator = i18n.Translator("ja")
    template = Environment(autoescape=True).from_string(
        '{% macro link() %}<a href="{{ url }}">Log in</a>{% endmacro %}'
        '{{ t_html("auth.link", name=name, link=link()) }}'
    )
    result = template.render(t_html=translator.html, url='/login?next="<tag>', name='<img onerror="x">')
    assert result.startswith('<a href="/login?next=&#34;&lt;tag&gt;">Log in</a>')
    assert '<b>' not in result and '<img' not in result
    assert '&lt;b&gt;' in result and '&lt;img' in result and '{literal}' in result
    assert isinstance(translator.html("auth.link", name="A", link=Markup('<a href="/login">Log in</a>')), Markup)
    assert '&lt;a' in translator.html("auth.link", name="A", link='<a href="/login">Log in</a>')
    assert translator.html("auth.link", name="private") == "Unable to display this message."


@pytest.mark.asyncio
async def test_concurrent_request_locales_reuse_identity_and_do_not_leak(catalogs):
    async def render(language):
        request = Request({"type": "http", "headers": [(b"accept-language", b"de")]})
        user = SimpleNamespace(ui_language=language, preferred_languages_json='["fr"]')
        translator = i18n.get_translator(request, user)
        await asyncio.sleep(0)
        context = i18n.template_context(request)
        assert i18n.get_translator(request) is translator
        return context["ui_language"], context["t"]("sample.hello", name="A")

    assert await asyncio.gather(render("es"), render("ja")) == [("es", "Hola A"), ("ja", "こんにちは、A")]


def test_browser_payload_is_limited_to_required_domains_and_cannot_mutate_cache(catalogs):
    payload = i18n.Translator("ja").browser_payload(["sample"])
    assert set(payload["resources"]) == {"en", "ja"}
    assert set(payload["resources"]["ja"]) == {"common", "sample"}
    payload["resources"]["ja"]["sample"]["items"]["other"] = "changed"
    assert i18n.Translator("ja").t("sample.items", count=2) == "2 件"
    with pytest.raises(TypeError):
        catalogs["ja"]["sample"]["hello"] = "changed"


def test_real_catalogs_are_reused_without_file_reads_per_message(monkeypatch):
    loaded = i18n.get_catalogs()

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Translation reread a catalog")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    for language in ("en", "es", "ja"):
        translator = i18n.Translator(language)
        assert translator.t("account.password.requirement.min_length", count=8)
        assert translator.browser_payload(["account"])["language"] == language
        assert i18n.get_catalogs() is loaded


@pytest.mark.parametrize("text", [
    '{"error.generic":"Error", "error.generic":"Duplicate"}',
    '{"error.generic":"Error", "bad":"{value.name}"}',
    '{"error.generic":"Error", "bad":{"one":"{count} item"}}',
    '{"error.generic":"Error", "bad":{"one":"{count} item","other":"Items"}}',
])
def test_invalid_catalogs_fail_before_publication(tmp_path, text):
    (tmp_path / "en").mkdir()
    (tmp_path / "en/common.json").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        i18n.read_catalogs(tmp_path)
