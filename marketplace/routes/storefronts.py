"""Creator storefront and profile routes for the marketplace package."""

from __future__ import annotations

import asyncio
import io
import os
import re

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image as PilImage
from PIL import UnidentifiedImageError

from auth import get_current_user
from captcha_service import get_captcha_config
from common import (
    GOOGLE_CLIENT_ID,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_UPLOAD_SIZE,
    generate_user_hash,
    get_template_context,
    templates,
    users_directory,
)
from database import get_db_connection
from log_config import logger
from marketplace.config import require_storefronts_enabled
from marketplace.services.storefronts import (
    generate_unique_creator_slug,
    get_creator_profile_by_slug,
    get_creator_storefront_data,
    get_own_creator_profile,
    validate_social_links,
)
from models import User
from save_images import resize_image
from security_config import is_forbidden_prompt_name


router = APIRouter()


def _save_creator_avatar_variants(
    content: bytes,
    profile_dir: str,
    user_hash: str,
    suffix: str,
) -> None:
    """Decode, validate, resize, and persist a creator avatar off the event loop."""
    with PilImage.open(io.BytesIO(content)) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Image dimensions too large. Maximum is "
                    f"{MAX_IMAGE_PIXELS:,} pixels"
                ),
            )

        image.load()
        os.makedirs(profile_dir, exist_ok=True)
        for size in (32, 64, 128, "fullsize"):
            if size == "fullsize":
                resized = image
                filename = f"{user_hash}{suffix}_fullsize.webp"
            else:
                resized = resize_image(image, size)
                filename = f"{user_hash}{suffix}_{size}.webp"

            file_path = os.path.join(profile_dir, filename)
            resized.save(file_path, "WEBP")


@router.get("/my-storefront")
async def my_storefront_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the creator storefront management page."""
    require_storefronts_enabled()

    if current_user is None:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "captcha": get_captcha_config(),
                "google_oauth_available": bool(GOOGLE_CLIENT_ID),
            },
        )
    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only users can manage storefronts")

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("my_storefront.html", context)


@router.get("/api/creator-profile")
async def get_creator_profile_api(request: Request, current_user: User = Depends(get_current_user)):
    """Get current user's creator profile data."""
    require_storefronts_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Only users can access creator profiles"}, status_code=403)

    profile = await get_own_creator_profile(current_user.id)
    return JSONResponse(content={"profile": profile})


@router.put("/api/creator-profile")
async def update_creator_profile(request: Request, current_user: User = Depends(get_current_user)):
    """Update current user's creator profile."""
    require_storefronts_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Only users can update creator profiles"}, status_code=403)

    data = await request.json()
    display_name = data.get("display_name", "").strip()
    if not display_name or len(display_name) > 200:
        return JSONResponse(content={"error": "Display name is required (max 200 characters)"}, status_code=400)

    bio = data.get("bio", "").strip()
    if len(bio) > 2000:
        return JSONResponse(content={"error": "Bio must be 2000 characters or less"}, status_code=400)

    slug = data.get("slug", "").strip().lower()
    if slug:
        if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", slug) and len(slug) > 1:
            return JSONResponse(content={"error": "Invalid slug format. Use only lowercase letters, numbers, and hyphens."}, status_code=400)
        if len(slug) > 64:
            return JSONResponse(content={"error": "Slug must be 64 characters or less"}, status_code=400)
        if is_forbidden_prompt_name(slug):
            return JSONResponse(content={"error": "This slug is reserved"}, status_code=400)

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM CREATOR_PROFILES WHERE slug = ? AND user_id != ?",
                (slug, current_user.id),
            )
            if await cursor.fetchone():
                return JSONResponse(content={"error": "This slug is already taken"}, status_code=409)
    else:
        slug = await generate_unique_creator_slug(display_name, exclude_user_id=current_user.id)

    social_links_raw = data.get("social_links", {})
    social_links = validate_social_links(social_links_raw) if social_links_raw else {}
    social_links_json = orjson.dumps(social_links).decode() if social_links else None
    is_public = bool(data.get("is_public", False))

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM CREATOR_PROFILES WHERE user_id = ?",
            (current_user.id,),
        )
        exists = await cursor.fetchone()

        if exists:
            await conn.execute(
                """
                UPDATE CREATOR_PROFILES
                SET display_name = ?, slug = ?, bio = ?, social_links = ?,
                    is_public = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (display_name, slug, bio or None, social_links_json, is_public, current_user.id),
            )
        else:
            await conn.execute(
                """
                INSERT INTO CREATOR_PROFILES (user_id, slug, display_name, bio, social_links, is_public)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (current_user.id, slug, display_name, bio or None, social_links_json, is_public),
            )

        await conn.commit()

    return JSONResponse(content={"success": True, "slug": slug})


@router.post("/api/creator-profile/avatar")
async def upload_creator_avatar(
    file: UploadFile,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Upload creator profile avatar. Saves 4 sizes like profile pictures."""
    require_storefronts_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only users can upload creator avatars")

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
    profile_dir = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "profile")

    content = await file.read()

    if len(content) > MAX_IMAGE_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"Image too large. Maximum size is {MAX_IMAGE_UPLOAD_SIZE // (1024*1024)}MB")

    suffix = "_creator"
    base_url = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/profile/{user_hash}{suffix}"

    try:
        await asyncio.to_thread(
            _save_creator_avatar_variants,
            content,
            profile_dir,
            user_hash,
            suffix,
        )
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="Invalid image file")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error saving creator avatar: %s", exc)
        raise HTTPException(status_code=500, detail="Error processing image")

    async with get_db_connection() as conn:
        await conn.execute(
            "UPDATE CREATOR_PROFILES SET avatar_url = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (base_url, current_user.id),
        )
        await conn.commit()

    return JSONResponse(content={"avatar_url": base_url})


@router.get("/api/creator-profile/check-slug")
async def check_creator_slug(slug: str, current_user: User = Depends(get_current_user)):
    """Check if a slug is available."""
    require_storefronts_enabled()

    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")

    if is_forbidden_prompt_name(slug):
        return JSONResponse(content={"available": False, "reason": "reserved"})

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM CREATOR_PROFILES WHERE slug = ? AND user_id != ?",
            (slug, current_user.id),
        )
        taken = await cursor.fetchone()

    return JSONResponse(content={"available": not taken})


@router.get("/store/{slug}", response_class=HTMLResponse)
async def creator_storefront(request: Request, slug: str, current_user: User = Depends(get_current_user)):
    """Render a creator's public storefront page."""
    require_storefronts_enabled()

    profile = await get_creator_profile_by_slug(slug)
    if not profile:
        raise HTTPException(status_code=404, detail="Creator not found")

    viewer_id = current_user.id if current_user else None
    storefront = await get_creator_storefront_data(profile["user_id"], viewer_id)
    if not storefront:
        raise HTTPException(status_code=404, detail="Creator not found")

    context = await get_template_context(request, current_user, branding_context={"storefront_slug": slug})
    context["storefront"] = storefront
    context["is_authenticated"] = current_user is not None

    return templates.TemplateResponse("storefront.html", context)
