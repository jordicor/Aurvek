import json
import re
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

import i18n


LANGUAGES = ("en", "es", "ja", "fr", "pt", "it", "de")


@pytest.mark.parametrize("language", LANGUAGES)
def test_native_chat_renders_labels_and_its_required_browser_catalogs(language):
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    environment.globals.update(get_static_url=lambda value: value, get_static_theme_hashes=lambda: {})
    translator = i18n.Translator(language)
    html = environment.get_template("chat/chat.html").render(
        ui_language=language, t=translator.t, t_html=translator.html,
        i18n_payload=translator.browser_payload,
        marketplace=SimpleNamespace(discovery_enabled=False),
        admin_view=False, conversation_id=42, start_date_iso="2026-09-08T01:00:00Z",
        have_vision=False, can_send_files=True, max_api_image_size_mb=20,
        max_chat_image_dimension=2048, attachment_upload_chunk_size_bytes=524288,
        user_id=7, username="Original 日本語", is_admin=False, is_user=True,
        prompt_description="Original prompt", llm_models=[], api_key_mode="system_only",
        can_send_messages=True, requires_own_keys=False, has_own_keys=False,
        chat_folders=[], initial_conversations=[], web_search_enabled=False,
    )
    assert f'lang="{language}"' in html
    assert translator.render("chat_ui.composer.send") in html
    assert translator.render("chat_ui.folder.add") in html
    assert translator.render("chat_ui.tools.ai_voice") in html
    payload = json.loads(re.search(r'<script[^>]*id="aurvek-i18n"[^>]*>(.*?)</script>', html, re.S)[1])
    assert payload["language"] == language
    assert set(payload["resources"][language]) == {
        "common", "navigation", "chat", "chat_widgets", "chat_ui", "chat_errors",
    }
    assert html.index('id="aurvek-i18n"') < html.index('/static/js/chat/chat.js')
    assert 'Original prompt' in html


def test_media_gallery_renders_localized_shell_in_all_languages():
    environment = Environment(loader=FileSystemLoader("templates"), autoescape=True)
    environment.globals.update(
        get_static_url=lambda value: value,
        get_static_theme_hashes=lambda: {},
    )
    template = environment.get_template("media_gallery.html")
    for language in LANGUAGES:
        translator = i18n.Translator(language)
        html = template.render(
            ui_language=language,
            t=translator.t,
            t_html=translator.html,
            i18n_payload=translator.browser_payload,
            images=[],
            pdf_token="",
            mp3_token="",
            cdn_files_url="",
            marketplace=SimpleNamespace(discovery_enabled=False),
            username="Test",
            user_profile_picture="",
            is_admin=False,
            is_user=True,
        )
        assert f'lang="{language}"' in html
        assert translator.t("chat_ui.gallery.title") in html
        assert translator.t("chat_ui.gallery.delete_selected") in html
        assert 'id="aurvek-i18n"' in html
