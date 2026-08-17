import asyncio
import base64
import io
import json
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import List
from unicodedata import normalize

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from filelock import FileLock, Timeout as FileLockTimeout
from PIL import Image as PilImage, UnidentifiedImageError

from auth import get_current_user, unauthenticated_response
from captcha_service import get_captcha_config
from cloudflare_geo import (
    CloudflareGeoClient,
    geo_sync_engine,
    get_all_geo_data,
    get_countries_for_continent,
    validate_continent_codes,
    validate_country_codes,
)
from common import (
    GOOGLE_CLIENT_ID,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_UPLOAD_SIZE,
    PRIMARY_APP_DOMAIN,
    fix_landing_seo_tags,
    get_template_context,
    slugify,
    templates,
    validate_path_within_directory,
)
from database import get_db_connection
from llm_catalog import get_selector_llms
from log_config import logger
from marketplace.config import require_creator_tools_enabled
from marketplace.landing.cache import invalidate_landing_cache
from marketplace.landing.jobs import (
    JOBS_DIR,
    get_active_job_for_prompt,
    get_active_welcome_job_for_prompt,
    get_job,
    start_job,
)
from marketplace.landing.paths import get_active_custom_domain
from marketplace.landing.wizard import (
    delete_all_landing_files,
    delete_all_welcome_files,
    is_claude_available,
    list_prompt_files,
    list_welcome_files,
)
from marketplace.services.landing_registration import (
    get_landing_registration_config,
    set_landing_registration_config,
)
from models import User
from prompts import (
    can_manage_prompt,
    create_prompt_directory,
    get_prompt_components_dir,
    get_prompt_info,
    get_prompt_path,
)
from security_guard_llm import check_security


router = APIRouter()

ALLOWED_COMPONENT_TYPES = {"html", "css", "js"}
ALLOWED_EXTENSIONS = {"webp", "jpg", "jpeg", "png", "gif", "ico"}
PUBLICATION_VERIFICATION_UNAVAILABLE = "Publication verification is unavailable."
_PROMPT_PIPELINE_DIR = Path(__file__).resolve().parents[2] / "tools" / "prompt_pipeline"
_LANDING_SAVE_LOCK_DIR = JOBS_DIR / "save-locks"
_LANDING_SAVE_LOCK_TIMEOUT_SECONDS = 30


def _run_landing_publication_check(prompt_id: int, content: str) -> List[str]:
    """Run the private verifier synchronously, failing closed on any gap."""
    if not _PROMPT_PIPELINE_DIR.is_dir():
        return [PUBLICATION_VERIFICATION_UNAVAILABLE]

    try:
        truth_sheet = import_module("tools.prompt_pipeline.truth_sheet")
        verifier = import_module("tools.prompt_pipeline.verifier")
        truth = truth_sheet.load_truth_sheet(prompt_id)
        if truth is None:
            return [PUBLICATION_VERIFICATION_UNAVAILABLE]
        return verifier.check_publication_readiness(content, truth) or []
    except Exception:
        logger.exception(
            "Landing publication verification failed for prompt %s",
            prompt_id,
        )
        return [PUBLICATION_VERIFICATION_UNAVAILABLE]


async def _get_landing_publication_errors(prompt_id: int, content: str) -> List[str]:
    """Offload publication verification so its SQLite work cannot block async I/O."""
    return await asyncio.to_thread(
        _run_landing_publication_check,
        prompt_id,
        content,
    )


async def _revoke_landing_publication(prompt_id: int) -> None:
    """Fail closed before checking freshly written landing content."""
    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE PROMPTS SET has_landing_page = 0, landing_trusted = 0 WHERE id = ?",
            (prompt_id,),
        )
        cursor = await conn.execute(
            "SELECT public_id FROM PROMPTS WHERE id = ?",
            (prompt_id,),
        )
        row = await cursor.fetchone()
        await conn.commit()

    if row and row[0]:
        invalidate_landing_cache(row[0])


@asynccontextmanager
async def _landing_save_lock(prompt_id: int):
    """Serialize a prompt's revoke/write/verify/publish sequence across workers."""
    _LANDING_SAVE_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(_LANDING_SAVE_LOCK_DIR / f"prompt-{prompt_id}.lock"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LANDING_SAVE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.acquire(timeout=0)
            break
        except FileLockTimeout:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(0.05, remaining))
    try:
        yield
    finally:
        lock.release()


