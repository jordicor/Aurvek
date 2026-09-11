"""Application admission around the existing WhatsApp/Telegram pipelines."""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import inspect

from integrations.embed.models import EmbedError
from chat.services.localization import chat_translator

_messaging_state = ContextVar("application_messaging_state", default=None)


def current_messaging_state():
    return _messaging_state.get()


async def check_current_messaging(capability):
    await check_messaging(current_messaging_state(), capability, delivery=capability == "tts")


async def start_current_messaging_tts():
    await check_current_messaging("tts")
    state = current_messaging_state()
    if state is not None and state.tts_adapter is not None:
        state.tts_adapter.dispatched = True


@dataclass
class ApplicationMessagingState:
    service: object
    admission: object
    operation: object | None = None
    tts_adapter: object | None = None
    require_current_route: bool = True

    async def check(self, capability=None, *, delivery=False):
        from .billing import revalidate_application_operation
        async with self.service.store.connection(readonly=True) as connection:
            scope = await self.service.revalidate(connection, self.admission,
                require_current_route=self.require_current_route and not delivery,
                require_current_identity=delivery, capability=capability)
            if self.operation is not None:
                await revalidate_application_operation(self.operation, connection=connection)
        return scope

    def attach(self, context):
        original_guard = context.commit_guard
        original_recovery = context.recover_stale_context

        async def guard(current, connection):
            await self.service.revalidate(connection, self.admission)
            if original_guard is not None:
                result = original_guard(current, connection)
                return await result if inspect.isawaitable(result) else result
            return True

        async def recover(current):
            await self.check()
            recovered = await original_recovery(current) if original_recovery else None
            return self.attach(recovered) if recovered is not None else None

        return replace(context, application=self.admission.scope,
            application_channel=self.admission, commit_guard=guard,
            recover_stale_context=recover if original_recovery else None)


async def resolve_messaging(envelope, text, *, contact=None):
    """Resolve only a provider-authenticated identity; no native identity fallback."""
    from .channels import get_application_channel_service
    service = get_application_channel_service()
    if envelope.channel == 'telegram' and contact is not None:
        resolution = await service.telegram_contact(envelope, contact)
        if resolution.status == 'admitted':
            return ApplicationMessagingState(service, resolution.admission), chat_translator().t('channel_notices.application_connected')
    else:
        resolution = await service.resolve_inbound(envelope)
    if resolution.status == "native":
        return None, None
    if resolution.status == "admitted":
        if resolution.admission is None:
            raise EmbedError("application_channel_unavailable", 403)
        # An admitted sender is already identified. Ordinary messages must not
        # inspect or consume another invitation, even if they resemble a command.
        return ApplicationMessagingState(service, resolution.admission), None
    parts = text.strip().split()
    handled = False
    if (resolution.status == "proof_required" and len(parts) == 2
            and parts[0].lower() in {"link", "!link", "/start"}):
        resolution = await service.consume_proof(envelope, parts[1],
            challenge_id=resolution.challenge_id)
        handled = True
    elif (resolution.status == "context_required" and len(parts) == 3
            and parts[0].lower() == "!context"):
        resolution = await service.select_context(envelope, parts[1], parts[2])
        handled = True
    tr = chat_translator()
    if resolution.status == "proof_required":
        return None, tr.t("channel_notices.application_link")
    if resolution.status == "context_required":
        choices = []
        for choice in resolution.choices:
            choice_id = choice.get("choice_id")
            label = choice.get("display_name") or choice.get("label") or choice_id
            choices.append(f"{label}: !context {resolution.challenge_id} {choice_id}")
        return None, tr.t("channel_notices.application_context") + "\n".join(choices)
    if resolution.status != "admitted" or resolution.admission is None:
        raise EmbedError("application_channel_unavailable", 403)
    state = ApplicationMessagingState(service, resolution.admission)
    if handled:
        from auth import get_user_by_id
        tr = chat_translator(await get_user_by_id(state.admission.scope.user_id))
        return state, tr.t("channel_notices.application_connected")
    return state, None


async def application_command(state, text, event_id, *, translator=None):
    """Commands use the application's existing conversation/assistant services."""
    from .models import OpenConversationRequest
    text = text.strip()
    aliases = {"text_mode": "!text", "text mode": "!text", "voice_mode": "!voice", "voice mode": "!voice"}
    text = aliases.get(text.lower(), text)
    if not text.startswith("!"):
        return None
    await state.check()
    tr = chat_translator(translator)
    parts = text.split()
    command = parts[0].lower()
    operation_id = "channel:" + hashlib.sha256(f"{state.admission.receiver_id}:{event_id}:{command}".encode()).hexdigest()
    if command == "!help":
        return tr.t("channel_notices.help_application")
    if command in {"!text", "!voice"} and len(parts) == 1:
        mode = "voice" if command == "!voice" else "text"
        if mode == "voice":
            await state.check("tts")
        state.admission = await state.service.set_response_mode(state.admission, mode)
        return tr.t("channel_notices.application_mode", mode=tr.t("channel_notices.mode_" + mode))
    if command == "!chats" and len(parts) == 1:
        rows = await state.service.list_conversations(state.admission)
        return "\n".join(f"{row['conversation_id']}: {row.get('title') or row.get('assistant_id') or tr.t('channel_notices.conversation')}" for row in rows) or tr.t("channel_notices.application_no_conversations")
    if command == "!prompt" and len(parts) == 2 and parts[1].lower() == "list":
        rows = await state.service.list_assistants(state.admission)
        return "\n".join(f"{row['assistant_id']}: {row.get('display_name') or row['assistant_id']}" for row in rows) or tr.t("channel_notices.application_no_assistants")
    if command == "!entry" and len(parts) == 1:
        state.admission = await state.service.reset_entry(state.admission, operation_id)
    elif command == "!new" and len(parts) == 1:
        state.admission = await state.service.open_conversation(state.admission,
            OpenConversationRequest(operation_id=operation_id, mode="new",
                assistant_id=state.admission.scope.assistant_id))
    elif command == "!set" and len(parts) == 2 and parts[1].lstrip("#").isdigit():
        state.admission = await state.service.open_conversation(state.admission,
            OpenConversationRequest(operation_id=operation_id, conversation_id=int(parts[1].lstrip("#"))))
    elif command == "!prompt" and len(parts) == 2:
        state.admission = await state.service.open_conversation(state.admission,
            OpenConversationRequest(operation_id=operation_id, assistant_id=parts[1]))
    else:
        return tr.t("channel_notices.application_command_unavailable")
    return tr.t("channel_notices.application_selected", id=state.admission.scope.conversation_id)


