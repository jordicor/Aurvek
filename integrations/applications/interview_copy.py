"""Explicit operator-only history import. This service is never an HTTP capability.

The caller provisions the independent app account and grants its context first.
All database work is one transaction; no inference, billing or provider work runs.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from pydantic import Field

from integrations.embed.models import EmbedError, InterviewBrief, StrictModel
from integrations.embed.store import _digest, _json, _one
from .models import ConversationId, ExternalReference, LocalId, OpenConversationRequest, OperationId
from .service import ApplicationService


class InterviewCopyRequest(StrictModel):
    operation_id: OperationId
    app_id: LocalId
    external_user_id: ExternalReference
    expected_subject: ExternalReference
    external_project_id: ExternalReference
    expected_context_id: ExternalReference
    assistant_id: LocalId = "default"
    source_user_id: ConversationId
    source_conversation_id: ConversationId
    brief: InterviewBrief = Field(default_factory=lambda: InterviewBrief(revision=1, language="es"))


async def _exists(connection, table):
    return bool(await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE", (table,)))


async def _rows(connection, sql, values=()):
    return [dict(row) for row in await (await connection.execute(sql, values)).fetchall()]


def _file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ApplicationInterviewCopyService:
    def __init__(self, store, *, users_root=None):
        self.store = store
        self.applications = ApplicationService(store)
        if users_root is None:
            from common import users_directory
            users_root = users_directory
        self.users_root = Path(users_root).resolve()

    def _directory(self, username, conversation_id):
        from common import generate_user_hash
        first, second, user_hash = generate_user_hash(username)
        number = f"{conversation_id:07d}"
        path = self.users_root / first / second / user_hash / "files" / number[:3] / number[3:]
        if not path.resolve().is_relative_to(self.users_root) or any(p.is_symlink() for p in (path, *path.parents) if p != self.users_root.parent):
            raise EmbedError("import_unsafe_media_path", 409)
        return path

    async def _destination(self, connection, request):
        account = await _one(connection, """SELECT a.*,s.user_id,u.username FROM APPLICATION_ACCOUNTS a
            JOIN EMBED_SUBJECTS s USING(subject) JOIN USERS u ON u.id=s.user_id
            WHERE a.app_id=? AND a.external_user_id=?""", (request.app_id, request.external_user_id))
        if (not account or not account["created_here"] or account["subject"] != request.expected_subject
                or account["user_id"] == request.source_user_id):
            raise EmbedError("import_destination_mismatch", 409)
        await self.store._live(connection, request.app_id, account["user_id"], account["subject"])
        config, participant, _, caps = await self.applications._scope(connection, request.app_id,
            account["subject"], account["user_id"], request.expected_context_id, request.assistant_id)
        if participant["context_ref"] != request.external_project_id or not caps.get("text"):
            raise EmbedError("import_destination_mismatch", 409)
        return account, config

    async def _prior(self, connection, request):
        row = await _one(connection, "SELECT * FROM APPLICATION_INTERVIEW_IMPORTS WHERE app_id=? AND operation_id=?",
                         (request.app_id, request.operation_id))
        if row and row["request_hash"] != _digest(_json(request.model_dump())):
            raise EmbedError("import_operation_conflict", 409)
        if row and row["state"] != "copied":
            raise EmbedError("import_rolled_back", 409)
        if row:
            binding = await self.applications.find_binding(connection, row["conversation_id"])
            if not binding or (binding["app_id"], binding["subject"], binding["context_id"]) != (
                    request.app_id, request.expected_subject, request.expected_context_id):
                raise EmbedError("import_destination_missing", 409)
        return row

    async def _inventory(self, connection, request):
        account, config = await self._destination(connection, request)
        source = await _one(connection, """SELECT c.*,u.username FROM CONVERSATIONS c JOIN USERS u ON u.id=c.user_id
            WHERE c.id=? AND c.user_id=?""", (request.source_conversation_id, request.source_user_id))
        if (not source or source["role_id"] != config.prompt_id
                or any(source.get(key) for key in ("is_incognito", "hidden_from_history", "purge_on_close", "locked"))):
            raise EmbedError("import_source_mismatch", 409)
        if await _one(connection, "SELECT 1 FROM APPLICATION_CONVERSATIONS WHERE app_id=? AND subject=? AND context_id=?",
                      (request.app_id, request.expected_subject, request.expected_context_id)):
            raise EmbedError("import_destination_not_empty", 409)
        if await _one(connection, "SELECT 1 FROM EMBED_INTERVIEWS WHERE app_id=? AND subject=? AND external_project_id=?",
                      (request.app_id, request.expected_subject, request.external_project_id)):
            raise EmbedError("import_destination_not_empty", 409)
        messages = await _rows(connection, "SELECT * FROM MESSAGES WHERE conversation_id=? ORDER BY id", (source["id"],))
        if not messages or any(row["user_id"] != request.source_user_id for row in messages):
            raise EmbedError("import_source_messages_invalid", 409)
        directory = self._directory(source["username"], source["id"])
        prefix = directory.relative_to(self.users_root).as_posix()
        media_rows = await _rows(connection, "SELECT * FROM GENERATED_MEDIA_FILES WHERE conversation_id=? AND user_id=?",
                                 (source["id"], source["user_id"]))
        # Only existing source-owned conversation files enter the copy. The
        # snapshot's message references and generated ledger define relevance.
        content = "\n".join(str(row["message"]) for row in messages).replace("\\/", "/")
        files = []
        for path in sorted(directory.rglob("*")) if directory.exists() else []:
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                raise EmbedError("import_unsafe_media_path", 409)
            if not path.is_file():
                continue
            rel = path.relative_to(self.users_root).as_posix()
            if rel not in content and not any(row["rel_path"] == rel for row in media_rows):
                continue
            files.append({"relative_path": rel, "size_bytes": path.stat().st_size, "sha256": _file_hash(path)})
        present = {item["relative_path"] for item in files}
        missing = [{"relative_path": row["rel_path"], "reason": "missing_legacy_file"}
                   for row in media_rows if row["rel_path"] not in present]
        referenced = {unquote(value).split("?", 1)[0] for value in re.findall(re.escape(prefix) + r'/[^\s"<>\\]+', content)}
        missing.extend({"relative_path": value, "reason": "missing_legacy_file"}
                       for value in sorted(referenced - present) if not any(row.get("relative_path") == value for row in missing))
        attachments = []
        if await _exists(connection, "FILE_ATTACHMENTS"):
            import file_storage
            attachments = await _rows(connection, """SELECT a.public_id,a.blob_id,a.message_id,b.sha256,
                b.size_bytes,b.storage_key FROM FILE_ATTACHMENTS a JOIN FILE_BLOBS b ON b.id=a.blob_id
                WHERE a.user_id=? AND a.conversation_id=? AND a.status='active' AND b.status='ready'
                AND a.message_id IN (SELECT id FROM MESSAGES WHERE conversation_id=?)""",
                (source["user_id"], source["id"], source["id"]))
            for item in attachments:
                path = file_storage._path_from_storage_key(item.pop("storage_key"))
                item["available"] = path.is_file()
                if not item["available"]:
                    missing.append({"attachment_ref": item["public_id"], "reason": "missing_blob"})
                elif _file_hash(path) != item["sha256"]:
                    raise EmbedError("import_blob_hash_mismatch", 409)
            requested_refs = {ref for row in messages for ref in file_storage.extract_attachment_refs_from_message(row["message"])}
            if await _exists(connection, "MESSAGE_VOICE_NOTES"):
                requested_refs.update(row["audio_attachment_ref"] for row in await _rows(connection,
                    "SELECT audio_attachment_ref FROM MESSAGE_VOICE_NOTES WHERE message_id IN (SELECT id FROM MESSAGES WHERE conversation_id=?)",
                    (source["id"],)) if row["audio_attachment_ref"])
            missing.extend({"attachment_ref": ref, "reason": "unavailable_attachment"}
                for ref in sorted(requested_refs - {row["public_id"] for row in attachments}))
        memory = {}
        for table in ("MEMORY_PROVIDER_MESSAGE_LINKS", "MEMORY_PROVIDER_CONVERSATION_LINKS",
                      "MEMORY_PROVIDER_SYNC_STATE", "APPLICATION_MEMORY_DESTINATIONS", "APPLICATION_MEMORY_MESSAGE_DESTINATIONS"):
            if await _exists(connection, table):
                memory[table] = (await _one(connection, f"SELECT COUNT(*) AS count FROM {table} WHERE conversation_id=?", (source["id"],)))["count"]
        recordings = []
        if await _exists(connection, "PHONE_RECORDINGS"):
            from integrations.telephony.recording_storage import resolve_private_recording_path
            rows = await _rows(connection, """SELECT r.* FROM PHONE_RECORDINGS r JOIN PHONE_CALLS c ON c.id=r.call_id
                WHERE c.conversation_id=? AND c.owner_user_id=?""", (source["id"], source["user_id"]))
            for row in rows:
                tracks = {}
                for key in ("participant_path", "assistant_path", "mixed_path"):
                    if row[key]:
                        path = resolve_private_recording_path(row["call_id"], row[key])
                        tracks[key] = {"available": path.is_file(), "sha256": _file_hash(path) if path.is_file() else None}
                recordings.append({"recording_id": row["id"], "status": row["status"], "tracks": tracks, "copied": False})
        blobs = {item["blob_id"]: item["size_bytes"] for item in attachments if item["available"]}
        from storage_quota import ensure_known_growth_fits
        await ensure_known_growth_fits(connection, account["user_id"], sum(blobs.values()) + sum(item["size_bytes"] for item in files))
        manifest = {"source_user_id": source["user_id"], "source_conversation_id": source["id"],
            "destination_user_id": account["user_id"], "prompt_id": config.prompt_id,
            "cutoff_message_id": messages[-1]["id"], "message_count": len(messages),
            "source_messages_sha256": _digest(_json(messages)), "source_state_sha256": await self._destination_hash(connection, source["id"]),
            "source_media_prefix": prefix, "legacy_files": files,
            "attachments": attachments, "missing_files": missing, "recordings": recordings,
            "memory_inventory": memory, "memory_treatment": "not_copied; future memory uses the new application scope",
            "planned_at": self.store.now()}
        return account, source, messages, media_rows, prefix, manifest

    async def plan(self, request):
        request = InterviewCopyRequest.model_validate(request)
        async with self.store.connection(readonly=True) as connection:
            await connection.execute("BEGIN")
            await self._destination(connection, request)
            prior = await self._prior(connection, request)
            if prior:
                return {**json.loads(prior["result_json"]), "replayed": True}
            *_, manifest = await self._inventory(connection, request)
            return {"state": "planned", "manifest": manifest, "replayed": False}

    async def _destination_hash(self, connection, conversation_id):
        snapshot = {"conversation": await _one(connection, "SELECT * FROM CONVERSATIONS WHERE id=?", (conversation_id,))}
        for table in ("MESSAGES", "FILE_ATTACHMENTS", "GENERATED_MEDIA_FILES", "EMBED_INTERVIEWS", "EMBED_BRIEF_REVISIONS", "APPLICATION_CONVERSATIONS"):
            if await _exists(connection, table):
                snapshot[table] = await _rows(connection, f"SELECT * FROM {table} WHERE conversation_id=? ORDER BY rowid", (conversation_id,))
        for table in ("MESSAGE_CHANNEL_PROVENANCE", "MESSAGE_INPUT_PROVENANCE", "MESSAGE_VOICE_NOTES"):
            if await _exists(connection, table):
                snapshot[table] = await _rows(connection, f"SELECT * FROM {table} WHERE message_id IN (SELECT id FROM MESSAGES WHERE conversation_id=?) ORDER BY message_id", (conversation_id,))
        return _digest(_json(snapshot))

    async def copy(self, request):
        request = InterviewCopyRequest.model_validate(request)
        destination_directory = None
        created_blob_paths = []
        try:
            async with self.store.transaction() as connection:
                account, _ = await self._destination(connection, request)
                prior = await self._prior(connection, request)
                if prior:
                    return {**json.loads(prior["result_json"]), "replayed": True}
                account, source, messages, media_rows, prefix, manifest = await self._inventory(connection, request)
                live = SimpleNamespace(app_id=request.app_id, subject=account["subject"], user_id=account["user_id"])
                opened = await self.applications._open_in_connection(connection, live, OpenConversationRequest(
                    operation_id=request.operation_id, context_ref=request.external_project_id,
                    assistant_id=request.assistant_id, mode="new"), record_operation=False)
                conversation_id = opened["conversation_id"]
                candidate = self._directory(account["username"], conversation_id)
                if candidate.exists():
                    raise EmbedError("import_destination_files_exist", 409)
                destination_directory = candidate
                new_prefix = candidate.relative_to(self.users_root).as_posix()
                import file_storage
                from integrations.messaging_voice_notes.service import clone_message_channel_provenance_for_branch
                await file_storage.ensure_file_storage_schema(connection)
                last_blob_id = (await _one(connection, "SELECT COALESCE(MAX(id),0) AS id FROM FILE_BLOBS"))["id"]
                missing_refs = {row["attachment_ref"] for row in manifest["missing_files"] if "attachment_ref" in row}
                mappings = []
                for row in messages:
                    content = row["message"]
                    for ref in missing_refs:
                        content = file_storage.remove_attachment_ref_from_message(content, ref)
                    content = content.replace(prefix, new_prefix).replace(prefix.replace("/", "\\/"), new_prefix.replace("/", "\\/"))
                    cursor = await connection.execute("""INSERT INTO MESSAGES
                        (conversation_id,user_id,message,type,date,llm_id,citations_json)
                        VALUES (?,?,?,?,?,?,?) RETURNING id""", (conversation_id, account["user_id"], content,
                        row["type"], row["date"], row.get("llm_id"), row.get("citations_json")))
                    new_id = (await cursor.fetchone())[0]
                    content = await file_storage.clone_attachments_for_branch(connection, old_message_id=row["id"],
                        new_message_id=new_id, new_conversation_id=conversation_id, user_id=account["user_id"],
                        source_user_id=source["user_id"], message_json=content)
                    await connection.execute("UPDATE MESSAGES SET message=? WHERE id=?", (content, new_id))
                    if await _exists(connection, "MESSAGE_CHANNEL_PROVENANCE"):
                        await clone_message_channel_provenance_for_branch(connection, old_message_id=row["id"],
                            old_conversation_id=source["id"], new_message_id=new_id, new_conversation_id=conversation_id,
                            user_id=account["user_id"], source_user_id=source["user_id"])
                    mappings.append({"source_message_id": row["id"], "message_id": new_id})
                await connection.execute("UPDATE CONVERSATIONS SET chat_name=? WHERE id=?", (source.get("chat_name"), conversation_id))
                media_urls = {}
                from chat.services.generated_media import generated_media_url
                for item in manifest["legacy_files"]:
                    path = self.users_root / item["relative_path"]
                    target = self.users_root / item["relative_path"].replace(prefix, new_prefix, 1)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
                    if _file_hash(target) != item["sha256"]:
                        raise EmbedError("import_media_changed", 409)
                    ledger = next((row for row in media_rows if row["rel_path"] == item["relative_path"]), None)
                    suffix = target.suffix.lower()
                    kind = ledger["kind"] if ledger else ("image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                        else "video" if suffix in {".mp4", ".webm", ".mov"} else None)
                    if kind:
                        cursor = await connection.execute("""INSERT INTO GENERATED_MEDIA_FILES(user_id,conversation_id,kind,rel_path,size_bytes)
                            VALUES (?,?,?,?,?) RETURNING id""", (account["user_id"], conversation_id, kind,
                            target.relative_to(self.users_root).as_posix(), item["size_bytes"]))
                        new_url = generated_media_url(conversation_id, (await cursor.fetchone())[0])
                        media_urls["/users/" + target.relative_to(self.users_root).as_posix()] = new_url
                        if ledger:
                            media_urls[generated_media_url(source["id"], ledger["id"])] = new_url
                # Private generated-media URLs encode the ledger id. Rebind it
                # too, and drop native signed URL query tokens on copied media.
                def rewrite_media(value):
                    if isinstance(value, list):
                        return [rewrite_media(item) for item in value]
                    if isinstance(value, dict):
                        if value.get("type") in {"document_url", "text_file"}:
                            return value  # Converted to FILE_ATTACHMENTS below.
                        return {key: rewrite_media(item) for key, item in value.items()}
                    if not isinstance(value, str):
                        return value
                    if value.startswith(("/", "http://", "https://")):
                        parsed = urlsplit(value)
                        key = unquote(parsed.path)
                        if key in media_urls:
                            return media_urls[key]
                        if key + "?" + parsed.query in media_urls:
                            return media_urls[key + "?" + parsed.query]
                    return re.sub(r'(!\[[^\]]*\]\()([^)]*)(\))',
                        lambda match: match[1] + rewrite_media(match[2]) + match[3], value)
                for row in await _rows(connection, "SELECT id,message,citations_json FROM MESSAGES WHERE conversation_id=?", (conversation_id,)):
                    fields = []
                    for key in ("message", "citations_json"):
                        value = row[key]
                        if value is not None:
                            try:
                                payload = rewrite_media(json.loads(value))
                                if key == "message" and isinstance(payload, list):
                                    for index, block in enumerate(payload):
                                        if not isinstance(block, dict) or block.get("type") not in {"text_file", "document_url"}:
                                            continue
                                        info = block.get(block["type"], {})
                                        if info.get("attachment_ref"):
                                            continue
                                        rel = unquote(urlsplit(info.get("url", "")).path).lstrip("/").removeprefix("users/")
                                        expected_files = {item["relative_path"].replace(prefix, new_prefix, 1) for item in manifest["legacy_files"]}
                                        if rel not in expected_files:
                                            continue
                                        path = self.users_root / rel
                                        kind = "pdf" if block["type"] == "document_url" else "text"
                                        try:
                                            ref, _ = await file_storage.create_active_attachment_from_bytes(connection,
                                                data=path.read_bytes(), kind=kind, user_id=account["user_id"], conversation_id=conversation_id,
                                                message_id=row["id"], original_filename=path.name,
                                                mime_detected="application/pdf" if kind == "pdf" else "text/plain", legacy_block_index=index)
                                        finally:
                                            created_blobs = await _rows(connection, "SELECT id,storage_key FROM FILE_BLOBS WHERE id>?", (last_blob_id,))
                                            created_blob_paths = [file_storage._path_from_storage_key(item["storage_key"]) for item in created_blobs]
                                        file_storage._set_block_attachment_ref(block, ref)
                                value = _json(payload)
                            except json.JSONDecodeError:
                                value = rewrite_media(value)
                        fields.append(value)
                    await connection.execute("UPDATE MESSAGES SET message=?,citations_json=? WHERE id=?", (*fields, row["id"]))
                brief = _json(request.brief.model_dump())
                await connection.execute("INSERT INTO EMBED_INTERVIEWS VALUES (?,?,?,?,?,?,?,?,'active',?,?)",
                    (request.app_id, account["subject"], request.external_project_id, account["user_id"], conversation_id,
                     opened["prompt_id"], brief, request.brief.revision, self.store.now(), self.store.now()))
                await connection.execute("INSERT INTO EMBED_BRIEF_REVISIONS VALUES (?,?,?,?)",
                    (conversation_id, request.brief.revision, brief, self.store.now()))
                _, app = await self.store._app(connection, request.app_id)
                receipt = {"app_id": request.app_id, "external_user_id": request.external_user_id, "issuer": app.issuer,
                    "subject": account["subject"], "conversation_id": str(conversation_id), "context_id": request.expected_context_id,
                    "external_project_id": request.external_project_id, "assistant_id": request.assistant_id,
                    "source_conversation_id": str(source["id"]), "operation_id": request.operation_id,
                    "preservation": {"original_unchanged": True, "copy_independent": True}}
                if await self._destination_hash(connection, source["id"]) != manifest["source_state_sha256"]:
                    raise EmbedError("import_source_changed", 409)
                if await _one(connection, "SELECT 1 FROM FILE_ATTACHMENTS WHERE conversation_id=? AND user_id<>?", (conversation_id, account["user_id"])):
                    raise EmbedError("import_destination_mismatch", 409)
                manifest.update({"state": "copied", "operation_id": request.operation_id, "message_mapping": mappings,
                    "destination_conversation_id": conversation_id, "destination_media_prefix": new_prefix, "copied_at": self.store.now(),
                    "created_blobs": await _rows(connection, "SELECT id,storage_key FROM FILE_BLOBS WHERE id>?", (last_blob_id,))})
                result = {"receipt": receipt, "manifest": manifest, "replayed": False}
                await connection.execute("INSERT INTO APPLICATION_INTERVIEW_IMPORTS VALUES (?,?,?,?,?,'copied',?,?,?,?)",
                    (request.app_id, request.operation_id, _digest(_json(request.model_dump())), _json(request.model_dump()),
                     conversation_id, _json(result), await self._destination_hash(connection, conversation_id), self.store.now(), self.store.now()))
            return result
        except BaseException:
            # A commit error can be ambiguous. Never remove files backing a
            # durable receipt; the exact retry recovers that completed result.
            if destination_directory is not None:
                async with self.store.connection(readonly=True) as connection:
                    saved = await _one(connection, "SELECT 1 FROM APPLICATION_INTERVIEW_IMPORTS WHERE app_id=? AND operation_id=?",
                                       (request.app_id, request.operation_id))
                if not saved:
                    if destination_directory.exists():
                        shutil.rmtree(destination_directory)
                    for path in created_blob_paths:
                        path.unlink(missing_ok=True)
            raise

    async def rollback(self, app_id, operation_id):
        """Remove only an unchanged, unused destination created by this operation."""
        directory = None
        blob_paths = []
        async with self.store.transaction() as connection:
            row = await _one(connection, "SELECT * FROM APPLICATION_INTERVIEW_IMPORTS WHERE app_id=? AND operation_id=?", (app_id, operation_id))
            if not row:
                raise EmbedError("not_found", 404)
            if row["state"] == "rolled_back":
                return {"state": "rolled_back", "replayed": True}
            request = InterviewCopyRequest.model_validate_json(row["request_json"])
            account, _ = await self._destination(connection, request)
            conversation_id = row["conversation_id"]
            if row["destination_hash"] != await self._destination_hash(connection, conversation_id):
                raise EmbedError("import_destination_changed", 409)
            for table in ("APPLICATION_OPERATIONS", "EMBED_TICKETS", "EMBED_FRAMES", "PHONE_CALLS", "PHONE_CALL_JOBS",
                          "MEMORY_PROVIDER_MESSAGE_LINKS", "MEMORY_PROVIDER_CONVERSATION_LINKS", "APPLICATION_MEMORY_ACTIVATIONS",
                          "APPLICATION_MEMORY_DESTINATIONS", "APPLICATION_MEMORY_MESSAGE_DESTINATIONS"):
                if await _exists(connection, table) and await _one(connection, f"SELECT 1 FROM {table} WHERE conversation_id=? LIMIT 1", (conversation_id,)):
                    raise EmbedError("import_destination_in_use", 409)
            if await _exists(connection, "MESSAGE_TRANSCRIPTION_REVISIONS") and await _one(connection,
                    "SELECT 1 FROM MESSAGE_TRANSCRIPTION_REVISIONS WHERE message_id IN (SELECT id FROM MESSAGES WHERE conversation_id=?) LIMIT 1",
                    (conversation_id,)):
                raise EmbedError("import_destination_in_use", 409)
            directory = self._directory(account["username"], conversation_id)
            manifest = json.loads(row["result_json"])["manifest"]
            expected = {item["relative_path"].replace(manifest["source_media_prefix"],
                manifest["destination_media_prefix"], 1): item["sha256"] for item in manifest["legacy_files"]}
            paths = list(directory.rglob("*")) if directory.exists() else []
            if any(path.is_symlink() or not path.resolve().is_relative_to(directory) for path in paths):
                raise EmbedError("import_unsafe_media_path", 409)
            actual = {path.relative_to(self.users_root).as_posix(): _file_hash(path) for path in paths if path.is_file()}
            if actual != expected:
                raise EmbedError("import_destination_changed", 409)
            await connection.execute("DELETE FROM EMBED_INTERVIEWS WHERE conversation_id=?", (conversation_id,))
            await connection.execute("DELETE FROM GENERATED_MEDIA_FILES WHERE conversation_id=? AND user_id=?", (conversation_id, account["user_id"]))
            await connection.execute("DELETE FROM FILE_ATTACHMENTS WHERE conversation_id=? AND user_id=?", (conversation_id, account["user_id"]))
            import file_storage
            for blob in manifest.get("created_blobs", []):
                cursor = await connection.execute("""DELETE FROM FILE_BLOBS WHERE id=? AND storage_key=?
                    AND NOT EXISTS(SELECT 1 FROM FILE_ATTACHMENTS WHERE blob_id=?)""", (blob["id"], blob["storage_key"], blob["id"]))
                if cursor.rowcount:
                    blob_paths.append(file_storage._path_from_storage_key(blob["storage_key"]))
            await connection.execute("DELETE FROM MESSAGES WHERE conversation_id=? AND user_id=?", (conversation_id, account["user_id"]))
            await connection.execute("DELETE FROM CONVERSATIONS WHERE id=? AND user_id=?", (conversation_id, account["user_id"]))
            await connection.execute("UPDATE APPLICATION_INTERVIEW_IMPORTS SET state='rolled_back',updated_at=? WHERE app_id=? AND operation_id=?",
                (self.store.now(), app_id, operation_id))
        if directory.exists():
            shutil.rmtree(directory)
        for path in blob_paths:
            path.unlink(missing_ok=True)
        return {"state": "rolled_back", "replayed": False, "conversation_id": str(conversation_id)}


def main():
    """Explicit CLI; schema installation/provisioning are separate operations."""
    import argparse
    import asyncio
    from integrations.embed.store import EmbedStore
    from log_config import cli_diagnostics_to_stderr

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "copy", "rollback"))
    parser.add_argument("--request", type=Path, help="Private JSON InterviewCopyRequest file")
    parser.add_argument("--app-id")
    parser.add_argument("--operation-id")
    parser.add_argument("--receipt-file", type=Path)
    parser.add_argument("--manifest-file", type=Path)
    args = parser.parse_args()
    with cli_diagnostics_to_stderr():
        service = ApplicationInterviewCopyService(EmbedStore())
        if args.mode == "rollback":
            if not args.app_id or not args.operation_id:
                parser.error("rollback requires --app-id and --operation-id")
            action = service.rollback(args.app_id, args.operation_id)
        else:
            if args.request is None:
                parser.error("plan/copy requires --request")
            request = InterviewCopyRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
            action = getattr(service, args.mode)(request)
        result = asyncio.run(action)
    if args.receipt_file and "receipt" in result:
        args.receipt_file.write_text(json.dumps(result["receipt"], indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    if args.manifest_file and "manifest" in result:
        args.manifest_file.write_text(json.dumps(result["manifest"], indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps(result.get("receipt", result), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
