import asyncio
import math
from urllib.parse import quote, urlencode

import aiohttp
import orjson
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user, unauthenticated_response
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, get_template_context, templates
from database import get_db_connection
from ai_runtime.voice_resolution import (
    CanonicalVoiceResolutionError,
    require_elevenlabs_webrtc_compatible,
    resolve_prompt_voice,
)
from log_config import logger
from i18n import get_translator
from models import User
from tools.tts_config import (
    VALID_FORMATS,
    VALID_MODELS,
    WS_INCOMPATIBLE_MODELS,
    get_tts_config,
    invalidate_tts_config_cache,
)
from tools.tts_load_balancer import get_elevenlabs_key


router = APIRouter()


async def _prompt_webrtc_voice_status(conn, prompt_id: int, t) -> dict:
    try:
        voice = await resolve_prompt_voice(prompt_id, conn=conn)
    except CanonicalVoiceResolutionError as exc:
        return {
            "webrtc_compatible": False,
            "compatibility_code": exc.code,
            "compatibility_reason": t("admin_voice_integrations.voice." + exc.code),
            "canonical_voice_code": None,
            "canonical_voice_provider": None,
            "canonical_voice_inherited": False,
        }

    try:
        require_elevenlabs_webrtc_compatible(voice)
    except CanonicalVoiceResolutionError as exc:
        compatible = False
        code = exc.code
        reason = t("admin_voice_integrations.voice." + exc.code, provider=voice.provider)
    else:
        compatible = True
        code = None
        reason = t("admin_voice_integrations.voice.compatible")

    return {
        "webrtc_compatible": compatible,
        "compatibility_code": code,
        "compatibility_reason": reason,
        "canonical_voice_code": voice.voice_code,
        "canonical_voice_provider": voice.provider,
        "canonical_voice_inherited": voice.inherited_default,
    }


def _login_response(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        },
    )


@router.get("/admin/elevenlabs-agents", response_class=HTMLResponse)
async def admin_elevenlabs_agents(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    message = request.query_params.get("message")
    error = request.query_params.get("error")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT agent_id, agent_name, is_default, created_at FROM ELEVENLABS_AGENTS ORDER BY created_at DESC"
        )
        agents = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute(
            """
            SELECT pam.prompt_id, p.name AS prompt_name, pam.agent_id
            FROM PROMPT_AGENT_MAPPING pam
            LEFT JOIN PROMPTS p ON p.id = pam.prompt_id
            ORDER BY p.name COLLATE NOCASE ASC
            """
        )
        mappings = [dict(row) for row in await cursor.fetchall()]

        cursor = await conn.execute("SELECT id, name FROM PROMPTS ORDER BY name ASC")
        prompts = [dict(row) for row in await cursor.fetchall()]
        voice_canonicalization_conflicts = []
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='VOICE_CANONICALIZATION_CONFLICTS'"
        )
        if await cursor.fetchone() is not None:
            cursor = await conn.execute(
                """
                SELECT c.prompt_id_snapshot, p.name AS prompt_name,
                       c.legacy_voice_code, c.reason, c.created_at
                FROM VOICE_CANONICALIZATION_CONFLICTS c
                LEFT JOIN PROMPTS p ON p.id=c.prompt_id
                WHERE c.status='needs_attention'
                ORDER BY c.created_at DESC, c.id DESC
                """
            )
            voice_canonicalization_conflicts = [
                dict(row) for row in await cursor.fetchall()
            ]
        for conflict in voice_canonicalization_conflicts:
            reason = conflict["reason"]
            conflict["reason_label"] = t(
                "admin_voice_integrations.voice." + reason
                if reason in {"explicit_voice_mismatch", "legacy_voice_unresolved", "legacy_voice_ambiguous"}
                else "admin_voice_integrations.voice.conflict"
            )
        for mapping in mappings:
            mapping_prompt_id = int(mapping["prompt_id"])
            mapping.update(
                await _prompt_webrtc_voice_status(conn, mapping_prompt_id, t)
            )

    context = await get_template_context(request, current_user)
    context.update(
        {
            "agents": agents,
            "mappings": mappings,
            "prompts": prompts,
            "voice_canonicalization_conflicts": voice_canonicalization_conflicts,
            "message": message,
            "error": error,
        }
    )
    return templates.TemplateResponse("admin_elevenlabs.html", context)


