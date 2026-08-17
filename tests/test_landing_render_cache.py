from __future__ import annotations

import asyncio

import pytest
from cachetools import TTLCache

from marketplace.landing import rendering


@pytest.mark.asyncio
async def test_rendered_landing_cache_avoids_reloads_and_related_queries(
    tmp_path, monkeypatch
):
    html_path = tmp_path / "home.html"
    html_path.write_text("<html><body>first</body></html>", encoding="utf-8")
    rendering.clear_landing_render_cache()
    monkeypatch.setattr(rendering, "LANDING_RELATED_LINKS_ENABLED", True)
    calls = 0

    async def related(prompt_id, max_links):
        nonlocal calls
        calls += 1
        return [{"name": "Related", "url": "/p/related/"}]

    monkeypatch.setattr(rendering, "get_related_landing_links", related)
    try:
        first = await rendering.render_prompt_landing_html(
            html_path,
            42,
            page="home",
            is_preview=False,
            is_unlisted=False,
        )
        second = await rendering.render_prompt_landing_html(
            html_path,
            42,
            page="home",
            is_preview=False,
            is_unlisted=False,
        )

        assert first == second
        assert calls == 1
        assert "Related assistants" in first
        assert "_aurvek_analytics_loaded" in first

        html_path.write_text(
            "<html><body>second version</body></html>", encoding="utf-8"
        )
        refreshed = await rendering.render_prompt_landing_html(
            html_path,
            42,
            page="home",
            is_preview=False,
            is_unlisted=False,
        )
        assert "second version" in refreshed
        assert calls == 2
    finally:
        rendering.clear_landing_render_cache()


@pytest.mark.asyncio
async def test_rendered_landing_cache_expires_by_ttl(tmp_path, monkeypatch):
    html_path = tmp_path / "home.html"
    html_path.write_text("<html><body>cached</body></html>", encoding="utf-8")
    monkeypatch.setattr(
        rendering,
        "_landing_render_cache",
        TTLCache(maxsize=10, ttl=0.01),
    )
    monkeypatch.setattr(rendering, "LANDING_RELATED_LINKS_ENABLED", True)
    calls = 0

    async def related(prompt_id, max_links):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(rendering, "get_related_landing_links", related)
    await rendering.render_prompt_landing_html(
        html_path,
        43,
        page="home",
        is_preview=False,
        is_unlisted=False,
    )
    await asyncio.sleep(0.02)
    await rendering.render_prompt_landing_html(
        html_path,
        43,
        page="home",
        is_preview=False,
        is_unlisted=False,
    )
    assert calls == 2


@pytest.mark.asyncio
async def test_preview_render_is_separate_and_has_no_tracking(tmp_path, monkeypatch):
    html_path = tmp_path / "home.html"
    html_path.write_text("<html><body>preview</body></html>", encoding="utf-8")
    rendering.clear_landing_render_cache()

    async def unexpected_related(*args, **kwargs):
        raise AssertionError("preview must not query related landings")

    monkeypatch.setattr(rendering, "get_related_landing_links", unexpected_related)
    try:
        rendered = await rendering.render_prompt_landing_html(
            html_path,
            44,
            page="home",
            is_preview=True,
            is_unlisted=False,
        )
        assert "_aurvek_analytics_loaded" not in rendered
        assert "Related assistants" not in rendered
    finally:
        rendering.clear_landing_render_cache()


@pytest.mark.asyncio
async def test_custom_domain_render_uses_file_aware_cache(tmp_path, monkeypatch):
    html_path = tmp_path / "home.html"
    html_path.write_text("<html><body>custom</body></html>", encoding="utf-8")
    rendering.clear_landing_render_cache()
    calls = 0
    original = rendering.inject_custom_domain_analytics

    def counted(html_content, prompt_id):
        nonlocal calls
        calls += 1
        return original(html_content, prompt_id)

    monkeypatch.setattr(rendering, "inject_custom_domain_analytics", counted)
    try:
        first = await rendering.render_custom_domain_landing_html(html_path, 45)
        second = await rendering.render_custom_domain_landing_html(html_path, 45)
        assert first == second
        assert calls == 1
        assert "_aurvek_analytics_loaded" in first

        html_path.write_text(
            "<html><body>custom updated</body></html>",
            encoding="utf-8",
        )
        refreshed = await rendering.render_custom_domain_landing_html(
            html_path,
            45,
        )
        assert "custom updated" in refreshed
        assert calls == 2
    finally:
        rendering.clear_landing_render_cache()
