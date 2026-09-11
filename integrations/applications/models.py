"""Small application contracts. F0 defines data; later phases expose operations.

Wire requests never accept an Aurvek user, prompt or payer as authority. Services
resolve those from authenticated membership and the durable conversation binding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Literal, Mapping

from pydantic import Field, model_validator

from integrations.embed.models import StrictModel

CONTRACT_VERSION = "aurvek_applications.v1"
LocalId = Annotated[str, Field(strict=True, min_length=1, max_length=64,
                               pattern=r"^[a-z][a-z0-9_-]*$")]
ExternalReference = Annotated[str, Field(strict=True, min_length=1, max_length=160)]
OperationId = Annotated[str, Field(strict=True, min_length=8, max_length=128)]
ConversationId = Annotated[int, Field(strict=True, gt=0)]
Channel = Literal["web", "phone", "whatsapp", "telegram", "device"]


class MemoryPolicy(StrictModel):
    read_private: bool = Field(default=True, strict=True)
    read_shared: bool = Field(default=False, strict=True)
    write_space: Literal["private", "shared", "none"] = "private"


class AssistantConfig(StrictModel):
    assistant_id: LocalId
    display_name: str = Field(min_length=1, max_length=120)
    prompt_id: ConversationId
    enabled: bool = Field(default=True, strict=True)
    capabilities: dict[str, bool] = Field(default_factory=lambda: {"text": True})
    tools: list[str] = Field(default_factory=list, max_length=64)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)


class OpenConversationRequest(StrictModel):
    operation_id: OperationId
    context_ref: ExternalReference | None = None
    assistant_id: LocalId | None = None
    mode: Literal["resume", "new"] = "resume"
    conversation_id: ConversationId | None = None

    @model_validator(mode="after")
    def validate_destination(self):
        if self.mode == "new" and self.conversation_id is not None:
            raise ValueError("A new conversation cannot specify an existing destination")
        return self


class HandoffRequest(StrictModel):
    operation_id: OperationId
    source_conversation_id: ConversationId
    assistant_id: LocalId
    mode: Literal["resume", "new"] = "resume"
    conversation_id: ConversationId | None = None
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_destination(self):
        if self.mode == "new" and self.conversation_id is not None:
            raise ValueError("A new conversation cannot specify an existing destination")
        return self


class EntryPreferenceRequest(StrictModel):
    context_ref: ExternalReference | None = None
    channel: Channel
    assistant_id: LocalId | None = None


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    """Internal resolved scope, not proof of permission by itself.

    Re-resolve membership before admission; persist attribution on the operation
    so settlement and background work never rely on a browser request context.
    A None payer is unresolved (F1), never an instruction to use personal funds.
    """

    app_id: str
    subject: str
    user_id: int
    context_id: str
    assistant_id: str
    prompt_id: int
    conversation_id: int
    membership_version: int
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    beneficiary_ref: str | None = None
    payer_user_id: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
