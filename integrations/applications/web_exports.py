"""Application access and funding around the native PDF/MP3 Dramatiq exports."""
from __future__ import annotations

import json
import re
import secrets
import time
from contextlib import nullcontext
from dataclasses import fields
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from database import get_db_connection
from integrations.embed.context import get_embed_principal
from integrations.embed.models import EmbedError
from .models import ApplicationContext
from .runtime import authorize_application_read

router = APIRouter()
JOB_TTL = 86_400
JOB_TIMEOUT = 900  # Native worker: at most five minutes queued and ten running.
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
TERMINAL = {"completed", "failed", "cancelled"}


def _key(job_id):
    return f"application_export:{job_id}"


def _lock(conversation_id, user_id, kind):
    return f"pdf_lock:{conversation_id}" if kind == "pdf" else f"mp3_lock:{conversation_id}:{user_id}"


def _url(conversation_id, kind, job_id):
    return f"/api/conversations/{conversation_id}/exports/{kind}/{job_id}"


async def export_scope(connection, conversation_id, user_id, kind, *, mutation=False):
    if kind not in {"pdf", "mp3"}:
        raise EmbedError("not_found", 404)
    scope = await authorize_application_read(connection, conversation_id, user_id)
    if scope is None or not scope.capabilities.get(f"export_{kind}"):
        raise EmbedError("application_capability_denied", 403)
    if kind == "mp3" and not scope.capabilities.get("tts"):
        raise EmbedError("application_capability_denied", 403)
    row = await (await connection.execute(
        "SELECT locked,is_incognito FROM CONVERSATIONS WHERE id=? AND user_id=?",
        (conversation_id, user_id))).fetchone()
    if not row or row["is_incognito"] or (mutation and row["locked"]):
        raise EmbedError("conversation_unavailable", 403)
    return scope


async def _request(request, conversation_id, kind, *, mutation=False):
    from auth import get_current_user
    from common import READONLY_MODE
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(401, "unauthenticated")
    if mutation:
        from request_security import validate_mutation_request
        if READONLY_MODE:
            raise HTTPException(503, "read_only")
        if validate_mutation_request(request) is not None:
            raise HTTPException(403, "unauthenticated")
    principal = get_embed_principal(request)
    if principal is not None and principal.conversation_id != conversation_id:
        raise HTTPException(404, "not_found")
    async with get_db_connection(readonly=True) as connection:
        try:
            scope = await export_scope(connection, conversation_id, user.id, kind, mutation=mutation)
        except EmbedError as exc:
            raise HTTPException(exc.status_code, exc.code) from exc
    return user, scope


async def _job(redis, job_id, scope, kind):
    if not JOB_ID.fullmatch(job_id):
        raise HTTPException(404, "not_found")
    job = await redis.hgetall(_key(job_id))
    if not job or (job["conversation_id"], job["user_id"], job["kind"]) != (
            str(scope.conversation_id), str(scope.user_id), kind):
        raise HTTPException(404, "not_found")
    expected = ApplicationContext(**json.loads(job["scope"]))
    if scope != expected:
        raise HTTPException(403, "application_scope_changed")
    if job["status"] not in TERMINAL and time.time() > float(job["created_at"]) + JOB_TIMEOUT:
        job.update(status="failed", error="export_timed_out")
        await redis.hset(_key(job_id), mapping={"status": "failed", "error": job["error"], "stop": "1"})
    return job


def _public(job, job_id):
    status = job["status"]
    if status not in TERMINAL and job.get("stop") == "1":
        status = "ending"
    url = _url(job["conversation_id"], job["kind"], job_id)
    result = {"job_id": job_id, "status": status, "status_url": url}
    if status == "completed":
        result["download_url"] = url + "/content"
    if job.get("error"):
        result["error"] = job["error"]
    return result


