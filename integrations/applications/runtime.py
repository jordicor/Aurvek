"""Application admission shared by native web and non-browser channels.

Channel attribution is a snapshot, never a credential. Re-resolve live scope
before starting each turn; persistence/settlement can retain that admitted scope.
"""

from __future__ import annotations

from dataclasses import replace
from functools import wraps
from inspect import signature
from typing import TYPE_CHECKING, Any

from integrations.applications.models import ApplicationContext
from integrations.embed.models import EmbedError

if TYPE_CHECKING:
    from ai_runtime.channel_turns import ChannelContext
    from integrations.applications.service import ApplicationService


async def authorize_application_read(
    connection: Any,
    conversation_id: int,
    user_id: int,
    *,
    service: ApplicationService | None = None,
) -> ApplicationContext | None:
    """Revalidate a durable application binding before exposing native resources."""
    if service is None:
        from integrations.applications.service import ApplicationService
        from integrations.embed.identity import get_embed_store

        service = ApplicationService(get_embed_store())
    return await service.authorize_runtime_conversation(
        connection, user_id, conversation_id,
    )


async def admit_application_turn(
    connection: Any,
    *,
    conversation_id: int,
    user_id: int,
    channel_context: ChannelContext,
    has_attachments: bool = False,
    required_capability: str = "text",
    service: ApplicationService | None = None,
) -> ChannelContext:
    """Validate durable ownership and return immutable, server-resolved scope."""
    resolved = await authorize_application_read(
        connection, conversation_id, user_id, service=service,
    )
    supplied = channel_context.application
    if supplied is not None:
        # A channel may retain attribution across turns, but cannot move it to
        # another app/context/assistant or attach it to an ordinary native chat.
        scope = (
            "app_id", "subject", "user_id", "context_id", "assistant_id",
            "prompt_id", "conversation_id",
        )
        if resolved is None or any(
            getattr(supplied, key) != getattr(resolved, key) for key in scope
        ):
            raise EmbedError("application_scope_mismatch", 403)
    if resolved is None:
        return channel_context
    if (not resolved.capabilities.get("text", False)
            or not resolved.capabilities.get(required_capability, False)):
        raise EmbedError("application_capability_denied", 403)
    if channel_context.channel == 'web':
        live_voice = (channel_context.input_origin == 'web.live_voice'
                      and channel_context.persistence == 'deferred'
                      and channel_context.input_perception == 'transcript_only')
        if ((has_attachments and not resolved.capabilities.get('attachments')) or channel_context.ingest_only
                or (channel_context.input_perception != 'text' and not live_voice)
                or (live_voice and not all(resolved.capabilities.get(key)
                                          for key in ('voice', 'stt', 'tts')))):
            raise EmbedError('application_capability_denied', 403)
    elif channel_context.channel in {'phone', 'whatsapp', 'telegram'}:
        from .channels import get_application_channel_service
        admission = channel_context.application_channel
        if admission is None:
            raise EmbedError('application_channel_required', 403)
        await get_application_channel_service().revalidate(
            connection, admission, require_current_route=channel_context.channel != 'phone',
            require_current_identity=True,
            capability=channel_context.channel)
        if (has_attachments and not resolved.capabilities.get('attachments')
                or channel_context.input_perception != 'text' and not resolved.capabilities.get('stt')):
            raise EmbedError('application_capability_denied', 403)
    else:
        raise EmbedError('application_capability_denied', 403)
    from .billing import current_application_operation
    operation = current_application_operation(user_id)
    from .activity import check_application_admission
    check_application_admission(conversation_id, operation)
    if operation is not None:
        if operation.scope.conversation_id != conversation_id:
            raise EmbedError("application_scope_mismatch", 403)
        resolved = replace(resolved, payer_user_id=operation.payer_user_id)
    return replace(channel_context, application=resolved)


def application_billing_turn(function):
    """Admit app funding before canonical preflight, including direct callers.

    Native requests keep their existing route-level guard. An app request owns
    one frozen operation through both preflight and the deferred response body.
    """
    parameters = signature(function)

    @wraps(function)
    async def guarded(*args, **kwargs):
        from ai_runtime.channel_turns import ChannelContext
        from billing.usage_reservations import serialize_user_billing_response
        from fastapi.responses import JSONResponse
        from .billing import admit_application_billing, current_application_operation
        from .activity import (
            begin_application_admission, bind_application_admission,
            end_application_operation,
        )

        values = parameters.bind(*args, **kwargs)
        user = values.arguments["current_user"]
        conversation_id = values.arguments["conversation_id"]
        context = values.arguments.get("channel_context")
        if (values.arguments.get("runtime_llm_id") is not None
                and (context is None or context.channel != "phone")):
            # Preserve the canonical syntactic rejection before database access.
            return await function(*args, **kwargs)
        operation = current_application_operation(user.id)
        admission = None
        try:
            if operation is not None:
                if operation.scope.conversation_id != conversation_id:
                    raise EmbedError("application_scope_mismatch", 403)
                return await function(*args, **kwargs)
            # Use the canonical runtime's connection factory; no independent
            # connection to another database during isolated runtime tests.
            connect = function.__globals__["get_db_connection"]
            async with connect(readonly=True) as connection:
                context = values.arguments.get("channel_context") or ChannelContext(
                    channel="whatsapp" if values.arguments.get("is_whatsapp") else "web")
                context = await admit_application_turn(
                    connection, conversation_id=conversation_id, user_id=user.id,
                    channel_context=context,
                    has_attachments=bool(values.arguments.get("files") or values.arguments.get("attachment_refs")),
                )
                if context.application is not None:
                    admission = begin_application_admission(context.application)
            if context.application is None:
                return await function(*args, **kwargs)
            operation = await admit_application_billing(context.application)
            bind_application_admission(admission, operation)
            values.arguments["channel_context"] = replace(context, application=operation.scope)
            return await serialize_user_billing_response(
                user.id, function(*values.args, **values.kwargs), application_operation=operation)
        except EmbedError as exc:
            return JSONResponse({"success": False, "error_code": exc.code, "message": exc.code},
                                status_code=exc.status_code)
        finally:
            end_application_operation(admission)

    return guarded