@asynccontextmanager
async def application_messaging_turn(state, *, operation=None):
    if state is None:
        yield
        return
    from billing.usage_reservations import user_billing_guard
    from .billing import admit_application_billing, binding_contextmanager
    scope = await state.check()
    if operation is not None and replace(operation.scope, payer_user_id=None) != replace(scope, payer_user_id=None):
        raise EmbedError("application_scope_changed", 403)
    state.operation = operation or await admit_application_billing(scope)
    token = _messaging_state.set(state)
    try:
        with binding_contextmanager(state.operation):
            async with user_billing_guard(scope.user_id):
                await state.check()
                yield
    finally:
        _messaging_state.reset(token)


async def check_messaging(state, capability=None, *, delivery=False):
    if state is not None:
        await state.check(capability, delivery=delivery)


class AdmittedMessagingClient:
    """A request-local client that checks each delivery/download, never a global swap."""
    def __init__(self, client, state):
        self.client = client
        self.state = state

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if name not in {"send_message", "send_voice", "get_file", "download_file"}:
            return method

        async def admitted(*args, **kwargs):
            await check_messaging(self.state, delivery=name.startswith("send_"))
            return await method(*args, **kwargs)
        return admitted


def messaging_tts_billing(state):
    if state is None:
        return None
    state.tts_adapter = MessagingTTSBillingAdapter(state)
    return state.tts_adapter


class MessagingTTSBillingAdapter:
    """Reuse native fixed reservations for the already frozen application payer."""
    def __init__(self, state):
        self.state = state
        self.dispatched = False

    async def cache_hit(self, **kwargs):
        await self.state.check("tts", delivery=True)

    async def reserve(self, *, provider, characters):
        from billing.usage_reservations import reserve_fixed_usage
        from common import Cost
        await self.state.check("tts", delivery=True)
        rate, service_id = Cost.get_tts_service(provider)
        if service_id is None or rate <= 0:
            raise EmbedError("application_tts_billing_unavailable", 503)
        return await reserve_fixed_usage(user_id=self.state.admission.scope.user_id,
            purpose="tts", amount=rate * characters, service_id=service_id,
            usage_quantity=characters)

    async def provider_started(self, token):
        from billing.usage_reservations import claim_fixed_usage_provider
        await self.state.check("tts", delivery=True)
        return await claim_fixed_usage_provider(token, purpose="tts", user_id=self.state.admission.scope.user_id)

    async def settle(self, token):
        from billing.usage_reservations import mark_fixed_usage_provider_succeeded, settle_fixed_usage
        if not await mark_fixed_usage_provider_succeeded(token, purpose="tts", user_id=self.state.admission.scope.user_id):
            raise EmbedError("application_tts_billing_unavailable", 503)
        await settle_fixed_usage(token)

    async def failed(self, token, *, provider_started, reason):
        from billing.usage_reservations import refund_fixed_usage
        if not self.dispatched:
            await refund_fixed_usage(token)


async def voice_note_application_admission(connection, message_id, owner_user_id, *, capability=None):
    """Resolve retained media from its original proof, without following today's route."""
    import orjson
    from .channel_models import ChannelAdmission
    from .channels import get_application_channel_service
    from .runtime import authorize_application_read
    cursor = await connection.execute("SELECT conversation_id FROM MESSAGES WHERE id=? AND user_id=?",
        (int(message_id), int(owner_user_id)))
    row = await cursor.fetchone()
    if row is None:
        return None
    scope = await authorize_application_read(connection, int(row[0]), int(owner_user_id))
    cursor = await connection.execute("SELECT metadata_json FROM MESSAGE_CHANNEL_PROVENANCE WHERE message_id=?", (message_id,))
    provenance = await cursor.fetchone()
    if scope is None:
        try:
            recorded_app = orjson.loads(provenance[0]).get("application_channel") if provenance else None
        except (ValueError, TypeError):
            recorded_app = None
        if recorded_app is not None:
            raise EmbedError("application_channel_required", 403)
        return None
    try:
        admission = ChannelAdmission.from_dict(orjson.loads(provenance[0])["application_channel"])
    except (ValueError, TypeError, KeyError, IndexError):
        raise EmbedError("application_channel_required", 403) from None
    if admission.scope != scope:
        raise EmbedError("application_scope_changed", 403)
    await get_application_channel_service().revalidate(connection, admission,
        require_current_route=False, capability=capability)
    return admission
