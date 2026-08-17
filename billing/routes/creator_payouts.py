from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from auth import get_current_user, unauthenticated_response
from billing.connect import (
    create_connect_onboarding_response,
    get_connect_status_response,
    handle_connect_return_response,
)
from billing.creator_payouts import request_creator_payout_response
from marketplace.config import require_creator_tools_enabled
from models import User


router = APIRouter()


async def _can_use_creator_billing(current_user: User) -> bool:
    return await current_user.is_user or await current_user.is_admin


@router.post("/api/creator/request-payout")
async def request_creator_payout(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await _can_use_creator_billing(current_user):
        return JSONResponse(content={"success": False, "message": "Access denied"}, status_code=403)

    return await request_creator_payout_response(current_user)


@router.post("/api/connect/onboard")
async def stripe_connect_onboard(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await _can_use_creator_billing(current_user):
        return JSONResponse(content={"success": False, "message": "Access denied"}, status_code=403)

    return await create_connect_onboarding_response(request, current_user)


@router.get("/api/connect/return")
async def stripe_connect_return(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return RedirectResponse(url="/login?next=/my-earnings", status_code=302)

    return await handle_connect_return_response(current_user)


@router.get("/api/connect/refresh")
async def stripe_connect_refresh(request: Request):
    require_creator_tools_enabled()
    return RedirectResponse(url="/my-earnings?warning=link_expired", status_code=302)


@router.get("/api/connect/status")
async def stripe_connect_status(request: Request, current_user: User = Depends(get_current_user)):
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    if not await _can_use_creator_billing(current_user):
        return JSONResponse(content={"success": False, "message": "Access denied"}, status_code=403)

    return await get_connect_status_response(current_user)
