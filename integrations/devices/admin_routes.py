from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from i18n import get_translator
from auth import get_current_user
from captcha_service import get_captcha_config
from common import GOOGLE_CLIENT_ID, PRIMARY_APP_DOMAIN, get_template_context, templates
from integrations.devices.service import (
    DeviceValidationError,
    add_device_to_group,
    clear_binding,
    create_device,
    create_group,
    get_admin_page_data,
    remove_device_from_group,
    rotate_device_token,
    set_binding,
    set_device_enabled,
    soft_delete_device,
    soft_delete_group,
    update_device,
    update_group,
)
from models import User
from log_config import logger


router = APIRouter()


# The service also serves device clients. Localize only this admin boundary;
# never expose an unrecognized service exception or operator value as UI copy.
_VALIDATION_KEYS = {
    'Icon class must be a Font Awesome class.': 'invalid_icon',
    'Owner user not found': 'owner_not_found',
    'Owner user is disabled': 'owner_disabled',
    'Conversation not found for selected owner': 'conversation_not_found',
    'External devices cannot be attached to WhatsApp or Telegram conversations': 'classic_conflict',
    'One or more groups do not belong to the owner': 'groups_owner',
    'Device and group must belong to the same user': 'same_owner',
    'Invalid binding target': 'invalid_target',
    'Binding target not found': 'target_not_found',
    'Display name is required': 'display_name_required',
    'Device slug already exists for this user': 'device_slug_exists',
    'Device not found': 'device_not_found',
    'Group name is required': 'group_name_required',
    'Group slug already exists for this user': 'group_slug_exists',
    'Group not found': 'group_not_found',
    'Membership not found': 'membership_not_found',
    'Invalid response mode': 'invalid_response_mode',
    'Binding not found': 'binding_not_found',
}
_FIELD_KEYS = {
    "Owner": "owner", "Group": "group", "Conversation": "conversation",
    "Device": "device", "Priority": "priority", "Target": "target",
    "Display name": "display_name", "Group name": "group_name",
    "Device type": "device_type", "Icon class": "icon_class",
    "Notes": "notes", "Slug": "slug",
}
_FIELD_ERROR_KEYS = {
    "must be a number": "field_number", "must be positive": "field_positive",
    "is too long": "field_long",
    "must use lowercase letters, numbers, and hyphens.": "field_slug",
}


def _validation_message(request: Request, exc: DeviceValidationError) -> str:
    t = get_translator(request).t
    message = str(exc)
    key = _VALIDATION_KEYS.get(message)
    if key:
        return t("admin_channels." + key)
    for field, field_key in _FIELD_KEYS.items():
        for suffix, error_key in _FIELD_ERROR_KEYS.items():
            if message == f"{field} {suffix}":
                return t("admin_channels." + error_key, field=t("admin_channels." + field_key))
    logger.warning("Device admin validation failed: %s", exc)
    return t("admin_channels.validation_failed")


def _login_response(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID),
        },
    )