@router.post("/api/conversations/{conversation_id}/exports/{kind}")
async def start_export(conversation_id: int, kind: str, request: Request):
    from i18n import get_translator
    from rediscfg import redis_client
    from storage_quota import ensure_generation_headroom, StorageQuotaExceededError
    from tasks import generate_pdf_task, generate_mp3_task
    from .billing import admit_application_billing
    user, scope = await _request(request, conversation_id, kind, mutation=True)
    try:
        async with get_db_connection(readonly=True) as connection:
            await ensure_generation_headroom(connection, scope.user_id)
    except StorageQuotaExceededError as exc:
        raise HTTPException(413, "storage_quota_exceeded") from exc
    job_id = secrets.token_urlsafe(24)
    lock = _lock(conversation_id, user.id, kind)
    if not await redis_client.set(lock, job_id, nx=True, ex=JOB_TIMEOUT):
        raise HTTPException(409, "export_in_progress")
    try:
        operation = await admit_application_billing(scope) if kind == "mp3" else None
        data = {"status": "queued", "kind": kind, "conversation_id": str(conversation_id),
            "user_id": str(user.id), "created_at": str(time.time()), "stop": "0",
            "operation_id": operation.operation_id if operation else "",
            "scope": json.dumps({field.name: dict(scope.capabilities) if field.name == "capabilities"
                else getattr(scope, field.name) for field in fields(scope)})}
        await redis_client.hset(_key(job_id), mapping=data)
        await redis_client.expire(_key(job_id), JOB_TTL)
        actor = generate_pdf_task if kind == "pdf" else generate_mp3_task
        actor.send(conversation_id=conversation_id, user_id=user.id, is_admin=False,
            ui_language=get_translator(request, user).language, application_job_id=job_id)
    except BaseException as exc:
        await redis_client.delete(_key(job_id))
        await _release_lock(redis_client, lock, job_id)
        if isinstance(exc, EmbedError):
            raise HTTPException(exc.status_code, exc.code) from exc
        raise
    return JSONResponse(_public(data, job_id), status_code=202)


@router.get("/api/conversations/{conversation_id}/exports/{kind}/{job_id}")
async def export_status(conversation_id: int, kind: str, job_id: str, request: Request):
    from rediscfg import redis_client
    _, scope = await _request(request, conversation_id, kind)
    return JSONResponse(_public(await _job(redis_client, job_id, scope, kind), job_id),
        headers={"Cache-Control": "private, no-store"})


@router.post("/api/conversations/{conversation_id}/exports/{kind}/{job_id}/stop")
async def stop_export(conversation_id: int, kind: str, job_id: str, request: Request):
    from rediscfg import redis_client
    _, scope = await _request(request, conversation_id, kind, mutation=True)
    job = await _job(redis_client, job_id, scope, kind)
    if job["status"] not in TERMINAL:
        await redis_client.eval("""redis.call('HSET',KEYS[1],'stop','1')
            if redis.call('HGET',KEYS[1],'status')=='queued' then
                redis.call('HSET',KEYS[1],'status','cancelled') end return 1""", 1, _key(job_id))
        job = await redis_client.hgetall(_key(job_id))
        if job["status"] == "cancelled":
            await _release_lock(redis_client, _lock(conversation_id, scope.user_id, kind), job_id)
    return _public(job, job_id)


@router.get("/api/conversations/{conversation_id}/exports/{kind}/{job_id}/content")
async def download_export(conversation_id: int, kind: str, job_id: str, request: Request):
    from chat.services.generated_media import generated_media_path
    from rediscfg import redis_client
    _, scope = await _request(request, conversation_id, kind)
    job = await _job(redis_client, job_id, scope, kind)
    if job["status"] != "completed" or not job.get("media_id"):
        raise HTTPException(409, "export_not_ready")
    async with get_db_connection(readonly=True) as connection:
        row = await (await connection.execute(
            "SELECT rel_path FROM GENERATED_MEDIA_FILES WHERE id=? AND conversation_id=? AND user_id=? AND kind=?",
            (job["media_id"], conversation_id, scope.user_id, kind))).fetchone()
    if row is None:
        raise HTTPException(404, "not_found")
    path = generated_media_path(dict(row))
    if not path.is_file():
        raise HTTPException(404, "not_found")
    return FileResponse(path, media_type="application/pdf" if kind == "pdf" else "audio/mpeg",
        filename=f"conversation-{conversation_id}.{kind}", headers={"Cache-Control": "private, no-store"})


