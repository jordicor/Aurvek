"""A small account surface. It cannot authorize chat or native settings."""

import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import Field

from i18n import LANGUAGES, Translator
from integrations.embed.api import PrivateRoute, read_bounded_body
from integrations.embed.context import get_embed_principal
from integrations.embed.identity import get_embed_store
from integrations.embed.models import EmbedError, StrictModel
from .profile import ApplicationProfileService, ConversationalProfileUpdate
from .profile_frames import ProfileFrames
from .accounts import PhoneChallengeRequest, PhoneCodeRequest, PhoneUpdateRequest
from typing import Literal


class ProfileRoute(PrivateRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            from phone_verification import PhoneVerificationError

            try:
                return await handler(request)
            except PhoneVerificationError as error:
                return JSONResponse(
                    {"error": error.code}, status_code=error.status_code
                )

        return wrapped


router = APIRouter(route_class=ProfileRoute)


def principal(request, app_id, frame_instance_id):
    actor = get_embed_principal(request)
    if (
        not actor
        or (actor.app_id, actor.frame_instance_id) != (app_id, frame_instance_id)
        or actor.conversation_id is not None
    ):
        raise EmbedError("not_found", 404)
    return actor


@router.post("/embed/profile/bootstrap")
async def bootstrap(request: Request):
    if (
        request.url.query
        or request.headers.get("content-type", "").split(";", 1)[0]
        != "application/x-www-form-urlencoded"
    ):
        raise EmbedError("invalid_request", 400)
    try:
        values = parse_qs(
            (await read_bounded_body(request, 4096)).decode("ascii"),
            strict_parsing=True,
            max_num_fields=1,
        )
        (ticket,) = values["ticket"]
        if set(values) != {"ticket"} or not 1 <= len(ticket) <= 512:
            raise ValueError()
    except (ValueError, KeyError, UnicodeDecodeError):
        raise EmbedError("invalid_request", 400) from None
    cookie, actor = await ProfileFrames(get_embed_store()).consume(
        ticket, request.headers.get("host", ""), request.headers.get("origin", "")
    )
    response = RedirectResponse(
        f"/embed/{actor.app_id}/{actor.frame_instance_id}/profile", status_code=303
    )
    response.set_cookie(
        "__Host-aurvek_embed",
        cookie,
        max_age=max(0, int(actor.expires_at - time.time())),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/embed/{app_id}/{frame_instance_id}/profile")
async def page(request: Request, app_id: str, frame_instance_id: str):
    from common import templates, get_asset_version
    from hashlib import sha256
    from pathlib import Path
    from user_languages import selectable_user_languages
    from user_timezone import selectable_user_timezones

    actor = principal(request, app_id, frame_instance_id)
    tr = Translator(actor.ui_language)
    app = await get_embed_store().get_app(app_id)
    # Reuse startup hashes, retaining same-origin URLs for the private frame CSP.
    assets = {}
    for suffix in ("css/application-profile.css", "js/application-profile.js"):
        path = "/static/" + suffix
        version = get_asset_version(path) or sha256(
            (Path(__file__).resolve().parents[2] / "data" / path.lstrip("/")).read_bytes()
        ).hexdigest()[:8]
        assets[suffix.split("/", 1)[0]] = path + "?v=" + version
    return templates.TemplateResponse(
        "applications/profile.html",
        {
            "request": request,
            "t": tr.t,
            "language": actor.ui_language,
            "theme": app.theme,
            "languages": selectable_user_languages(actor.ui_language),
            "timezones": selectable_user_timezones(),
            "assets": assets,
            "metadata": {
                "app_id": app_id,
                "frame_instance_id": frame_instance_id,
                "parent_origin": actor.parent_origin,
                "csrf_token": actor.csrf_token,
                "ui_language": actor.ui_language,
                "messages": tr.browser_payload(("profile", "application")),
            },
        },
    )


@router.get("/embed/{app_id}/{frame_instance_id}/profile/state")
async def state(request: Request, app_id: str, frame_instance_id: str):
    return await ApplicationProfileService(get_embed_store()).state(
        principal(request, app_id, frame_instance_id)
    )


@router.post("/embed/{app_id}/{frame_instance_id}/profile/save")
async def save(
    request: Request,
    app_id: str,
    frame_instance_id: str,
    body: ConversationalProfileUpdate,
):
    return await ApplicationProfileService(get_embed_store()).update(
        principal(request, app_id, frame_instance_id), body
    )


class ProfilePhoneBody(StrictModel):
    action: Literal["request", "verify", "update"]
    phone_number: str = Field(min_length=8, max_length=40)
    challenge_id: str | None = Field(default=None, min_length=32, max_length=128)
    code: str | None = Field(default=None, pattern=r"^[0-9]{4,10}$")
    operation_id: str | None = Field(default=None, min_length=8, max_length=128)
    expected_version: int | None = Field(default=None, ge=1, strict=True)
    expected_contact_version: str | None = Field(
        default=None, min_length=64, max_length=64
    )


class ProfileChannelBody(StrictModel):
    receiver_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    action: Literal["connect", "disconnect"]
    expected_version: int | None = Field(default=None, strict=True, ge=1)


@router.post("/embed/{app_id}/{frame_instance_id}/profile/channel")
async def channel(
    request: Request, app_id: str, frame_instance_id: str, body: ProfileChannelBody
):
    from .channels import ApplicationChannelService
    from .contacts import contact_state, ensure_phone_adapters
    from integrations.embed.store import _one

    actor = principal(request, app_id, frame_instance_id)
    store = get_embed_store()
    async with store.transaction() as connection:
        account = await ApplicationProfileService(store)._account(connection, actor)
        receiver = await _one(
            connection,
            "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE app_id=? AND receiver_id=? AND enabled=1",
            (app_id, body.receiver_id),
        )
        if not receiver:
            raise EmbedError("channel_receiver_unavailable", 403)
        contact = await contact_state(connection, app_id, actor.subject)
        if body.action == "connect":
            if not contact["verified"] or receiver["channel"] == "telegram":
                raise EmbedError("phone_proof_required", 403)
            await ensure_phone_adapters(
                connection, app_id, actor.subject, contact["phone_number"], store.now()
            )
            link = await _one(
                connection,
                "SELECT * FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND subject=? AND provider_identity=?",
                (body.receiver_id, actor.subject, contact["phone_number"]),
            )
            if (
                body.expected_version is not None
                and link["version"] != body.expected_version
            ):
                raise EmbedError("version_conflict", 409)
            if not link["active"]:
                await connection.execute(
                    "UPDATE APPLICATION_CHANNEL_LINKS SET active=1,version=version+1 WHERE link_id=?",
                    (link["link_id"],),
                )
                # A disconnected durable route remains fenced; next user selection can restore it.
            return await ApplicationProfileService(store)._state(connection, account)
        link = await _one(
            connection,
            "SELECT * FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND subject=? AND active=1",
            (body.receiver_id, actor.subject),
        )
        if not link or body.expected_version != link["version"]:
            raise EmbedError("version_conflict", 409)
    await ApplicationChannelService(store).unlink(
        app_id,
        account["external_user_id"],
        link["link_id"],
        expected_version=body.expected_version,
    )
    return await ApplicationProfileService(store).state(actor)


@router.post("/embed/{app_id}/{frame_instance_id}/profile/phone")
async def phone(
    request: Request, app_id: str, frame_instance_id: str, body: ProfilePhoneBody
):
    from .api import account_service
    from .account_cleanup import cleanup_membership

    actor = principal(request, app_id, frame_instance_id)
    service = account_service()
    async with service.store.connection(readonly=True) as connection:
        account = await ApplicationProfileService(service.store)._account(
            connection, actor
        )
    values = {
        "external_user_id": account["external_user_id"],
        "phone_number": body.phone_number,
    }
    if body.action == "request":
        return await service.request_phone(
            actor,
            PhoneChallengeRequest(**values),
            request_ip=request.client.host,
            recent_auth_time=actor.source_auth_time,
        )
    if body.action == "verify":
        if not body.challenge_id or not body.code:
            raise EmbedError("invalid_request", 400)
        return await service.verify_phone(
            actor,
            PhoneCodeRequest(**values, challenge_id=body.challenge_id, code=body.code),
            recent_auth_time=actor.source_auth_time,
        )
    if not all(
        (
            body.challenge_id,
            body.operation_id,
            body.expected_version,
            body.expected_contact_version,
        )
    ):
        raise EmbedError("invalid_request", 400)
    await service.update_phone(
        actor,
        PhoneUpdateRequest(
            **values,
            challenge_id=body.challenge_id,
            operation_id=body.operation_id,
            expected_version=body.expected_version,
            expected_contact_version=body.expected_contact_version,
        ),
        recent_auth_time=actor.source_auth_time,
    )
    await cleanup_membership(app_id, actor.subject)
    return await ApplicationProfileService(service.store).state(actor)


class LanguageBody(StrictModel):
    ui_language: str = Field(max_length=16)


@router.post("/embed/{app_id}/{frame_instance_id}/profile/language")
async def language(
    request: Request, app_id: str, frame_instance_id: str, body: LanguageBody
):
    from user_languages import selectable_user_languages

    actor = principal(request, app_id, frame_instance_id)
    if body.ui_language not in LANGUAGES:
        raise EmbedError("invalid_request", 400)
    async with get_embed_store().transaction() as connection:
        await connection.execute(
            "UPDATE APPLICATION_PROFILE_FRAMES SET ui_language=? WHERE app_id=? AND frame_instance_id=? AND subject=?",
            (body.ui_language, app_id, frame_instance_id, actor.subject),
        )
    return {
        "ui_language": body.ui_language,
        "languages": selectable_user_languages(body.ui_language),
        "messages": Translator(body.ui_language).browser_payload(
            ("profile", "application")
        ),
    }
