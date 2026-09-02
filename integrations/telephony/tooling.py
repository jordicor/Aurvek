"""Provider-neutral tools exposed for the active conversational channel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ai_runtime.channel_turns import ChannelContext


PHONE_END_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "end_call",
        "description": (
            "End the current telephone call naturally after speaking a final "
            "message. Use only when the conversation is genuinely finished or "
            "the participant asked to hang up. Aurvek waits until the final "
            "message is audibly played before disconnecting."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "final_message": {
                    "type": "string",
                    "description": "The natural final sentence to say before hanging up.",
                }
            },
            "required": ["final_message"],
            "additionalProperties": False,
        },
    },
    "strict": True,
}


@dataclass(frozen=True, slots=True)
class CallStartDirective:
    """One immediate call request produced by the current real turn."""

    reply_message: str


class CallStartController:
    """One-shot in-memory request coupled to the eventual message commit."""

    def __init__(self, mode: Literal["on_request", "proactive"]) -> None:
        if mode not in {"on_request", "proactive"}:
            raise ValueError("Call start mode must permit AI initiation")
        self.mode = mode
        self._directive: CallStartDirective | None = None

    @property
    def directive(self) -> CallStartDirective | None:
        return self._directive

    def request(self, reply_message: str) -> CallStartDirective:
        message = str(reply_message or "").strip()
        if not message:
            raise ValueError("An immediate call request needs a reply message")
        directive = CallStartDirective(reply_message=message)
        if self._directive is not None and self._directive != directive:
            raise RuntimeError("This turn already requested a different phone call")
        self._directive = directive
        return directive


def _start_call_tool(mode: Literal["on_request", "proactive"]) -> dict:
    if mode == "on_request":
        policy = (
            "Use this only when the latest user message explicitly asks to be "
            "called now. Do not infer permission from an older message."
        )
    else:
        policy = (
            "The prompt permits offering an immediate call as a direct consequence "
            "of this current user turn, even without a literal request."
        )
    return {
        "type": "function",
        "function": {
            "name": "start_phone_call",
            "description": (
                "Request one immediate telephone call for this conversation. "
                f"{policy} The reply_message is delivered and durably saved in the "
                "current text channel before Aurvek queues the call. Never use this "
                "for a future, recurring, autonomous, or background call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reply_message": {
                        "type": "string",
                        "description": (
                            "A natural message telling the user that the immediate "
                            "call will be attempted now."
                        ),
                    }
                },
                "required": ["reply_message"],
                "additionalProperties": False,
            },
        },
        "strict": True,
    }


def phone_tools_for_context(context: ChannelContext | None) -> list[dict]:
    if context is None:
        return []
    if context.channel == "phone":
        return [PHONE_END_CALL_TOOL]
    controller = context.provenance.get("call_start_controller")
    if not isinstance(controller, CallStartController):
        return []
    return [_start_call_tool(controller.mode)]


__all__ = [
    "CallStartController",
    "CallStartDirective",
    "PHONE_END_CALL_TOOL",
    "phone_tools_for_context",
]
