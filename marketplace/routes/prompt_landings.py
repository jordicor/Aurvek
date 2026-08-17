import re
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from auth import get_current_user
from auth_flows import handle_login_request
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, is_internal_ip, slugify, templates
from database import get_db_connection
from log_config import logger
from marketplace.config import require_public_landings_enabled, marketplace_public_landings_enabled
from marketplace.landing.cache import get_landing_path_cached
from marketplace.landing.isolation import (
    build_creator_content_url,
    creator_content_unavailable_response,
    is_creator_content_request,
    is_host_isolated_from_primary,
    sign_content_token,
    verify_content_token,
)
from marketplace.landing.paths import build_prompt_filesystem_path, get_active_custom_domain
from marketplace.landing.rendering import (
    file_response_for_landing_static,
    landing_404_response,
    render_custom_domain_landing_html,
    render_prompt_landing_html,
)
from marketplace.services.acquisition_context import get_prompt_for_registration


router = APIRouter()
custom_domain_router = APIRouter()


def _valid_public_id(public_id: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9]{8}$", public_id))


def _valid_page_name(page: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_-]+$", page))


def _valid_resource_path(resource_path: str) -> bool:
    if not resource_path or "\\" in resource_path or resource_path.startswith("/"):
        return False
    return ".." not in PurePosixPath(resource_path).parts


async def _can_preview_prompt(request: Request, prompt_id: int) -> bool:
    current_user = await get_current_user(request)
    if current_user is None:
        return False
    if await current_user.is_admin:
        return True
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT created_by_user_id FROM PROMPTS WHERE id = ?",
            (prompt_id,),
        )
        row = await cursor.fetchone()
    return bool(row and int(row[0]) == int(current_user.id))


@router.get("/p/{public_id}/{slug}/static/{resource_path:path}")
async def public_landing_static(
    public_id: str,
    slug: str,
    resource_path: str,
    request: Request = None,
):
    """
    Serve static resources for public prompt landing pages.
    """
    try:
        require_public_landings_enabled()

        if not _valid_public_id(public_id):
            raise HTTPException(status_code=400, detail="Invalid public_id format")
        if not _valid_resource_path(resource_path):
            raise HTTPException(status_code=404, detail="Resource not found")

        landing_data = await get_landing_path_cached(public_id)
        if slug != slugify(landing_data["prompt_name"]):
            raise HTTPException(status_code=404, detail="Resource not found")

        if not is_creator_content_request(request):
            custom_domain = await get_active_custom_domain(landing_data["prompt_id"])
            if custom_domain and is_host_isolated_from_primary(custom_domain):
                return RedirectResponse(
                    url=f"https://{custom_domain}/static/{resource_path}",
                    status_code=302,
                )
            if not (landing_data["has_landing_page"] and landing_data["landing_trusted"]):
                # Non-servable (no landing page) or untrusted creator assets stay
                # on the isolated origin (or fail closed). Trusted, servable assets
                # fall through and are served directly from the primary domain,
                # matching the trusted page policy.
                #
                # `public` is intentionally NOT part of this gate (unlike the page
                # and /internal/resolve-landing routes, which enforce it): an admin
                # previewing a not-yet-public landing loads it from the primary
                # domain, and its asset sub-requests carry no preview auth, so
                # requiring `public` here would break preview of trusted landings.
                isolated_url = build_creator_content_url(
                    f"/p/{public_id}/{slug}/static/{resource_path}"
                )
                if not isolated_url:
                    return creator_content_unavailable_response()
                return RedirectResponse(url=isolated_url, status_code=302)

        static_root = landing_data["path"] / "static"
        static_path = static_root / resource_path
        try:
            resolved = static_path.resolve(strict=False)
            resolved.relative_to(static_root.resolve())
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="Resource not found")

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="Resource not found")

        return file_response_for_landing_static(resolved)

    except HTTPException as e:
        if e.status_code == 404:
            return landing_404_response()
        raise
    except Exception as e:
        logger.error(f"Error serving landing static resource: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/p/{public_id}/{slug}")
async def public_landing_redirect_trailing_slash(public_id: str, slug: str):
    """
    Redirect to trailing slash so relative URLs work correctly.
    """
    require_public_landings_enabled()
    return RedirectResponse(url=f"/p/{public_id}/{slug}/", status_code=301)


