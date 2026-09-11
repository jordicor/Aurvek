import asyncio
import math
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Request, status
from fastapi.responses import RedirectResponse

from auth import (
    create_login_response,
    create_user_info,
    get_user_by_id,
    get_user_by_username,
    verify_password,
)
from captcha_service import get_captcha_config, verify_captcha
from common import GOOGLE_CLIENT_ID, templates
from database import get_db_connection
from i18n import get_translator
from marketplace.config import marketplace_discovery_enabled
from rate_limiter import (
    RateLimitConfig as RLC,
    check_login_failure_limits,
    check_rate_limits,
    clear_login_failures,
    get_login_backoff_seconds,
    get_client_ip,
    record_failure,
    record_login_failure,
)


async def get_after_login_redirect(user_id: int) -> str:
    """Get user's preferred after-login redirect from home_preferences."""
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT home_preferences FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            row = await cursor.fetchone()
            if row and row["home_preferences"]:
                import json

                prefs = json.loads(row["home_preferences"])
                after_login = prefs.get("after_login")
                allowed_routes = {"/home", "/chat", "/dashboard"}
                if marketplace_discovery_enabled():
                    allowed_routes.add("/explore")
                if after_login in allowed_routes:
                    return after_login
    except Exception:
        pass
    return "/home"


def _browser_bound_embed_next(request: Request, value: str | None) -> str | None:
    """Only resume a transaction already bound to this native browser session."""
    if not value or len(value) > 256:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != "/embed/authorize/complete":
        return None
    query = parse_qs(parsed.query)
    if set(query) != {"transaction"} or len(query["transaction"]) != 1:
        return None
    transaction = query["transaction"][0]
    if transaction not in request.session.get("embed_authorizations", []):
        return None
    return "/embed/authorize/complete?" + urlencode({"transaction": transaction})


def remember_embed_registration_next(request: Request) -> dict:
    next_url = _browser_bound_embed_next(request, request.query_params.get("next"))
    if not next_url:
        return {}
    request.session["embed_registration_next"] = next_url
    return {"login_url": "/login?" + urlencode({"next": next_url}),
            "google_oauth_url": "/auth/google?" + urlencode({"next": next_url}),
            "embed_registration_transaction": parse_qs(urlsplit(next_url).query)["transaction"][0]}


def consume_embed_registration_next(request: Request) -> str | None:
    return _browser_bound_embed_next(request, request.session.pop("embed_registration_next", None))


