from __future__ import annotations

import base64
import io
import asyncio as stdlib_asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import orjson
import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image as PilImage

import app as app_module
import prompts as prompts_module
from ai_runtime.attachments import pdf as pdf_attachments
from chat.routes import voice_io
from marketplace.routes import packs, prompt_landing_builder, storefronts
from tools import tts as tts_tools


class DummyUser:
    def __init__(self, *, user_id: int = 31, username: str = "creator"):
        self.id = user_id
        self.username = username

    @property
    async def is_user(self):
        return True

    @property
    async def is_admin(self):
        return False


class ToThreadSpy:
    def __init__(self):
        self.functions = []

    async def __call__(self, function, *args, **kwargs):
        self.functions.append(function)
        return function(*args, **kwargs)


def _png_bytes(size: tuple[int, int] = (80, 40)) -> bytes:
    output = io.BytesIO()
    PilImage.new("RGB", size, color=(20, 40, 60)).save(output, "PNG")
    return output.getvalue()


def _upload(filename: str = "image.png") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(_png_bytes()))


@asynccontextmanager
async def _empty_connection(*_args, **_kwargs):
    yield object()


@pytest.mark.asyncio
async def test_profile_picture_variants_run_as_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()
    monkeypatch.setattr(app_module, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(app_module, "users_directory", str(tmp_path))

    base_url = await app_module.upload_profile_picture(
        _upload("avatar.png"),
        request=None,
        current_user=DummyUser(username="alice"),
    )

    assert to_thread.functions == [app_module._save_profile_picture_variants]
    base_path = tmp_path / base_url.removeprefix("users/")
    for label, expected_size in (
        ("32", (32, 32)),
        ("64", (64, 64)),
        ("128", (128, 128)),
        ("fullsize", (80, 40)),
    ):
        image_path = base_path.parent / f"{base_path.name}_{label}.webp"
        with PilImage.open(image_path) as image:
            assert image.size == expected_size


def test_wallpaper_rejects_excess_pixels_before_decoding(tmp_path, monkeypatch) -> None:
    class OversizedImage:
        size = (app_module.MAX_IMAGE_PIXELS + 1, 1)
        format = "PNG"
        load_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def load(self):
            self.load_called = True

    oversized = OversizedImage()
    monkeypatch.setattr(app_module.PilImage, "open", lambda *_args, **_kwargs: oversized)

    with pytest.raises(HTTPException) as exc_info:
        app_module._save_nekoglass_wallpaper(
            b"image",
            str(tmp_path / "wallpaper.webp"),
        )

    assert exc_info.value.status_code == 400
    assert oversized.load_called is False


@pytest.mark.asyncio
async def test_creator_avatar_variants_run_as_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()
    executed = []

    class FakeConnection:
        async def execute(self, query, params):
            executed.append((query, params))

        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_connection(*_args, **_kwargs):
        yield FakeConnection()

    monkeypatch.setattr(storefronts, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(storefronts, "users_directory", str(tmp_path))
    monkeypatch.setattr(storefronts, "get_db_connection", fake_connection)
    monkeypatch.setattr(storefronts, "require_storefronts_enabled", lambda: None)

    response = await storefronts.upload_creator_avatar(
        _upload("creator.png"),
        request=None,
        current_user=DummyUser(),
    )

    payload = orjson.loads(response.body)
    assert to_thread.functions == [storefronts._save_creator_avatar_variants]
    assert payload["avatar_url"].endswith("_creator")
    assert len(list(tmp_path.rglob("*_creator_*.webp"))) == 4
    assert executed and "UPDATE CREATOR_PROFILES" in executed[0][0]


@pytest.mark.asyncio
async def test_pack_cover_variants_run_as_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()
    pack_row = {
        "id": 7,
        "name": "Demo Pack",
        "username": "creator",
        "created_by_user_id": 31,
        "cover_image": None,
        "public_id": None,
    }

    async def fake_get_pack(_conn, _pack_id):
        return pack_row

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(packs, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(packs, "require_creator_tools_enabled", lambda: None)
    monkeypatch.setattr(packs, "get_db_connection", _empty_connection)
    monkeypatch.setattr(packs, "get_pack", fake_get_pack)
    monkeypatch.setattr(packs, "_require_admin_or_user", no_op)
    monkeypatch.setattr(packs, "_require_pack_owner", no_op)
    monkeypatch.setattr(packs, "update_pack", no_op)
    monkeypatch.setattr(
        packs,
        "_build_pack_filesystem_path",
        lambda *_args: tmp_path / "pack",
    )

    response = await packs.api_upload_cover_image(
        7,
        file=_upload("cover.png"),
        current_user=DummyUser(),
    )

    assert response.status_code == 200
    assert to_thread.functions == [packs._save_pack_cover_variants]
    assert len(list((tmp_path / "pack" / "static" / "img").glob("*.webp"))) == 3


@pytest.mark.asyncio
async def test_pack_landing_image_runs_as_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()
    pack_row = {
        "id": 7,
        "name": "Demo Pack",
        "username": "creator",
        "created_by_user_id": 31,
        "public_id": "pack-public",
        "slug": "demo-pack",
    }

    async def fake_get_pack(_conn, _pack_id):
        return pack_row

    async def no_op(*_args, **_kwargs):
        return None

    async def fake_pack_dir(*_args, **_kwargs):
        return tmp_path / "pack", "creator"

    monkeypatch.setattr(packs, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(packs, "require_creator_tools_enabled", lambda: None)
    monkeypatch.setattr(packs, "get_db_connection", _empty_connection)
    monkeypatch.setattr(packs, "get_pack", fake_get_pack)
    monkeypatch.setattr(packs, "_require_admin_or_user", no_op)
    monkeypatch.setattr(packs, "_require_pack_owner", no_op)
    monkeypatch.setattr(packs, "_get_pack_dir_and_info", fake_pack_dir)

    response = await packs.pack_landing_upload_images(
        7,
        images=[_upload("hero.png")],
        names=["hero"],
        current_user=DummyUser(),
    )

    payload = orjson.loads(response.body)
    assert to_thread.functions == [packs._save_pack_landing_image]
    assert payload["images"][0]["id"] == "hero.webp"
    assert (tmp_path / "pack" / "static" / "img" / "hero.webp").is_file()


@pytest.mark.asyncio
async def test_prompt_landing_image_runs_as_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()

    async def can_manage(*_args, **_kwargs):
        return True

    async def prompt_info(_prompt_id):
        return {"name": "Demo Prompt"}

    monkeypatch.setattr(
        prompt_landing_builder,
        "asyncio",
        SimpleNamespace(to_thread=to_thread),
    )
    monkeypatch.setattr(
        prompt_landing_builder,
        "require_creator_tools_enabled",
        lambda: None,
    )
    monkeypatch.setattr(prompt_landing_builder, "can_manage_prompt", can_manage)
    monkeypatch.setattr(prompt_landing_builder, "get_prompt_info", prompt_info)
    monkeypatch.setattr(
        prompt_landing_builder,
        "get_prompt_path",
        lambda *_args: str(tmp_path / "prompt"),
    )

    result = await prompt_landing_builder.upload_images(
        9,
        images=[_upload("hero.png")],
        names=["hero"],
        current_user=DummyUser(),
    )

    assert to_thread.functions == [prompt_landing_builder._save_prompt_landing_image]
    assert result["images"][0]["id"] == "hero.png"
    assert result["images"][0]["url"].endswith("/hero.webp")
    assert (tmp_path / "prompt" / "static" / "img" / "hero.webp").is_file()


@pytest.mark.asyncio
async def test_legacy_pdf_read_and_encoding_share_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()
    pdf_data = b"legacy-pdf-bytes"
    pdf_path = tmp_path / "legacy.pdf"
    pdf_path.write_bytes(pdf_data)

    monkeypatch.setattr(
        pdf_attachments,
        "asyncio",
        SimpleNamespace(to_thread=to_thread),
    )
    monkeypatch.setattr(
        pdf_attachments,
        "_resolve_legacy_attachment_path",
        lambda *_args, **_kwargs: ("legacy.pdf", pdf_path),
    )

    hydrated = await pdf_attachments.hydrate_pdf_for_context(
        {
            "document_url": {
                "url": "legacy.pdf",
                "filename": "legacy.pdf",
                "pages": 1,
            }
        },
        "Claude",
    )

    assert to_thread.functions == [pdf_attachments._load_legacy_pdf_payload]
    assert hydrated["source"]["data"] == base64.b64encode(pdf_data).decode("ascii")


@pytest.mark.asyncio
async def test_prompt_image_variants_run_as_one_worker_block(tmp_path, monkeypatch) -> None:
    to_thread = ToThreadSpy()

    class FakeCursor:
        def __init__(self):
            self.query = ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, query, _params):
            self.query = query

        async def fetchone(self):
            if "JOIN PROMPT_PERMISSIONS" in self.query:
                return ("creator",)
            return (1,)

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        async def execute(self, *_args, **_kwargs):
            return None

        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_connection(*_args, **_kwargs):
        yield FakeConnection()

    monkeypatch.setattr(prompts_module, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(prompts_module, "users_directory", str(tmp_path))
    monkeypatch.setattr(prompts_module, "get_db_connection", fake_connection)

    base_url = await prompts_module.process_prompt_image_upload(
        42,
        _upload("prompt.png"),
        {"name": "Demo Prompt"},
        DummyUser(),
    )

    assert to_thread.functions == [prompts_module._save_prompt_image_variants]
    assert base_url.endswith("/42_demo_prompt")
    assert len(list(tmp_path.rglob("42_demo_prompt_*.webp"))) == 4


@pytest.mark.asyncio
async def test_transcribe_audio_decode_runs_in_worker(monkeypatch) -> None:
    to_thread = ToThreadSpy()
    decode_calls = []
    settled = []

    def fake_from_file(file_obj, **options):
        decode_calls.append((file_obj.read(), options))
        return SimpleNamespace(duration_seconds=60.0)

    async def reserve_stt_attempt(**_kwargs):
        return "reservation"

    async def settle_stt_attempt(reservation_id, **_kwargs):
        settled.append(reservation_id)

    async def transcribe_with_deepgram(**_kwargs):
        return "transcribed"

    monkeypatch.setattr(voice_io, "asyncio", SimpleNamespace(to_thread=to_thread))
    monkeypatch.setattr(
        voice_io,
        "AudioSegment",
        SimpleNamespace(from_file=fake_from_file),
    )
    monkeypatch.setattr(voice_io, "get_browser", lambda _user_agent: "chrome")
    monkeypatch.setattr(voice_io, "stt_engine", "deepgram")
    monkeypatch.setattr(voice_io, "stt_fallback_enabled", False)
    monkeypatch.setattr(voice_io, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(voice_io, "settle_stt_attempt", settle_stt_attempt)
    monkeypatch.setattr(voice_io, "transcribe_with_deepgram", transcribe_with_deepgram)

    result = await voice_io.transcribe(
        SimpleNamespace(headers={"user-agent": "Chrome"}),
        audio=UploadFile(filename="voice.webm", file=io.BytesIO(b"audio")),
        user_id=31,
    )

    assert result == "transcribed"
    assert to_thread.functions == [voice_io._decode_audio_duration]
    assert decode_calls == [(b"audio", {"format": "webm", "codec": "opus"})]
    assert settled == ["reservation"]


@pytest.mark.asyncio
async def test_tts_decode_combine_and_export_run_as_one_worker_block(
    tmp_path,
    monkeypatch,
) -> None:
    to_thread = ToThreadSpy()
    decode_formats = []

    class FakeAudioSegment:
        def __init__(self, payload):
            self.payload = payload

        def __iadd__(self, other):
            self.payload += other.payload
            return self

        def export(self, output, *, format, codec):
            assert (format, codec) == ("ogg", "libopus")
            output.write(b"ogg:" + self.payload)

    def fake_from_file(file_obj, *, format):
        decode_formats.append(format)
        return FakeAudioSegment(file_obj.read())

    async def get_tts_profile(_context):
        return SimpleNamespace(
            model_id="model",
            output_format="mp3_44100_128",
            stability=0.45,
            similarity_boost=0.89,
            ws_enabled=False,
            chunk_schedule=[120],
        )

    async def audio_generator():
        yield b"first"
        yield b"second"

    output_path = tmp_path / "cache.ogg"
    monkeypatch.setattr(
        tts_tools,
        "asyncio",
        SimpleNamespace(
            to_thread=to_thread,
            CancelledError=stdlib_asyncio.CancelledError,
        ),
    )
    monkeypatch.setattr(
        tts_tools,
        "AudioSegment",
        SimpleNamespace(from_file=fake_from_file),
    )
    monkeypatch.setattr(tts_tools, "get_tts_profile", get_tts_profile)
    monkeypatch.setattr(tts_tools, "format_to_pydub", lambda _format: "mp3")
    monkeypatch.setattr(
        tts_tools,
        "get_file_path",
        lambda _digest: (str(tmp_path), str(output_path)),
    )
    monkeypatch.setattr(
        tts_tools,
        "get_tts_generator",
        lambda *_args, **_kwargs: audio_generator(),
    )
    monkeypatch.setattr(tts_tools, "tts_engine", "elevenlabs")
    monkeypatch.setattr(tts_tools, "_maybe_cleanup_cache", lambda: None)

    result = await tts_tools.handle_tts_request(
        websocket=None,
        data={"text": "hello", "conversationId": 7},
        current_user=DummyUser(),
        is_whatsapp=True,
        sample_voice_id="sample-voice",
    )

    assert result == (str(output_path), None)
    assert to_thread.functions == [tts_tools._decode_and_export_audio_chunks]
    assert decode_formats == ["mp3", "mp3"]
    assert output_path.read_bytes() == b"ogg:firstsecond"
