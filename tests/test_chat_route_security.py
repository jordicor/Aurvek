import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from chat.routes import media, voice_io


class DummyRequest:
    headers = {"user-agent": "Chrome"}


class DummyUser:
    def __init__(self, user_id: int, *, admin: bool = False):
        self.id = user_id
        self._admin = admin

    @property
    async def is_admin(self):
        return self._admin


async def _seed_conversation(conn, *, user_id: int, conversation_id: int) -> None:
    await conn.execute(
        "INSERT INTO USERS (id, username) VALUES (?, ?)",
        (user_id, f"user{user_id}"),
    )
    await conn.execute(
        "INSERT INTO CONVERSATIONS (id, user_id, role_id) VALUES (?, ?, 1)",
        (conversation_id, user_id),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_transcribe_web_requires_conversation_access_before_transcribing(mock_db, monkeypatch):
    monkeypatch.setattr(voice_io, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await _seed_conversation(conn, user_id=2, conversation_id=10)

    async def fail_transcribe(*args, **kwargs):
        raise AssertionError("transcribe should not run without conversation access")

    monkeypatch.setattr(voice_io, "transcribe", fail_transcribe)

    with pytest.raises(HTTPException) as exc_info:
        await voice_io.transcribe_web(
            DummyRequest(),
            audio=None,
            conversation_id="10",
            current_user=DummyUser(1),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_transcribe_web_bills_authenticated_owner(mock_db, monkeypatch):
    monkeypatch.setattr(voice_io, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await _seed_conversation(conn, user_id=1, conversation_id=11)

    seen = {}

    async def fake_transcribe(request, audio, user_id):
        seen["user_id"] = user_id
        return "hola"

    monkeypatch.setattr(voice_io, "transcribe", fake_transcribe)

    response = await voice_io.transcribe_web(
        DummyRequest(),
        audio=None,
        conversation_id="11",
        current_user=DummyUser(1),
    )

    assert response.status_code == 200
    assert seen == {"user_id": 1}


@pytest.mark.asyncio
async def test_download_pdf_checks_access_before_redis_lock(mock_db, monkeypatch):
    monkeypatch.setattr(voice_io, "get_db_connection", mock_db)
    async with mock_db() as conn:
        await _seed_conversation(conn, user_id=2, conversation_id=12)

    class RedisShouldNotRun:
        async def set(self, *args, **kwargs):
            raise AssertionError("redis lock should not be created without conversation access")

    monkeypatch.setattr(voice_io, "redis_client", RedisShouldNotRun())

    with pytest.raises(HTTPException) as exc_info:
        await voice_io.initiate_download_pdf(
            conversation_id=12,
            request=DummyRequest(),
            current_user=DummyUser(1),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "relative_directory", "filename", "media_type"),
    (
        (
            media.download_pdf,
            "files/000/0042/pdf/uploads",
            "Informe clínico.pdf",
            "application/pdf",
        ),
        (
            media.download_mp3,
            "files/000/0042/mp3",
            "Sesión privada.mp3",
            "audio/mpeg",
        ),
    ),
)
async def test_authenticated_download_serves_validated_file_directly(
    tmp_path,
    monkeypatch,
    endpoint,
    relative_directory,
    filename,
    media_type,
):
    user_directory = tmp_path / "alice"
    file_path = user_directory / relative_directory / filename
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"private media")
    monkeypatch.setattr(
        media,
        "get_user_directory",
        lambda _username: str(user_directory),
    )

    relative_path = file_path.relative_to(user_directory).as_posix()
    response = await endpoint(
        quote(relative_path, safe="/"),
        current_user=SimpleNamespace(username="alice"),
    )

    assert isinstance(response, FileResponse)
    assert Path(response.path) == file_path.resolve()
    assert response.media_type == media_type
    assert response.filename == filename


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "filename"),
    (
        (media.download_pdf, "other-user.pdf"),
        (media.download_mp3, "other-user.mp3"),
    ),
)
async def test_download_rejects_traversal_and_cross_user_paths(
    tmp_path,
    monkeypatch,
    endpoint,
    filename,
):
    user_directory = tmp_path / "alice"
    other_directory = tmp_path / "bob"
    user_directory.mkdir()
    other_file = other_directory / filename
    other_file.parent.mkdir(parents=True)
    other_file.write_bytes(b"secret")
    monkeypatch.setattr(
        media,
        "get_user_directory",
        lambda _username: str(user_directory),
    )

    attempted_paths = (
        f"../bob/{filename}",
        str(other_file),
    )
    for attempted_path in attempted_paths:
        with pytest.raises(HTTPException) as exc_info:
            await endpoint(
                attempted_path,
                current_user=SimpleNamespace(username="alice"),
            )
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_auth_file_rejects_expired_token(monkeypatch):
    monkeypatch.setattr(
        media,
        "decode_jwt_cached",
        lambda token, secret: {"username": "alice", "exp": int(time.time()) - 1},
    )

    with pytest.raises(HTTPException) as exc_info:
        await media.auth_file(DummyRequest(), request_uri="files/demo.pdf", token="token")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_file_requires_token_user_path_prefix(monkeypatch):
    monkeypatch.setattr(
        media,
        "decode_jwt_cached",
        lambda token, secret: {"username": "alice", "exp": int(time.time()) + 60},
    )
    monkeypatch.setattr(media, "validate_path_within_directory", lambda relative, base: base / relative)

    hash_prefix1, hash_prefix2, user_hash = media.generate_user_hash("alice")
    own_uri = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/files/demo.pdf"
    response = await media.auth_file(DummyRequest(), request_uri=own_uri, token="token")
    assert response.status_code == 200

    with pytest.raises(HTTPException) as exc_info:
        await media.auth_file(
            DummyRequest(),
            request_uri="users/aa/bbb/not_alice/files/demo.pdf",
            token="token",
        )

    assert exc_info.value.status_code == 403
