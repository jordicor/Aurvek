"""Native owner controls for funding a specific application account."""
from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import Field

from auth import get_current_user
from common import get_template_context, templates
from i18n import get_translator
from integrations.embed.context import is_embed_request
from integrations.embed.models import EmbedError, StrictModel
from request_security import ensure_csrf_token, validate_mutation_request


_ERRORS = {"application_owner_required", "app_unavailable", "account_unavailable",
           "application_owner_unavailable", "application_payer_unconfigured", "account_funding_changed",
           "account_funding_override_conflict", "invalid_funding"}


class FundingRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def secured(request):
            try:
                response = await handler(request)
            except EmbedError as exc:
                key = exc.code if exc.code in _ERRORS else "generic"
                response = JSONResponse({"detail": get_translator(request).t("application_funding.error." + key),
                                         "error_code": exc.code}, status_code=exc.status_code)
            except RequestValidationError:
                response = JSONResponse({"detail": get_translator(request).t("application_funding.error.invalid_funding"),
                                         "error_code": "invalid_funding"}, status_code=400)
            response.headers["Cache-Control"] = "private, no-store, max-age=0"
            return response
        return secured


class FundingChange(StrictModel):
    monthly_limit: float | None = Field(strict=True, ge=0, allow_inf_nan=False)
    active: bool = Field(strict=True)
    expected_version: int = Field(strict=True, ge=0)


router = APIRouter(prefix="/applications/manage/{app_id}/funding", route_class=FundingRoute)


def _service():
    from .account_funding import get_account_funding_service
    return get_account_funding_service()


async def _actor(request, user):
    if user is None or is_embed_request(request) or getattr(user, "embed_principal", None) is not None:
        raise EmbedError("application_owner_required", 403)
    return {"user_id": user.id, "is_admin": bool(await user.is_admin)}


@router.get("")
async def funding_page(app_id: str, request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user)
    state = await _service().state(app_id, **actor)
    context = await get_template_context(request, user)
    context.update(funding_state=state, funding_csrf=ensure_csrf_token(request))
    return templates.TemplateResponse("applications/funding.html", context)


@router.post("/accounts/{external_user_id}")
async def update_account(app_id: str, external_user_id: str, body: FundingChange,
                         request: Request, user=Depends(get_current_user)):
    actor = await _actor(request, user)
    rejected = validate_mutation_request(request)
    if rejected is not None:
        return rejected
    # The service resolves the configured application wallet and audits atomically.
    return await _service().update(app_id, external_user_id, **body.model_dump(), **actor, request=request)
