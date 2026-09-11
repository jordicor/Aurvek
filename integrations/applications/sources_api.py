"""Server source access; a reference never grants permission by itself."""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from integrations.embed.identity import get_embed_store
from integrations.embed.models import AppConfig, DelegatedRequest
from .api import ApplicationRoute
from .authentication import application_backend_client
from .models import ConversationId, ExternalReference
from .sources import ApplicationSourceService


class ConversationSourceBody(DelegatedRequest):
    conversation_id: ConversationId
    context_ref: ExternalReference | None


class SourceListBody(ConversationSourceBody):
    kind: Literal["messages", "attachments", "generated_media", "handoffs"] = "messages"
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    cursor: str | None = Field(default=None, max_length=1024)


class SourceContentBody(ConversationSourceBody):
    source_ref: str = Field(min_length=3, max_length=192, pattern=r"^(attachment|generated|phone_audio):[A-Za-z0-9_-]+$")


class ConversationSnapshotBody(ConversationSourceBody):
    mode: Literal["snapshot", "revision"] = "snapshot"


router = APIRouter(prefix="/api/applications/v1", route_class=ApplicationRoute)


@router.post("/conversation-sources")
async def conversation_sources(body: SourceListBody, app: AppConfig = Depends(application_backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    return await ApplicationSourceService(store).list_sources(identity, body.conversation_id, body.context_ref,
        kind=body.kind, limit=body.limit, cursor=body.cursor)


@router.post("/conversation-snapshot")
async def conversation_snapshot(body: ConversationSnapshotBody, app: AppConfig = Depends(application_backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    return await ApplicationSourceService(store).conversation_snapshot(
        identity, body.conversation_id, body.context_ref, mode=body.mode)


@router.post("/source-content")
async def source_content(body: SourceContentBody, app: AppConfig = Depends(application_backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    return await ApplicationSourceService(store).source_content(identity, body.conversation_id, body.context_ref, body.source_ref)
