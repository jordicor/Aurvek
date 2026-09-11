"""A host button uses the same configured HTTP tool as the conversation model."""
from fastapi import APIRouter, Depends
from pydantic import Field

from integrations.embed.identity import get_embed_store
from integrations.embed.models import AppConfig, DelegatedRequest
from .api import ApplicationRoute
from .authentication import application_backend_client
from .http_tools import ApplicationHTTPToolService
from .models import CONTRACT_VERSION, ConversationId, ExternalReference, OperationId
from .service import ApplicationService


class InvokeToolRequest(DelegatedRequest):
    conversation_id: ConversationId
    context_ref: ExternalReference | None
    name: str = Field(pattern=r"^app_http_[a-z][a-z0-9_]{0,47}$")
    arguments: dict
    operation_id: OperationId


router = APIRouter(prefix="/api/applications/v1", route_class=ApplicationRoute)


@router.post("/invoke-tool")
async def invoke_tool(body: InvokeToolRequest, app: AppConfig = Depends(application_backend_client)):
    store = get_embed_store()
    identity = await store.inspect_identity(app.app_id, body.delegated_credential)
    service = ApplicationService(store)
    async with store.connection(readonly=True) as connection:
        context = await service.authorize_backend_conversation(
            connection, identity, body.conversation_id, body.context_ref)
    result = await ApplicationHTTPToolService(service).run_http_tool(
        context, body.name, body.arguments, body.operation_id)
    return {"contract_version": CONTRACT_VERSION, "result": result}
