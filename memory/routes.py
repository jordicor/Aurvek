from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import get_current_user
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, get_template_context, templates
from database import get_db_connection
from log_config import logger
from memory.config import (
    get_active_memory_provider,
    get_memory_config,
    get_user_memory_preferences,
    mem0_config_from_mapping,
    reset_mem0_admin_config,
    save_mem0_admin_config,
    save_memory_admin_config,
    save_no_memory_context_config,
    template_memory_config,
)
from memory.health import (
    get_admin_memory_health_snapshot,
    get_user_memory_health_snapshot,
)
from memory.providers.mem0 import Mem0Provider
from memory.sync import DEFAULT_BATCH_SIZE, get_memory_sync_status, start_mem0_history_sync
from models import User


router = APIRouter()


def _login_response(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        },
    )


async def _admin_only(request: Request, current_user: User | None):
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    return None


async def _read_json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError("Invalid JSON payload.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid JSON payload.")
    return data


async def _active_admin_memory_health_snapshot() -> dict[str, Any]:
    return get_admin_memory_health_snapshot(await get_active_memory_provider())


async def _test_provider_before_activation(data: dict[str, Any]) -> tuple[bool, str]:
    provider = str(data.get("active_provider") or "").strip().lower()
    if provider in {"", "none"}:
        return True, ""

    if provider == "atagia":
        from atagia_bridge import AtagiaBridge
        from atagia_config import bridge_config_from_mapping, get_atagia_config

        config = bridge_config_from_mapping(await get_atagia_config(), enabled_override=True)
        bridge = AtagiaBridge(config)
        try:
            ok, message = await bridge.test_connection()
        finally:
            await bridge.close()
        if not ok:
            return False, f"Atagia cannot be activated: {message or 'connection test failed'}"
        return True, ""

    if provider == "mem0":
        mem0 = Mem0Provider(mem0_config_from_mapping(await get_memory_config()))
        ok, message = await mem0.test_connection()
        if not ok:
            return False, f"Mem0 cannot be activated: {message or 'connection test failed'}"
        return True, ""

    return True, ""


