"""Provider-neutral tools exposed for the active conversational channel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

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
class CallSchedulePolicy:
    """Prompt policy authorizing future calls from the current real turn."""

    mode: Literal["on_request", "proactive"]
    prompt_id: int | None = None
    request_nonce: str = field(
        default_factory=lambda: uuid4().hex,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.mode not in {"on_request", "proactive"}:
            raise ValueError("Call schedule mode must permit AI initiation")
        if self.prompt_id is not None and int(self.prompt_id) <= 0:
            raise ValueError("Call schedule prompt id must be positive")
        if not str(self.request_nonce or "").strip():
            raise ValueError("Call schedule request nonce is required")


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


def _schedule_call_tool(mode: Literal["on_request", "proactive"]) -> dict:
    if mode == "on_request":
        policy = (
            "Use this only after the latest user message explicitly requests "
            "the call, or explicitly accepts a concrete date and time you just "
            "proposed, or supplies the missing timezone/location you just asked "
            "for to complete their pending request. Do not infer agreement from "
            "unrelated older messages."
        )
    else:
        policy = (
            "You may propose a future call when it is a natural consequence of "
            "the conversation, but call this function only after the user "
            "explicitly requests or accepts the concrete date and time."
        )
    return {
        "type": "function",
        "function": {
            "name": "schedule_phone_call",
            "description": (
                "Durably schedule one future telephone call for this conversation. "
                f"{policy} scheduled_at must be the agreed local wall-clock time "
                "in ISO format without a UTC offset, and timezone_name must be the "
                "agreed IANA timezone. If the timezone is unknown, ask for city and "
                "country instead of calling this function. Never claim the call is "
                "scheduled until this function returns status=scheduled."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scheduled_at": {
                        "type": "string",
                        "description": (
                            "Agreed local date and time in ISO 8601 format without "
                            "an offset, for example 2026-09-07T19:00:00."
                        ),
                    },
                    "timezone_name": {
                        "type": "string",
                        "description": (
                            "Agreed IANA timezone, for example America/New_York."
                        ),
                    },
                    "fold": {
                        "type": "string",
                        "enum": ["not_ambiguous", "first", "second"],
                        "description": (
                            "Use not_ambiguous normally. If daylight-saving time "
                            "makes the agreed local time occur twice, use first or "
                            "second only after the user clarifies which occurrence."
                        ),
                    },
                },
                "required": ["scheduled_at", "timezone_name", "fold"],
                "additionalProperties": False,
            },
        },
        "strict": True,
    }


def phone_tools_for_context(context: ChannelContext | None) -> list[dict]:
    if context is None:
        return []
    tools: list[dict] = []
    if context.channel == "phone":
        tools.append(PHONE_END_CALL_TOOL)
    controller = context.provenance.get("call_start_controller")
    if context.channel != "phone" and isinstance(controller, CallStartController):
        tools.append(_start_call_tool(controller.mode))
    schedule_policy = context.provenance.get("call_schedule_policy")
    if isinstance(schedule_policy, CallSchedulePolicy):
        tools.append(_schedule_call_tool(schedule_policy.mode))
    return tools


__all__ = [
    "CallSchedulePolicy",
    "CallStartController",
    "CallStartDirective",
    "PHONE_END_CALL_TOOL",
    "phone_tools_for_context",
]
