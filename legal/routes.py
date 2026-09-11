"""Public legal/support pages used by web, mobile config, and App Store metadata."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse


from auth import get_current_user
from public_pages import public_page


router = APIRouter()

@router.get("/privacy", response_class=HTMLResponse)
@router.get("/privacy.html", response_class=HTMLResponse)
async def privacy_page(request: Request, current_user=Depends(get_current_user)):
    return public_page(request, "privacy.html", current_user)


@router.get("/terms", response_class=HTMLResponse)
@router.get("/terms.html", response_class=HTMLResponse)
async def terms_page(request: Request, current_user=Depends(get_current_user)):
    return public_page(request, "terms.html", current_user)


@router.get("/support", response_class=HTMLResponse)
async def support_page(request: Request, current_user=Depends(get_current_user)):
    external_url = os.getenv("SUPPORT_URL", "").strip()
    if external_url and not external_url.rstrip("/").endswith("/support"):
        return RedirectResponse(external_url, status_code=302)

    support_email = (
        os.getenv("MOBILE_SUPPORT_EMAIL", "").strip()
        or os.getenv("SUPPORT_EMAIL", "").strip()
        or "support@example.com"
    )
    return public_page(request, "support.html", current_user, support_email=support_email)
