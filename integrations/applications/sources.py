"""Authorized source inventories over native messages and file ledgers.

Pages use stable keys and content revisions, not an incremental change feed.
Consumers refresh from the first page and replace their inventory to observe
edits/deletions. No table, provider call, signed URL or public file is created.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from integrations.embed.models import EmbedError
from integrations.embed.store import _one
from .models import CONTRACT_VERSION
from .service import ApplicationService


KINDS = {"messages", "attachments", "generated_media", "handoffs"}
SNAPSHOT_VERSION = "aurvek.conversation_snapshot.v1"
SNAPSHOT_MAX_MESSAGES = 5000
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
_MEDIA_BLOCKS = {"image_url", "video_url", "pdf_url", "text_file", "audio_url"}
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _cursor(scope, kind, after):
    value = [scope.app_id, scope.subject, scope.context_id, scope.conversation_id, kind, after]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _after(cursor, scope, kind):
    if cursor is None:
        return "" if kind == "handoffs" else 0
    try:
        value = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        expected = [scope.app_id, scope.subject, scope.context_id, scope.conversation_id, kind]
        if not isinstance(value, list) or len(value) != 6 or value[:5] != expected:
            raise ValueError()
        position = value[5]
        if kind == "handoffs":
            if not isinstance(position, str) or not 1 <= len(position) <= 128:
                raise ValueError()
        elif type(position) is not int or not 0 < position <= (1 << 63) - 1:
            raise ValueError()
        return position
    except (ValueError, TypeError, UnicodeError):
        raise EmbedError("invalid_cursor", 400) from None


def _attachment_allowed(scope, row):
    if row["attachment_type"] == "audio":
        return bool(scope.capabilities.get("voice") and scope.capabilities.get(
            "stt" if row["message_type"] == "user" else "tts"))
    return bool(scope.capabilities.get("attachments"))


def _generated_allowed(scope, kind):
    required = {"image": ("image_generation",), "video": ("video_generation",),
                "pdf": ("export_pdf",), "mp3": ("export_mp3", "tts"),
                "wav": ("voice", "tts")}.get(kind, ())
    return bool(required) and all(scope.capabilities.get(key) for key in required)


def _attachment_item(row):
    return {"source_ref": "attachment:" + row["public_id"], "kind": row["attachment_type"],
            "message_id": row["message_id"], "filename": row["display_name"] or row["original_filename"],
            "mime_type": row["mime_detected"], "size_bytes": row["size_bytes"],
            "sha256": row["sha256"], "created_at": row["created_at"]}


def _generated_item(row):
    from chat.services.generated_media import generated_media_path
    try:
        path = generated_media_path(row)
        stat = path.stat()
        if not path.is_file():
            return None
    except (OSError, ValueError):
        return None
    return {"source_ref": f"generated:{row['id']}", "kind": row["kind"],
            "filename": path.name, "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "size_bytes": stat.st_size, "modified_ns": stat.st_mtime_ns, "created_at": row["created_at"]}


_ATTACHMENTS = """SELECT a.*,b.mime_detected,b.size_bytes,b.sha256,m.type AS message_type
    FROM FILE_ATTACHMENTS a JOIN FILE_BLOBS b ON b.id=a.blob_id
    JOIN MESSAGES m ON m.id=a.message_id AND m.conversation_id=a.conversation_id AND m.user_id=a.user_id
    WHERE a.conversation_id=? AND a.user_id=? AND a.status='active' AND b.status='ready'"""


class ApplicationSourceService:
    def __init__(self, store):
        self.store = store
        self.applications = ApplicationService(store)

    async def _scope(self, connection, identity, conversation_id, context_ref):
        scope = await self.applications.authorize_backend_conversation(
            connection, identity, conversation_id, context_ref)
        if not scope.capabilities.get("text"):
            raise EmbedError("application_capability_denied", 403)
        return scope

    async def conversation_snapshot(self, identity, conversation_id, context_ref, *, mode="snapshot"):
        """Capture one bounded SQLite revision while allowing the interview to continue.

        The original authorized message structure and provenance are retained.
        Media bytes and private file URLs are not exported. Revision-only reads
        use the same digest, so edits and removals invalidate accepted analyses
        even when the number of messages has not changed.
        """
        if mode not in {"snapshot", "revision"}:
            raise EmbedError("invalid_request", 400)
        async with self.store.connection(readonly=True) as connection:
            await connection.execute("BEGIN")
            scope = await self._scope(connection, identity, conversation_id, context_ref)
            size = await _one(connection, """SELECT COUNT(*) AS message_count,
                COALESCE(SUM(LENGTH(CAST(message AS BLOB))),0) AS content_bytes
                FROM MESSAGES WHERE conversation_id=? AND user_id=?""",
                (conversation_id, scope.user_id))
            if size["message_count"] > SNAPSHOT_MAX_MESSAGES or size["content_bytes"] > SNAPSHOT_MAX_BYTES:
                raise EmbedError("source_snapshot_too_large", 413)
            cursor = await connection.execute("""SELECT id,message,type,date,llm_id
                FROM MESSAGES WHERE conversation_id=? AND user_id=? ORDER BY id""",
                (conversation_id, scope.user_id))
            items, revisions = [], []
            exported_bytes = 0
            while rows := await cursor.fetchmany(400):
                records = [dict(row) for row in rows]
                projected = await self._messages(connection, scope, records)
                for original, item in zip(records, projected, strict=True):
                    # Include the stored row as well as its authorized projection:
                    # changes to unavailable media syntax must also invalidate it.
                    item["revision"] = _revision({"stored": original, "source": item})
                    revisions.append(item["revision"])
                    exported_bytes += len(json.dumps(item, ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8"))
                    if exported_bytes > SNAPSHOT_MAX_BYTES:
                        raise EmbedError("source_snapshot_too_large", 413)
                    if mode == "snapshot":
                        items.append(item)
            payload = {"contract_version": CONTRACT_VERSION, "snapshot_version": SNAPSHOT_VERSION,
                "app_id": scope.app_id, "subject": scope.subject,
                "conversation_id": conversation_id, "context_id": scope.context_id,
                "context_ref": context_ref, "assistant_id": scope.assistant_id,
                "kind": "messages", "message_count": size["message_count"],
                "content_bytes": size["content_bytes"], "complete": True}
            payload["revision"] = _revision({**payload, "message_revisions": revisions})
            payload["captured_at"] = datetime.now(timezone.utc).isoformat()
            if mode == "snapshot":
                payload["items"] = items
            return payload

    async def list_sources(self, identity, conversation_id, context_ref, *, kind="messages", limit=50, cursor=None):
        if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 100:
            raise EmbedError("invalid_request", 400)
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
            raise EmbedError("invalid_cursor", 400)
        async with self.store.connection(readonly=True) as connection:
            await connection.execute("BEGIN")
            scope = await self._scope(connection, identity, conversation_id, context_ref)
            after = _after(cursor, scope, kind)
            args = (conversation_id, scope.user_id)
            if kind == "messages":
                rows = await (await connection.execute("""SELECT id,message,type,date,llm_id
                    FROM MESSAGES WHERE conversation_id=? AND user_id=? AND id>?
                    ORDER BY id LIMIT ?""", (*args, after, limit + 1))).fetchall()
                items = await self._messages(connection, scope, [dict(row) for row in rows[:limit]])
            elif kind == "attachments":
                allowed = []
                if scope.capabilities.get("attachments"):
                    allowed.append("a.attachment_type<>'audio'")
                if scope.capabilities.get("voice"):
                    if scope.capabilities.get("stt"):
                        allowed.append("(a.attachment_type='audio' AND m.type='user')")
                    if scope.capabilities.get("tts"):
                        allowed.append("(a.attachment_type='audio' AND m.type='bot')")
                if not allowed:
                    raise EmbedError("application_capability_denied", 403)
                rows = await (await connection.execute(_ATTACHMENTS + " AND (" + " OR ".join(allowed)
                    + ") AND a.id>? ORDER BY a.id LIMIT ?", (*args, after, limit + 1))).fetchall()
                items = [_attachment_item(dict(row)) for row in rows[:limit]]
            elif kind == "generated_media":
                allowed = [value for value in ("image", "video", "pdf", "mp3", "wav") if _generated_allowed(scope, value)]
                if not allowed:
                    raise EmbedError("application_capability_denied", 403)
                marks = ",".join("?" for _ in allowed)
                rows = await (await connection.execute(f"""SELECT * FROM GENERATED_MEDIA_FILES
                    WHERE conversation_id=? AND user_id=? AND kind IN ({marks}) AND id>?
                    ORDER BY id LIMIT ?""", (*args, *allowed, after, limit + 1))).fetchall()
                items = [item for row in rows[:limit] if (item := _generated_item(dict(row)))]
            else:
                rows = await (await connection.execute("""SELECT handoff_id,source_conversation_id,
                    conversation_id,source_assistant_id,assistant_id,note,note_origin,after_message_id,created_at
                    FROM APPLICATION_HANDOFFS WHERE app_id=? AND subject=? AND context_id=?
                    AND (conversation_id=? OR source_conversation_id=?) AND handoff_id>?
                    ORDER BY handoff_id LIMIT ?""", (scope.app_id, scope.subject, scope.context_id,
                    conversation_id, conversation_id, after, limit + 1))).fetchall()
                items = [{**dict(row), "source_ref": "handoff:" + row["handoff_id"]} for row in rows[:limit]]
            for item in items:
                item["revision"] = _revision(item)
            last_key = "handoff_id" if kind == "handoffs" else "id"
            next_cursor = _cursor(scope, kind, rows[limit - 1][last_key]) if len(rows) > limit else None
            payload = {"contract_version": CONTRACT_VERSION, "app_id": scope.app_id, "subject": scope.subject,
                       "conversation_id": conversation_id, "context_id": scope.context_id, "context_ref": context_ref,
                       "assistant_id": scope.assistant_id, "kind": kind, "items": items, "next_cursor": next_cursor}
            payload["revision"] = _revision(payload)
            return payload

    async def _messages(self, connection, scope, rows):
        if not rows:
            return []
        from ai_runtime.context.message_provenance import load_historical_input_metadata
        ids = [row["id"] for row in rows]
        marks = ",".join("?" for _ in ids)
        metadata = await load_historical_input_metadata(ids, connection=connection)
        attachments = [dict(row) for row in await (await connection.execute(
            _ATTACHMENTS + f" AND a.message_id IN ({marks}) ORDER BY a.id",
            (scope.conversation_id, scope.user_id, *ids))).fetchall()]
        attachments = {row["public_id"]: row for row in attachments if _attachment_allowed(scope, row)}
        voice_rows = await (await connection.execute(f"""SELECT message_id,original_transcript,active_transcript,
            duration_seconds,retention_status FROM MESSAGE_VOICE_NOTES WHERE message_id IN ({marks})""", ids)).fetchall()
        voices = {row["message_id"]: dict(row) for row in voice_rows}
        # Resolve only references on this page, never load all media in the conversation.
        media_ids, paths = set(), set()
        def collect(value):
            if isinstance(value, list):
                for part in value:
                    collect(part)
            elif isinstance(value, dict):
                for key, part in value.items():
                    if key in {"url", "fullsize_url"} and isinstance(part, str):
                        remember(part)
                    else:
                        collect(part)
            elif isinstance(value, str):
                for match in _MARKDOWN_IMAGE.finditer(value):
                    remember(match[2])
        def remember(url):
            from chat.services.generated_media import parse_generated_media_url
            reference = parse_generated_media_url(url)
            if reference and reference[0] == scope.conversation_id:
                media_ids.add(reference[1])
            path = unquote(urlsplit(url).path).lstrip("/")
            if path.startswith("users/"):
                paths.add(path)
        contents = {}
        for row in rows:
            try:
                value = json.loads(row["message"])
            except (ValueError, TypeError):
                value = row["message"]
            if not isinstance(value, (list, dict)):
                value = row["message"]
            contents[row["id"]] = value
            collect(value)
        media = {}
        lookups = [("id", value) for value in media_ids] + [("rel_path", value) for value in paths]
        for offset in range(0, len(lookups), 400):
            batch = lookups[offset:offset + 400]
            where = " OR ".join(field + "=?" for field, _ in batch)
            found = await (await connection.execute("SELECT * FROM GENERATED_MEDIA_FILES "
                "WHERE conversation_id=? AND user_id=? AND (" + where + ")",
                (scope.conversation_id, scope.user_id, *(value for _, value in batch)))).fetchall()
            for row in found:
                record = dict(row)
                if _generated_allowed(scope, record["kind"]):
                    item = _generated_item(record)
                    if item:
                        media[("id", record["id"])] = media[("rel_path", record["rel_path"])] = item
        def media_reference(url):
            from chat.services.generated_media import parse_generated_media_url
            reference = parse_generated_media_url(url)
            if reference:
                return media.get(("id", reference[1])) if reference[0] == scope.conversation_id else None
            return media.get(("rel_path", unquote(urlsplit(url or "").path).lstrip("/")))
        def sanitize(value, message_id):
            if isinstance(value, list):
                return [sanitize(part, message_id) for part in value]
            if isinstance(value, dict):
                kind = value.get("type")
                if isinstance(kind, str) and kind in _MEDIA_BLOCKS:
                    data = value.get(kind) or {}
                    ref = data.get("attachment_ref") if isinstance(data, dict) else None
                    attachment = attachments.get(ref) if isinstance(ref, str) else None
                    item = (_attachment_item(attachment) if attachment and attachment["message_id"] == message_id
                            else media_reference(data.get("fullsize_url") or data.get("url") or "")
                            if isinstance(data, dict) and not ref else None)
                    return {"type": kind, kind: item or {"unavailable": True}}
                return {key: sanitize(part, message_id) for key, part in value.items()}
            if isinstance(value, str):
                def replace(match):
                    item = media_reference(match[2])
                    return f"![{match[1]}]({item['source_ref']})" if item else f"[{match[1]}]"
                return _MARKDOWN_IMAGE.sub(replace, value)
            return value
        phone = {}
        has_phone = scope.capabilities.get("voice") and await _one(connection,
            f"SELECT 1 FROM PHONE_CALL_MESSAGE_LINKS WHERE message_id IN ({marks}) LIMIT 1", ids)
        if has_phone:
            from chat.services.phone_history import load_phone_history_page
            @asynccontextmanager
            async def current_connection(readonly=False):
                yield connection
            history = await load_phone_history_page(current_connection, conversation_id=scope.conversation_id,
                owner_user_id=scope.user_id, message_ids=ids, newest_page=False)
            phone = history.message_metadata
        result = []
        for row in rows:
            mid = row["id"]
            provenance = metadata.get(mid)
            item = {"source_ref": f"message:{mid}", "message_id": mid, "role": row["type"],
                    "content": sanitize(contents[mid], mid), "created_at": row["date"], "llm_id": row["llm_id"],
                    "provenance": {"origin": provenance.origin, "perception": provenance.perception} if provenance else None,
                    "files": [_attachment_item(value) for value in attachments.values() if value["message_id"] == mid]}
            if mid in voices:
                item["transcription"] = voices[mid]
            info = phone.get(mid, {})
            if info.get("provenance", {}).get("audio", {}).get("available") and scope.capabilities.get(
                    "stt" if row["type"] == "user" else "tts"):
                item["files"].append({"source_ref": f"phone_audio:{mid}", "kind": "audio", "mime_type": "audio/wav",
                    "message_id": mid, "phone_call_id": info["phone_call_id"], "participant": info["participant"]})
            result.append(item)
        return result

    async def source_content(self, identity, conversation_id, context_ref, source_ref):
        """Return a private FileResponse/StreamingResponse after live authorization."""
        from fastapi.responses import FileResponse
        from file_storage import get_attachment_path_for_user
        from chat.services.generated_media import generated_media_path
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        async with self.store.connection(readonly=True) as connection:
            scope = await self._scope(connection, identity, conversation_id, context_ref)
            prefix, _, key = source_ref.partition(":")
            if prefix == "attachment" and key:
                row = await _one(connection, _ATTACHMENTS + " AND a.public_id=?",
                                 (conversation_id, scope.user_id, key))
                if not row or not _attachment_allowed(scope, row):
                    raise EmbedError("not_found", 404)
                value = await get_attachment_path_for_user(connection, public_id=key, user_id=scope.user_id)
                if value:
                    path, record = value
                    return FileResponse(path, media_type=record["mime_detected"],
                        filename=row["display_name"] or row["original_filename"], headers=headers)
            elif prefix == "generated" and key.isdigit() and 0 < int(key) <= (1 << 63) - 1:
                row = await _one(connection, "SELECT * FROM GENERATED_MEDIA_FILES WHERE id=? AND conversation_id=? AND user_id=?",
                                 (int(key), conversation_id, scope.user_id))
                if row and _generated_allowed(scope, row["kind"]):
                    try:
                        path = generated_media_path(row)
                        if path.is_file():
                            return FileResponse(path, filename=path.name, headers=headers)
                    except ValueError:
                        pass
            elif prefix == "phone_audio" and key.isdigit() and 0 < int(key) <= (1 << 63) - 1:
                row = await _one(connection, "SELECT type FROM MESSAGES WHERE id=? AND conversation_id=? AND user_id=?",
                                 (int(key), conversation_id, scope.user_id))
                if row and scope.capabilities.get("voice") and scope.capabilities.get("stt" if row["type"] == "user" else "tts"):
                    return await self._phone_audio(scope, int(key), headers)
        raise EmbedError("not_found", 404)

    async def _phone_audio(self, scope, message_id, headers):
        from fastapi.responses import StreamingResponse
        from starlette.background import BackgroundTask
        from integrations.telephony.repository import TelephonyNotFoundError
        from integrations.telephony.purge import PhoneDataPurgeRepository
        from integrations.telephony.recording_storage import resolve_private_recording_path
        from integrations.telephony.message_audio import PhoneMessageAudioRange, open_pcmu_range_as_wav_chunks, wav_content_length
        try:
            audio = await PhoneDataPurgeRepository(self.store.connection).get_owned_message_audio(
                owner_user_id=scope.user_id, message_id=message_id)
            if audio["conversation_id"] != scope.conversation_id:
                raise EmbedError("not_found", 404)
            path = resolve_private_recording_path(audio["call_id"], audio["path"])
            if path.is_symlink():
                raise EmbedError("not_found", 404)
            span = PhoneMessageAudioRange(audio["start_byte"], audio["end_byte"])
            body = open_pcmu_range_as_wav_chunks(path, span)
        except (TelephonyNotFoundError, OSError, ValueError):
            raise EmbedError("not_found", 404) from None
        return StreamingResponse(body, media_type="audio/wav",
            headers={**headers, "Content-Length": str(wav_content_length(span))}, background=BackgroundTask(body.close))