def _redirect(message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {}
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    query = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(url=f"/admin/devices{query}", status_code=303)


def _form_int(value, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise DeviceValidationError(f"{field} must be a number")
    if parsed <= 0:
        raise DeviceValidationError(f"{field} must be positive")
    return parsed


def _optional_form_int(value, field: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _form_int(value, field)


def _form_int_list(values, field: str) -> list[int]:
    parsed = []
    for value in values:
        if value is None or str(value).strip() == "":
            continue
        parsed.append(_form_int(value, field))
    return parsed


async def _render_admin_devices(
    request: Request,
    current_user: User,
    *,
    message: str | None = None,
    error: str | None = None,
    token_value: str | None = None,
    token_slug: str | None = None,
) -> HTMLResponse:
    page_data = await get_admin_page_data()
    context = await get_template_context(request, current_user)
    token_block = None
    if token_value and token_slug:
        token_block = "\n".join(
            [
                f"AURVEK_BASE_URL=https://{PRIMARY_APP_DOMAIN}",
                f"AURVEK_DEVICE_SLUG={token_slug}",
                f"AURVEK_DEVICE_TOKEN={token_value}",
            ]
        )
    context.update(
        {
            **page_data,
            "current_user_id": current_user.id,
            "message": message or request.query_params.get("message"),
            "error": error or request.query_params.get("error"),
            "token_block": token_block,
        }
    )
    return templates.TemplateResponse("admin_devices.html", context)


@router.get("/admin/devices", response_class=HTMLResponse)
async def admin_devices(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("admin_channels.access_denied"))

    return await _render_admin_devices(request, current_user)


@router.post("/admin/devices", response_class=HTMLResponse)
async def admin_devices_action(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    t = get_translator(request, current_user).t
    if current_user is None:
        return _login_response(request)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail=t("admin_channels.access_denied"))

    form = await request.form()
    action = (form.get("action") or "").strip()

    try:
        if action == "create_device":
            owner_user_id = _optional_form_int(form.get("owner_user_id"), "Owner")
            if owner_user_id is None:
                owner_user_id = current_user.id
            slug = (form.get("slug") or "").strip()
            result = await create_device(
                owner_user_id=owner_user_id,
                display_name=form.get("display_name"),
                slug=slug,
                device_type=form.get("device_type") or "custom",
                notes=form.get("notes") or "",
                capability_names=form.getlist("capabilities"),
                group_ids=_form_int_list(form.getlist("group_ids"), "Group"),
                conversation_id=_optional_form_int(form.get("conversation_id"), "Conversation"),
            )
            return await _render_admin_devices(
                request,
                current_user,
                message=t("admin_channels.device_created"),
                token_value=result.token,
                token_slug=slug,
            )

        if action == "update_device":
            await update_device(
                device_id=_form_int(form.get("device_id"), "Device"),
                display_name=form.get("display_name"),
                slug=form.get("slug"),
                device_type=form.get("device_type") or "custom",
                notes=form.get("notes") or "",
                capability_names=form.getlist("capabilities"),
                icon_class=form.get("icon_class"),
            )
            return _redirect(message=t("admin_channels.device_updated"))

        if action == "set_device_enabled":
            await set_device_enabled(
                _form_int(form.get("device_id"), "Device"),
                (form.get("enabled") or "0") == "1",
            )
            return _redirect(message=t("admin_channels.device_status_updated"))

        if action == "rotate_device_token":
            device_id = _form_int(form.get("device_id"), "Device")
            result = await rotate_device_token(device_id)
            token_slug = (form.get("device_slug") or f"device-{device_id}").strip()
            return await _render_admin_devices(
                request,
                current_user,
                message=t("admin_channels.token_rotated"),
                token_value=result.token,
                token_slug=token_slug,
            )

        if action == "soft_delete_device":
            await soft_delete_device(_form_int(form.get("device_id"), "Device"))
            return _redirect(message=t("admin_channels.device_removed"))

        if action == "create_group":
            await create_group(
                owner_user_id=_form_int(form.get("owner_user_id"), "Owner"),
                name=form.get("name"),
                slug=form.get("slug"),
                notes=form.get("notes") or "",
                icon_class=form.get("icon_class"),
            )
            return _redirect(message=t("admin_channels.group_created"))

        if action == "update_group":
            await update_group(
                group_id=_form_int(form.get("group_id"), "Group"),
                name=form.get("name"),
                slug=form.get("slug"),
                notes=form.get("notes") or "",
                icon_class=form.get("icon_class"),
            )
            return _redirect(message=t("admin_channels.group_updated"))

        if action == "soft_delete_group":
            await soft_delete_group(_form_int(form.get("group_id"), "Group"))
            return _redirect(message=t("admin_channels.group_removed"))

        if action == "add_membership":
            await add_device_to_group(
                device_id=_form_int(form.get("device_id"), "Device"),
                group_id=_form_int(form.get("group_id"), "Group"),
                is_primary_route_group=bool(form.get("is_primary_route_group")),
                routing_priority=_form_int(form.get("routing_priority") or 100, "Priority"),
            )
            return _redirect(message=t("admin_channels.device_added_group"))

        if action == "remove_membership":
            await remove_device_from_group(
                device_id=_form_int(form.get("device_id"), "Device"),
                group_id=_form_int(form.get("group_id"), "Group"),
            )
            return _redirect(message=t("admin_channels.device_removed_group"))

        if action == "set_binding":
            await set_binding(
                target_type=(form.get("target_type") or "").strip(),
                target_id=_form_int(form.get("target_id"), "Target"),
                conversation_id=_form_int(form.get("conversation_id"), "Conversation"),
                response_mode=form.get("response_mode") or "text",
            )
            return _redirect(message=t("admin_channels.binding_updated"))

        if action == "clear_binding":
            await clear_binding(
                target_type=(form.get("target_type") or "").strip(),
                target_id=_form_int(form.get("target_id"), "Target"),
            )
            return _redirect(message=t("admin_channels.binding_cleared"))

        return _redirect(error=t("admin_channels.unknown_action"))
    except DeviceValidationError as exc:
        return await _render_admin_devices(request, current_user, error=_validation_message(request, exc))
