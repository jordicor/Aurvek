"""Application selection over Aurvek's existing tool catalog and executor."""
from __future__ import annotations

from jsonschema import Draft202012Validator

from integrations.embed.models import EmbedError
from .models import AssistantConfig


# A capability opens only the native operations whose resource and billing
# paths support it. Unknown Python handlers are not implicitly app-safe.
NATIVE_TOOL_CAPABILITIES = {
    "atFieldActivate": "tools", "zipItDrEvil": "tools", "pass_turn": "tools",
    "get_directions": "tools", "lookup_platform_help": "tools",
    "query_perplexity": "web_search",
    "generateImage": "image_generation", "generateVideo": "video_generation",
    "generateQRCode": "image_generation", "advanceExtension": "extensions",
    "changeResponseMode": "messaging", "start_phone_call": "phone",
    "schedule_phone_call": "phone", "end_call": "phone",
}

# These handlers save app media behind conversation-scoped browser access.
# Other channels do not yet deliver those private files to their recipients.
WEB_MEDIA_TOOLS = frozenset({"generateImage", "generateVideo", "generateQRCode", "get_directions"})


def needs_application_search_connector(machine: str, model: str) -> bool:
    """Use the metered native connector when hosted billing lacks a bound.

    xAI/Gemini 3 expose counts after work, without a documented per-request
    search-call ceiling. OpenAI mini hosted search has a separate fixed content
    block whose attribution is not yet verified by this adapter.
    """
    return (machine == "xAI"
            or (machine == "Gemini" and not model.startswith("gemini-2.5"))
            or (machine in {"GPT", "O1"} and model.startswith(("gpt-4o-mini", "gpt-4.1-mini"))))


async def application_search_catalog(user_id, conversation_id, machine, model,
                                     mode, context, selected, native_tools, *, service=None):
    if (context is None or mode != "native"
            or not needs_application_search_connector(machine, model)):
        return mode, selected
    # Reuse the configured native connector and the same explicit tool grant.
    # A search capability does not silently authorize another unlisted handler.
    candidates = list(selected) + [item for item in native_tools
        if item.get("function", {}).get("name") == "query_perplexity"]
    _, permitted = await application_tool_catalog(user_id, conversation_id, candidates, service=service)
    if not any(item.get("function", {}).get("name") == "query_perplexity" for item in permitted):
        raise EmbedError("application_search_connector_required", 403)
    return "perplexity", permitted


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False}}}


async def _services(service=None):
    if service is None:
        from integrations.embed.identity import get_embed_store
        from .service import ApplicationService
        service = ApplicationService(get_embed_store())
    return service


async def application_tool_catalog(user_id, conversation_id, native_tools, *, service=None):
    """Return (scope, catalog); a None scope preserves the native catalog."""
    service = await _services(service)
    context = await service.resolve_runtime_context(conversation_id, user_id)
    if context is None:
        return None, list(native_tools)
    if not context.capabilities.get("tools"):
        return context, []
    async with service.store.connection(readonly=True) as connection:
        config, _, _, _ = await service._scope(connection, context.app_id, context.subject,
            context.user_id, context.context_id, context.assistant_id)
    selected = set(config.tools)
    from ai_runtime.channel_turns import current_channel_turn
    turn = current_channel_turn()
    if turn is not None and turn.context.channel != "web":
        selected.difference_update(WEB_MEDIA_TOOLS)
    catalog = [tool for tool in native_tools
        if (name := tool.get("function", {}).get("name")) in selected
        and context.capabilities.get(NATIVE_TOOL_CAPABILITIES.get(name, ""), False)]
    from .handoff import ApplicationHandoffService
    state = await ApplicationHandoffService(service.store).state(context, conversation_id)
    aliases = [item["assistant_id"] for item in state["assistants"]]
    names = ", ".join(f'{item["assistant_id"]}: {item["display_name"]}' for item in state["assistants"])
    if "transfer_to_assistant" in selected and aliases:
        catalog.append(_tool("transfer_to_assistant",
            "Transfer this session to an authorized assistant. Resume their separate conversation by default; "
            "new starts a deliberately new conversation. This ends your turn; do not claim success before "
            "the system confirms it. The optional note is a short declared handoff, not a copy of history. "
            "Call this tool without emitting a preamble or transfer announcement. Put your single announcement "
            "only in reply_message; the system delivers it after preparing the destination. "
            f"Available assistants: {names}",
            {"assistant_id": {"type": "string", "enum": aliases},
             "mode": {"type": "string", "enum": ["resume", "new"]},
             "conversation_id": {"type": ["integer", "null"], "minimum": 1},
             "note": {"type": ["string", "null"], "maxLength": 2000},
             "reply_message": {"type": "string", "minLength": 1, "maxLength": 1000,
                 "description": "The single brief transfer announcement, in the user's language. Do not also emit it as assistant text."}},
            ("assistant_id", "reply_message")))
    if "set_entry_assistant" in selected and aliases:
        catalog.append(_tool("set_entry_assistant",
            "Save or reset the assistant for FUTURE sessions in the specified channel, only when the user "
            "asks to save a preference. This does not transfer the current conversation. " + names,
            {"assistant_id": {"type": ["string", "null"], "enum": [*aliases, None]},
             "channel": {"type": "string", "enum": ["web", "phone", "whatsapp", "telegram", "device"]}},
            ("assistant_id", "channel")))
    from .http_tools import ApplicationHTTPToolService
    catalog.extend(await ApplicationHTTPToolService(service).list_http_tools(context))
    return context, catalog


async def authorize_application_tool(user_id, conversation_id, name, arguments, *,
                                     offered_tools=None, service=None):
    """Recheck the same catalog at execution, including native-URL entry."""
    service = await _services(service)
    if offered_tools is None:
        from tools import tools
        offered_tools = tools
    context, catalog = await application_tool_catalog(user_id, conversation_id, offered_tools, service=service)
    if context is None:
        return None
    tool = next((item for item in catalog if item.get("function", {}).get("name") == name), None)
    if tool is None:
        raise EmbedError("application_tool_denied", 403)
    if not isinstance(arguments, dict) or list(Draft202012Validator(tool["function"]["parameters"]).iter_errors(arguments)):
        raise EmbedError("invalid_tool_arguments", 422)
    # This legacy analysis accepts a conversation ID. Sharing a user/context
    # does not authorize it to read another assistant's private transcript.
    if name == "dream_of_consciousness" and arguments.get("conversation_id") != context.conversation_id:
        raise EmbedError("application_tool_resource_denied", 403)
    return context


async def set_entry_from_tool(context, arguments, *, service=None):
    from .handoff import ApplicationHandoffService
    from .models import EntryPreferenceRequest
    from .service import _one
    service = await _services(service)
    async with service.store.connection(readonly=True) as connection:
        row = await _one(connection, "SELECT context_ref FROM APPLICATION_PARTICIPANTS WHERE app_id=? AND subject=? AND context_id=?",
                         (context.app_id, context.subject, context.context_id))
    if not row:
        raise EmbedError("not_found", 404)
    result = await ApplicationHandoffService(service.store).set_entry_preference(context,
        EntryPreferenceRequest(context_ref=row["context_ref"], **arguments))
    return {"ok": True, "channel": result["channel"], "assistant_id": result["assistant_id"],
            "current_conversation_changed": False}
