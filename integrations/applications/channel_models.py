"""Internal verified channel envelopes and durable application admission."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal

from pydantic import Field, model_validator

from integrations.embed.models import StrictModel
from .models import ApplicationContext, ExternalReference, LocalId, OperationId

ChannelKind = Literal["phone", "whatsapp", "telegram"]
CredentialReference = str | None


def validate_address(channel, provider, receiver_key, provider_identity=None):
    if channel not in {"phone", "whatsapp", "telegram"}:
        raise ValueError("Unsupported application channel")
    if (channel == "telegram" and provider != "telegram") or (channel != "telegram" and provider != "twilio"):
        raise ValueError("Channel provider mismatch")
    pattern = r"[1-9][0-9]{0,19}" if channel == "telegram" else r"\+[1-9][0-9]{7,14}"
    for value in (receiver_key, provider_identity):
        if value is not None and not re.fullmatch(pattern, value):
            raise ValueError("Channel address is not canonical")


class ReceiverConfig(StrictModel):
    """Operator configuration. References are environment names, never secrets."""
    receiver_id: LocalId
    app_id: LocalId
    channel: ChannelKind
    provider: Literal["twilio", "telegram"]
    receiver_key: ExternalReference
    provider_account_id: str = Field(default="", max_length=128)
    enabled: bool = Field(default=True, strict=True)
    allow_outbound: bool = Field(default=False, strict=True)
    phone_auth: Literal["pin", "linked_caller"] = "pin"
    phone_language: Literal["en", "es", "ja", "fr", "pt", "it", "de"] = "en"
    telegram_token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    telegram_webhook_secret_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    twilio_auth_token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    twilio_integration_id: LocalId | None = None
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_provider(self):
        validate_address(self.channel, self.provider, self.receiver_key)
        if self.provider == "twilio" and not self.provider_account_id:
            raise ValueError("Twilio receiver requires its verified AccountSid")
        if self.twilio_integration_id and (self.provider != "twilio" or self.twilio_auth_token_env):
            raise ValueError("Twilio integration requires an exclusive Twilio credential reference")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedChannelEnvelope:
    """Created by an authenticated webhook adapter, never from a host JSON body."""
    channel: ChannelKind
    provider: str
    receiver_key: str
    provider_identity: str
    session_key: str
    event_id: str
    provider_account_id: str = ""

    def __post_init__(self):
        validate_address(self.channel, self.provider, self.receiver_key, self.provider_identity)
        if not self.session_key or len(self.session_key) > 160 or not self.event_id or len(self.event_id) > 160:
            raise ValueError("Channel session/event identifier is invalid")
        if self.provider == "twilio" and not self.provider_account_id:
            raise ValueError("Verified Twilio AccountSid is required")
        if self.channel != "phone" and self.session_key != "messaging":
            raise ValueError("Private messaging uses its durable channel session")


@dataclass(frozen=True, slots=True)
class ChannelAdmission:
    receiver_id: str
    receiver_version: int
    link_id: str
    link_version: int
    route_id: str
    route_version: int
    provider_identity: str
    session_key: str
    scope: ApplicationContext
    response_mode: Literal["text", "voice"] = "text"

    def as_dict(self):
        scope = {name: getattr(self.scope, name) for name in self.scope.__dataclass_fields__}
        scope["capabilities"] = dict(self.scope.capabilities)
        return {name: (scope if name == "scope" else getattr(self, name)) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "scope": ApplicationContext(**value["scope"])})


@dataclass(frozen=True, slots=True)
class ChannelResolution:
    status: Literal["native", "proof_required", "context_required", "admitted"]
    admission: ChannelAdmission | None = None
    challenge_id: str | None = None
    choices: tuple[dict, ...] = field(default_factory=tuple)


class LinkInvitationRequest(StrictModel):
    operation_id: OperationId
    external_user_id: ExternalReference
    receiver_id: LocalId
    provider_identity: ExternalReference
    context_ref: ExternalReference | None = None
    access_pin: str | None = Field(default=None, pattern=r"^[0-9]{6,10}$")
