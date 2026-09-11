"""
Custom Domain Middleware for Landing Pages.

Handles routing for custom domains pointing to prompt landing pages.
Uses in-memory cache with TTL to minimize database lookups.

Static files for custom domains are served directly from this middleware
to bypass FastAPI's StaticFiles mount at /static which would otherwise
intercept and look in the global data/static/ directory.
"""

import logging
import re
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Optional, Dict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from cachetools import TTLCache
from i18n import get_translator

from marketplace.landing.isolation import (
    apply_creator_content_headers,
    get_creator_content_config,
    is_host_isolated_from_primary,
    primary_app_url,
    verify_content_token,
)

logger = logging.getLogger(__name__)

# Media type mapping for static file serving
_MEDIA_TYPES = {
    '.css': 'text/css',
    '.js': 'application/javascript',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
    '.mp3': 'audio/mpeg',
    '.mp4': 'video/mp4',
    '.json': 'application/json',
}

# Cache: domain -> prompt_data (5 minute TTL, max 1000 entries)
_domain_cache: TTLCache = TTLCache(maxsize=1000, ttl=300)

# Primary domains that should skip custom domain lookup
_primary_domains: set = set()

_PAGE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9]{8}$")
_WELCOME_PAGE_RE = re.compile(r"^/_aurvek/welcome/(prompt|pack)/(\d+)/([^/]+)/$")
_WELCOME_STATIC_RE = re.compile(
    r"^/_aurvek/welcome/(prompt|pack)/(\d+)/([^/]+)/static/(.+)$"
)
_ALLOWED_COOKIE_NAMES = {"_aurvek_visitor"}


def _build_prompt_path(username: str, prompt_id: int, prompt_name: str) -> Path:
    """Build filesystem path to a prompt's landing page directory."""
    from common import generate_user_hash, sanitize_name, DATA_DIR

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
    padded_id = f"{prompt_id:07d}"
    safe_name = sanitize_name(prompt_name)

    return (
        DATA_DIR / "users" / hash_prefix1 / hash_prefix2 / user_hash
        / "prompts" / padded_id[:3] / f"{padded_id[3:]}_{safe_name}"
    )


def set_primary_domains(domains: list):
    """
    Set the primary domains (call during app startup).
    Requests to these domains skip the custom domain DB lookup.
    """
    global _primary_domains
    _primary_domains = {d.lower().strip() for d in domains if d}


def is_primary_domain(host: str) -> bool:
    """Check if host is a primary domain."""
    host = host.lower().strip()
    return host in _primary_domains or host in ("localhost", "127.0.0.1", "")


