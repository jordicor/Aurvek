"""Localized Aurvek marketing pages; creator-authored HTML has a separate renderer."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from auth import get_current_user
from i18n import get_translator
from public_pages import public_page
from marketplace.config import require_creator_tools_enabled, require_discovery_enabled


router = APIRouter()


@router.get("/for-creators", response_class=HTMLResponse)
@router.get("/for-creators-landing.html", response_class=HTMLResponse)
async def for_creators_landing(request: Request, current_user=Depends(get_current_user)):
    require_creator_tools_enabled(get_translator(request, current_user))
    return public_page(request, "for-creators-landing.html", current_user)


@router.get("/for-agencies", response_class=HTMLResponse)
@router.get("/for-agencies-landing.html", response_class=HTMLResponse)
async def for_agencies_landing(request: Request, current_user=Depends(get_current_user)):
    require_creator_tools_enabled(get_translator(request, current_user))
    return public_page(request, "for-agencies-landing.html", current_user)


@router.get("/explore-landing", response_class=HTMLResponse)
@router.get("/explore-landing.html", response_class=HTMLResponse)
async def explore_marketing_landing(request: Request, current_user=Depends(get_current_user)):
    require_discovery_enabled(get_translator(request, current_user))
    return public_page(request, "explore-landing.html", current_user)


@router.get("/for-teams", response_class=HTMLResponse)
@router.get("/for-teams-landing.html", response_class=HTMLResponse)
async def for_teams_landing(request: Request, current_user=Depends(get_current_user)):
    return public_page(request, "for-teams-landing.html", current_user)


@router.get("/infrastructure-landing", response_class=HTMLResponse)
@router.get("/infrastructure-landing.html", response_class=HTMLResponse)
async def infrastructure_landing(request: Request, current_user=Depends(get_current_user)):
    return public_page(request, "infrastructure-landing.html", current_user)
