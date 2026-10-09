"""A closing capability owned by one authenticated native Live delegation."""

from __future__ import annotations

from integrations.embed.models import EmbedError


END_LIVE_SESSION_TOOL = {
    "type": "function",
    "function": {
        "name": "end_live_session",
        "description": (
            "End this live browser or device session only when the user requests it. "
            "Complete any requested work first. Put the complete verified result and "
            "farewell in final_message; nothing in that message has been spoken yet. "
            "The voice assistant will say it before the server ends this session."
        ),
        "parameters": {
            "type": "object",
            "properties": {"final_message": {"type": "string", "minLength": 1, "maxLength": 4000}},
            "required": ["final_message"],
            "additionalProperties": False,
        },
    },
}


class LiveSessionControl:
    def __init__(self, session, delegation_id):
        self.session = session
        self.delegation_id = delegation_id
        self.generation = session.generation
        self.canceled = False
        self.final_message = None
        self.reply_started = None
        self.start_mark = None
        self.playout_target = None

    def matches(self, context, *, user_id, conversation_id):
        session = self.session
        return (
            session._active and not self.canceled
            and session._tool_control is self
            and self.generation == session.generation
            and self.delegation_id not in session.canceled
            and user_id == session._owner_user_id
            and conversation_id == session.conversation_id
            and context.channel == session.channel
            and context.channel in {"web", "device"}
            and context.input_origin == f"{session.channel}.live_voice"
            and context.persistence == "delegation"
            and context.provenance.get("native_audio_bridge") is session.client
            and context.provenance.get("live_session_control") is self
        )

    async def request(self, arguments, context, *, user_id, conversation_id):
        if (not isinstance(arguments, dict) or set(arguments) != {"final_message"}
                or not isinstance(arguments["final_message"], str)
                or not 1 <= len(arguments["final_message"].strip()) <= 4000):
            raise EmbedError("invalid_tool_arguments", 422)
        if not self.matches(context, user_id=user_id, conversation_id=conversation_id):
            raise EmbedError("live_session_control_unavailable", 403)
        await self.session.check_access(generation=True)
        # Identity validation yields: an interruption may revoke us meanwhile.
        if not self.matches(context, user_id=user_id, conversation_id=conversation_id):
            raise EmbedError("live_session_control_unavailable", 403)
        if self.final_message is not None:
            raise EmbedError("live_session_end_already_requested", 409)
        self.final_message = arguments["final_message"].strip()
        return self.result()

    def result(self):
        return {
            "status": "live_session_end_requested",
            "final_message": self.final_message,
            "instruction": (
                "Say final_message once in full, including its verified result and farewell, "
                "then stop speaking. The server will close after playback is confirmed. "
                "Do not claim the session has already ended."
            ),
        }


def live_tools_for_context(context, *, user_id, conversation_id):
    control = context.provenance.get("live_session_control")
    if isinstance(control, LiveSessionControl) and control.matches(
            context, user_id=user_id, conversation_id=conversation_id):
        return [END_LIVE_SESSION_TOOL]
    return []


def live_control_for_context(context, *, user_id, conversation_id):
    if context is None:
        return None
    control = context.provenance.get("live_session_control")
    return control if isinstance(control, LiveSessionControl) and control.matches(
        context, user_id=user_id, conversation_id=conversation_id) else None


async def revalidate_live_delegation(context, *, user_id, conversation_id):
    if context is None or context.channel not in {"web", "device"} or context.persistence != "delegation":
        return
    control = live_control_for_context(context, user_id=user_id, conversation_id=conversation_id)
    if control is None:
        raise EmbedError("live_configuration_changed", 409)
    await control.session.check_access(generation=True)
    if not control.matches(context, user_id=user_id, conversation_id=conversation_id):
        raise EmbedError("live_configuration_changed", 409)