async def _persist_landing_page(
    *,
    prompt_id: int,
    section: str,
    content: str,
    use_default_template: bool,
    prompt_info: dict,
    is_admin: bool,
) -> JSONResponse:
    """Persist one landing section while the prompt-level save lock is held."""
    prompt_dir = create_prompt_directory(
        prompt_info["created_by_username"],
        prompt_id,
        prompt_info["name"],
    )

    prompt_base = Path(prompt_dir)
    validated_path = validate_path_within_directory(f"{section}.html", prompt_base)
    file_path = str(validated_path)

    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    if section == "home" and PRIMARY_APP_DOMAIN:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT public_id FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            row = await cursor.fetchone()
        if row and row[0]:
            prompt_slug = slugify(prompt_info["name"])
            canonical = f"https://{PRIMARY_APP_DOMAIN}/p/{row[0]}/{prompt_slug}/"
            content = fix_landing_seo_tags(content, canonical, canonical)

    if section == "home":
        # Revoke before replacing the file. The prompt-level file lock keeps a
        # second worker from publishing content verified by a different save.
        await _revoke_landing_publication(prompt_id)

    with open(file_path, "w", encoding="utf-8") as file:
        file.write(content)

    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO PROMPT_SECTION_CONFIGS (prompt_id, section, use_default)
            VALUES (?, ?, ?)
            ON CONFLICT(prompt_id, section) DO UPDATE SET use_default = ?
            """,
            (prompt_id, section, use_default_template, use_default_template),
        )
        await conn.commit()

    if section == "home":
        publication_errors = await _get_landing_publication_errors(
            prompt_id,
            content,
        )
        if publication_errors:
            logger.warning(
                "Landing for prompt %s saved but NOT published (%s blocking issue(s)): %s",
                prompt_id,
                len(publication_errors),
                publication_errors,
            )
            return JSONResponse(
                {
                    "success": True,
                    "published": False,
                    "publication_errors": publication_errors,
                    "message": "Changes saved, but the landing was NOT published: fix the listed issues first.",
                }
            )

        # Platform/admin-authored landings are trusted and served directly
        # from the primary domain. Third-party creator landings are NOT: they
        # fail closed (503) until a sanitized-HTML path or a custom domain is
        # configured. A non-admin re-saving an existing landing revokes trust,
        # since the content is now (partly) creator-authored.
        landing_trusted = 1 if is_admin else 0
        async with get_db_connection() as conn2:
            await conn2.execute(
                "UPDATE PROMPTS SET has_landing_page = 1, landing_trusted = ? WHERE id = ?",
                (landing_trusted, prompt_id),
            )
            await conn2.commit()
            cursor = await conn2.execute(
                "SELECT public_id FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            row = await cursor.fetchone()

        # Refresh the cached resolution so the new publication and trust flags
        # are visible immediately.
        if row and row[0]:
            invalidate_landing_cache(row[0])

    return JSONResponse(
        {
            "success": True,
            "published": True,
            "message": "Changes saved and section configuration updated!",
        }
    )


def _login_template(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        },
    )


def ensure_directories(prompt_id, prompt_info):
    prompt_dir = get_prompt_path(prompt_id, prompt_info)
    directories = [
        get_prompt_components_dir(prompt_id, prompt_info),
        os.path.join(prompt_dir, "static", "css"),
        os.path.join(prompt_dir, "static", "js"),
        os.path.join(prompt_dir, "static", "img"),
    ]
    for directory in directories:
        os.makedirs(directory, exist_ok=True)


class _LandingImageValidationError(ValueError):
    """A client-visible validation failure while decoding a landing image."""


def _save_prompt_landing_image(
    content: bytes,
    *,
    original_filename: str,
    requested_name: str,
    img_dir: Path,
) -> tuple[str, str]:
    """Validate and persist one prompt landing image off the event loop."""
    filename = secure_filename(requested_name)
    ext = Path(original_filename).suffix.lower()
    if not filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
        filename += ext

    img_dir.mkdir(parents=True, exist_ok=True)
    file_path = validate_path_within_directory(filename, img_dir)

    try:
        with PilImage.open(io.BytesIO(content)) as verified_image:
            verified_image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise _LandingImageValidationError(
            f"Invalid image file: {original_filename}"
        ) from exc

    try:
        with PilImage.open(io.BytesIO(content)) as decoded_image:
            width, height = decoded_image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise _LandingImageValidationError(
                    f"Image {original_filename} dimensions too large. Maximum is "
                    f"{MAX_IMAGE_PIXELS:,} pixels"
                )
            decoded_image.load()
            converted_image = (
                decoded_image.copy()
                if ext in {".jpg", ".jpeg", ".png"}
                else None
            )
    except _LandingImageValidationError:
        raise
    except Exception as exc:
        raise _LandingImageValidationError(
            f"Could not process image: {original_filename}"
        ) from exc

    if converted_image is not None:
        webp_path = file_path.with_suffix(".webp")
        converted_image.save(str(webp_path), "WEBP")
        return filename, webp_path.name

    file_path.write_bytes(content)
    return filename, file_path.name


def secure_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and invalid characters."""
    filename = normalize("NFKD", filename).encode("ASCII", "ignore").decode("ASCII")
    filename = filename.replace("\\", "/")
    filename = filename.split("/")[-1]
    while ".." in filename:
        filename = filename.replace("..", "")
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", filename)
    filename = filename.lstrip(".").strip().replace(" ", "_")
    filename = filename[:160]
    if not filename:
        filename = "unnamed_file"
    return filename


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@router.get("/landing/{prompt_id}", response_class=HTMLResponse)
async def landing_config(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """
    Configuration page for Public Profile / Landing Pages.
    """
    require_creator_tools_enabled()

    if current_user is None:
        return _login_template(request)

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            raise HTTPException(
                status_code=403,
                detail="Access denied. You don't have permission to manage this prompt.",
            )

        prompt_info = await get_prompt_info(prompt_id)

        async with get_db_connection(readonly=True) as conn:
            async with conn.execute(
                "SELECT public_id FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            ) as cursor:
                row = await cursor.fetchone()
                public_id = row[0] if row else None

            async with conn.execute(
                """
                SELECT custom_domain, verification_status, is_active,
                       activated_by_admin, last_verification_attempt,
                       verification_error, activated_at
                FROM PROMPT_CUSTOM_DOMAINS
                WHERE prompt_id = ?
                """,
                (prompt_id,),
            ) as cursor:
                domain_row = await cursor.fetchone()

        from marketplace.routes.custom_domains import (
            CNAME_TARGET,
            SLOT_PRICE,
            VSTATUS_NAMES,
            get_user_slots_info,
        )

        domain_config = None
        if domain_row:
            domain_config = {
                "domain": domain_row[0],
                "verification_status": VSTATUS_NAMES.get(domain_row[1], "pending"),
                "verification_status_int": domain_row[1],
                "is_active": bool(domain_row[2]),
                "activated_by_admin": bool(domain_row[3]),
                "last_check": domain_row[4],
                "verification_error": domain_row[5],
                "activated_at": domain_row[6],
            }

        user_slots = await get_user_slots_info(current_user.id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)

        pages = []
        has_home_page = False
        if os.path.exists(prompt_dir):
            for f in os.listdir(prompt_dir):
                if f.endswith(".html") and os.path.isfile(os.path.join(prompt_dir, f)):
                    page_name = f[:-5]
                    is_home = page_name == "home"
                    if is_home:
                        has_home_page = True
                    pages.append(
                        {
                            "name": page_name,
                            "url_path": "/" if is_home else f"/{page_name}",
                            "is_home": is_home,
                        }
                    )
        pages.sort(key=lambda p: (not p["is_home"], p["name"]))

        components = {"html": [], "css": [], "js": []}

        components_dir = os.path.join(prompt_dir, "templates", "components")
        if os.path.exists(components_dir):
            for f in os.listdir(components_dir):
                if f.endswith(".html"):
                    components["html"].append(f[:-5])

        css_dir = os.path.join(prompt_dir, "static", "css")
        if os.path.exists(css_dir):
            for f in os.listdir(css_dir):
                if f.endswith(".css"):
                    components["css"].append(f[:-4])

        js_dir = os.path.join(prompt_dir, "static", "js")
        if os.path.exists(js_dir):
            for f in os.listdir(js_dir):
                if f.endswith(".js"):
                    components["js"].append(f[:-3])

        slug = slugify(prompt_info["name"])
        base_url = str(request.base_url).rstrip("/")
        public_url_path = f"/p/{public_id}/{slug}/" if public_id else "#"
        public_url_full = f"{base_url}{public_url_path}" if public_id else "#"

        prompt = {
            "id": prompt_id,
            "name": prompt_info["name"],
            "public_id": public_id,
        }
        wizard_available, _ = is_claude_available()

        context = await get_template_context(request, current_user)
        context.update(
            {
                "prompt": prompt,
                "pages": pages,
                "components": components,
                "public_url": public_url_full,
                "public_url_path": public_url_path,
                "has_home_page": has_home_page,
                "domain_config": domain_config,
                "cname_target": CNAME_TARGET,
                "slot_price": SLOT_PRICE,
                "user_slots": user_slots,
                "wizard_available": wizard_available,
            }
        )
        return templates.TemplateResponse("landing_config.html", context)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in admin_landing_config: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/landing/{prompt_id}/pages", response_class=JSONResponse)
async def create_landing_page(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Create a new landing page for a prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        data = await request.json()
        page_name = data.get("page_name", "").strip().lower()

        if not page_name or not re.match(r"^[a-zA-Z0-9_-]+$", page_name):
            return JSONResponse({"success": False, "message": "Invalid page name"}, status_code=400)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = create_prompt_directory(
            prompt_info["created_by_username"],
            prompt_id,
            prompt_info["name"],
        )
        page_path = os.path.join(prompt_dir, f"{page_name}.html")

        if os.path.exists(page_path):
            return JSONResponse({"success": False, "message": "Page already exists"}, status_code=400)

        default_dir = os.path.join(prompt_dir, "default")
        default_template = os.path.join(default_dir, f"{page_name}.html")

        if os.path.exists(default_template):
            shutil.copy(default_template, page_path)
        else:
            with open(page_path, "w", encoding="utf-8") as f:
                f.write(
                    f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{page_name.capitalize()}</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-50 min-h-screen">
    <div class="container mx-auto px-4 py-8">
        <h1 class="text-3xl font-bold text-gray-800">{page_name.capitalize()}</h1>
        <p class="mt-4 text-gray-600">Edit this page to add your content.</p>
    </div>
</body>
</html>"""
                )

        return JSONResponse({"success": True, "message": f"Page '{page_name}' created successfully"})

    except Exception as e:
        logger.error(f"Error creating landing page: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.delete("/api/landing/{prompt_id}/pages/{page_name}", response_class=JSONResponse)
async def delete_landing_page(
    prompt_id: int,
    page_name: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a landing page from a prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        if not page_name or not re.match(r"^[a-zA-Z0-9_-]+$", page_name):
            return JSONResponse({"success": False, "message": "Invalid page name"}, status_code=400)

        if page_name.lower() == "home":
            return JSONResponse({"success": False, "message": "Cannot delete the home page"}, status_code=400)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)
        page_path = os.path.join(prompt_dir, f"{page_name}.html")

        if not os.path.exists(page_path):
            return JSONResponse({"success": False, "message": "Page not found"}, status_code=404)

        os.remove(page_path)
        return JSONResponse({"success": True, "message": f"Page '{page_name}' deleted successfully"})

    except Exception as e:
        logger.error(f"Error deleting landing page: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/landing/{prompt_id}/registration", response_class=JSONResponse)
async def get_landing_config_endpoint(
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Get the landing registration configuration for a prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        config = await get_landing_registration_config(prompt_id)
        preserved_llm_ids = []
        for key in ("default_llm_id", "_prompt_forced_llm_id"):
            if config.get(key):
                preserved_llm_ids.append(config.get(key))

        async with get_db_connection(readonly=True) as conn:
            # A prompt/landing default LLM is not a per-user personal subscription;
            # GPTSub (ChatGPT subscription) must never be selectable here. Hide it,
            # including an already-stored GPTSub default (fail closed).
            llm_rows = await get_selector_llms(conn, preserve_ids=preserved_llm_ids)
            llms = [{"id": row["id"], "name": f"{row['machine']} - {row['model']}"} for row in llm_rows]

            async with conn.execute("SELECT id, name FROM CATEGORIES ORDER BY display_order, name") as cursor:
                categories = [{"id": row[0], "name": row[1]} for row in await cursor.fetchall()]

        return JSONResponse(
            {
                "success": True,
                "config": config,
                "available_llms": llms,
                "available_categories": categories,
            }
        )

    except HTTPException as he:
        return JSONResponse({"success": False, "message": he.detail}, status_code=he.status_code)
    except Exception as e:
        logger.error(f"Error getting landing config for prompt {prompt_id}: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.put("/api/landing/{prompt_id}/registration", response_class=JSONResponse)
async def set_landing_config_endpoint(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Set the landing registration configuration for a prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"success": False, "message": "Invalid JSON"}, status_code=400)

        billing_mode = data.get("billing_mode", "customer_pays")
        if billing_mode == "user_pays":
            async with get_db_connection(readonly=True) as conn:
                async with conn.execute(
                    "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                    (current_user.id,),
                ) as cursor:
                    result = await cursor.fetchone()
                    owner_balance = result[0] if result else 0

            if owner_balance <= 0:
                return JSONResponse(
                    {
                        "success": False,
                        "message": "You need a positive balance to enable 'user pays' mode",
                    },
                    status_code=400,
                )

        if data.get("public_prompts_access") is False or data.get("public_prompts_access") == 0:
            active_domain = await get_active_custom_domain(prompt_id)
            if not active_domain:
                return JSONResponse(
                    {
                        "success": False,
                        "message": "Restricting marketplace access requires an active custom domain. Configure and activate a domain first.",
                    },
                    status_code=400,
                )

        success = await set_landing_registration_config(prompt_id, data)

        if success:
            return JSONResponse({"success": True, "message": "Registration settings saved successfully"})
        return JSONResponse({"success": False, "message": "Failed to save registration settings"}, status_code=500)

    except HTTPException as he:
        return JSONResponse({"success": False, "message": he.detail}, status_code=he.status_code)
    except Exception as e:
        logger.error(f"Error setting landing config for prompt {prompt_id}: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/landing/{prompt_id}/geo", response_class=JSONResponse)
async def get_landing_geo(prompt_id: int, current_user: User = Depends(get_current_user)):
    """Get geo-blocking policy for a landing page and global blocks."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        client = CloudflareGeoClient()
        if not client.is_configured():
            return JSONResponse({"success": False, "message": "Cloudflare not configured"}, status_code=404)

        async with get_db_connection(readonly=True) as conn:
            async with conn.execute("SELECT geo_policy FROM PROMPTS WHERE id = ?", (prompt_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return JSONResponse({"success": False, "message": "Prompt not found"}, status_code=404)

            policy = None
            try:
                policy = json.loads(row[0]) if row[0] else None
            except (json.JSONDecodeError, TypeError):
                pass

            global_blocks = set()
            async with conn.execute(
                "SELECT key, value FROM SYSTEM_CONFIG WHERE key IN ('geo_enabled', 'geo_global_mode', 'geo_global_blocked_countries', 'geo_global_blocked_continents')"
            ) as cursor:
                global_config = {}
                async for r in cursor:
                    global_config[r[0]] = r[1]

            if global_config.get("geo_enabled") == "1" and global_config.get("geo_global_mode") == "deny":
                try:
                    countries = json.loads(global_config.get("geo_global_blocked_countries", "[]"))
                    continents = json.loads(global_config.get("geo_global_blocked_continents", "[]"))
                    for cont in continents:
                        global_blocks.update(get_countries_for_continent(cont))
                    global_blocks.update(countries)
                except (json.JSONDecodeError, TypeError):
                    pass

        return JSONResponse(
            {
                "success": True,
                "policy": policy,
                "global_blocks": sorted(global_blocks),
                "geo_data": get_all_geo_data(),
            }
        )

    except Exception as e:
        logger.error(f"Error getting geo policy for prompt {prompt_id}: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.put("/api/landing/{prompt_id}/geo", response_class=JSONResponse)
async def set_landing_geo(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Save geo-blocking policy for a landing page and sync to Cloudflare."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        data = await request.json()

        enabled = bool(data.get("enabled", False))
        mode = data.get("mode", "deny")
        if mode not in ("deny", "allow"):
            return JSONResponse({"success": False, "message": "Invalid mode"}, status_code=400)

        countries = validate_country_codes(data.get("countries", []))
        continents = validate_continent_codes(data.get("continents", []))

        policy = {
            "enabled": enabled,
            "mode": mode,
            "countries": countries,
            "continents": continents,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        policy_json = json.dumps(policy) if enabled else None

        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE PROMPTS SET geo_policy = ? WHERE id = ?",
                (policy_json, prompt_id),
            )
            await conn.commit()

        sync_result = None
        async with get_db_connection(readonly=True) as conn:
            async with conn.execute("SELECT value FROM SYSTEM_CONFIG WHERE key = 'geo_enabled'") as cursor:
                row = await cursor.fetchone()
                geo_enabled = row[0] == "1" if row else False

        if geo_enabled:
            sync_result = await geo_sync_engine.sync_all()

        return JSONResponse(
            {
                "success": True,
                "message": "Geo-blocking policy saved" + (" and synced to Cloudflare" if sync_result else ""),
                "sync": sync_result,
            }
        )

    except Exception as e:
        logger.error(f"Error saving geo policy for prompt {prompt_id}: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


async def _run_security_check(text: str, *, prompt_id: int, label: str):
    try:
        security_result = await check_security(text)
        if not security_result.get("checked"):
            logger.error(
                f"Security Guard unavailable for {label} on prompt {prompt_id}: "
                f"{security_result.get('reason', 'unknown error')}"
            )
            return JSONResponse(
                {
                    "success": False,
                    "message": "AI Wizard security check is temporarily unavailable",
                    "error_code": "SECURITY_GUARD_UNAVAILABLE",
                },
                status_code=503,
            )
        if not security_result["allowed"]:
            logger.warning(
                f"Security Guard BLOCKED {label} for prompt {prompt_id}: "
                f"Threat level: {security_result['threat_level']}, "
                f"Threats: {security_result['threats']}, "
                f"Reason: {security_result['reason']}"
            )
            return JSONResponse(
                {
                    "success": False,
                    "message": "Your request was blocked by security check",
                    "security_block": True,
                    "threat_level": security_result["threat_level"],
                    "reason": security_result["reason"],
                },
                status_code=403,
            )
    except Exception as e:
        logger.error(f"Security Guard check error (blocking request): {e}")
        return JSONResponse(
            {
                "success": False,
                "message": "AI Wizard security check is temporarily unavailable",
                "error_code": "SECURITY_GUARD_UNAVAILABLE",
            },
            status_code=503,
        )
    return None


@router.post("/api/landing/{prompt_id}/ai/generate", response_class=JSONResponse)
async def generate_landing_with_wizard(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Starts a background job to generate a landing page using Claude Code."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    claude_available, _ = is_claude_available()
    if not claude_available:
        return JSONResponse(
            {
                "success": False,
                "message": "AI Wizard is disabled until a verified OS sandbox is configured.",
                "error_code": "WIZARD_SANDBOX_UNAVAILABLE",
            },
            status_code=503,
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON"}, status_code=400)

    description = data.get("description", "").strip()
    if not description:
        return JSONResponse({"success": False, "message": "Description is required"}, status_code=400)
    if len(description) < 20:
        return JSONResponse({"success": False, "message": "Description must be at least 20 characters"}, status_code=400)

    style = data.get("style", "modern")
    if style not in ["modern", "minimalist", "corporate", "creative"]:
        style = "modern"
    primary_color = data.get("primary_color", "#3B82F6")
    secondary_color = data.get("secondary_color", "#10B981")
    language = data.get("language", "es")
    if language not in ["es", "en"]:
        language = "es"

    try:
        timeout_minutes = int(data.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    security_response = await _run_security_check(description, prompt_id=prompt_id, label="landing wizard")
    if security_response:
        return security_response

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        existing_job = get_active_job_for_prompt(prompt_id)
        if existing_job:
            return JSONResponse(
                {
                    "success": False,
                    "message": "A job is already running for this prompt",
                    "existing_task_id": existing_job["task_id"],
                    "existing_status": existing_job["status"],
                },
                status_code=409,
            )

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)

        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse({"success": False, "message": "Prompt directory not found"}, status_code=404)

        ai_system_prompt = ""
        product_description = ""
        public_id = ""
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT prompt, description, public_id FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            row = await cursor.fetchone()
            if row:
                ai_system_prompt = row[0] or ""
                product_description = row[1] or ""
                public_id = row[2] or ""

        landing_url = ""
        if public_id and PRIMARY_APP_DOMAIN:
            prompt_slug = slugify(prompt_info["name"])
            landing_url = f"https://{PRIMARY_APP_DOMAIN}/p/{public_id}/{prompt_slug}/"

        params = {
            "description": description,
            "style": style,
            "primary_color": primary_color,
            "secondary_color": secondary_color,
            "language": language,
            "timeout": timeout_seconds,
            "product_name": prompt_info["name"],
            "ai_system_prompt": ai_system_prompt,
            "product_description": product_description,
            "landing_url": landing_url,
        }

        logger.info(f"Starting AI wizard job for prompt {prompt_id}, user {current_user.id}, timeout={timeout_seconds}s")
        result = start_job(
            prompt_id=prompt_id,
            job_type="generate",
            prompt_dir=str(prompt_dir),
            params=params,
            timeout_seconds=timeout_seconds,
        )

        if result.get("success"):
            logger.info(f"AI wizard job started for prompt {prompt_id}: task_id={result['task_id']}")
            return JSONResponse(
                {
                    "success": True,
                    "message": "Job started",
                    "task_id": result["task_id"],
                    "status": result["status"],
                }
            )
        logger.error(f"Failed to start AI wizard job for prompt {prompt_id}: {result.get('error')}")
        return JSONResponse(
            {
                "success": False,
                "message": result.get("error", "Failed to start job"),
                "existing_task_id": result.get("existing_task_id"),
            },
            status_code=500,
        )

    except Exception as e:
        logger.error(f"Error in generate_landing_with_wizard: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/landing/{prompt_id}/files", response_class=JSONResponse)
async def get_landing_files(prompt_id: int, current_user: User = Depends(get_current_user)):
    """List files in the prompt's landing page directory."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)

        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse(
                {
                    "success": True,
                    "files": {"pages": [], "css": [], "js": [], "images": [], "other": [], "total_count": 0},
                }
            )

        files = list_prompt_files(str(prompt_dir))
        return JSONResponse({"success": True, "files": files})

    except Exception as e:
        logger.error(f"Error in get_landing_files: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/landing/{prompt_id}/ai/status/{task_id}", response_class=JSONResponse)
async def get_landing_job_status(
    prompt_id: int,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get the status of a landing page generation/modification job."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        job = get_job(task_id)
        if not job:
            return JSONResponse({"success": False, "message": "Job not found"}, status_code=404)
        if job.get("prompt_id") != prompt_id:
            return JSONResponse({"success": False, "message": "Job does not belong to this prompt"}, status_code=403)

        response = {
            "success": True,
            "task_id": job["task_id"],
            "status": job["status"],
            "type": job.get("type"),
            "started_at": job.get("started_at"),
            "updated_at": job.get("updated_at"),
            "completed_at": job.get("completed_at"),
        }
        if job["status"] == "completed":
            response["files_created"] = job.get("files_created", [])
        elif job["status"] in ("failed", "timeout"):
            response["error"] = job.get("error")

        return JSONResponse(response)

    except Exception as e:
        logger.error(f"Error in get_landing_job_status: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/landing/{prompt_id}/ai/active-job", response_class=JSONResponse)
async def get_active_landing_job(prompt_id: int, current_user: User = Depends(get_current_user)):
    """Check if there's an active landing job for this prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        job = get_active_job_for_prompt(prompt_id)
        if job:
            return JSONResponse(
                {
                    "success": True,
                    "has_active_job": True,
                    "task_id": job["task_id"],
                    "status": job["status"],
                    "type": job.get("type"),
                    "started_at": job.get("started_at"),
                }
            )
        return JSONResponse({"success": True, "has_active_job": False})

    except Exception as e:
        logger.error(f"Error in get_active_landing_job: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.post("/api/landing/{prompt_id}/ai/modify", response_class=JSONResponse)
async def modify_landing_with_wizard(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Starts a background job to modify an existing landing page."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    claude_available, _ = is_claude_available()
    if not claude_available:
        return JSONResponse(
            {
                "success": False,
                "message": "AI Wizard is disabled until a verified OS sandbox is configured.",
                "error_code": "WIZARD_SANDBOX_UNAVAILABLE",
            },
            status_code=503,
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON"}, status_code=400)

    instructions = data.get("instructions", "").strip()
    if not instructions:
        return JSONResponse({"success": False, "message": "Instructions are required"}, status_code=400)
    if len(instructions) < 10:
        return JSONResponse({"success": False, "message": "Instructions must be at least 10 characters"}, status_code=400)

    try:
        timeout_minutes = int(data.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    security_response = await _run_security_check(instructions, prompt_id=prompt_id, label="landing modify")
    if security_response:
        return security_response

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        existing_job = get_active_job_for_prompt(prompt_id)
        if existing_job:
            return JSONResponse(
                {
                    "success": False,
                    "message": "A job is already running for this prompt",
                    "existing_task_id": existing_job["task_id"],
                    "existing_status": existing_job["status"],
                },
                status_code=409,
            )

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)

        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse({"success": False, "message": "Prompt directory not found"}, status_code=404)

        files = list_prompt_files(str(prompt_dir))
        if files["total_count"] == 0:
            return JSONResponse(
                {"success": False, "message": "No files to modify. Use 'Create new' instead."},
                status_code=400,
            )

        ai_system_prompt = ""
        product_description = ""
        public_id = ""
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT prompt, description, public_id FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            row = await cursor.fetchone()
            if row:
                ai_system_prompt = row[0] or ""
                product_description = row[1] or ""
                public_id = row[2] or ""

        landing_url = ""
        if public_id and PRIMARY_APP_DOMAIN:
            prompt_slug = slugify(prompt_info["name"])
            landing_url = f"https://{PRIMARY_APP_DOMAIN}/p/{public_id}/{prompt_slug}/"

        params = {
            "instructions": instructions,
            "timeout": timeout_seconds,
            "product_name": prompt_info["name"],
            "ai_system_prompt": ai_system_prompt,
            "product_description": product_description,
            "landing_url": landing_url,
        }

        logger.info(f"Starting modify wizard job for prompt {prompt_id}, user {current_user.id}, timeout={timeout_seconds}s")
        result = start_job(
            prompt_id=prompt_id,
            job_type="modify",
            prompt_dir=str(prompt_dir),
            params=params,
            timeout_seconds=timeout_seconds,
        )

        if result.get("success"):
            logger.info(f"Modify wizard job started for prompt {prompt_id}: task_id={result['task_id']}")
            return JSONResponse(
                {
                    "success": True,
                    "message": "Job started",
                    "task_id": result["task_id"],
                    "status": result["status"],
                }
            )
        logger.error(f"Failed to start modify wizard job for prompt {prompt_id}: {result.get('error')}")
        return JSONResponse(
            {
                "success": False,
                "message": result.get("error", "Failed to start job"),
                "existing_task_id": result.get("existing_task_id"),
            },
            status_code=500,
        )

    except Exception as e:
        logger.error(f"Error in modify_landing_with_wizard: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.delete("/api/landing/{prompt_id}/files", response_class=JSONResponse)
async def delete_landing_files(prompt_id: int, current_user: User = Depends(get_current_user)):
    """Delete all landing page files for a prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)

        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse({"success": True, "message": "No files to delete", "deleted_count": 0})

        logger.info(f"Deleting landing files for prompt {prompt_id}, user {current_user.id}")
        result = delete_all_landing_files(str(prompt_dir), keep_images=True)

        if result["success"]:
            logger.info(f"Deleted {result.get('deleted_count', 0)} files for prompt {prompt_id}")
            async with get_db_connection() as conn:
                cursor = await conn.execute("SELECT public_id FROM PROMPTS WHERE id = ?", (prompt_id,))
                row = await cursor.fetchone()
                # Retire the landing and drop its trust flag so it cannot keep a
                # dangling trusted state if it is later re-created by a non-admin.
                await conn.execute(
                    "UPDATE PROMPTS SET has_landing_page = 0, landing_trusted = 0 WHERE id = ?",
                    (prompt_id,),
                )
                await conn.commit()

            # Drop the cached resolution so the new has_landing_page=0 /
            # landing_trusted=0 policy takes effect immediately (a stale entry
            # would keep serving it).
            if row and row[0]:
                invalidate_landing_cache(row[0])

            return JSONResponse(
                {
                    "success": True,
                    "message": result.get("message", "Files deleted"),
                    "deleted_count": result.get("deleted_count", 0),
                }
            )
        logger.error(f"Delete failed for prompt {prompt_id}: {result.get('error')}")
        return JSONResponse(
            {
                "success": False,
                "message": result.get("error", "Unknown error"),
                "deleted_count": result.get("deleted_count", 0),
            },
            status_code=500,
        )

    except Exception as e:
        logger.error(f"Error in delete_landing_files: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.post("/api/welcome/{prompt_id}/ai/generate", response_class=JSONResponse)
async def generate_welcome_with_wizard(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Starts a background job to generate a welcome page."""
    if current_user is None:
        return unauthenticated_response()

    claude_available, _ = is_claude_available()
    if not claude_available:
        return JSONResponse(
            {
                "success": False,
                "message": "AI Wizard is disabled until a verified OS sandbox is configured.",
                "error_code": "WIZARD_SANDBOX_UNAVAILABLE",
            },
            status_code=503,
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON"}, status_code=400)

    description = data.get("description", "").strip()
    if not description:
        return JSONResponse({"success": False, "message": "Description is required"}, status_code=400)
    if len(description) < 20:
        return JSONResponse({"success": False, "message": "Description must be at least 20 characters"}, status_code=400)

    style = data.get("style", "modern")
    if style not in ["modern", "minimalist", "corporate", "creative"]:
        style = "modern"
    primary_color = data.get("primary_color", "#3B82F6")
    secondary_color = data.get("secondary_color", "#10B981")
    language = data.get("language", "es")
    if language not in ["es", "en"]:
        language = "es"

    try:
        timeout_minutes = int(data.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    security_response = await _run_security_check(description, prompt_id=prompt_id, label="welcome wizard")
    if security_response:
        return security_response

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        existing_job = get_active_welcome_job_for_prompt(prompt_id)
        if existing_job:
            return JSONResponse(
                {
                    "success": False,
                    "message": "A job is already running for this prompt",
                    "existing_task_id": existing_job["task_id"],
                    "existing_status": existing_job["status"],
                },
                status_code=409,
            )

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)
        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse({"success": False, "message": "Prompt directory not found"}, status_code=404)

        ai_system_prompt = ""
        product_description = ""
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT prompt, description FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            row = await cursor.fetchone()
            if row:
                ai_system_prompt = row[0] or ""
                product_description = row[1] or ""

        avatar_path = ""
        img_dir = os.path.join(str(prompt_dir), "static", "img")
        if os.path.isdir(img_dir):
            for fname in os.listdir(img_dir):
                if fname.lower().endswith((".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg")):
                    avatar_path = f"static/img/{fname}"
                    break

        params = {
            "description": description,
            "style": style,
            "primary_color": primary_color,
            "secondary_color": secondary_color,
            "language": language,
            "timeout": timeout_seconds,
            "product_name": prompt_info["name"],
            "ai_system_prompt": ai_system_prompt,
            "product_description": product_description,
            "avatar_path": avatar_path,
            "chat_url": f"/chat?prompt={prompt_id}",
        }

        logger.info(f"Starting welcome wizard job for prompt {prompt_id}, user {current_user.id}, timeout={timeout_seconds}s")
        result = start_job(
            prompt_id=prompt_id,
            job_type="generate",
            prompt_dir=str(prompt_dir),
            params=params,
            timeout_seconds=timeout_seconds,
            target="welcome",
        )

        if result.get("success"):
            logger.info(f"Welcome wizard job started for prompt {prompt_id}: task_id={result['task_id']}")
            return JSONResponse(
                {
                    "success": True,
                    "message": "Job started",
                    "task_id": result["task_id"],
                    "status": result["status"],
                }
            )
        logger.error(f"Failed to start welcome wizard job for prompt {prompt_id}: {result.get('error')}")
        return JSONResponse(
            {
                "success": False,
                "message": result.get("error", "Failed to start job"),
                "existing_task_id": result.get("existing_task_id"),
            },
            status_code=500,
        )

    except Exception as e:
        logger.error(f"Error in generate_welcome_with_wizard: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.post("/api/welcome/{prompt_id}/ai/modify", response_class=JSONResponse)
async def modify_welcome_with_wizard(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    """Starts a background job to modify an existing welcome page."""
    if current_user is None:
        return unauthenticated_response()

    claude_available, _ = is_claude_available()
    if not claude_available:
        return JSONResponse(
            {
                "success": False,
                "message": "AI Wizard is disabled until a verified OS sandbox is configured.",
                "error_code": "WIZARD_SANDBOX_UNAVAILABLE",
            },
            status_code=503,
        )

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid JSON"}, status_code=400)

    instructions = data.get("instructions", "").strip()
    if not instructions:
        return JSONResponse({"success": False, "message": "Instructions are required"}, status_code=400)
    if len(instructions) < 10:
        return JSONResponse({"success": False, "message": "Instructions must be at least 10 characters"}, status_code=400)

    try:
        timeout_minutes = int(data.get("timeout_minutes", 5))
    except (ValueError, TypeError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(60, timeout_minutes))
    timeout_seconds = timeout_minutes * 60

    security_response = await _run_security_check(instructions, prompt_id=prompt_id, label="welcome modify")
    if security_response:
        return security_response

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        existing_job = get_active_welcome_job_for_prompt(prompt_id)
        if existing_job:
            return JSONResponse(
                {
                    "success": False,
                    "message": "A job is already running for this prompt",
                    "existing_task_id": existing_job["task_id"],
                    "existing_status": existing_job["status"],
                },
                status_code=409,
            )

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)
        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse({"success": False, "message": "Prompt directory not found"}, status_code=404)

        files = list_welcome_files(str(prompt_dir))
        if files["total_count"] == 0:
            return JSONResponse(
                {"success": False, "message": "No files to modify. Use 'Create new' instead."},
                status_code=400,
            )

        ai_system_prompt = ""
        product_description = ""
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT prompt, description FROM PROMPTS WHERE id = ?",
                (prompt_id,),
            )
            row = await cursor.fetchone()
            if row:
                ai_system_prompt = row[0] or ""
                product_description = row[1] or ""

        avatar_path = ""
        img_dir = os.path.join(str(prompt_dir), "static", "img")
        if os.path.isdir(img_dir):
            for fname in os.listdir(img_dir):
                if fname.lower().endswith((".webp", ".png", ".jpg", ".jpeg", ".gif", ".svg")):
                    avatar_path = f"static/img/{fname}"
                    break

        params = {
            "instructions": instructions,
            "timeout": timeout_seconds,
            "product_name": prompt_info["name"],
            "ai_system_prompt": ai_system_prompt,
            "product_description": product_description,
            "avatar_path": avatar_path,
            "chat_url": f"/chat?prompt={prompt_id}",
        }

        logger.info(f"Starting welcome modify wizard job for prompt {prompt_id}, user {current_user.id}, timeout={timeout_seconds}s")
        result = start_job(
            prompt_id=prompt_id,
            job_type="modify",
            prompt_dir=str(prompt_dir),
            params=params,
            timeout_seconds=timeout_seconds,
            target="welcome",
        )

        if result.get("success"):
            logger.info(f"Welcome modify wizard job started for prompt {prompt_id}: task_id={result['task_id']}")
            return JSONResponse(
                {
                    "success": True,
                    "message": "Job started",
                    "task_id": result["task_id"],
                    "status": result["status"],
                }
            )
        logger.error(f"Failed to start welcome modify wizard job for prompt {prompt_id}: {result.get('error')}")
        return JSONResponse(
            {
                "success": False,
                "message": result.get("error", "Failed to start job"),
                "existing_task_id": result.get("existing_task_id"),
            },
            status_code=500,
        )

    except Exception as e:
        logger.error(f"Error in modify_welcome_with_wizard: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/welcome/{prompt_id}/ai/status/{task_id}", response_class=JSONResponse)
async def get_welcome_job_status(
    prompt_id: int,
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Get the status of a welcome page generation/modification job."""
    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        job = get_job(task_id)
        if not job:
            return JSONResponse({"success": False, "message": "Job not found"}, status_code=404)
        if job.get("prompt_id") != prompt_id:
            return JSONResponse({"success": False, "message": "Job does not belong to this prompt"}, status_code=403)

        response = {
            "success": True,
            "task_id": job["task_id"],
            "status": job["status"],
            "type": job.get("type"),
            "started_at": job.get("started_at"),
            "updated_at": job.get("updated_at"),
            "completed_at": job.get("completed_at"),
        }

        if job["status"] == "completed":
            response["files_created"] = job.get("files_created", [])
            try:
                async with get_db_connection() as db:
                    await db.execute("UPDATE PROMPTS SET has_welcome_page = 1 WHERE id = ?", (prompt_id,))
                    await db.commit()
            except Exception as db_err:
                logger.warning(f"Could not update has_welcome_page for prompt {prompt_id}: {db_err}")
        elif job["status"] in ("failed", "timeout"):
            response["error"] = job.get("error")

        return JSONResponse(response)

    except Exception as e:
        logger.error(f"Error in get_welcome_job_status: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/welcome/{prompt_id}/ai/active-job", response_class=JSONResponse)
async def get_active_welcome_job(prompt_id: int, current_user: User = Depends(get_current_user)):
    """Check if there's an active welcome job for this prompt."""
    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        job = get_active_welcome_job_for_prompt(prompt_id)
        if job:
            return JSONResponse(
                {
                    "success": True,
                    "has_active_job": True,
                    "task_id": job["task_id"],
                    "status": job["status"],
                    "type": job.get("type"),
                    "started_at": job.get("started_at"),
                }
            )
        return JSONResponse({"success": True, "has_active_job": False})

    except Exception as e:
        logger.error(f"Error in get_active_welcome_job: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/welcome/{prompt_id}/files", response_class=JSONResponse)
async def get_welcome_files(prompt_id: int, current_user: User = Depends(get_current_user)):
    """List files in the prompt's welcome page directory."""
    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)
        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse(
                {
                    "success": True,
                    "files": {"pages": [], "css": [], "js": [], "images": [], "other": [], "total_count": 0},
                }
            )

        files = list_welcome_files(str(prompt_dir))
        return JSONResponse({"success": True, "files": files})

    except Exception as e:
        logger.error(f"Error in get_welcome_files: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.delete("/api/welcome/{prompt_id}/files", response_class=JSONResponse)
async def delete_welcome_files(prompt_id: int, current_user: User = Depends(get_current_user)):
    """Delete all welcome page files for a prompt."""
    if current_user is None:
        return unauthenticated_response()

    try:
        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)
        if not prompt_dir or not os.path.exists(prompt_dir):
            return JSONResponse({"success": True, "message": "No files to delete", "deleted_count": 0})

        logger.info(f"Deleting welcome files for prompt {prompt_id}, user {current_user.id}")
        result = delete_all_welcome_files(str(prompt_dir), keep_images=True)

        if result["success"]:
            logger.info(f"Deleted {result.get('deleted_count', 0)} welcome files for prompt {prompt_id}")
            try:
                async with get_db_connection() as db:
                    await db.execute("UPDATE PROMPTS SET has_welcome_page = 0 WHERE id = ?", (prompt_id,))
                    await db.commit()
            except Exception as db_err:
                logger.warning(f"Could not update has_welcome_page for prompt {prompt_id}: {db_err}")
            return JSONResponse(
                {
                    "success": True,
                    "message": result.get("message", "Files deleted"),
                    "deleted_count": result.get("deleted_count", 0),
                }
            )
        logger.error(f"Welcome delete failed for prompt {prompt_id}: {result.get('error')}")
        return JSONResponse(
            {
                "success": False,
                "message": result.get("error", "Unknown error"),
                "deleted_count": result.get("deleted_count", 0),
            },
            status_code=500,
        )

    except Exception as e:
        logger.error(f"Error in delete_welcome_files: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/landing/{prompt_id}/pages/{section}/edit", response_class=HTMLResponse)
async def edit_landing_page(
    request: Request,
    prompt_id: int,
    section: str,
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    if current_user is None:
        return _login_template(request)

    try:
        if not re.match(r"^[a-zA-Z0-9_-]+$", section):
            raise HTTPException(status_code=400, detail="Invalid section name")

        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            raise HTTPException(status_code=403, detail="Access denied")

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, prompt_info)

        prompt_base = Path(prompt_dir)
        validated_path = validate_path_within_directory(f"{section}.html", prompt_base)
        file_path = str(validated_path)
        default_dir = os.path.join(prompt_dir, "default")

        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        if not os.path.exists(file_path):
            example_file = os.path.join(default_dir, f"{section}.html")
            if os.path.exists(example_file):
                shutil.copy(example_file, file_path)
            else:
                with open(file_path, "w", encoding="utf-8") as file:
                    file.write(f"<h1>Welcome to the {section} page</h1>")

        with open(file_path, "r", encoding="utf-8") as file:
            section_content = file.read()

        async with get_db_connection(readonly=True) as conn:
            async with conn.execute(
                """
                SELECT use_default FROM PROMPT_SECTION_CONFIGS
                WHERE prompt_id = ? AND section = ?
                """,
                (prompt_id, section),
            ) as cursor:
                result = await cursor.fetchone()
                use_default = result[0] if result else False

        flash_message = request.session.pop("flash_message", None)

        context = await get_template_context(request, current_user)
        context.update(
            {
                "content": section_content,
                "prompt_id": prompt_id,
                "section": section,
                "prompt_info": prompt_info,
                "flash_message": flash_message,
                "use_default": use_default,
            }
        )
        return templates.TemplateResponse("web/web_edit.html", context)

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Unexpected error in edit_section: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/api/landing/{prompt_id}/pages/{section}", response_class=JSONResponse)
async def save_landing_page(
    request: Request,
    prompt_id: int,
    section: str,
    encodedContent: str = Form(...),
    use_default_template: bool = Form(False),
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    if current_user is None:
        return _login_template(request)

    try:
        if not re.match(r"^[a-zA-Z0-9_-]+$", section):
            return JSONResponse({"success": False, "message": "Invalid section name"}, status_code=400)

        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)

        content = base64.b64decode(encodedContent).decode("utf-8")
        content = re.sub(r"\n\s*\n", "\n", content.strip())
        content = re.sub(r"\r\n", "\n", content)

        async with _landing_save_lock(prompt_id):
            return await _persist_landing_page(
                prompt_id=prompt_id,
                section=section,
                content=content,
                use_default_template=use_default_template,
                prompt_info=prompt_info,
                is_admin=is_admin,
            )

    except FileLockTimeout:
        logger.warning("Timed out waiting to save landing for prompt %s", prompt_id)
        return JSONResponse(
            {
                "success": False,
                "message": "Another landing save is still in progress; try again.",
            },
            status_code=503,
        )
    except Exception as e:
        logger.exception(f"Error saving landing page for prompt {prompt_id}: {e}")
        return JSONResponse({"success": False, "message": "Error saving changes"}, status_code=500)


@router.get("/landing/{prompt_id}/components", response_class=HTMLResponse)
async def list_components(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        prompt_info = await get_prompt_info(prompt_id)
        ensure_directories(prompt_id, prompt_info)

        base_dir = get_prompt_path(prompt_id, prompt_info)
        components_dir = get_prompt_components_dir(prompt_id, prompt_info)
        css_dir = os.path.join(base_dir, "static", "css")
        js_dir = os.path.join(base_dir, "static", "js")

        def list_files(directory, extension):
            if os.path.exists(directory):
                return [f[: -len(extension)] for f in os.listdir(directory) if f.endswith(extension)]
            return []

        components_by_type = {
            "html": list_files(components_dir, ".html"),
            "css": list_files(css_dir, ".css"),
            "js": list_files(js_dir, ".js"),
        }

        context = await get_template_context(request, current_user)
        context.update(
            {
                "components_by_type": components_by_type,
                "prompt_id": prompt_id,
                "prompt_name": prompt_info["name"],
                "title": f"Components for {prompt_info['name']}",
            }
        )
        return templates.TemplateResponse("web/components_list.html", context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error listing components: {str(e)}")


@router.get("/landing/{prompt_id}/components/{component_type}/{component_name}/edit", response_class=HTMLResponse)
async def edit_component(
    request: Request,
    prompt_id: int,
    component_type: str,
    component_name: str,
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    if current_user is None:
        return _login_template(request)

    if component_type not in ALLOWED_COMPONENT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid component type")

    component_name = secure_filename(component_name)

    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        raise HTTPException(status_code=403, detail="Access denied")

    prompt_info = await get_prompt_info(prompt_id)
    base_dir = Path(get_prompt_path(prompt_id, prompt_info))

    if component_type == "html":
        target_dir = base_dir / "templates" / "components"
        filename = f"{component_name}.html"
    elif component_type == "css":
        target_dir = base_dir / "static" / "css"
        filename = f"{component_name}.css"
    elif component_type == "js":
        target_dir = base_dir / "static" / "js"
        filename = f"{component_name}.js"

    validated_path = validate_path_within_directory(filename, target_dir)

    if not validated_path.exists():
        raise HTTPException(status_code=404, detail="Component not found")

    with open(str(validated_path), "r", encoding="utf-8") as file:
        component_content = file.read()

    context = await get_template_context(request, current_user)
    context.update(
        {
            "content": component_content,
            "component_name": component_name,
            "component_type": component_type,
            "prompt_id": prompt_id,
            "prompt_name": prompt_info["name"],
            "title": f"Edit {component_type.upper()} Component: {component_name} for {prompt_info['name']}",
        }
    )
    return templates.TemplateResponse("web/component_edit.html", context)


@router.put("/api/landing/{prompt_id}/components/{component_type}/{component_name}")
async def save_component(
    request: Request,
    prompt_id: int,
    component_type: str,
    component_name: str,
    encodedContent: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    try:
        if current_user is None:
            return unauthenticated_response()

        if component_type not in ALLOWED_COMPONENT_TYPES:
            return JSONResponse(content={"success": False, "message": "Invalid component type"}, status_code=400)

        component_name = secure_filename(component_name)

        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse(content={"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)
        ensure_directories(prompt_id, prompt_info)

        base_dir = Path(get_prompt_path(prompt_id, prompt_info))

        if component_type == "html":
            target_dir = base_dir / "templates" / "components"
            filename = f"{component_name}.html"
        elif component_type == "css":
            target_dir = base_dir / "static" / "css"
            filename = f"{component_name}.css"
        elif component_type == "js":
            target_dir = base_dir / "static" / "js"
            filename = f"{component_name}.js"

        validated_path = validate_path_within_directory(filename, target_dir)

        content = base64.b64decode(encodedContent).decode("utf-8")
        content = re.sub(r"\n\s*\n", "\n", content.strip())
        content = re.sub(r"\r\n", "\n", content)

        with open(str(validated_path), "w", encoding="utf-8") as file:
            file.write(content)

        return JSONResponse(content={"success": True, "message": "Component saved successfully"})
    except Exception as e:
        return JSONResponse(content={"success": False, "message": str(e)}, status_code=500)


@router.post("/api/landing/{prompt_id}/components", response_class=JSONResponse)
async def create_component(
    request: Request,
    prompt_id: int,
    component_type: str = Form(...),
    component_name: str = Form(...),
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if component_type not in ALLOWED_COMPONENT_TYPES:
        return JSONResponse({"success": False, "message": "Invalid component type"}, status_code=400)

    component_name = secure_filename(component_name)

    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

    prompt_info = await get_prompt_info(prompt_id)
    base_dir = Path(get_prompt_path(prompt_id, prompt_info))

    if component_type == "html":
        target_dir = base_dir / "templates" / "components"
        filename = f"{component_name}.html"
    elif component_type == "css":
        target_dir = base_dir / "static" / "css"
        filename = f"{component_name}.css"
    elif component_type == "js":
        target_dir = base_dir / "static" / "js"
        filename = f"{component_name}.js"

    os.makedirs(str(target_dir), exist_ok=True)
    validated_path = validate_path_within_directory(filename, target_dir)

    if validated_path.exists():
        return JSONResponse({"success": False, "message": "Component already exists"}, status_code=400)

    try:
        with open(str(validated_path), "w", encoding="utf-8") as file:
            if component_type == "html":
                file.write("<div>\n    <!-- Your component content here -->\n</div>")
            elif component_type == "css":
                file.write("/* Your CSS styles here */")
            elif component_type == "js":
                file.write("// Your JavaScript code here")

        return JSONResponse(
            {
                "success": True,
                "message": "Component created successfully",
                "redirect_url": f"/landing/{prompt_id}/components",
            }
        )
    except Exception as e:
        return JSONResponse({"success": False, "message": f"Error creating component: {str(e)}"}, status_code=500)


@router.delete("/api/landing/{prompt_id}/components/{component_type}/{component_name}", response_class=JSONResponse)
async def delete_component(
    prompt_id: int,
    component_type: str,
    component_name: str,
    current_user: User = Depends(get_current_user),
):
    """Delete a component from a prompt."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    try:
        if component_type not in {"html", "css", "js"}:
            return JSONResponse({"success": False, "message": "Invalid component type"}, status_code=400)
        if not component_name or not re.match(r"^[a-zA-Z0-9_-]+$", component_name):
            return JSONResponse({"success": False, "message": "Invalid component name"}, status_code=400)

        is_admin = await current_user.is_admin
        if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
            return JSONResponse({"success": False, "message": "Access denied"}, status_code=403)

        prompt_info = await get_prompt_info(prompt_id)
        prompt_dir = Path(get_prompt_path(prompt_id, prompt_info))

        if component_type == "html":
            file_path = prompt_dir / "templates" / "components" / f"{component_name}.html"
        elif component_type == "css":
            file_path = prompt_dir / "static" / "css" / f"{component_name}.css"
        elif component_type == "js":
            file_path = prompt_dir / "static" / "js" / f"{component_name}.js"

        if not file_path.exists():
            return JSONResponse({"success": False, "message": "Component not found"}, status_code=404)

        os.remove(str(file_path))
        return JSONResponse({"success": True, "message": f"Component '{component_name}' deleted successfully"})

    except Exception as e:
        logger.error(f"Error deleting component: {e}")
        return JSONResponse({"success": False, "message": "Internal server error"}, status_code=500)


@router.get("/api/landing/{prompt_id}/images")
async def get_images(prompt_id: int, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        raise HTTPException(status_code=403, detail="Access denied")

    prompt_info = await get_prompt_info(prompt_id)
    base_dir = get_prompt_path(prompt_id, prompt_info)
    img_dir = os.path.join(base_dir, "static", "img")

    async with get_db_connection(readonly=True) as conn:
        async with conn.execute("SELECT public_id FROM PROMPTS WHERE id = ?", (prompt_id,)) as cursor:
            row = await cursor.fetchone()
            public_id = row[0] if row else None

    slug = slugify(prompt_info["name"])

    images = []
    if os.path.exists(img_dir) and public_id:
        for filename in os.listdir(img_dir):
            if filename.lower().endswith(tuple(ALLOWED_EXTENSIONS)):
                image_url = f"/p/{public_id}/{slug}/static/img/{filename}"
                images.append({"id": filename, "name": filename, "url": image_url})

    return {"images": images}


@router.post("/api/landing/{prompt_id}/images")
async def upload_images(
    prompt_id: int,
    images: List[UploadFile] = File(...),
    names: List[str] = Form(...),
    current_user: User = Depends(get_current_user),
):
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        raise HTTPException(status_code=403, detail="Access denied")

    prompt_info = await get_prompt_info(prompt_id)
    base_dir = get_prompt_path(prompt_id, prompt_info)
    img_dir = Path(base_dir) / "static" / "img"

    uploaded_files = []
    for image, name in zip(images, names):
        if image and allowed_file(image.filename):
            content = await image.read()
            if len(content) > MAX_IMAGE_UPLOAD_SIZE:
                return {
                    "message": f"Image {image.filename} too large. Maximum size is {MAX_IMAGE_UPLOAD_SIZE // (1024 * 1024)}MB",
                    "images": 0,
                }

            try:
                filename, stored_filename = await asyncio.to_thread(
                    _save_prompt_landing_image,
                    content,
                    original_filename=image.filename,
                    requested_name=name,
                    img_dir=img_dir,
                )
            except _LandingImageValidationError as exc:
                return {"message": str(exc), "images": 0}

            image_url = f"/web/{prompt_id}/static/img/{stored_filename}"

            uploaded_files.append({"id": filename, "name": filename, "url": image_url})
        else:
            return {"message": f"Invalid file format: {image.filename}", "images": 0}

    if uploaded_files:
        return {"message": f"Successfully uploaded {len(uploaded_files)} images", "images": uploaded_files}
    return {"message": "No valid images were uploaded", "images": 0}


@router.delete("/api/landing/{prompt_id}/images/{image_id}")
async def delete_landing_image(
    prompt_id: int,
    image_id: str,
    current_user: User = Depends(get_current_user),
):
    """Delete an image from a landing page's static/img directory."""
    require_creator_tools_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    is_admin = await current_user.is_admin
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin):
        raise HTTPException(status_code=403, detail="Access denied")

    prompt_info = await get_prompt_info(prompt_id)
    base_dir = get_prompt_path(prompt_id, prompt_info)
    img_dir = Path(base_dir) / "static" / "img"

    safe_filename = secure_filename(image_id)
    if not safe_filename:
        raise HTTPException(status_code=400, detail="Invalid image filename")

    try:
        validated_path = validate_path_within_directory(safe_filename, img_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid image path")

    if not validated_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")

    try:
        validated_path.unlink()
        return {"success": True, "message": "Image deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting landing image: {e}")
        raise HTTPException(status_code=500, detail="Error deleting image")
