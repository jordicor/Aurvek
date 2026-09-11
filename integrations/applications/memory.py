"""Small application memory boundary shared by live and background adapters.

Provider identities isolate even user-wide memories. Stored destinations are
provenance for erasure, not permission to retrieve or ingest after revocation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import asynccontextmanager
import hashlib
import json
from typing import Any

from integrations.applications.models import AssistantConfig
from integrations.applications.memory_schema import SCHEMA
from integrations.embed.models import EmbedError


@dataclass(frozen=True, slots=True)
class MemoryNamespace:
    app_id: str
    context_id: str
    subject: str
    space: str
    assistant_id: str | None = None

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in (self.app_id, self.context_id, self.subject)):
            raise ValueError("Memory identity must be complete")
        if self.space not in {"private", "shared"}:
            raise ValueError("Invalid application memory space")
        if (self.space == "private") != bool(self.assistant_id):
            raise ValueError("Only private memory requires an assistant alias")

    @property
    def key(self) -> str:
        raw = json.dumps([1, self.app_id, self.context_id, self.subject,
                          self.space, self.assistant_id], ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @property
    def provider_user_id(self) -> str:
        return f"aurvek:application-memory:v1:{self.key}"

    def provider_conversation_id(self, conversation_id: int | str) -> str:
        return f"{self.provider_user_id}:conversation:{int(conversation_id)}"

    def provider_message_id(self, message_id: int | str) -> str:
        return f"{self.provider_user_id}:message:{int(message_id)}"

    @property
    def character_id(self) -> str:
        # Shared writes must have the same character across assistant aliases.
        return f"application-memory:{self.key}"

    @property
    def persona_id(self) -> str:
        return f"application-memory:{self.key}"

    @property
    def platform_id(self) -> str:
        return "aurvek-applications-v1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryNamespace":
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ApplicationMemoryScope:
    reads: tuple[MemoryNamespace, ...]
    write: MemoryNamespace | None
    after_message_id: int

    def allows_historical_message(self, message_id: int) -> bool:
        return self.write is not None and int(message_id) > self.after_message_id


async def _one(connection, sql, values=()):
    cursor = await connection.execute(sql, values)
    return await cursor.fetchone()


async def _ensure_schema(connection):
    # executescript commits a caller's transaction; keep these statements inside it.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            await connection.execute(statement)


@asynccontextmanager
async def _memory_connection(service, connection):
    if connection is not None:
        yield connection
    else:
        async with service.store.transaction() as owned:
            yield owned


async def resolve_application_memory(
    conversation_id: int, user_id: int, *, service=None, connection=None,
) -> ApplicationMemoryScope | None:
    """Revalidate durable membership; only a genuinely native chat returns None."""
    if service is None:
        from integrations.applications.service import ApplicationService
        from integrations.embed.identity import get_embed_store

        service = ApplicationService(get_embed_store())
    async with _memory_connection(service, connection) as connection:
        context = await service.authorize_runtime_conversation(connection, user_id, conversation_id)
        if context is None:
            return None
        row = await _one(connection,
            "SELECT config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?",
            (context.app_id, context.assistant_id))
        if not row:
            raise EmbedError("assistant_unavailable", 403)
        policy = AssistantConfig.model_validate_json(row[0]).memory
        if not context.capabilities.get("memory"):
            return ApplicationMemoryScope((), None, 0)
        # Initialize at admission, before provider I/O. Existing history is not
        # silently imported when memory is enabled on a previously used chat.
        await _ensure_schema(connection)
        activation = await _one(connection,
            "SELECT after_message_id FROM APPLICATION_MEMORY_ACTIVATIONS WHERE conversation_id=?",
            (conversation_id,))
        if activation is None:
            table = await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='MESSAGES'")
            latest = await _one(connection, "SELECT COALESCE(MAX(id),0) FROM MESSAGES WHERE conversation_id=?",
                                (conversation_id,)) if table else (0,)
            await connection.execute(
                "INSERT OR IGNORE INTO APPLICATION_MEMORY_ACTIVATIONS(conversation_id,after_message_id) VALUES (?,?)",
                (conversation_id, int(latest[0])))
            activation = await _one(connection,
                "SELECT after_message_id FROM APPLICATION_MEMORY_ACTIVATIONS WHERE conversation_id=?",
                (conversation_id,))
        # Include the kind in the subject input to avoid a beneficiary reference
        # accidentally matching the opaque member subject.
        subject = ("beneficiary:" + context.beneficiary_ref if context.beneficiary_ref is not None
                   else "member:" + context.subject)
        private = MemoryNamespace(context.app_id, context.context_id, subject, "private", context.assistant_id)
        shared = MemoryNamespace(context.app_id, context.context_id, subject, "shared")
        reads = tuple(n for enabled, n in ((policy.read_private, private), (policy.read_shared, shared)) if enabled)
        write = {"private": private, "shared": shared, "none": None}[policy.write_space]
        return ApplicationMemoryScope(reads, write, int(activation[0]))


async def record_memory_destination(
    *, provider: str, conversation_id: int, user_id: int,
    namespace: MemoryNamespace, message_id: int | None = None, connection=None,
) -> None:
    """Record before provider I/O, including outcomes that may be ambiguous."""
    if connection is None:
        from database import get_db_connection

        async with get_db_connection() as conn:
            await record_memory_destination(provider=provider, conversation_id=conversation_id,
                user_id=user_id, namespace=namespace, message_id=message_id, connection=conn)
            await conn.commit()
        return
    await _ensure_schema(connection)
    if message_id is not None:
        existing = await _one(connection, "SELECT conversation_id,user_id,namespace_json FROM APPLICATION_MEMORY_MESSAGE_DESTINATIONS WHERE provider=? AND message_id=?",
                              (provider, message_id))
        if existing is not None and (existing[0] != conversation_id or existing[1] != user_id
                or MemoryNamespace.from_dict(json.loads(existing[2])).key != namespace.key):
            raise ValueError("A memory message cannot move to another namespace")
        await connection.execute("""INSERT OR IGNORE INTO APPLICATION_MEMORY_MESSAGE_DESTINATIONS
            (provider,message_id,conversation_id,user_id,namespace_json) VALUES (?,?,?,?,?)""",
            (provider, message_id, conversation_id, user_id, json.dumps(namespace.as_dict())))
    await connection.execute("""INSERT OR IGNORE INTO APPLICATION_MEMORY_DESTINATIONS
        (provider,conversation_id,user_id,namespace_key,namespace_json) VALUES (?,?,?,?,?)""",
        (provider, conversation_id, user_id, namespace.key, json.dumps(namespace.as_dict())))


async def list_memory_destinations(
    *, conversation_id: int | None = None, user_id: int | None = None,
    provider: str | None = None, connection=None,
) -> list[dict[str, Any]]:
    if conversation_id is None and user_id is None:
        raise ValueError("A conversation or user is required")
    if connection is None:
        from database import get_db_connection

        async with get_db_connection(readonly=True) as conn:
            return await list_memory_destinations(conversation_id=conversation_id, user_id=user_id,
                                                   provider=provider, connection=conn)
    if not await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_MEMORY_DESTINATIONS'"):
        return []
    filters, values = [], []
    for column, value in (("conversation_id", conversation_id), ("user_id", user_id), ("provider", provider)):
        if value is not None:
            filters.append(f"{column}=?")
            values.append(value)
    cursor = await connection.execute("SELECT provider,conversation_id,user_id,namespace_json FROM APPLICATION_MEMORY_DESTINATIONS WHERE "
                                      + " AND ".join(filters), values)
    return [{"provider": row[0], "conversation_id": row[1], "user_id": row[2],
             "namespace": MemoryNamespace.from_dict(json.loads(row[3]))} for row in await cursor.fetchall()]


async def list_memory_message_destinations(
    *, conversation_id: int | None = None, message_id: int | None = None,
    user_id: int | None = None, provider: str | None = None, connection=None,
) -> list[dict[str, Any]]:
    if conversation_id is None and message_id is None and user_id is None:
        raise ValueError("A conversation, message or user is required")
    if connection is None:
        from database import get_db_connection

        async with get_db_connection(readonly=True) as conn:
            return await list_memory_message_destinations(conversation_id=conversation_id,
                message_id=message_id, user_id=user_id, provider=provider, connection=conn)
    if not await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_MEMORY_MESSAGE_DESTINATIONS'"):
        return []
    filters, values = [], []
    for column, value in (("conversation_id", conversation_id), ("message_id", message_id),
                          ("user_id", user_id), ("provider", provider)):
        if value is not None:
            filters.append(f"{column}=?")
            values.append(value)
    cursor = await connection.execute("SELECT provider,message_id,conversation_id,user_id,namespace_json FROM APPLICATION_MEMORY_MESSAGE_DESTINATIONS WHERE "
                                      + " AND ".join(filters), values)
    return [{"provider": row[0], "message_id": row[1], "conversation_id": row[2], "user_id": row[3],
             "namespace": MemoryNamespace.from_dict(json.loads(row[4]))} for row in await cursor.fetchall()]