@router.post("/admin/elevenlabs-agents")
async def create_or_update_elevenlabs_agent(
    request: Request,
    current_user: User = Depends(get_current_user),
    agent_id: str = Form(...),
    agent_name: str = Form(""),
    make_default: str | None = Form(None),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    agent_id_clean = (agent_id or "").strip()
    agent_name_clean = (agent_name or "").strip()
    make_default_flag = bool(make_default)

    if not agent_id_clean:
        query = urlencode({"error": t("admin_voice_integrations.error.agent_id")})
        return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    existing = None
    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                "SELECT id, is_default FROM ELEVENLABS_AGENTS WHERE agent_id = ?",
                (agent_id_clean,),
            )
            existing = await cursor.fetchone()

            if make_default_flag:
                await conn.execute("UPDATE ELEVENLABS_AGENTS SET is_default = 0 WHERE is_default = 1")

            if existing:
                current_default = int(existing["is_default"])
                new_default = 1 if make_default_flag else current_default
                await conn.execute(
                    "UPDATE ELEVENLABS_AGENTS SET agent_name = ?, is_default = ? WHERE agent_id = ?",
                    (agent_name_clean or None, new_default, agent_id_clean),
                )
            else:
                await conn.execute(
                    "INSERT INTO ELEVENLABS_AGENTS (agent_id, agent_name, is_default) VALUES (?, ?, ?)",
                    (agent_id_clean, agent_name_clean or None, 1 if make_default_flag else 0),
                )

            if make_default_flag:
                await conn.execute(
                    "UPDATE ELEVENLABS_AGENTS SET is_default = 1 WHERE agent_id = ?",
                    (agent_id_clean,),
                )

            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            logger.exception("[ElevenLabs] Failed to save agent %s: %s", agent_id_clean, exc)
            query = urlencode({"error": t("admin_voice_integrations.error.agent_save")})
            return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    message = t("admin_voice_integrations.notice.agent_updated") if existing else t("admin_voice_integrations.notice.agent_created")
    query = urlencode({"message": message})
    return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)


@router.post("/admin/elevenlabs-agents/{agent_id}/set-default")
async def set_default_elevenlabs_agent(
    request: Request,
    agent_id: str,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    agent_id_clean = (agent_id or "").strip()
    if not agent_id_clean:
        query = urlencode({"error": t("admin_voice_integrations.error.agent_missing")})
        return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute("UPDATE ELEVENLABS_AGENTS SET is_default = 0 WHERE is_default = 1")
            cursor = await conn.execute(
                "UPDATE ELEVENLABS_AGENTS SET is_default = 1 WHERE agent_id = ?",
                (agent_id_clean,),
            )
            if cursor.rowcount == 0:
                await conn.rollback()
                query = urlencode({"error": t("admin_voice_integrations.error.agent_missing")})
                return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            logger.exception("[ElevenLabs] Failed to set default agent %s: %s", agent_id_clean, exc)
            query = urlencode({"error": t("admin_voice_integrations.error.agent_update")})
            return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    query = urlencode({"message": t("admin_voice_integrations.notice.default_updated")})
    return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)


@router.post("/admin/elevenlabs-agents/{agent_id}/delete")
async def delete_elevenlabs_agent(
    request: Request,
    agent_id: str,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    agent_id_clean = (agent_id or "").strip()
    if not agent_id_clean:
        query = urlencode({"error": t("admin_voice_integrations.error.agent_missing")})
        return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute("DELETE FROM PROMPT_AGENT_MAPPING WHERE agent_id = ?", (agent_id_clean,))
            cursor = await conn.execute(
                "DELETE FROM ELEVENLABS_AGENTS WHERE agent_id = ?",
                (agent_id_clean,),
            )
            if cursor.rowcount == 0:
                await conn.rollback()
                query = urlencode({"error": t("admin_voice_integrations.error.agent_missing")})
                return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            logger.exception("[ElevenLabs] Failed to delete agent %s: %s", agent_id_clean, exc)
            query = urlencode({"error": t("admin_voice_integrations.error.agent_delete")})
            return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    query = urlencode({"message": t("admin_voice_integrations.notice.agent_deleted")})
    return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)