async def _release_lock(redis, lock, job_id):
    await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0",
        1, lock, job_id)


class ExportCancelled(Exception):
    pass


async def run_export_job(kind, job_id, conversation_id, user_id, ui_language, *, redis=None):
    """Called by the native export actors; Redis carries only progress and stop."""
    from redis.asyncio import Redis
    from rediscfg import redis_manager
    from log_config import logger
    from storage_quota import normalize_rel_path
    from .billing import binding_contextmanager, load_application_operation, revalidate_application_operation
    from .browser_voice import VoiceTTSBilling
    owned_redis = redis is None
    if redis is None:
        # Each Dramatiq invocation owns a fresh event loop, so don't share its pool.
        redis = Redis(host=redis_manager.REDIS_HOST, port=redis_manager.REDIS_PORT,
            db=redis_manager.REDIS_DB, decode_responses=True)
    lock = _lock(conversation_id, user_id, kind)
    claimed = False
    try:
        job = await redis.hgetall(_key(job_id))
        if (not job or job.get("status") != "queued" or
                (job["kind"], job["conversation_id"], job["user_id"]) != (kind, str(conversation_id), str(user_id))):
            return
        if not await redis.hsetnx(_key(job_id), "worker_started", "1"):
            return
        claimed = True
        expected = ApplicationContext(**json.loads(job["scope"]))
        operation = None
        if kind == "mp3":
            async with get_db_connection(readonly=True) as connection:
                operation = await load_application_operation(connection, job["operation_id"])

        async def check_access():
            if await redis.hget(_key(job_id), "stop") == "1":
                raise ExportCancelled()
            async with get_db_connection(readonly=True) as connection:
                live = await export_scope(connection, conversation_id, user_id, kind, mutation=True)
                if live != expected:
                    raise EmbedError("application_scope_changed", 403)
                if operation is not None:
                    await revalidate_application_operation(operation, connection=connection)

        await check_access()
        await redis.hset(_key(job_id), mapping={"status": "running"})
        with binding_contextmanager(operation) if operation else nullcontext():
            if kind == "pdf":
                from tools.download_pdf import generate_and_save_pdf
                path = await generate_and_save_pdf(conversation_id, user_id, False,
                    ui_language=ui_language, check_access=check_access)
            else:
                from common import Cost
                from tools.download_mp3 import generate_and_save_mp3
                await Cost.initialize()
                adapter = VoiceTTSBilling(SimpleNamespace(user=SimpleNamespace(id=user_id), check_access=check_access))
                path = await generate_and_save_mp3(conversation_id, user_id, False,
                    ui_language=ui_language, check_access=check_access, billing_adapter=adapter)
        await check_access()
        if not path:
            raise RuntimeError("export_unavailable")
        async with get_db_connection(readonly=True) as connection:
            row = await (await connection.execute(
                "SELECT id FROM GENERATED_MEDIA_FILES WHERE rel_path=? AND conversation_id=? AND user_id=? AND kind=?",
                (normalize_rel_path(path), conversation_id, user_id, kind))).fetchone()
        if row is None:
            raise RuntimeError("export_unavailable")
        await redis.hset(_key(job_id), mapping={"status": "completed", "media_id": str(row["id"])})
    except ExportCancelled:
        await redis.hset(_key(job_id), mapping={"status": "cancelled"})
    except Exception as exc:
        logger.warning("Application %s export failed conversation_id=%s (%s)", kind, conversation_id, type(exc).__name__)
        await redis.hset(_key(job_id), mapping={"status": "failed", "error":
            exc.code if isinstance(exc, EmbedError) else "export_unavailable"})
    finally:
        if claimed:
            await _release_lock(redis, lock, job_id)
        if owned_redis:
            await redis.aclose()