async def handle_login_request(
    request: Request,
    prompt_context: dict | None = None,
    login_url: str = "/login",
    register_url: str = "/register",
):
    """
    Shared login logic for both /login and /p/{public_id}/{slug}/login.
    """
    t = get_translator(request).t
    next_url = request.query_params.get("next")

    template_context = {
        "request": request,
        "prompt": prompt_context,
        "login_url": login_url,
        "register_url": register_url,
        "captcha": get_captcha_config(),
        "google_oauth_available": bool(GOOGLE_CLIENT_ID),
    }

    def _update_auth_template_links(current_next_url: Optional[str]) -> None:
        recovery_url = "/magic-link-recovery"
        if current_next_url:
            recovery_url += "?" + urlencode({"next": current_next_url})

        oauth_params = {}
        if prompt_context:
            oauth_params["prompt_id"] = prompt_context["id"]
        if current_next_url:
            oauth_params["next"] = current_next_url

        google_oauth_url = "/auth/google"
        if oauth_params:
            google_oauth_url += "?" + urlencode(oauth_params)

        template_context["next_url"] = current_next_url or ""
        template_context["recovery_url"] = recovery_url
        template_context["google_oauth_url"] = google_oauth_url
        # Registration keeps the same native internal return through login/OAuth.
        template_context["register_url"] = register_url
        if current_next_url and current_next_url.startswith("/") and not current_next_url.startswith("//"):
            separator = "&" if "?" in register_url else "?"
            template_context["register_url"] += separator + urlencode({"next": current_next_url})

    _update_auth_template_links(next_url)

    if request.method == "POST":
        form = await request.form()
        next_url = form.get("next") or next_url
        _update_auth_template_links(next_url)

        magic_token = form.get("magic_token")
        if magic_token:
            expected_nonce = request.session.pop("ml_confirm_nonce", None)
            submitted_nonce = form.get("confirm_nonce")
            if not expected_nonce or expected_nonce != submitted_nonce:
                record_failure(request, "magic_link")
                return templates.TemplateResponse(
                    "login.html",
                    {**template_context, "error": t("auth.error.invalid_request")},
                )

            rate_error = check_rate_limits(
                request,
                ip_limit=RLC.LOGIN_BY_IP_ALL,
                action_name="magic_link",
            )
            if rate_error:
                return templates.TemplateResponse(
                    "login.html",
                    {**template_context, "error": rate_error["message"]},
                )

            async with get_db_connection(readonly=False) as conn:
                cursor = await conn.cursor()
                await cursor.execute(
                    "SELECT user_id, expires_at FROM magic_links WHERE token = ?",
                    (magic_token,),
                )
                magic_link = await cursor.fetchone()

                if magic_link:
                    try:
                        expires_at = datetime.strptime(
                            magic_link["expires_at"], "%Y-%m-%d %H:%M:%S.%f"
                        )
                    except ValueError:
                        expires_at = datetime.strptime(
                            magic_link["expires_at"], "%Y-%m-%d %H:%M:%S"
                        )

                    if expires_at >= datetime.now():
                        user_obj = await get_user_by_id(magic_link["user_id"])
                        if user_obj and user_obj.is_enabled:
                            user_info = await create_user_info(user_obj, True)
                            default_redirect = await get_after_login_redirect(user_obj.id)

                            await cursor.execute(
                                "DELETE FROM magic_links WHERE token = ?",
                                (magic_token,),
                            )
                            if cursor.rowcount == 1:
                                await conn.commit()
                                remaining = math.ceil(
                                    (expires_at - datetime.now()).total_seconds()
                                )
                                if remaining <= 0:
                                    record_failure(request, "magic_link")
                                    return templates.TemplateResponse(
                                        "login.html",
                                        {
                                            **template_context,
                                            "error": t("auth.error.magic_expired"),
                                        },
                                    )
                                clear_login_failures(request, user_obj.username)
                                return create_login_response(
                                    user_info,
                                    redirect_url=next_url,
                                    default_redirect=default_redirect,
                                    expires_delta=timedelta(seconds=remaining),
                                )

            record_failure(request, "magic_link")
            return templates.TemplateResponse(
                "login.html",
                {**template_context, "error": t("auth.error.magic_invalid_expired")},
            )

        username = form.get("username", "").strip().lower()
        password = form.get("password", "")
        captcha_token = (
            form.get("captcha_token", "")
            or form.get("cf-turnstile-response", "")
            or form.get("g-recaptcha-response", "")
        )

        rate_error = check_rate_limits(
            request,
            ip_limit=RLC.LOGIN_BY_IP_ALL,
            action_name="login",
        )
        if rate_error:
            return templates.TemplateResponse(
                "login.html",
                {**template_context, "error": rate_error["message"]},
            )

        fail_error = check_login_failure_limits(request, username)
        if fail_error:
            return templates.TemplateResponse(
                "login.html",
                {**template_context, "error": fail_error["message"]},
            )

        client_ip = get_client_ip(request)
        captcha_ok, captcha_error = await verify_captcha(captcha_token, client_ip)
        if not captcha_ok:
            record_failure(request, "login_captcha")
            return templates.TemplateResponse(
                "login.html",
                {**template_context, "error": t("auth.error.captcha")},
            )

        user_result = await get_user_by_username(username)

        if user_result and not user_result.is_enabled:
            failure_count = record_login_failure(request, username)
            await asyncio.sleep(get_login_backoff_seconds(failure_count))
            return templates.TemplateResponse(
                "login.html",
                {
                    **template_context,
                    "error": t("auth.error.account_disabled"),
                },
            )

        if user_result and user_result.password and user_result.can_use_password():
            if verify_password(user_result.password, password):
                clear_login_failures(request, username)
                user_info = await create_user_info(user_result, False)
                default_redirect = await get_after_login_redirect(user_result.id)
                return create_login_response(
                    user_info,
                    redirect_url=next_url,
                    default_redirect=default_redirect,
                )
        else:
            failure_count = record_login_failure(request, username)
            await asyncio.sleep(get_login_backoff_seconds(failure_count))
            return templates.TemplateResponse(
                "login.html",
                {
                    **template_context,
                    "error": t("auth.error.credentials"),
                },
            )

        failure_count = record_login_failure(request, username)
        await asyncio.sleep(get_login_backoff_seconds(failure_count))
        return templates.TemplateResponse(
            "login.html",
            {
                **template_context,
                "error": t("auth.error.credentials"),
            },
        )

    token = request.query_params.get("token")
    if token:
        nonce = secrets.token_urlsafe(16)
        request.session["ml_confirm_nonce"] = nonce

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                """
                SELECT user_id, expires_at
                FROM magic_links
                WHERE token = ?
                """,
                (token,),
            )
            magic_link = await cursor.fetchone()

            if magic_link:
                try:
                    expires_at = datetime.strptime(
                        magic_link["expires_at"], "%Y-%m-%d %H:%M:%S.%f"
                    )
                except ValueError:
                    expires_at = datetime.strptime(
                        magic_link["expires_at"], "%Y-%m-%d %H:%M:%S"
                    )

                if expires_at < datetime.now():
                    return RedirectResponse(
                        url=template_context["recovery_url"],
                        status_code=status.HTTP_302_FOUND,
                    )

                user_obj = await get_user_by_id(magic_link["user_id"])
                if user_obj and user_obj.is_enabled:
                    return templates.TemplateResponse(
                        "magic_link_confirm.html",
                        {
                            "request": request,
                            "token": token,
                            "username": user_obj.username,
                            "login_url": login_url,
                            "next_url": next_url or "",
                            "confirm_nonce": nonce,
                        },
                    )

        record_failure(request, "magic_link")
        return templates.TemplateResponse(
            "login.html",
            {**template_context, "error": t("auth.error.magic_invalid")},
        )

    return templates.TemplateResponse("login.html", template_context)


def generate_username_from_email(email: str) -> str:
    """Generate a username from email address."""
    base = email.split("@")[0]
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", base)
    if len(safe) < 3:
        safe = safe + secrets.token_hex(2)
    return safe[:20]


async def username_exists(username: str) -> bool:
    """Check if a username already exists in the database."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM USERS WHERE LOWER(username) = LOWER(?)",
            (username,),
        )
        return await cursor.fetchone() is not None


async def generate_unique_username(email: str) -> str:
    """Generate a unique username from email, adding suffix if needed."""
    base = generate_username_from_email(email)
    username = base
    suffix = 1

    while await username_exists(username):
        max_base_len = 20 - len(str(suffix))
        username = f"{base[:max_base_len]}{suffix}"
        suffix += 1
        if suffix > 999:
            username = f"{base[:12]}{secrets.token_hex(4)}"
            break

    return username