@router.post("/admin/elevenlabs-agents/mapping")
async def update_elevenlabs_mapping(
    request: Request,
    current_user: User = Depends(get_current_user),
    prompt_id: int = Form(...),
    agent_id: str = Form(""),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    agent_id_clean = (agent_id or "").strip()

    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if agent_id_clean:
                cursor = await conn.execute(
                    "SELECT 1 FROM ELEVENLABS_AGENTS WHERE agent_id = ?",
                    (agent_id_clean,),
                )
                if not await cursor.fetchone():
                    await conn.rollback()
                    query = urlencode({"error": t("admin_voice_integrations.error.agent_missing")})
                    return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

                voice_status = await _prompt_webrtc_voice_status(conn, prompt_id, t)
                if voice_status["compatibility_code"] == "prompt_not_found":
                    await conn.rollback()
                    query = urlencode({"error": voice_status["compatibility_reason"]})
                    return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

                await conn.execute(
                    "INSERT INTO PROMPT_AGENT_MAPPING (prompt_id, agent_id, voice_id) "
                    "VALUES (?, ?, NULL) "
                    "ON CONFLICT(prompt_id) DO UPDATE SET "
                    "agent_id = excluded.agent_id, voice_id = NULL, "
                    "created_at = CURRENT_TIMESTAMP",
                    (prompt_id, agent_id_clean),
                )
                if voice_status["webrtc_compatible"]:
                    message = t("admin_voice_integrations.notice.mapping_compatible")
                else:
                    message = (
                        t("admin_voice_integrations.notice.mapping_incompatible", reason=voice_status["compatibility_reason"])
                    )
            else:
                await conn.execute("DELETE FROM PROMPT_AGENT_MAPPING WHERE prompt_id = ?", (prompt_id,))
                message = t("admin_voice_integrations.notice.mapping_deleted")
            await conn.commit()
        except Exception as exc:
            await conn.rollback()
            logger.exception("[ElevenLabs] Failed to update prompt mapping for %s: %s", prompt_id, exc)
            query = urlencode({"error": t("admin_voice_integrations.error.mapping")})
            return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)

    query = urlencode({"message": message})
    return RedirectResponse(url=f"/admin/elevenlabs-agents?{query}", status_code=303)


@router.get("/admin/elevenlabs-voices", response_class=HTMLResponse)
async def admin_elevenlabs_voices(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        return JSONResponse(content={"error": t("management_operations_errors.admin_required")}, status_code=403)

    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            "SELECT voice_code FROM VOICES WHERE tts_service = 1 ORDER BY name"
        ) as cursor:
            enabled_voices = [row[0] for row in await cursor.fetchall()]

    context = await get_template_context(request, current_user)
    context.update(
        {
            "enabled_voices": enabled_voices,
            "enabled_count": len(enabled_voices),
        }
    )
    return templates.TemplateResponse("admin_elevenlabs_voices.html", context)


