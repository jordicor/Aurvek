"""Private application media URLs backed by the existing generated-file ledger."""

import asyncio
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from common import users_directory
from database import get_db_connection
from integrations.applications.runtime import authorize_application_read
from storage_quota import normalize_rel_path


_CONTENT_PATH = re.compile(r"^/api/conversations/(\d+)/media/content$")


def generated_media_url(conversation_id: int, media_id: int) -> str:
    return f"/api/conversations/{conversation_id}/media/content?media_id={media_id}"


def parse_generated_media_url(url: str) -> tuple[int, int] | None:
    parsed = urlsplit(url or "")
    match = _CONTENT_PATH.fullmatch(parsed.path)
    ids = parse_qs(parsed.query).get("media_id", [])
    if match and len(ids) == 1 and ids[0].isdigit():
        return int(match[1]), int(ids[0])
    return None


def generated_media_path(record: dict) -> Path:
    root = Path(users_directory).resolve()
    path = (root / normalize_rel_path(record["rel_path"])).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Generated media path is outside user storage")
    return path


async def private_generated_media_urls(connection, *, user_id, conversation_id, paths):
    """Return stable URLs for app outputs, leaving native outputs unchanged."""
    application = await authorize_application_read(connection, conversation_id, user_id)
    if application is None:
        return None
    canonical = [normalize_rel_path(path) for path in paths]
    cursor = await connection.execute(
        "SELECT id,rel_path FROM GENERATED_MEDIA_FILES WHERE user_id=? AND conversation_id=? "
        f"AND rel_path IN ({','.join('?' for _ in canonical)})",
        (user_id, conversation_id, *canonical),
    )
    records = {row["rel_path"]: int(row["id"]) for row in await cursor.fetchall()}
    return [generated_media_url(conversation_id, records[path]) for path in canonical]


async def preload_generated_media_for_messages(messages, *, user_id, conversation_id):
    """Resolve only the media URLs on a history page; no query for text-only pages."""
    import orjson

    candidates = set()
    for _message_id, message in messages:
        try:
            blocks = orjson.loads(message)
        except orjson.JSONDecodeError:
            candidates.update(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", message))
            continue
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") in {"image_url", "video_url"}:
                    info = block.get(block["type"], {})
                    if not info.get("attachment_ref"):
                        candidates.update(url for url in (info.get("url"), info.get("fullsize_url")) if url)
    lookup = {}
    for url in candidates:
        parsed = parse_generated_media_url(url)
        if parsed:
            if parsed[0] == conversation_id:
                lookup[url] = ("id", parsed[1])
        else:
            path = unquote(urlsplit(url).path).lstrip("/")
            if path.startswith("users/"):
                try:
                    lookup[url] = ("rel_path", normalize_rel_path(path))
                except ValueError:
                    pass
    if not lookup:
        return {}
    result = {}
    entries = list(lookup.items())
    async with get_db_connection(readonly=True) as connection:
        for offset in range(0, len(entries), 400):
            batch = entries[offset:offset + 400]
            predicates = " OR ".join(f"{field}=?" for _, (field, _) in batch)
            cursor = await connection.execute(
                "SELECT id,kind,rel_path FROM GENERATED_MEDIA_FILES WHERE user_id=? AND conversation_id=? "
                f"AND ({predicates})",
                (user_id, conversation_id, *(value for _, (_, value) in batch)),
            )
            records = [dict(row) for row in await cursor.fetchall()]
            for url, (field, value) in batch:
                record = next((row for row in records if row[field] == value), None)
                if record:
                    result[url] = record
    return result


async def read_generated_image_bytes(url: str, *, user_id: int, conversation_id: int | None):
    reference = parse_generated_media_url(url)
    if reference is None or reference[0] != conversation_id:
        return None
    async with get_db_connection(readonly=True) as connection:
        cursor = await connection.execute(
            "SELECT id,kind,rel_path FROM GENERATED_MEDIA_FILES "
            "WHERE id=? AND conversation_id=? AND user_id=? AND kind='image'",
            (reference[1], conversation_id, user_id),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    path = generated_media_path(dict(row))
    try:
        return await asyncio.to_thread(path.read_bytes)
    except FileNotFoundError:
        return None