class CustomDomainMiddleware(BaseHTTPMiddleware):
    """
    Middleware to handle custom domain routing for prompt landing pages.

    Flow:
    1. Extract Host header
    2. If Host is primary domain, pass through (no DB lookup)
    3. If Host is different, check cache -> DB for custom domain mapping
    4. If verified and active, inject prompt data into request.state
    5. Otherwise, return 404
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # The outer ASGI embed boundary has classified the exact registered host,
        # restricted the path, and stripped native cookies before reaching here.
        # This is not a new primary domain or a creator-controlled landing host.
        if getattr(request.state, "embed_host", False):
            return await call_next(request)
        host = request.headers.get("host", "").lower().split(":")[0]

        creator_config = get_creator_content_config(primary_hosts=_primary_domains)
        if creator_config and host == creator_config.host:
            request.state.creator_content_origin = True
            self._strip_application_cookies(request)
            response = await self._dispatch_creator_content(request, call_next)
            # This is an Aurvek-owned, dedicated origin. User-supplied custom
            # domains share the catch-all server and deliberately do not get a
            # persistent HSTS policy that would outlive their configuration.
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
            return response

        # Skip for primary domains
        if is_primary_domain(host):
            # Legacy welcome assets contain creator-authored CSS/JS.  Welcome
            # pages now load them through signed paths on the isolated host;
            # keeping the old primary-origin route reachable would preserve a
            # same-origin execution primitive.
            if request.url.path.startswith("/home/static/"):
                return Response(status_code=404, headers={"Cache-Control": "no-store"})
            return await call_next(request)

        from marketplace.config import marketplace_public_landings_enabled
        if not marketplace_public_landings_enabled():
            return self._not_found_response(request)

        # A custom landing domain must be on a separate site from Aurvek.  A
        # sibling subdomain could otherwise receive same-site cookies and make
        # creator JavaScript an application-origin concern again.
        if not is_host_isolated_from_primary(host, primary_hosts=_primary_domains):
            return self._not_found_response(request)

        # Check if this is a custom domain
        domain_data = await self._get_domain_data(host)

        if domain_data is None:
            # Not a known custom domain -> 404
            return self._not_found_response(request)

        # Inject prompt data into request state
        request.state.custom_domain = True
        request.state.custom_domain_host = host
        request.state.prompt_id = domain_data["prompt_id"]
        request.state.prompt_name = domain_data["prompt_name"]
        request.state.username = domain_data["username"]
        request.state.public_id = domain_data["public_id"]

        self._strip_application_cookies(request)

        return await self._dispatch_custom_domain(request, call_next, domain_data)

    async def _dispatch_creator_content(self, request: Request, call_next) -> Response:
        """Allow only creator-content routes on the cookie-less shared host."""
        path = request.url.path
        method = request.method.upper()

        if path == "/api/analytics/track-visit" and method == "POST":
            return self._finalize_isolated_response(await call_next(request), path=path)

        welcome_match = _WELCOME_PAGE_RE.fullmatch(path)
        if welcome_match and method in {"GET", "HEAD"}:
            response = await self._serve_isolated_welcome(request, welcome_match)
            return self._finalize_isolated_response(
                response,
                path=path,
                allow_primary_frame=True,
                no_store=True,
            )

        welcome_static_match = _WELCOME_STATIC_RE.fullmatch(path)
        if welcome_static_match and method in {"GET", "HEAD"}:
            response = await self._serve_isolated_welcome_static(
                request,
                welcome_static_match,
            )
            return self._finalize_isolated_response(
                response,
                path=path,
                allow_primary_frame=True,
                no_store=True,
            )

        route_kind = self._creator_route_kind(path)
        if route_kind == "auth" and method in {"GET", "HEAD"}:
            target = primary_app_url(path)
            if target:
                return self._finalize_isolated_response(
                    RedirectResponse(target, status_code=302),
                    path=path,
                    no_store=True,
                )
            return self._not_found_response(request)

        if route_kind == "content" and method in {"GET", "HEAD"}:
            is_preview = request.query_params.get("preview") == "1"
            is_embed = request.query_params.get("embed") == "1"
            return self._finalize_isolated_response(
                await call_next(request),
                path=path,
                allow_primary_frame=is_embed,
                no_store=is_preview,
            )

        return self._not_found_response(request)

    async def _dispatch_custom_domain(
        self,
        request: Request,
        call_next,
        domain_data: Dict,
    ) -> Response:
        """Expose a strict landing-only surface on creator custom domains."""
        path = request.url.path
        method = request.method.upper()

        if path == "/api/analytics/track-visit" and method == "POST":
            return self._finalize_isolated_response(await call_next(request), path=path)

        if path in {"/register", "/login"}:
            if method not in {"GET", "HEAD"}:
                return self._not_found_response(request)
            from common import slugify

            auth_path = (
                f"/p/{domain_data['public_id']}/{slugify(domain_data['prompt_name'])}{path}"
            )
            target = primary_app_url(auth_path)
            if not target:
                return self._not_found_response(request)
            return self._finalize_isolated_response(
                RedirectResponse(target, status_code=302),
                path=path,
                no_store=True,
            )

        # Serve static files directly to bypass the global StaticFiles mount.
        # Without this, /static/* requests hit app.mount("/static", StaticFiles(...))
        # which looks in data/static/ (global) instead of the prompt's directory.
        path = request.url.path
        if path.startswith("/static/") and method in {"GET", "HEAD"}:
            return self._finalize_isolated_response(
                self._serve_landing_static(domain_data, path[8:]),
                path=path,
            )  # strip "/static/"

        if method in {"GET", "HEAD"} and self._valid_custom_landing_path(path):
            is_embed = request.query_params.get("embed") == "1"
            return self._finalize_isolated_response(
                self._serve_custom_landing_html(domain_data, path, request),
                path=path,
                allow_primary_frame=is_embed,
            )

        return self._not_found_response(request)

    @staticmethod
    def _creator_route_kind(path: str) -> str | None:
        """Classify the narrow route surface supported by the shared host."""
        parts = path.split("/")
        # /p/{public_id}/{slug}[/{page|static/...}]
        if len(parts) >= 4 and parts[1] == "p" and _PUBLIC_ID_RE.fullmatch(parts[2]):
            if not parts[3] or len(parts[3]) > 200:
                return None
            if len(parts) == 4 or (len(parts) == 5 and parts[4] == ""):
                return "content"
            if len(parts) == 5 and parts[4] in {"register", "login"}:
                return "auth"
            if len(parts) == 5 and _PAGE_RE.fullmatch(parts[4]):
                return "content"
            if len(parts) >= 6 and parts[4] == "static" and all(parts[5:]):
                return "content"
            return None

        # /pack/{public_id}/{slug}/[static/...]
        if len(parts) >= 4 and parts[1] == "pack" and _PUBLIC_ID_RE.fullmatch(parts[2]):
            if not parts[3] or len(parts[3]) > 200:
                return None
            if len(parts) == 4 or (len(parts) == 5 and parts[4] == ""):
                return "content"
            if len(parts) == 5 and parts[4] in {"register", "login"}:
                return "auth"
            if len(parts) >= 6 and parts[4] == "static" and all(parts[5:]):
                return "content"
        return None

    @staticmethod
    def _valid_custom_landing_path(path: str) -> bool:
        if path in {"/", "/index.html"}:
            return True
        parts = path.split("/")
        return len(parts) == 2 and bool(_PAGE_RE.fullmatch(parts[1]))

    @staticmethod
    def _strip_application_cookies(request: Request) -> None:
        """Remove Aurvek auth/session cookies before isolated route handling."""
        cookie_header = request.headers.get("cookie", "")
        safe_pairs: list[str] = []
        if cookie_header:
            try:
                parsed = SimpleCookie()
                parsed.load(cookie_header)
                safe_pairs = [
                    f"{name}={parsed[name].value}"
                    for name in _ALLOWED_COOKIE_NAMES
                    if name in parsed
                ]
            except Exception:
                safe_pairs = []

        headers = [
            (key, value)
            for key, value in request.scope.get("headers", [])
            if key.lower() != b"cookie"
        ]
        if safe_pairs:
            headers.append((b"cookie", "; ".join(safe_pairs).encode("latin-1")))
        request.scope["headers"] = headers
        # Starlette caches Headers and cookie parsing lazily on the request.
        request.__dict__.pop("_headers", None)
        request.__dict__.pop("_cookies", None)

    @staticmethod
    def _filter_response_cookies(response: Response) -> None:
        safe_headers = []
        for key, value in response.raw_headers:
            if key.lower() != b"set-cookie":
                safe_headers.append((key, value))
                continue
            cookie_name = value.split(b"=", 1)[0].decode("latin-1", "ignore").strip()
            if cookie_name in _ALLOWED_COOKIE_NAMES:
                safe_headers.append((key, value))
        response.raw_headers = safe_headers

    def _finalize_isolated_response(
        self,
        response: Response,
        *,
        path: str,
        allow_primary_frame: bool = False,
        no_store: bool = False,
    ) -> Response:
        self._filter_response_cookies(response)
        is_static_asset = path.startswith("/static/") or "/static/" in path
        response = apply_creator_content_headers(
            response,
            allow_primary_frame=allow_primary_frame,
            # Explore and welcome iframes intentionally omit
            # allow-same-origin. Their local subresources therefore have an
            # opaque initiator and must opt into CORP cross-origin.
            allow_cross_origin_resource=is_static_asset,
            no_store=no_store,
        )
        if is_static_asset:
            # Sandboxed Explore/welcome documents have an opaque Origin
            # (serialized as null). Public/signed static assets carry no
            # ambient credentials, so a wildcard is safe and is required by
            # webfonts and ES modules in addition to CORP.
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    async def _serve_isolated_welcome(self, request: Request, match: re.Match) -> Response:
        entity_type, raw_entity_id, token = match.groups()
        entity_id = int(raw_entity_id)
        expected = {
            "purpose": "welcome",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        if verify_content_token(token, expected=expected) is None:
            return self._not_found_response(request)

        from welcome_service import build_world

        world = await build_world(entity_type, entity_id)
        if not world:
            return self._not_found_response(request)
        index_path = Path(world["path"]) / "welcome" / "index.html"
        try:
            html = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return self._not_found_response(request)

        prefix = f"/_aurvek/welcome/{entity_type}/{entity_id}/{token}/"
        html = html.replace("/home/static/", prefix + "static/")
        primary_chat = primary_app_url("/chat")
        if primary_chat:
            html = html.replace(
                'href="/chat',
                f'target="_top" href="{primary_chat}',
            )
            html = html.replace(
                "href='/chat",
                f"target='_top' href='{primary_chat}",
            )

        return HTMLResponse(html)

    async def _serve_isolated_welcome_static(
        self,
        request: Request,
        match: re.Match,
    ) -> Response:
        entity_type, raw_entity_id, token, resource_path = match.groups()
        entity_id = int(raw_entity_id)
        expected = {
            "purpose": "welcome",
            "entity_type": entity_type,
            "entity_id": entity_id,
        }
        if verify_content_token(token, expected=expected) is None:
            return self._not_found_response(request)

        from welcome_service import build_world

        world = await build_world(entity_type, entity_id)
        if not world:
            return self._not_found_response(request)
        static_root = (Path(world["path"]) / "welcome" / "static").resolve()
        try:
            resolved = (static_root / resource_path).resolve(strict=True)
            resolved.relative_to(static_root)
        except (OSError, ValueError):
            return self._not_found_response(request)
        if not resolved.is_file():
            return self._not_found_response(request)

        suffix = resolved.suffix.lower()
        media_type = _MEDIA_TYPES.get(suffix, "application/octet-stream")
        return FileResponse(resolved, media_type=media_type)

    def _not_found_response(self, request: Request) -> Response:
        from marketplace.landing.rendering import landing_404_response

        response = landing_404_response(get_translator(request), domain=True)
        return apply_creator_content_headers(response, no_store=True)

    async def _get_domain_data(self, domain: str) -> Optional[Dict]:
        """Get prompt data for a custom domain (cached)."""
        # Check cache first
        if domain in _domain_cache:
            return _domain_cache[domain]

        # DB lookup
        from database import get_db_connection

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute("""
                SELECT
                    pcd.prompt_id,
                    p.name as prompt_name,
                    p.public_id,
                    u.username
                FROM PROMPT_CUSTOM_DOMAINS pcd
                JOIN PROMPTS p ON pcd.prompt_id = p.id
                JOIN USERS u ON p.created_by_user_id = u.id
                WHERE pcd.custom_domain = ?
                  AND pcd.is_active = 1
                  AND pcd.verification_status = 1
            """, (domain,))
            result = await cursor.fetchone()

        if result:
            data = {
                "prompt_id": result[0],
                "prompt_name": result[1],
                "public_id": result[2],
                "username": result[3]
            }
            _domain_cache[domain] = data
            return data

        return None

    def _serve_landing_static(self, domain_data: Dict, resource_path: str) -> Response:
        """
        Serve a static file from the prompt's landing page directory.
        Returns FileResponse on success, 404 on failure.
        """
        if not resource_path or ".." in resource_path:
            return Response(status_code=404)

        prompt_dir = _build_prompt_path(
            domain_data["username"],
            domain_data["prompt_id"],
            domain_data["prompt_name"],
        )
        static_path = prompt_dir / "static" / resource_path

        # Security first: validate path stays within prompt directory
        try:
            resolved = static_path.resolve(strict=False)
            resolved.relative_to(prompt_dir.resolve())
        except (ValueError, OSError):
            return Response(status_code=404)

        if not resolved.is_file():
            return Response(status_code=404)

        suffix = resolved.suffix.lower()
        media_type = _MEDIA_TYPES.get(suffix, 'application/octet-stream')

        return FileResponse(
            resolved,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )

    def _serve_custom_landing_html(self, domain_data: Dict, path: str, request: Request) -> Response:
        """Serve creator HTML directly so application routes cannot win routing."""
        if path in {"/", "/index.html"}:
            page = "home"
        else:
            page = path.strip("/")
        if not _PAGE_RE.fullmatch(page):
            return self._not_found_response(request)

        prompt_dir = _build_prompt_path(
            domain_data["username"],
            domain_data["prompt_id"],
            domain_data["prompt_name"],
        )
        html_path = prompt_dir / f"{page}.html"
        try:
            resolved = html_path.resolve(strict=True)
            resolved.relative_to(prompt_dir.resolve())
            html = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return self._not_found_response(request)

        from marketplace.landing.rendering import inject_custom_domain_analytics

        html = inject_custom_domain_analytics(html, domain_data["prompt_id"])
        return HTMLResponse(html)



def invalidate_domain_cache(domain: str):
    """Invalidate cache for a specific domain (call after updates)."""
    domain = domain.lower().strip()
    if domain in _domain_cache:
        del _domain_cache[domain]


def clear_domain_cache():
    """Clear entire domain cache (for maintenance)."""
    _domain_cache.clear()
