"""Exercise the real public routes and the owned shell around creator HTML."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from auth import get_current_user
from i18n import LANGUAGES, Translator
from legal.routes import router as legal_router
from marketplace.routes.marketing import router as marketing_router
from marketplace.landing import rendering
from public_pages import public_page


@pytest.fixture
def public_client(monkeypatch):
    for key in ("MARKETPLACE_ENABLED", "MARKETPLACE_CREATOR_TOOLS_ENABLED", "MARKETPLACE_DISCOVERY_ENABLED"):
        monkeypatch.setenv(key, "true")
    app = FastAPI()
    app.include_router(legal_router)
    app.include_router(marketing_router)
    app.dependency_overrides[get_current_user] = lambda: None

    @app.get("/")
    def root(request: Request):
        return public_page(request, "index.html")

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("language", LANGUAGES)
def test_real_public_routes_render_localized_metadata_body_and_aliases(public_client, language):
    routes = {
        "/": ("public_home.aurvek_ai_infrastructure_platform", "/index.html"),
        "/for-creators": ("public_creators", "/for-creators-landing.html"),
        "/for-teams": ("public_teams", "/for-teams-landing.html"),
        "/for-agencies": ("public_agencies", "/for-agencies-landing.html"),
        "/explore-landing": ("public_explore", "/explore-landing.html"),
        "/infrastructure-landing": ("public_infrastructure", "/infrastructure-landing.html"),
        "/privacy": ("public_privacy.privacy_policy", "/privacy.html"),
        "/terms": ("public_terms", "/terms.html"),
        "/support": ("public_shell.support", None),
    }
    from i18n import get_catalogs
    catalogs = get_catalogs()
    for url, (key, alias) in routes.items():
        domain = key.split(".", 1)[0]
        assert set(catalogs[language][domain]) == set(catalogs["en"][domain])
        if "." not in key:
            key += "." + next(iter(catalogs["en"][key]))
        response = public_client.get(url, headers={"Accept-Language": language})
        assert response.status_code == 200, url
        assert f'<html lang="{language}">' in response.text
        # HTML entities are escaped by Jinja; parsed text and attributes must agree.
        from html import unescape
        assert Translator(language).render(key) in unescape(response.text)
        assert response.headers["Content-Language"] == language
        assert "private" in response.headers["Cache-Control"]
        assert {"Cookie", "Accept-Language", "Authorization"} <= set(response.headers["Vary"].split(", "))
        assert "{{ t(" not in response.text and "{tag_" not in response.text
        if alias and url != "/":
            assert public_client.get(alias, headers={"Accept-Language": language}).text == response.text


def test_public_locale_precedence_and_support_escaping(public_client, monkeypatch):
    monkeypatch.delenv("SUPPORT_URL", raising=False)
    monkeypatch.setenv("MOBILE_SUPPORT_EMAIL", '<script>alert("x")</script>@example.com')
    public_client.cookies.set("ui_language", "ja")
    response = public_client.get("/support", headers={"Accept-Language": "de"})
    assert response.headers["Content-Language"] == "ja"
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text
    public_client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(ui_language="pt")
    assert public_client.get("/support").headers["Content-Language"] == "pt"
    monkeypatch.setenv("SUPPORT_URL", "https://support.example.com/help?token=unchanged")
    redirect = public_client.get("/support", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["Location"] == "https://support.example.com/help?token=unchanged"


def test_marketing_feature_gates_remain_in_effect(public_client, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_CREATOR_TOOLS_ENABLED", "false")
    assert public_client.get("/for-creators").status_code == 404
    assert public_client.get("/for-agencies").status_code == 404
    monkeypatch.setenv("MARKETPLACE_DISCOVERY_ENABLED", "false")
    assert public_client.get("/explore-landing").status_code == 404
    assert public_client.get("/privacy").status_code == 200


@pytest.mark.asyncio
async def test_related_shell_cache_separates_languages_without_evaluating_creator_html(tmp_path, monkeypatch):
    path = tmp_path / "home.html"
    original = '<html lang="en"><body>{{ creator_expression }}<p>Creator original</p></body></html>'
    path.write_text(original, encoding="utf-8")
    calls = []

    async def related(*args):
        calls.append(args)
        return [{"name": '<Creator & name>', "url": "/p/unchanged/"}]

    monkeypatch.setattr(rendering, "get_related_landing_links", related)
    monkeypatch.setattr(rendering, "LANDING_RELATED_LINKS_ENABLED", True)
    rendering.clear_landing_render_cache()
    try:
        for locale in ("ja", "es", "ja"):
            body = await rendering.render_prompt_landing_html(path, 42, page="home", is_preview=False, is_unlisted=False, ui_language=locale)
            assert Translator(locale).t("public_shell.related") in body
            assert "{{ creator_expression }}" in body and "Creator original" in body
            assert "&lt;Creator &amp; name&gt;" in body
            assert 'href="/p/unchanged/"' in body
        assert len(calls) == 2
    finally:
        rendering.clear_landing_render_cache()


@pytest.mark.parametrize("language", LANGUAGES)
def test_world_navbar_is_rendered_and_fail_closed_pages_are_localized(language):
    from welcome_service import render_aurvek_world_navbar
    from marketplace.landing.isolation import creator_content_unavailable_response
    request = Request({"type": "http", "headers": [(b"accept-language", language.encode())]})
    navbar = render_aurvek_world_navbar(request)
    assert Translator(language).t("public_shell.classic_home") in navbar
    assert 'id="aurvek-i18n"' in navbar
    assert "{{" not in navbar
    for response in (rendering.landing_404_response(Translator(language)), creator_content_unavailable_response(Translator(language))):
        assert response.headers["Content-Language"] == language
        assert response.headers["Cache-Control"] == "no-store"


def test_nginx_does_not_serve_owned_templates_as_static_html():
    main = Path("nginx/aurvek-main.conf").read_text(encoding="utf-8")
    cdn = Path("nginx/aurvek-cdn.conf").read_text(encoding="utf-8")
    assert "location = /for-teams { include {{SNIPPETS_PATH}}/fastapi-proxy.conf; }" in main
    assert "proxy_pass http://127.0.0.1:{{FASTAPI_PORT}}/infrastructure-landing;" in cdn
    assert "try_files /infrastructure-landing.html" not in cdn
    for name in ("index", "privacy", "terms", "for-teams-landing", "for-creators-landing", "for-agencies-landing", "explore-landing", "infrastructure-landing"):
        assert not Path(f"data/{name}.html").exists()