async def _load_context_exception_options() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    llm_options: list[dict[str, Any]] = []
    prompt_options: list[dict[str, Any]] = []
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT id, machine, model, COALESCE(NULLIF(display_name, ''), model) AS label
            FROM LLM
            WHERE COALESCE(enabled, 1) = 1
              AND machine != 'GPTSub'
            ORDER BY machine ASC, label ASC, id ASC
            """
        )
        llm_rows = await cursor.fetchall()
        llm_options = [
            {
                "id": int(row[0]),
                "label": f"{row[1]} - {row[3] or row[2]}",
            }
            for row in llm_rows
        ]

        cursor = await conn.execute(
            """
            SELECT id, COALESCE(NULLIF(name, ''), 'Prompt ' || id) AS label
            FROM PROMPTS
            ORDER BY label ASC, id ASC
            """
        )
        prompt_rows = await cursor.fetchall()
        prompt_options = [
            {"id": int(row[0]), "label": str(row[1])}
            for row in prompt_rows
        ]
    return llm_options, prompt_options


def _enrich_context_exceptions(
    exceptions: list[dict[str, Any]],
    llm_options: list[dict[str, Any]],
    prompt_options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    labels = {
        "llm": {item["id"]: item["label"] for item in llm_options},
        "prompt": {item["id"]: item["label"] for item in prompt_options},
    }
    enriched = []
    for exception in exceptions:
        item = dict(exception)
        item["label"] = (
            item.get("label")
            or labels.get(item.get("type"), {}).get(item.get("id"))
            or f"{item.get('type')} {item.get('id')}"
        )
        enriched.append(item)
    return enriched


@router.get("/admin/memory", response_class=HTMLResponse)
async def admin_memory_get(request: Request, current_user: User = Depends(get_current_user)):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return blocked

    from atagia_config import get_atagia_config, template_config as template_atagia_config

    memory_config = template_memory_config(await get_memory_config())
    llm_options, prompt_options = await _load_context_exception_options()
    memory_config["none_context"]["exceptions"] = _enrich_context_exceptions(
        memory_config["none_context"]["exceptions"],
        llm_options,
        prompt_options,
    )

    context = await get_template_context(request, current_user)
    context["memory_config"] = memory_config
    context["atagia_config"] = template_atagia_config(await get_atagia_config())
    context["llm_options"] = llm_options
    context["prompt_options"] = prompt_options
    return templates.TemplateResponse("admin_memory.html", context)


@router.get("/admin/mem0", response_class=HTMLResponse)
async def admin_mem0_get(request: Request, current_user: User = Depends(get_current_user)):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return blocked

    context = await get_template_context(request, current_user)
    context["memory_config"] = template_memory_config(await get_memory_config())
    return templates.TemplateResponse("admin_mem0.html", context)


@router.post("/admin/memory/provider")
async def admin_memory_provider_post(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    try:
        data = await _read_json_body(request)
        ok, message = await _test_provider_before_activation(data)
        if not ok:
            return JSONResponse(
                content={
                    "success": False,
                    "message": message,
                    "health": await _active_admin_memory_health_snapshot(),
                },
                status_code=502,
            )
        await save_memory_admin_config(data)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Failed to save memory provider settings: %s", exc, exc_info=True)
        return JSONResponse(
            content={"success": False, "message": "Failed to save memory provider settings."},
            status_code=500,
        )

    try:
        from atagia_bridge import reset_atagia_bridge

        await reset_atagia_bridge()
    except Exception:
        logger.warning("Failed to reset Atagia bridge after provider change", exc_info=True)
    return JSONResponse(
        content={
            "success": True,
            "message": "Memory provider settings saved.",
            "config": template_memory_config(await get_memory_config()),
            "health": await _active_admin_memory_health_snapshot(),
        }
    )


@router.get("/api/user/memory-status")
async def user_memory_status(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return JSONResponse(content={"success": False, "message": "Not authenticated"}, status_code=401)
    provider = await get_active_memory_provider()
    enabled = provider != "none"
    if enabled:
        preferences = await get_user_memory_preferences(current_user.id, provider)
        enabled = preferences.get("remember_across_chats") is not False
    return JSONResponse(
        content={
            "success": True,
            "health": get_user_memory_health_snapshot(provider, enabled=enabled),
        }
    )


@router.get("/admin/memory/status")
async def admin_memory_status(current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    active_provider = await get_active_memory_provider()
    return JSONResponse(
        content={
            "success": True,
            "active_provider": active_provider,
            "health": get_admin_memory_health_snapshot(active_provider),
            "providers": {
                "none": get_admin_memory_health_snapshot("none"),
                "atagia": get_admin_memory_health_snapshot("atagia"),
                "mem0": get_admin_memory_health_snapshot("mem0"),
            },
        }
    )


@router.post("/admin/memory/no-memory-context")
async def admin_memory_no_memory_context_post(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    try:
        data = await _read_json_body(request)
        await save_no_memory_context_config(data)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Failed to save no-memory context settings: %s", exc, exc_info=True)
        return JSONResponse(
            content={"success": False, "message": "Failed to save no-memory context settings."},
            status_code=500,
        )
    return JSONResponse(
        content={
            "success": True,
            "message": "No-memory context settings saved.",
            "config": template_memory_config(await get_memory_config()),
        }
    )


@router.post("/admin/memory/mem0")
async def admin_memory_mem0_post(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    try:
        data = await _read_json_body(request)
        await save_mem0_admin_config(data)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Failed to save Mem0 configuration: %s", exc, exc_info=True)
        return JSONResponse(
            content={"success": False, "message": "Failed to save Mem0 configuration."},
            status_code=500,
        )
    return JSONResponse(
        content={
            "success": True,
            "message": "Mem0 configuration saved.",
            "config": template_memory_config(await get_memory_config()),
        }
    )


@router.post("/admin/memory/mem0/defaults")
async def admin_memory_mem0_defaults(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    config = await reset_mem0_admin_config()
    return JSONResponse(
        content={
            "success": True,
            "message": "Mem0 defaults restored.",
            "config": template_memory_config(config),
        }
    )


@router.post("/admin/memory/mem0/test-connection")
async def admin_memory_mem0_test(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from memory.config import mem0_config_from_mapping, validate_mem0_base_url

    try:
        data = await _read_json_body(request)
        preview = dict(await get_memory_config())
        preview.update(
            {
                "mem0_base_url": data.get("base_url", preview.get("mem0_base_url")),
                "mem0_api_key": data.get("api_key") or preview.get("mem0_api_key", ""),
                "mem0_platform_id": data.get("platform_id", preview.get("mem0_platform_id")),
                "mem0_timeout_seconds": data.get("timeout_seconds", preview.get("mem0_timeout_seconds")),
                "mem0_top_k": data.get("top_k", preview.get("mem0_top_k")),
            }
        )
        ok, message = validate_mem0_base_url(str(preview.get("mem0_base_url") or ""))
        if not ok:
            raise ValueError(message)
        provider = Mem0Provider(mem0_config_from_mapping(preview))
        ok, message = await provider.test_connection()
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)

    return JSONResponse(
        content={"success": ok, "message": message},
        status_code=200,
    )


@router.post("/admin/memory/mem0/sync")
async def admin_memory_mem0_sync(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    blocked = await _admin_only(request, current_user)
    if blocked is not None:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        batch_size = int(data.get("batch_size") or DEFAULT_BATCH_SIZE)
    except (TypeError, ValueError):
        batch_size = DEFAULT_BATCH_SIZE
    result = await start_mem0_history_sync(batch_size=max(1, min(batch_size, 1000)))
    return JSONResponse(
        content={"success": bool(result.get("started")), **result},
        status_code=202 if result.get("started") else 409,
    )


@router.get("/admin/memory/mem0/sync-status")
async def admin_memory_mem0_sync_status(current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    return JSONResponse(content={"success": True, "status": await get_memory_sync_status("mem0")})


@router.get("/admin/atagia/redirect")
async def admin_atagia_redirect():
    return RedirectResponse(url="/admin/memory", status_code=302)
