"""HTTP adaptation of durable application authorization for native chat routes."""
from fastapi import HTTPException

from integrations.embed.models import EmbedError
from .runtime import authorize_application_read


async def require_application_access(connection, conversation_id, user_id, *, capability=None):
    try:
        context = await authorize_application_read(connection, conversation_id, user_id)
    except EmbedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    if context is not None and capability and not context.capabilities.get(capability, False):
        raise HTTPException(status_code=403, detail="application_capability_denied")
    return context
