"""Small, explicit wire contract for first-party Aurvek integrations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from i18n import LANGUAGES

if TYPE_CHECKING:
    from integrations.applications.models import ApplicationContext

CONTRACT_VERSION = "aurvek_embed.v1"
EMBED_COOKIE_NAME = "__Host-aurvek_embed"


class EmbedError(Exception):
    def __init__(self, code: str, status_code: int = 401):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def exact_https_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment
            or parsed.netloc != parsed.netloc.lower() or value != value.strip() or not value.isascii()
            or any(ord(char) <= 32 for char in value) or "*" in value
            or parsed.hostname.endswith(".")):
        raise ValueError("An exact HTTPS origin without a path is required")
    _ = parsed.port
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class InterviewBrief(StrictModel):
    schema_version: Literal[1] = 1
    revision: int = Field(ge=1, strict=True)
    language: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
    preferred_name: str | None = Field(default=None, max_length=160)
    scope_text: str | None = Field(default=None, max_length=4000)
    purpose_text: str | None = Field(default=None, max_length=4000)
    audience_text: str | None = Field(default=None, max_length=4000)
    boundaries_text: str | None = Field(default=None, max_length=4000)


class AppConfig(StrictModel):
    app_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    display_name: str = Field(min_length=1, max_length=120)
    issuer: str
    embed_origins: list[str] = Field(min_length=1, max_length=8)
    parent_origins: list[str] = Field(min_length=1, max_length=16)
    redirect_uris: list[str] = Field(min_length=1, max_length=16)
    prompt_id: int = Field(gt=0, strict=True)
    owner_user_id: int | None = Field(default=None, gt=0, strict=True)
    theme: str = Field(default="default", max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    capabilities: dict[str, bool] = Field(default_factory=lambda: {"text": True})
    enabled: bool = False
    remote_backend_access: bool = Field(default=False, strict=True)

    @field_validator("issuer")
    @classmethod
    def valid_issuer(cls, value):
        return exact_https_origin(value)

    @field_validator("embed_origins", "parent_origins")
    @classmethod
    def valid_origins(cls, values):
        return [exact_https_origin(value) for value in values]

    @field_validator("redirect_uris")
    @classmethod
    def valid_returns(cls, values):
        for value in values:
            parsed = urlsplit(value)
            exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
            if parsed.fragment or parsed.query or any(ord(c) <= 32 for c in value):
                raise ValueError("Return URLs must be exact HTTPS URLs without query or fragment")
        return values


@dataclass(frozen=True)
class EmbedPrincipal:
    app_id: str
    issuer: str
    subject: str
    user_id: int
    prompt_id: int
    delegated_session_id: str
    session_version: int
    membership_version: int
    expires_at: int
    capabilities: dict[str, bool] = field(default_factory=dict)
    conversation_id: int | None = None
    external_project_id: str | None = None
    parent_origin: str | None = None
    embed_origin: str | None = None
    frame_instance_id: str | None = None
    ui_language: str = "en"
    csrf_token: str = ""
    binding_kind: Literal["embed", "application"] = "embed"
    application: ApplicationContext | None = None
    source_auth_time: int = 0


class ExchangeCodeRequest(StrictModel):
    code: str = Field(min_length=32, max_length=256)
    code_verifier: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    redirect_uri: str = Field(max_length=2048)


class DelegatedRequest(StrictModel):
    delegated_credential: str = Field(min_length=32, max_length=256)


class InterviewRequest(DelegatedRequest):
    external_project_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")


class EnsureInterviewRequest(InterviewRequest):
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    brief: InterviewBrief


class UpdateBriefRequest(InterviewRequest):
    conversation_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    expected_revision: int = Field(ge=1, strict=True)
    brief: InterviewBrief


class BootstrapRequest(InterviewRequest):
    conversation_id: str = Field(min_length=1, max_length=32, pattern=r"^[0-9]+$")
    parent_origin: str = Field(max_length=256)
    embed_origin: str = Field(max_length=256)
    ui_language: str = Field(default="en", max_length=16)
    frame_instance_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("ui_language")
    @classmethod
    def valid_ui_language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError("Unsupported UI language")
        return value
