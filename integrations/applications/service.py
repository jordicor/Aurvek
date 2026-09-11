"""Application admission and native conversation persistence, independent of HTTP."""
from __future__ import annotations

import json
import secrets

from integrations.embed.models import EmbedError
from integrations.embed.store import _digest, _json, _one
from .models import ApplicationContext, AssistantConfig, CONTRACT_VERSION, OpenConversationRequest
from .schema import (SCHEMA, INTERVIEW_BINDING_SQL, column_statements, default_statements,
                     legacy_context_id, legacy_statements, matches_interview_binding)


class ApplicationService:
    def __init__(self, embed_store):
        self.store = embed_store

    async def initialize(self):
        async with self.store.connection() as connection:
            from .authentication import initialize_authentication_schema
            await initialize_authentication_schema(connection)
            await connection.executescript(SCHEMA)
            from .context_source import SCHEMA as CONTEXT_SOURCE_SCHEMA
            await connection.executescript(CONTEXT_SOURCE_SCHEMA)
            from .interview_copy_schema import SCHEMA as INTERVIEW_COPY_SCHEMA
            await connection.executescript(INTERVIEW_COPY_SCHEMA)
            from .handoff_schema import SCHEMA as HANDOFF_SCHEMA
            await connection.executescript(HANDOFF_SCHEMA)
            from .http_tools_schema import SCHEMA as HTTP_TOOLS_SCHEMA
            await connection.executescript(HTTP_TOOLS_SCHEMA)
            from .memory_schema import SCHEMA as MEMORY_SCHEMA
            await connection.executescript(MEMORY_SCHEMA)
            from .channel_schema import initialize_channel_schema
            await initialize_channel_schema(connection)
            from .billing_schema import initialize_billing_schema
            await initialize_billing_schema(connection, seed_existing_native=False)
            for table in ("EMBED_TICKETS", "EMBED_FRAMES"):
                rows = await (await connection.execute(f"PRAGMA table_info({table})")).fetchall()
                for sql in column_statements(table, {row[1] for row in rows}):
                    await connection.execute(sql)
            await connection.execute("BEGIN IMMEDIATE")
            try:
                apps = await (await connection.execute("SELECT * FROM EMBED_APPS")).fetchall()
                for app in apps:
                    await self.seed_app(connection, dict(app))
                rows = await (await connection.execute("SELECT * FROM EMBED_INTERVIEWS")).fetchall()
                for row in rows:
                    await self.adopt_legacy(connection, dict(row))
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        from .accounts import ApplicationAccountService
        await ApplicationAccountService(self.store).initialize()

    async def seed_app(self, connection, config):
        app = config if isinstance(config, dict) else {
            "app_id": config.app_id, "config_json": config.model_dump_json()}
        for sql, args in default_statements(app):
            await connection.execute(sql, args)

    async def adopt_legacy(self, connection, row):
        app, _ = await self.store._app(connection, row["app_id"], active=False)
        await self.seed_app(connection, app)
        binding = await _one(connection, INTERVIEW_BINDING_SQL, (row["conversation_id"],))
        if binding:
            if not matches_interview_binding(binding, row):
                raise EmbedError("application_conflict", 409)
            return
        prior = await _one(connection, """SELECT context_id FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND subject=? AND context_ref=?""",
            (row["app_id"], row["subject"], row["external_project_id"]))
        if prior and prior["context_id"] != legacy_context_id(row):
            raise EmbedError("context_conflict", 409)
        for sql, args in legacy_statements(row):
            await connection.execute(sql, args)

    async def register_assistants(self, app_id, assistants, entry_assistant_id):
        """Operator-only configuration; historical prompt bindings never change."""
        configs = [AssistantConfig.model_validate(item) for item in assistants]
        aliases = {item.assistant_id: item for item in configs}
        if len(aliases) != len(configs) or entry_assistant_id not in aliases:
            raise ValueError("Unique assistants and a configured entry assistant are required")
        if not aliases[entry_assistant_id].enabled:
            raise ValueError("Entry assistant must be enabled")
        async with self.store.transaction() as connection:
            await self.store._app(connection, app_id, active=False)
            for config in configs:
                if not await _one(connection, "SELECT id FROM PROMPTS WHERE id=?", (config.prompt_id,)):
                    raise ValueError("Prompt not found")
                old = await _one(connection, "SELECT * FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?",
                                 (app_id, config.assistant_id))
                if old and old["prompt_id"] != config.prompt_id:
                    bound = await _one(connection, """SELECT 1 FROM APPLICATION_CONVERSATIONS
                        WHERE app_id=? AND assistant_id=? LIMIT 1""", (app_id, config.assistant_id))
                    if bound or config.assistant_id == "default":
                        raise EmbedError("assistant_prompt_immutable", 409)
                await connection.execute("""INSERT INTO APPLICATION_ASSISTANTS VALUES (?,?,?,?)
                    ON CONFLICT(app_id,assistant_id) DO UPDATE SET
                    prompt_id=excluded.prompt_id,config_json=excluded.config_json""",
                    (app_id, config.assistant_id, config.prompt_id, config.model_dump_json()))
            await connection.execute("""INSERT INTO APPLICATION_SETTINGS VALUES (?,?)
                ON CONFLICT(app_id) DO UPDATE SET entry_assistant_id=excluded.entry_assistant_id""",
                (app_id, entry_assistant_id))

    async def grant_context(self, app_id, subject, context_ref, *, beneficiary_ref=None,
                            context_id=None, capabilities=None, assistant_ids=None):
        """Operator grant. An external reference alone never grants membership."""
        if context_ref is not None and (not isinstance(context_ref, str) or not 1 <= len(context_ref) <= 160):
            raise ValueError("Invalid context reference")
        if beneficiary_ref is not None and (not isinstance(beneficiary_ref, str) or not 1 <= len(beneficiary_ref) <= 160):
            raise ValueError("Invalid beneficiary reference")
        caps = {"text": True} if capabilities is None else capabilities
        if not isinstance(caps, dict) or any(not isinstance(k, str) or type(v) is not bool for k, v in caps.items()):
            raise ValueError("Capabilities must be boolean")
        async with self.store.transaction() as connection:
            await self.store._app(connection, app_id, active=False)
            member = await _one(connection, "SELECT * FROM EMBED_MEMBERSHIPS WHERE app_id=? AND subject=?", (app_id, subject))
            if not member:
                raise EmbedError("membership_inactive", 403)
            if assistant_ids is not None:
                for alias in assistant_ids:
                    if not await _one(connection, "SELECT 1 FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?", (app_id, alias)):
                        raise ValueError("Assistant not configured")
            prior = await _one(connection, """SELECT * FROM APPLICATION_PARTICIPANTS
                WHERE app_id=? AND subject=? AND context_ref IS ?""", (app_id, subject, context_ref))
            if prior and context_id and prior["context_id"] != context_id:
                raise EmbedError("context_immutable", 409)
            explicit = context_id is not None
            context_id = prior["context_id"] if prior else (context_id or secrets.token_urlsafe(24))
            context = await _one(connection, "SELECT * FROM APPLICATION_CONTEXTS WHERE context_id=?", (context_id,))
            if context:
                if context["app_id"] != app_id or (beneficiary_ref is not None and context["beneficiary_ref"] != beneficiary_ref):
                    raise EmbedError("context_immutable", 409)
            elif explicit:
                raise EmbedError("not_found", 404)
            else:
                await connection.execute("INSERT INTO APPLICATION_CONTEXTS VALUES (?,?,?,1)", (context_id, app_id, beneficiary_ref))
            existing_participant = await _one(connection, """SELECT context_ref FROM APPLICATION_PARTICIPANTS
                WHERE app_id=? AND context_id=? AND subject=?""", (app_id, context_id, subject))
            if existing_participant and existing_participant["context_ref"] != context_ref:
                raise EmbedError("context_immutable", 409)
            await connection.execute("""INSERT INTO APPLICATION_PARTICIPANTS VALUES (?,?,?,?,1,?,?)
                ON CONFLICT(app_id,context_id,subject) DO UPDATE SET active=1,
                capabilities_json=excluded.capabilities_json,assistant_ids_json=excluded.assistant_ids_json""",
                (app_id, context_id, subject, context_ref, _json(caps),
                 _json(assistant_ids) if assistant_ids is not None else None))
            return context_id

    async def find_binding(self, connection, conversation_id):
        table = await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_CONVERSATIONS' COLLATE NOCASE")
        binding = await _one(connection, "SELECT * FROM APPLICATION_CONVERSATIONS WHERE conversation_id=?", (conversation_id,)) if table else None
        if binding:
            return binding
        legacy = await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='EMBED_INTERVIEWS' COLLATE NOCASE")
        if legacy and await _one(connection, "SELECT 1 FROM EMBED_INTERVIEWS WHERE conversation_id=?", (conversation_id,)):
            raise EmbedError("service_unavailable", 503)
        return None

    async def _scope(self, connection, app_id, subject, user_id, context_id, assistant_id):
        _, app = await self.store._app(connection, app_id)
        _, member = await self.store._live(connection, app_id, user_id, subject)
        participant = await _one(connection, """SELECT p.*,c.beneficiary_ref FROM APPLICATION_PARTICIPANTS p
            JOIN APPLICATION_CONTEXTS c ON c.context_id=p.context_id AND c.app_id=p.app_id
            WHERE p.app_id=? AND p.subject=? AND p.context_id=? AND p.active=1 AND c.active=1""",
            (app_id, subject, context_id))
        row = await _one(connection, "SELECT * FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?", (app_id, assistant_id))
        if not participant or not row:
            raise EmbedError("not_found", 404)
        config = AssistantConfig.model_validate_json(row["config_json"])
        allowed = json.loads(participant["assistant_ids_json"]) if participant["assistant_ids_json"] is not None else None
        if not config.enabled or (allowed is not None and assistant_id not in allowed):
            raise EmbedError("assistant_unavailable", 403)
        user = await self.store._load_user(user_id)
        if not user:
            raise EmbedError("unauthenticated")
        if self.store._permission_checker:
            permitted = await self.store._permission_checker(user, config.prompt_id, connection)
        else:
            from prompts import can_user_access_prompt
            async with connection.cursor() as cursor:
                permitted = await can_user_access_prompt(user, config.prompt_id, cursor)
        if not permitted:
            raise EmbedError("membership_inactive", 403)
        sources = [app.capabilities, config.capabilities, json.loads(member["capabilities_json"]),
                   json.loads(participant["capabilities_json"])]
        keys = {"text", "voice", "attachments", "memory", "image_generation", "multi_ai"}
        for source in sources:
            keys.update(source)
        ready = {"text", "voice", "memory", "tools", "web_search", "phone", "whatsapp", "telegram",
                 "stt", "tts", "attachments", "image_generation", "video_generation",
                 "model_selection", "reasoning", "extensions", "multi_ai", "gransabio",
                 "export_pdf", "export_mp3"}
        caps = {key: key in ready and all(source.get(key) is True for source in sources) for key in keys}
        return config, participant, member, caps

    async def _authorize_binding(self, connection, binding):
        config, participant, member, caps = await self._scope(connection, binding["app_id"], binding["subject"],
            binding["user_id"], binding["context_id"], binding["assistant_id"])
        native = await _one(connection, "SELECT * FROM CONVERSATIONS WHERE id=?", (binding["conversation_id"],))
        legacy = await _one(connection, "SELECT state FROM EMBED_INTERVIEWS WHERE conversation_id=?", (binding["conversation_id"],))
        if legacy and legacy["state"] != "active":
            raise EmbedError("not_found", 404)
        if (not native or native["user_id"] != binding["user_id"] or native["role_id"] != binding["prompt_id"]
                or config.prompt_id != binding["prompt_id"]):
            raise EmbedError("not_found", 404)
        return ApplicationContext(app_id=binding["app_id"], subject=binding["subject"], user_id=binding["user_id"],
            context_id=binding["context_id"], assistant_id=binding["assistant_id"], prompt_id=binding["prompt_id"],
            conversation_id=binding["conversation_id"], membership_version=member["version"], capabilities=caps,
            beneficiary_ref=participant["beneficiary_ref"])

    async def authorize_conversation(self, connection, identity, conversation_id):
        live = await self.store._session_identity(connection, identity.delegated_session_id)
        if (live.app_id, live.subject, live.user_id) != (identity.app_id, identity.subject, identity.user_id):
            raise EmbedError("unauthenticated")
        binding = await self.find_binding(connection, conversation_id)
        if not binding or (binding["app_id"], binding["subject"], binding["user_id"]) != (live.app_id, live.subject, live.user_id):
            raise EmbedError("not_found", 404)
        return await self._authorize_binding(connection, binding)

    async def authorize_runtime_conversation(self, connection, user_id, conversation_id):
        binding = await self.find_binding(connection, conversation_id)
        if not binding:
            return None
        if binding["user_id"] != user_id:
            raise EmbedError("not_found", 404)
        return await self._authorize_binding(connection, binding)

    async def authorize_backend_conversation(self, connection, identity, conversation_id, context_ref):
        """Resolve delegated ownership and the consumer's explicit context together."""
        scope = await self.authorize_conversation(connection, identity, conversation_id)
        participant = await _one(connection, """SELECT context_ref FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND subject=? AND context_id=?""",
            (scope.app_id, scope.subject, scope.context_id))
        if not participant or participant["context_ref"] != context_ref:
            raise EmbedError("not_found", 404)
        return scope

    async def resolve_runtime_context(self, conversation_id, user_id):
        async with self.store.connection(readonly=True) as connection:
            return await self.authorize_runtime_conversation(connection, user_id, conversation_id)

    async def _validated_actor(self, connection, actor):
        if isinstance(actor, ApplicationContext):
            live = await self.authorize_runtime_conversation(connection, actor.user_id, actor.conversation_id)
            fields = ("app_id", "subject", "user_id", "context_id", "assistant_id", "prompt_id")
            if live is None or any(getattr(live, key) != getattr(actor, key) for key in fields):
                raise EmbedError("application_scope_mismatch", 403)
            return live
        live = await self.store._session_identity(connection, actor.delegated_session_id)
        if (actor.app_id, actor.subject, actor.user_id) != (live.app_id, live.subject, live.user_id):
            raise EmbedError("unauthenticated")
        return live

    async def _authorize_actor_destination(self, connection, actor, conversation_id):
        binding = await self.find_binding(connection, conversation_id)
        if not binding or (binding["app_id"], binding["subject"], binding["user_id"]) != (actor.app_id, actor.subject, actor.user_id):
            raise EmbedError("not_found", 404)
        context = await self._authorize_binding(connection, binding)
        if isinstance(actor, ApplicationContext) and context.context_id != actor.context_id:
            raise EmbedError("not_found", 404)
        return context

    async def open_conversation(self, principal, request: OpenConversationRequest, *, entry_channel="web"):
        request = OpenConversationRequest.model_validate(request)
        async with self.store.transaction() as connection:
            live = await self._validated_actor(connection, principal)
            return await self._open_in_connection(connection, live, request, entry_channel=entry_channel)

    async def _open_in_connection(self, connection, live, request, *, record_operation=True, entry_channel="web"):
        # Preserve hashes of historical web opens; only non-web entry adds a key.
        content = request.model_dump(exclude={"operation_id"})
        if entry_channel != "web":
            content["entry_channel"] = entry_channel
        request_hash = _digest(_json(content))
        operation = await _one(connection, """SELECT * FROM APPLICATION_OPERATIONS
            WHERE app_id=? AND subject=? AND operation_id=?""", (live.app_id, live.subject, request.operation_id)) if record_operation else None
        if operation:
            if operation["request_hash"] != request_hash:
                raise EmbedError("operation_conflict", 409)
            context = await self._authorize_actor_destination(connection, live, operation["conversation_id"])
            await self._require_unlocked(connection, context.conversation_id)
            if not context.capabilities.get("text"):
                raise EmbedError("capability_unavailable", 403)
            return {**json.loads(operation["result_json"]), "capabilities": dict(context.capabilities), "replayed": True}
        participant = await _one(connection, """SELECT * FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND subject=? AND context_ref IS ?""", (live.app_id, live.subject, request.context_ref))
        if not participant and request.context_ref is None:
            _, member = await self.store._live(connection, live.app_id, live.user_id, live.subject)
            _, app = await self.store._app(connection, live.app_id)
            admitted = {key: True for key, value in json.loads(member["capabilities_json"]).items()
                        if value is True and app.capabilities.get(key) is True}
            context_id = secrets.token_urlsafe(24)
            await connection.execute("INSERT INTO APPLICATION_CONTEXTS VALUES (?,?,NULL,1)", (context_id, live.app_id))
            await connection.execute("INSERT INTO APPLICATION_PARTICIPANTS VALUES (?,?,?,NULL,1,?,NULL)",
                (live.app_id, context_id, live.subject, _json(admitted)))
            participant = {"context_id": context_id}
        if not participant:
            raise EmbedError("not_found", 404)
        assistant_id = request.assistant_id
        if assistant_id is None:
            from .handoff import resolve_entry_assistant
            assistant_id = await resolve_entry_assistant(self, connection, live,
                                                         participant["context_id"], entry_channel)
        context_id = participant["context_id"]
        config, _, _, caps = await self._scope(connection, live.app_id, live.subject, live.user_id, context_id, assistant_id)
        if not caps.get("text"):
            raise EmbedError("capability_unavailable", 403)
        conversation_id = request.conversation_id
        if conversation_id is None and request.mode == "resume":
            row = await _one(connection, """SELECT b.conversation_id FROM APPLICATION_CONVERSATIONS b
                JOIN CONVERSATIONS c ON c.id=b.conversation_id WHERE b.app_id=? AND b.subject=?
                AND b.user_id=? AND b.context_id=? AND b.assistant_id=? AND b.prompt_id=?
                AND c.user_id=b.user_id AND c.role_id=b.prompt_id AND COALESCE(c.locked,0)=0
                ORDER BY c.last_activity DESC,b.conversation_id DESC LIMIT 1""",
                (live.app_id, live.subject, live.user_id, context_id, assistant_id, config.prompt_id))
            conversation_id = row["conversation_id"] if row else None
        created = conversation_id is None
        if created:
            user = await self.store._load_user(live.user_id)
            creator = self.store._conversation_creator
            if creator is None:
                from chat.services.conversations import create_conversation_core
                creator = create_conversation_core
            try:
                async with connection.cursor() as cursor:
                    conversation_id = await creator(live.user_id, cursor, user,
                        prompt_id=config.prompt_id, strict_prompt_access=True)
            except PermissionError as error:
                raise EmbedError("membership_inactive", 403) from error
            except ValueError as error:
                raise EmbedError("service_unavailable", 503) from error
            await connection.execute("INSERT INTO APPLICATION_CONVERSATIONS VALUES (?,?,?,?,?,?,?,?)",
                (conversation_id, live.app_id, context_id, live.subject, live.user_id,
                 assistant_id, config.prompt_id, self.store.now()))
        context = await self._authorize_actor_destination(connection, live, conversation_id)
        await self._require_unlocked(connection, context.conversation_id)
        if context.context_id != context_id or context.assistant_id != assistant_id:
            raise EmbedError("not_found", 404)
        result = {"contract_version": CONTRACT_VERSION, "app_id": live.app_id, "subject": live.subject,
            "context_id": context_id, "context_ref": request.context_ref, "assistant_id": assistant_id,
            "prompt_id": context.prompt_id, "conversation_id": conversation_id,
            "capabilities": dict(context.capabilities), "created": created, "replayed": False}
        if record_operation:
            await connection.execute("INSERT INTO APPLICATION_OPERATIONS VALUES (?,?,?,?,?,?)",
                (live.app_id, live.subject, request.operation_id, request_hash, conversation_id, _json(result)))
        return result

    @staticmethod
    async def _require_unlocked(connection, conversation_id):
        native = await _one(connection, "SELECT * FROM CONVERSATIONS WHERE id=?", (conversation_id,))
        if native and native.get("locked"):
            raise EmbedError("conversation_locked", 409)
