import hashlib

from fastapi import APIRouter, Request, Response

from integrations.elevenlabs.sdk_proxy import ElevenLabsSDKProxy


router = APIRouter()
SDK_CACHE_CONTROL = "public, max-age=3600, must-revalidate"


def _sdk_response(request: Request, content: bytes, media_type: str) -> Response:
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    headers = {"Cache-Control": SDK_CACHE_CONTROL, "ETag": etag}
    validators = request.headers.get("if-none-match", "")
    if any(
        candidate.strip() in {"*", etag, f"W/{etag}"}
        for candidate in validators.split(",")
    ):
        return Response(status_code=304, headers=headers)
    return Response(content, media_type=media_type, headers=headers)


@router.get("/sdk/elevenlabs-client.js")
async def serve_elevenlabs_sdk(request: Request):
    content = await ElevenLabsSDKProxy.get_sdk()
    return _sdk_response(request, content, "application/javascript")


@router.get("/sdk/elevenlabs-client.js.map")
async def serve_elevenlabs_sourcemap(request: Request):
    content = await ElevenLabsSDKProxy.get_sourcemap()
    return _sdk_response(request, content, "application/json")


@router.get("/sdk/lib.umd.js")
async def serve_elevenlabs_alias(request: Request):
    content = await ElevenLabsSDKProxy.get_sdk()
    return _sdk_response(request, content, "application/javascript")


@router.get("/sdk/lib.umd.js.map")
async def serve_elevenlabs_alias_map(request: Request):
    content = await ElevenLabsSDKProxy.get_sourcemap()
    return _sdk_response(request, content, "application/json")