@router.get("/admin/elevenlabs-tts", response_class=HTMLResponse)
async def admin_elevenlabs_tts(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    translator = get_translator(request, current_user)
    t = translator.t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    error = None
    error_code = request.query_params.get("error")
    if error_code == "ws_model":
        error = t("admin_elevenlabs.tts.warning_v3")
    elif error_code == "ws_format":
        error = t("admin_elevenlabs.tts.warning_format")
    elif (
        error_code == "invalid_value"
        and request.query_params.get("field") in {"model", "format"}
        and request.query_params.get("profile") in {"webchat", "external", "mp3"}
    ):
        profile_keys = {"webchat": "tts.webchat", "external": "tts.external", "mp3": "tts.download"}
        error = t(
            "admin_elevenlabs.error.invalid_value",
            field=t("admin_elevenlabs.label." + request.query_params["field"]),
            profile=t("admin_elevenlabs." + profile_keys[request.query_params["profile"]]),
        )
    elif error_code:
        error = t("admin_elevenlabs.error.save_failed")
    config = await get_tts_config()
    context = await get_template_context(request, current_user)
    context.update(
        {
            "config": config,
            "valid_models": [(value, t("admin_elevenlabs.model." + value)) for value, _description in VALID_MODELS],
            "valid_formats": [(value, t("admin_elevenlabs.format." + value), codec) for value, _description, codec in VALID_FORMATS],
            "ws_incompatible_models": list(WS_INCOMPATIBLE_MODELS),
            "message": t("admin_elevenlabs.notice.saved") if request.query_params.get("saved") == "1" else None,
            "error": error,
        }
    )
    return templates.TemplateResponse("admin_elevenlabs_tts.html", context)


@router.post("/admin/elevenlabs-tts", response_class=HTMLResponse)
async def admin_elevenlabs_tts_save(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    translator = get_translator(request, current_user)
    t = translator.t
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("management_operations_errors.admin_required"))

    form = await request.form()
    action = form.get("action")

    if action != "save_config":
        return RedirectResponse(url="/admin/elevenlabs-tts", status_code=303)

    valid_model_ids = {m[0] for m in VALID_MODELS}
    valid_format_ids = {f[0] for f in VALID_FORMATS}

    webchat_model = form.get("tts_webchat_model", "")
    webchat_ws = form.get("tts_webchat_ws_enabled")
    if webchat_model in WS_INCOMPATIBLE_MODELS and webchat_ws:
        return RedirectResponse(
            url="/admin/elevenlabs-tts?error=ws_model",
            status_code=303,
        )

    webchat_format = form.get("tts_webchat_format", "")
    if webchat_ws and webchat_format and not webchat_format.startswith("mp3"):
        return RedirectResponse(
            url="/admin/elevenlabs-tts?error=ws_format",
            status_code=303,
        )

    try:
        async with get_db_connection() as conn:
            for profile_name in ("webchat", "external", "mp3"):
                prefix = f"tts_{profile_name}_"

                for field in ("model", "format"):
                    val = form.get(f"{prefix}{field}", "")
                    valid_set = valid_model_ids if field == "model" else valid_format_ids
                    if not val:
                        continue
                    if val not in valid_set:
                        return RedirectResponse(
                            url=f"/admin/elevenlabs-tts?error=invalid_value&field={field}&profile={profile_name}",
                            status_code=303,
                        )
                    await conn.execute(
                        """
                        INSERT INTO SYSTEM_CONFIG (key, value, updated_at)
                        VALUES (?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (f"{prefix}{field}", val),
                    )

                for field in ("stability", "similarity"):
                    val = form.get(f"{prefix}{field}", "")
                    if val:
                        try:
                            numeric = float(val)
                            if not math.isfinite(numeric):
                                continue
                            clamped = max(0.0, min(1.0, numeric))
                        except (ValueError, TypeError):
                            continue
                        await conn.execute(
                            """
                            INSERT INTO SYSTEM_CONFIG (key, value, updated_at)
                            VALUES (?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(key) DO UPDATE SET
                                value = excluded.value,
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (f"{prefix}{field}", str(clamped)),
                        )

            ws_enabled = "1" if form.get("tts_webchat_ws_enabled") else "0"
            await conn.execute(
                """
                INSERT INTO SYSTEM_CONFIG (key, value, updated_at)
                VALUES ('tts_webchat_ws_enabled', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (ws_enabled,),
            )

            chunk_schedule = form.get("tts_webchat_chunk_schedule", "").strip()
            if chunk_schedule:
                try:
                    parsed = orjson.loads(chunk_schedule)
                    if (
                        isinstance(parsed, list)
                        and len(parsed) > 0
                        and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in parsed)
                    ):
                        await conn.execute(
                            """
                            INSERT INTO SYSTEM_CONFIG (key, value, updated_at)
                            VALUES ('tts_webchat_chunk_schedule', ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(key) DO UPDATE SET
                                value = excluded.value, updated_at = CURRENT_TIMESTAMP
                            """,
                            (chunk_schedule,),
                        )
                except Exception:
                    pass

            await conn.commit()

        invalidate_tts_config_cache()
        return RedirectResponse(
            url="/admin/elevenlabs-tts?saved=1",
            status_code=303,
        )
    except Exception as e:
        logger.error("Error saving TTS config: %s", e)
        return RedirectResponse(
            url="/admin/elevenlabs-tts?error=save_failed",
            status_code=303,
        )


@router.get("/api/elevenlabs/voices")
async def get_elevenlabs_voices(request: Request, current_user: User = Depends(get_current_user)):
    t = get_translator(request, current_user).t
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": t("management_operations_errors.admin_required")}, status_code=403)

    eleven_key = get_elevenlabs_key()
    if not eleven_key:
        return JSONResponse(content={"error": t("admin_voice_integrations.error.api_key")}, status_code=500)

    try:
        all_voices = []
        next_token = None

        async with aiohttp.ClientSession() as session:
            while True:
                params = {"page_size": 100}
                if next_token:
                    params["next_page_token"] = next_token

                async with session.get(
                    "https://api.elevenlabs.io/v2/voices",
                    headers={"xi-api-key": eleven_key},
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    if response.status != 200:
                        return JSONResponse(
                            content={"error": t("admin_voice_integrations.error.api")},
                            status_code=response.status,
                        )

                    data = await response.json()
                    for voice in data.get("voices", []):
                        labels = voice.get("labels", {})
                        label_list = []
                        for key in ("accent", "gender", "age", "use_case"):
                            if labels.get(key):
                                label_list.append(labels[key])

                        all_voices.append(
                            {
                                "voice_id": voice.get("voice_id", ""),
                                "name": voice.get("name") or t("admin_voice_integrations.ui.unknown"),
                                "category": voice.get("category", "unknown"),
                                "description": voice.get("description", ""),
                                "preview_url": voice.get("preview_url", ""),
                                "labels": label_list,
                                "labels_raw": labels,
                            }
                        )

                    if not data.get("has_more"):
                        break
                    next_token = data.get("next_page_token")

        category_order = {"premade": 0, "professional": 1, "cloned": 2, "generated": 3, "default": 4}
        all_voices.sort(key=lambda x: (category_order.get(x["category"], 99), x["name"].lower()))
        return JSONResponse(content={"voices": all_voices})

    except asyncio.TimeoutError:
        return JSONResponse(content={"error": t("admin_voice_integrations.error.timeout")}, status_code=504)
    except Exception as e:
        logger.exception("Error fetching ElevenLabs voices")
        return JSONResponse(content={"error": t("admin_voice_integrations.error.fetch")}, status_code=500)


@router.post("/api/elevenlabs/sync")
async def sync_elevenlabs_voices(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": t("management_operations_errors.admin_required")}, status_code=403)

    try:
        body = await request.json()
        voices_to_save = body.get("voices", [])
        api_voice_ids = set(body.get("api_voice_ids", []))

        async with get_db_connection() as conn:
            async with conn.execute(
                "SELECT id, voice_code FROM VOICES WHERE tts_service = 1"
            ) as cursor:
                existing = {row[1]: row[0] for row in await cursor.fetchall()}

            new_voice_codes = {voice["voice_id"] for voice in voices_to_save}
            voices_to_remove = [
                code
                for code in existing.keys()
                if code not in new_voice_codes and code in api_voice_ids
            ]
            removed_count = 0
            deprecated_count = 0
            if voices_to_remove:
                remove_ids = [existing[code] for code in voices_to_remove]
                placeholders = ",".join("?" * len(remove_ids))

                referenced_ids = set()
                async with conn.execute(
                    f"SELECT DISTINCT voice_id FROM PROMPTS WHERE voice_id IN ({placeholders})",
                    remove_ids,
                ) as cursor:
                    referenced_ids.update(row[0] for row in await cursor.fetchall())
                async with conn.execute(
                    f"SELECT DISTINCT voice_id FROM USER_DETAILS WHERE voice_id IN ({placeholders})",
                    remove_ids,
                ) as cursor:
                    referenced_ids.update(row[0] for row in await cursor.fetchall())

                for code in voices_to_remove:
                    voice_id = existing[code]
                    if voice_id in referenced_ids:
                        await conn.execute(
                            "UPDATE VOICES SET deprecated = 1 WHERE id = ?",
                            (voice_id,),
                        )
                        deprecated_count += 1
                    else:
                        await conn.execute("DELETE FROM VOICES WHERE id = ?", (voice_id,))
                        removed_count += 1

            added_count = 0
            updated_count = 0
            for voice in voices_to_save:
                voice_code = voice["voice_id"]
                voice_name = voice["name"]

                if voice_code in existing:
                    await conn.execute(
                        "UPDATE VOICES SET name = ?, deprecated = 0 WHERE id = ?",
                        (voice_name, existing[voice_code]),
                    )
                    updated_count += 1
                else:
                    await conn.execute(
                        "INSERT INTO VOICES (name, voice_code, tts_service) VALUES (?, ?, 1)",
                        (voice_name, voice_code),
                    )
                    added_count += 1

            await conn.commit()

        return JSONResponse(
            content={
                "success": True,
                "added": added_count,
                "updated": updated_count,
                "removed": removed_count,
                "deprecated": deprecated_count,
            }
        )

    except Exception as e:
        logger.exception("Error syncing ElevenLabs voices")
        return JSONResponse(content={"error": t("admin_voice_integrations.error.sync")}, status_code=500)
