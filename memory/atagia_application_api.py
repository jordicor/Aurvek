"""Atagia app factory adding scoped retrieval without ingesting the query.

Run in the existing Atagia service environment with Aurvek on PYTHONPATH. The
native factory owns its runtime, database, workers, lifecycle and all other API
routes. This module does not start another Atagia engine or service.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from integrations.applications.memory import MemoryNamespace


class NamespaceRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    app_id: str = Field(min_length=1, max_length=160)
    context_id: str = Field(min_length=1, max_length=160)
    subject: str = Field(min_length=1, max_length=512)
    space: Literal["private", "shared"]
    assistant_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_scope(self):
        self.namespace()
        return self

    def namespace(self) -> MemoryNamespace:
        return MemoryNamespace(**self.model_dump())


class ReadContextRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    namespace: NamespaceRequest
    conversation_id: int = Field(gt=0)
    message: str = Field(min_length=1, max_length=131072)
    mode: str = Field(min_length=1, max_length=160)
    platform_id: str = Field(min_length=1, max_length=160)


class _RuntimeFacade:
    """Only the three library operations needed by the shared retrieval helper."""

    def __init__(self, runtime):
        self.runtime = runtime

    async def _require_runtime(self):
        return self.runtime

    async def create_user(self, user_id):
        from atagia.services.sidecar_service import SidecarService

        connection = await self.runtime.open_connection()
        try:
            await SidecarService(self.runtime).ensure_user_exists(connection, user_id)
        finally:
            await connection.close()

    async def create_conversation(self, *, user_id, conversation_id, **namespace):
        from atagia.services.sidecar_service import SidecarService

        connection = await self.runtime.open_connection()
        try:
            sidecar = SidecarService(self.runtime)
            await sidecar.ensure_user_exists(connection, user_id)
            conversation = await sidecar.ensure_conversation(connection, user_id=user_id,
                conversation_id=conversation_id, workspace_id=None, assistant_mode_id=None, **namespace)
            return str(conversation["id"])
        finally:
            await connection.close()


def create_app(settings=None):
    """Uvicorn --factory entrypoint extending Atagia's own application factory."""
    from atagia.app import create_app as create_native_app
    from atagia.api.dependencies import ensure_user_access, get_auth_context, get_runtime

    app = create_native_app(settings)

    @app.post("/v1/context/read-only", tags=["memory"])
    async def read_context(payload: ReadContextRequest, request: Request, response: Response,
                           auth: Any = Depends(get_auth_context)) -> dict:
        # Library-mode HTTP may be deliberately allowed for old routes. This
        # extension always requires the native authenticated service contract.
        if not auth.service_mode or not auth.claimed_user_id:
            raise HTTPException(401, "Authenticated service identity is required")
        namespace = payload.namespace.namespace()
        ensure_user_access(namespace.provider_user_id, auth)
        from atagia_bridge import AtagiaBridgeConfig, read_application_atagia_context

        config = AtagiaBridgeConfig(enabled=True, transport="local", assistant_mode=payload.mode,
                                    platform_id=payload.platform_id)
        try:
            result = await read_application_atagia_context(_RuntimeFacade(get_runtime(request)),
                config=config, namespace=namespace, conversation_id=payload.conversation_id,
                message_text=payload.message)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Scoped memory retrieval is unavailable") from None
        if not isinstance(result, dict):
            raise HTTPException(503, "Scoped memory retrieval is unavailable")
        response.headers["Cache-Control"] = "no-store"
        return result

    return app