@router.get("/p/{public_id}/{slug}/register", response_class=HTMLResponse)
async def register_page_user(request: Request, public_id: str, slug: str):
    """
    Registration page for users from a prompt landing page.
    Must be defined before the generic page route.
    """
    require_public_landings_enabled()

    if not _valid_public_id(public_id):
        raise HTTPException(status_code=400, detail="Invalid public_id format")

    prompt = await get_prompt_for_registration(public_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    canonical_slug = slugify(prompt["name"])
    if slug != canonical_slug:
        raise HTTPException(status_code=404, detail="Page not found")

    base_url = f"/p/{public_id}/{canonical_slug}"
    response = templates.TemplateResponse(
        "register_public.html",
        {
            "request": request,
            "target_role": "customer",
            "prompt": prompt,
            "login_url": f"{base_url}/login",
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        },
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@router.api_route("/p/{public_id}/{slug}/login", methods=["GET", "POST"])
async def login_page_user(request: Request, public_id: str, slug: str):
    """
    Login page for users from a prompt landing page.
    Must be defined before the generic page route.
    """
    require_public_landings_enabled()

    if not _valid_public_id(public_id):
        raise HTTPException(status_code=400, detail="Invalid public_id format")

    prompt = await get_prompt_for_registration(public_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    canonical_slug = slugify(prompt["name"])
    if slug != canonical_slug:
        raise HTTPException(status_code=404, detail="Page not found")

    base_url = f"/p/{public_id}/{canonical_slug}"
    response = await handle_login_request(
        request,
        prompt_context=prompt,
        login_url=f"{base_url}/login",
        register_url=f"{base_url}/register",
    )
    response.headers["X-Robots-Tag"] = "noindex"
    return response


@router.get("/p/{public_id}/{slug}/")
@router.get("/p/{public_id}/{slug}/{page}")
async def public_landing_page(
    request: Request,
    public_id: str,
    slug: str,
    page: str = "home",
):
    """
    Serve public landing pages for prompts.
    """
    try:
        require_public_landings_enabled()

        if not _valid_public_id(public_id):
            raise HTTPException(status_code=400, detail="Invalid public_id format")

        if not _valid_page_name(page):
            raise HTTPException(status_code=400, detail="Invalid page name")

        landing_data = await get_landing_path_cached(public_id)
        canonical_slug = slugify(landing_data["prompt_name"])

        if slug != canonical_slug:
            raise HTTPException(status_code=404, detail="Page not found")

        is_preview = request.query_params.get("preview") == "1"
        is_embed = request.query_params.get("embed") == "1"
        # A landing is publicly servable only when it is public AND actually has
        # a landing page. Trusted (platform-authored) landings are safe to serve
        # directly from the primary domain; untrusted ones must stay on the
        # isolated creator-content origin.
        is_servable = bool(landing_data["public"]) and bool(landing_data["has_landing_page"])
        is_trusted = bool(landing_data["landing_trusted"])

        if is_creator_content_request(request):
            if is_preview:
                token = request.query_params.get("preview_token")
                expected = {
                    "purpose": "prompt_preview",
                    "prompt_id": int(landing_data["prompt_id"]),
                    "page": page,
                }
                if verify_content_token(token, expected=expected) is None:
                    raise HTTPException(status_code=404, detail="Page not found")
            elif not is_servable:
                raise HTTPException(status_code=404, detail="Page not found")
        elif is_preview:
            if not await _can_preview_prompt(request, landing_data["prompt_id"]):
                raise HTTPException(status_code=403, detail="Access denied")
            if not is_trusted:
                preview_token = sign_content_token(
                    {
                        "purpose": "prompt_preview",
                        "prompt_id": int(landing_data["prompt_id"]),
                        "page": page,
                    },
                    ttl_seconds=300,
                )
                isolated_url = build_creator_content_url(
                    f"/p/{public_id}/{canonical_slug}/{'' if page == 'home' else page}",
                    {"preview": 1, "preview_token": preview_token},
                )
                if not isolated_url or not preview_token:
                    return creator_content_unavailable_response()
                return RedirectResponse(
                    url=isolated_url,
                    status_code=302,
                    headers={"Cache-Control": "no-store"},
                )
            # Trusted preview is safe on the primary domain: an authorized admin
            # can review a non-public trusted landing without the isolated origin
            # being configured. Fall through to serve it directly below.
        else:
            custom_domain = await get_active_custom_domain(landing_data["prompt_id"])
            if custom_domain and is_host_isolated_from_primary(custom_domain):
                redirect_path = "/" if page == "home" else f"/{page}"
                if is_embed:
                    redirect_path += "?embed=1"
                return RedirectResponse(
                    url=f"https://{custom_domain}{redirect_path}",
                    status_code=302,
                )
            if not is_servable:
                raise HTTPException(status_code=404, detail="Page not found")
            if not is_trusted:
                isolated_path = (
                    f"/p/{public_id}/{canonical_slug}/"
                    if page == "home"
                    else f"/p/{public_id}/{canonical_slug}/{page}"
                )
                isolated_url = build_creator_content_url(
                    isolated_path,
                    {"embed": 1} if is_embed else None,
                )
                if not isolated_url:
                    return creator_content_unavailable_response()
                return RedirectResponse(url=isolated_url, status_code=302)
            # Trusted + servable: serve directly from the primary domain below.

        html_path = landing_data["path"] / f"{page}.html"

        if not html_path.is_file():
            raise HTTPException(status_code=404, detail="Page not found")

        html_content = await render_prompt_landing_html(
            html_path,
            landing_data["prompt_id"],
            page=page,
            is_preview=is_preview,
            is_unlisted=bool(landing_data["is_unlisted"]),
        )

        headers = {}
        if landing_data["is_unlisted"]:
            headers["X-Robots-Tag"] = "noindex, nofollow"
        if is_preview:
            headers["Cache-Control"] = "no-store"

        return HTMLResponse(content=html_content, headers=headers)

    except HTTPException as e:
        if e.status_code == 404:
            return landing_404_response()
        raise
    except Exception as e:
        logger.error(f"Error serving landing page: {e}")
        return landing_404_response()


@router.get("/internal/resolve-landing")
async def internal_resolve_landing(
    request: Request,
    public_id: str = Query(..., min_length=8, max_length=8),
    slug: str = Query(..., min_length=1),
    page: str = Query("home"),
):
    """
    Internal endpoint called by nginx to resolve landing page paths.
    """
    require_public_landings_enabled()

    client_ip = request.client.host if request.client else None
    if not client_ip or not is_internal_ip(client_ip):
        logger.warning(f"Blocked external request to /internal/resolve-landing from {client_ip}")
        raise HTTPException(status_code=403, detail="Forbidden - internal endpoint")

    try:
        if not _valid_public_id(public_id):
            raise HTTPException(status_code=400, detail="Invalid public_id format")
        if not _valid_page_name(page):
            raise HTTPException(status_code=400, detail="Invalid page name")

        landing_data = await get_landing_path_cached(public_id)
        canonical_slug = slugify(landing_data["prompt_name"])

        if slug != canonical_slug:
            raise HTTPException(status_code=404, detail="Page not found")

        # nginx serves the resolved file directly from the primary domain, so
        # only trusted, publicly-servable landings may resolve here. Untrusted or
        # non-public landings must never be served this way (they belong on the
        # isolated origin, if configured).
        if not (
            landing_data["public"]
            and landing_data["has_landing_page"]
            and landing_data["landing_trusted"]
        ):
            raise HTTPException(status_code=404, detail="Page not found")

        html_path = landing_data["path"] / f"{page}.html"
        if not html_path.is_file():
            raise HTTPException(status_code=404, detail="Page not found")

        headers = {"X-File-Path": str(html_path.resolve())}
        if landing_data["is_unlisted"]:
            headers["X-Robots-Tag"] = "noindex, nofollow"

        return Response(status_code=200, headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in internal resolve-landing: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def serve_custom_domain_home(request: Request):
    """
    Serve custom-domain home.html for the core / route.
    """
    if not marketplace_public_landings_enabled():
        return landing_404_response()

    try:
        prompt_id = request.state.prompt_id
        prompt_name = request.state.prompt_name
        username = request.state.username

        prompt_dir = build_prompt_filesystem_path(username, prompt_id, prompt_name)
        html_path = prompt_dir / "home.html"

        if html_path.is_file():
            html_content = await render_custom_domain_landing_html(
                html_path,
                prompt_id,
            )
            return HTMLResponse(content=html_content)
    except Exception as e:
        logger.error(f"Error serving custom domain landing at /: {e}")
    return landing_404_response()


@custom_domain_router.get("/{page:path}")
async def custom_domain_landing(request: Request, page: str = ""):
    """
    Serve landing pages for custom domains.
    This router must be included after all normal routes.
    """
    if not getattr(request.state, "custom_domain", False):
        return landing_404_response()
    if not marketplace_public_landings_enabled():
        return landing_404_response()

    try:
        prompt_id = request.state.prompt_id
        prompt_name = request.state.prompt_name
        username = request.state.username

        if not page or page == "/":
            page = "home"
        else:
            page = page.strip("/").split("/")[0]

        if not _valid_page_name(page):
            return landing_404_response()

        prompt_dir = build_prompt_filesystem_path(username, prompt_id, prompt_name)
        html_path = prompt_dir / f"{page}.html"

        if not html_path.is_file():
            return landing_404_response()

        html_content = await render_custom_domain_landing_html(
            html_path,
            prompt_id,
        )
        return HTMLResponse(content=html_content)

    except Exception as e:
        logger.error(f"Error serving custom domain landing: {e}")
        return landing_404_response()
