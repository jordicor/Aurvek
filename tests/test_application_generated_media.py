"""App-generated media keeps native storage and private, repeatable delivery."""

import io
from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest
from fastapi import HTTPException
from PIL import Image

import save_images
from ai_runtime.attachments.media import hydrate_image_for_context
from chat.routes import media
from chat.services import generated_media, message_rendering
from integrations.applications.runtime import authorize_application_read
from tests.test_application_attachments import application_files, store_file
from tests.test_application_frames import application_frames
from tests.test_embed_native_creation import native_pilot


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(buffer, "PNG")
    return buffer.getvalue()


def media_environment(fixture, monkeypatch, tmp_path):
    pilot, _, _, _ = fixture
    user_root = tmp_path / "data" / "users"
    for module in (save_images, generated_media, message_rendering, media):
        monkeypatch.setattr(module, "get_db_connection", pilot.connect)
    monkeypatch.setattr(save_images, "users_directory", str(user_root))
    monkeypatch.setattr(generated_media, "users_directory", str(user_root))


@pytest.mark.asyncio
async def test_generated_image_has_private_urls_rehydrates_and_rejects_other_context(application_files, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from ai_runtime.context.formatting import _format_messages_for_provider
    from ai_runtime.providers import claude
    from tests.test_claude_inference_parameters import _FakeSession, _CLAUDE_STREAM

    pilot, first, coach, user = application_files
    media_environment(application_files, monkeypatch, tmp_path)

    def no_token(*args, **kwargs):
        pytest.fail("Application output minted a native media token")

    monkeypatch.setattr(save_images, "generate_signed_url_cloudflare", no_token)
    monkeypatch.setattr(save_images, "generate_img_token", no_token)
    urls = await save_images.save_image_locally(None, image_bytes(), user, first["conversation_id"], "blue.png", "bot")
    assert urls[0] == urls[1] and urls[2] == urls[3]
    assert urls[0] != urls[2]
    conversation_id, media_id = generated_media.parse_generated_media_url(urls[2])
    response = await media.generated_media_content(conversation_id, media_id, user)
    assert response.headers["cache-control"] == "private, no-store"
    assert Path(response.path).is_file()
    block = {"type": "image_url", "image_url": {"url": urls[0], "fullsize_url": urls[2]}}
    assert (await hydrate_image_for_context(block, "Claude", user, conversation_id=conversation_id))["source"]["type"] == "base64"
    # The provider boundary is shared by ordinary chat and Multi-AI.
    messages = await _format_messages_for_provider([
        {"type": "user", "message": "Create a blue image."},
        {"type": "bot", "message": [{"type": "text", "text": "Here is the image."}, block]},
    ], "What color is the generated image?", "Prompt", "Claude", user, conversation_id=conversation_id)
    original = orjson.dumps(messages)
    captured = {}
    monkeypatch.setattr(claude.aiohttp, "ClientSession",
        lambda *args, **kwargs: _FakeSession(captured, stream_lines=_CLAUDE_STREAM))
    monkeypatch.setattr(claude, "record_provider_success_for_label", AsyncMock())
    chunks = [chunk async for chunk in claude.call_claude_api(
        messages, "claude-opus-4-8", 0.3, 100, "Prompt", conversation_id, user, None,
        user_api_key="test-key", save_to_db=False)]
    sent = captured["json"]["messages"]
    assert chunks and orjson.dumps(messages) == original
    assert sent[1]["role"] == "assistant" and sent[1]["content"][0]["text"] == "Here is the image."
    assert all(part["type"] != "image" for part in sent[1]["content"])
    assert "assistant-generated image 1" in sent[2]["content"][0]["text"]
    assert sent[2]["content"][1] == messages[1]["content"][1]
    assert sent[2]["content"][-1] == messages[2]["content"][0]
    assert await hydrate_image_for_context(block, "Claude", user, conversation_id=coach["conversation_id"]) is None
    with pytest.raises(HTTPException) as wrong_context:
        await media.generated_media_content(coach["conversation_id"], media_id, user)
    assert wrong_context.value.status_code == 404
    async with pilot.connect() as connection:
        await connection.execute("UPDATE GENERATED_MEDIA_FILES SET kind='pdf' WHERE id=?", (media_id,))
        await connection.commit()
    with pytest.raises(HTTPException) as export_bypass:
        await media.generated_media_content(conversation_id, media_id, user)
    assert export_bypass.value.status_code == 404
    async with pilot.connect() as connection:
        await connection.execute("UPDATE APPLICATION_PARTICIPANTS SET active=0")
        await connection.commit()
    with pytest.raises(HTTPException) as revoked:
        await media.generated_media_content(conversation_id, media_id, user)
    assert revoked.value.status_code == 404


@pytest.mark.asyncio
async def test_app_history_renders_authorized_upload_and_generated_media_without_tokens(application_files, monkeypatch, tmp_path):
    pilot, first, coach, user = application_files
    media_environment(application_files, monkeypatch, tmp_path)
    uploaded = await store_file(application_files, b"The launch color is blue.", "notes.txt", "text/plain")
    urls = await save_images.save_image_locally(None, image_bytes(), user, first["conversation_id"], "blue.png", "bot")
    forbidden = generated_media.generated_media_url(coach["conversation_id"], 999)
    message = orjson.dumps([
        {"type": "text", "text": "Here are the results."}, uploaded.block,
        {"type": "image_url", "image_url": {"url": urls[0], "fullsize_url": urls[2]}},
        {"type": "image_url", "image_url": {"url": forbidden}},
    ]).decode()
    async with pilot.connect() as connection:
        scope = await authorize_application_read(connection, first["conversation_id"], user.id)
    user.embed_principal = SimpleNamespace(application=scope)
    rendered = orjson.loads(await message_rendering.process_message(
        message, None, user, conversation_id=first["conversation_id"], application=scope,
    ))
    assert [entry["type"] for entry in rendered] == ["text", "text_file", "image_url"]
    assert rendered[1]["text_file"]["url"].startswith("/api/attachments/")
    assert rendered[2]["image_url"] == {"url": urls[0], "fullsize_url": urls[2]}
    assert "token=" not in orjson.dumps(rendered).decode()
    user.embed_principal = SimpleNamespace(application=None)
    legacy = orjson.loads(await message_rendering.process_message(
        message, None, user, conversation_id=first["conversation_id"],
    ))
    assert legacy == [{"type": "text", "text": "Here are the results."}]


@pytest.mark.asyncio
async def test_native_generated_image_retains_signed_url_delivery(application_files, monkeypatch, tmp_path):
    pilot, _, _, user = application_files
    media_environment(application_files, monkeypatch, tmp_path)
    async with pilot.connect() as connection:
        cursor = await connection.execute("INSERT INTO CONVERSATIONS(user_id,role_id,llm_id) VALUES(1,99,8)")
        conversation_id = cursor.lastrowid
        await connection.commit()
    monkeypatch.setattr(save_images, "CLOUDFLARE_FOR_IMAGES", True)
    monkeypatch.setattr(save_images, "generate_signed_url_cloudflare", lambda path, **_: f"https://cdn.example/{path}?signature=native")
    urls = await save_images.save_image_locally(None, image_bytes(), user, conversation_id, "blue.png", "bot")
    assert "signature=native" in urls[1]
    assert "signature=native" in urls[3]
    assert generated_media.parse_generated_media_url(urls[0]) is None


@pytest.mark.asyncio
async def test_generated_video_reuses_private_ledger_delivery(application_files, monkeypatch, tmp_path):
    import common
    from tools import generate_videos

    pilot, first, _, user = application_files
    media_environment(application_files, monkeypatch, tmp_path)
    monkeypatch.setattr(common, "users_directory", str(tmp_path / "data" / "users"))
    monkeypatch.setattr(generate_videos, "get_db_connection", pilot.connect)
    video_data = b"synthetic-video-transport-fixture"
    base, display, path = await generate_videos.save_video_locally(
        video_data, "clip", user, first["conversation_id"],
    )
    assert base == display
    conversation_id, media_id = generated_media.parse_generated_media_url(base)
    response = await media.generated_media_content(conversation_id, media_id, user)
    assert Path(response.path).read_bytes() == video_data
    assert Path(path) == response.path
