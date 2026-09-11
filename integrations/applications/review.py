"""Native, audited read access for an explicitly assigned application owner."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from admin_audit import log_admin_action
from auth import get_current_user
from common import custom_unescape, get_template_context, templates
from database import get_db_connection
from file_storage import get_attachment_path_for_user
from chat.services.generated_media import generated_media_path, preload_generated_media_for_messages
from chat.services.message_rendering import convert_legacy_markdown_images_to_blocks, preload_attachment_records_for_messages
from ai_runtime.reasoning_tags import strip_tagged_thinking_prefix
from integrations.embed.context import is_embed_request
from integrations.embed.models import AppConfig
from integrations.embed.store import _one


router = APIRouter(prefix="/applications/manage/{app_id}/review")
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store, max-age=0", "X-Content-Type-Options": "nosniff"}
_MEDIA_HEADERS = {**_PRIVATE_HEADERS, "Content-Security-Policy": "sandbox"}


async def _authorize(connection, app_id, request, user, conversation_id=None):
    if user is None or is_embed_request(request) or getattr(user, "embed_principal", None) is not None:
        raise HTTPException(403, "application_owner_required")
    row = await _one(connection, "SELECT config_json FROM EMBED_APPS WHERE app_id=?", (app_id,))
    if not row:
        raise HTTPException(404, "not_found")
    app = AppConfig.model_validate_json(row["config_json"])
    if app.owner_user_id != user.id and not await user.is_admin:
        raise HTTPException(403, "application_owner_required")
    binding = None
    if conversation_id is not None:
        binding = await _one(connection, """SELECT b.*,c.chat_name,u.username FROM APPLICATION_CONVERSATIONS b
            JOIN CONVERSATIONS c ON c.id=b.conversation_id AND c.user_id=b.user_id AND c.role_id=b.prompt_id
            JOIN USERS u ON u.id=b.user_id WHERE b.app_id=? AND b.conversation_id=?""", (app_id, conversation_id))
        if not binding:
            raise HTTPException(404, "not_found")
    return app, binding


async def _audit(request, user, app_id, *, binding=None, resource="conversation", resource_id=None):
    await log_admin_action(admin_id=user.id, action_type="application_review", request=request,
        target_user_id=binding["user_id"] if binding else None, target_resource_type=resource,
        target_resource_id=resource_id, details=f"Application {app_id}; read-only operator access")


async def _page(request, user, app, **values):
    context = await get_template_context(request, user)
    context.update(review_application={"app_id": app.app_id, "display_name": app.display_name}, **values)
    return templates.TemplateResponse("applications/review.html", context, headers=_PRIVATE_HEADERS)


@router.get("")
async def list_conversations(app_id: str, request: Request, user=Depends(get_current_user),
                             before_id: int | None = Query(None, gt=0)):
    async with get_db_connection(readonly=True) as connection:
        app, _ = await _authorize(connection, app_id, request, user)
        rows = await (await connection.execute("""SELECT c.id,c.chat_name,c.last_activity,b.assistant_id
            FROM APPLICATION_CONVERSATIONS b JOIN CONVERSATIONS c ON c.id=b.conversation_id
              AND c.user_id=b.user_id AND c.role_id=b.prompt_id
            WHERE b.app_id=? AND (? IS NULL OR c.id<?) ORDER BY c.id DESC LIMIT 26""", (app_id, before_id, before_id))).fetchall()
    await _audit(request, user, app_id, resource="application")
    conversations = [dict(row) for row in rows[:25]]
    return await _page(request, user, app, review_conversation=None, review_conversations=conversations,
                       review_next_id=conversations[-1]["id"] if len(rows) > 25 else None)


def _blocks(message, username):
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        payload = convert_legacy_markdown_images_to_blocks(message, username)
    return payload if isinstance(payload, list) else [{"type": "text", "text": message}]


@router.get("/{conversation_id}")
async def conversation(app_id: str, conversation_id: int, request: Request, user=Depends(get_current_user),
                       before_id: int | None = Query(None, gt=0)):
    async with get_db_connection(readonly=True) as connection:
        app, binding = await _authorize(connection, app_id, request, user, conversation_id)
        rows = await (await connection.execute("""SELECT id,message,type,date FROM MESSAGES
            WHERE conversation_id=? AND user_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT 51""",
            (conversation_id, binding["user_id"], before_id, before_id))).fetchall()
        page = [dict(row) for row in rows[:50]]
        audio = []
        if page:
            ids = [row["id"] for row in page]
            placeholders = ",".join("?" for _ in ids)
            audio = await (await connection.execute(f"""SELECT a.public_id,a.message_id,a.original_filename
                FROM FILE_ATTACHMENTS a JOIN FILE_BLOBS b ON b.id=a.blob_id
                WHERE a.conversation_id=? AND a.user_id=? AND a.message_id IN ({placeholders})
                  AND a.attachment_type='audio' AND a.status='active' AND b.status='ready' ORDER BY a.id""",
                (conversation_id, binding["user_id"], *ids))).fetchall()
    await _audit(request, user, app_id, binding=binding, resource_id=conversation_id)
    for row in page:
        row["message"] = custom_unescape(row["message"])
        if row["type"] == "bot":
            row["message"] = strip_tagged_thinking_prefix(row["message"])
    messages = [(row["id"], row["message"]) for row in page]
    attachments = await preload_attachment_records_for_messages(messages, user_id=binding["user_id"], conversation_id=conversation_id)
    media = await preload_generated_media_for_messages(messages, user_id=binding["user_id"], conversation_id=conversation_id)
    base = f"/applications/manage/{app_id}/review/{conversation_id}"
    for row in page:
        row["blocks"] = []
        for block in _blocks(row["message"], binding["username"]):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                row["blocks"].append({"kind": "text", "text": str(block.get("text", ""))})
                continue
            info = block.get(kind, {})
            if not isinstance(info, dict):
                continue
            attachment = attachments.get(info.get("attachment_ref"))
            if attachment and attachment["message_id"] == row["id"]:
                row["blocks"].append({"kind": "image" if attachment["attachment_type"] == "image" else "file",
                    "url": base + "/attachments/" + attachment["public_id"], "name": attachment["original_filename"]})
            elif (kind in {"image_url", "video_url"} and (record := media.get(info.get("url")))
                    and record["kind"] == ("image" if kind == "image_url" else "video")):
                row["blocks"].append({"kind": "image" if record["kind"] == "image" else "video",
                    "url": base + "/media/" + str(record["id"]), "name": info.get("alt", "")})
            else:
                row["blocks"].append({"kind": "unavailable"})
        row["audio"] = [{"url": base + "/attachments/" + item["public_id"], "name": item["original_filename"]}
                        for item in audio if item["message_id"] == row["id"]]
    return await _page(request, user, app, review_conversation=binding, review_messages=list(reversed(page)),
                       review_next_id=page[-1]["id"] if len(rows) > 50 else None)


@router.get("/{conversation_id}/attachments/{public_id}")
async def attachment(app_id: str, conversation_id: int, public_id: str, request: Request, user=Depends(get_current_user)):
    async with get_db_connection(readonly=True) as connection:
        _, binding = await _authorize(connection, app_id, request, user, conversation_id)
        resolved = await get_attachment_path_for_user(connection, public_id=public_id, user_id=binding["user_id"])
        if not resolved or resolved[1]["conversation_id"] != conversation_id or resolved[1]["message_id"] is None:
            raise HTTPException(404, "not_found")
        path, record = resolved
        message = await _one(connection, "SELECT 1 FROM MESSAGES WHERE id=? AND conversation_id=? AND user_id=?",
                             (record["message_id"], conversation_id, binding["user_id"]))
        if not message:
            raise HTTPException(404, "not_found")
    await _audit(request, user, app_id, binding=binding, resource="attachment", resource_id=record["id"])
    return FileResponse(path, media_type=record["mime_detected"], headers=_MEDIA_HEADERS,
        filename=record["original_filename"] if record["attachment_type"] in {"text", "pdf"} else None)


@router.get("/{conversation_id}/media/{media_id}")
async def generated_media(app_id: str, conversation_id: int, media_id: int, request: Request, user=Depends(get_current_user)):
    async with get_db_connection(readonly=True) as connection:
        _, binding = await _authorize(connection, app_id, request, user, conversation_id)
        record = await _one(connection, "SELECT * FROM GENERATED_MEDIA_FILES WHERE id=? AND conversation_id=? AND user_id=?",
                            (media_id, conversation_id, binding["user_id"]))
        if not record:
            raise HTTPException(404, "not_found")
        path = generated_media_path(record)
        if not path.is_file():
            raise HTTPException(404, "not_found")
    await _audit(request, user, app_id, binding=binding, resource="generated_media", resource_id=media_id)
    return FileResponse(path, headers=_MEDIA_HEADERS)
