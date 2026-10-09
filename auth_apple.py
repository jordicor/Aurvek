"""Availability contract for the unimplemented Sign in with Apple backend.

This module intentionally does not authenticate users yet. It exposes status and
placeholder endpoints so native iOS work can integrate against stable URLs while
the Apple Developer identifiers and server-side token verification are prepared.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


router = APIRouter()

APPLE_SIGN_IN_ENV_VARS = (
    "APPLE_TEAM_ID",
    "APPLE_CLIENT_ID",
    "APPLE_KEY_ID",
)


def get_apple_sign_in_status() -> dict[str, Any]:
    missing = [name for name in APPLE_SIGN_IN_ENV_VARS if not os.getenv(name, "").strip()]
    has_private_key = bool(
        os.getenv("APPLE_PRIVATE_KEY", "").strip()
        or os.getenv("APPLE_PRIVATE_KEY_PATH", "").strip()
    )
    if not has_private_key:
        missing.append("APPLE_PRIVATE_KEY or APPLE_PRIVATE_KEY_PATH")

    return {
        "enabled": False,
        "configured": not missing,
        "missing_env": missing,
        "native_callback_url": "/api/auth/apple/native-callback",
        "web_start_url": "/auth/apple",
        "web_callback_url": "/auth/apple/callback",
        "implementation_status": "pending_token_verification",
    }


def _unavailable_response() -> JSONResponse:
    status = get_apple_sign_in_status()
    return JSONResponse(
        {
            "error": "apple_sign_in_unavailable",
            "message": "Sign in with Apple is not implemented on this server yet.",
            "apple_sign_in": status,
        },
        status_code=503,
    )


@router.get("/api/auth/apple/status")
async def apple_sign_in_status():
    return JSONResponse({"apple_sign_in": get_apple_sign_in_status()})


@router.post("/api/auth/apple/native-callback")
async def apple_native_callback(request: Request):
    return _unavailable_response()


@router.get("/auth/apple")
async def apple_web_start():
    return _unavailable_response()


@router.get("/auth/apple/callback")
async def apple_web_callback():
    return _unavailable_response()
