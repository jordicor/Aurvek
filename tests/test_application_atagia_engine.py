"""Real Atagia runtime/SQLite/ASGI acceptance with deterministic LLM responses.

Run with Python >=3.12 and the adjacent Atagia src directory on PYTHONPATH.
No external model, socket, live database, or environment file is used. These
checks validate the engine integration, not semantic extraction by a real LLM.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from dataclasses import replace

import pytest

if sys.version_info < (3, 12):
    pytest.skip("The real Atagia package requires Python 3.12", allow_module_level=True)
pytest.importorskip("atagia")

import httpx
import pytest_asyncio

from atagia.core.config import Settings, default_resource_path
from atagia.core.repositories import MemoryObjectRepository
from atagia.engine import Atagia
from atagia.models.schemas_memory import (
    MemoryObjectType, MemoryScope, MemorySensitivity, MemorySourceKind,
)
from atagia.services.llm_client import LLMClient, LLMProvider, LLMCompletionResponse

from atagia_bridge import AtagiaBridge, AtagiaBridgeConfig, read_application_atagia_context
from integrations.applications.memory import MemoryNamespace
from memory import atagia_application_api


class DeterministicRetrievalProvider(LLMProvider):
    """Only the model responses are controlled; retrieval and storage are real."""
    name = "openai"

    def __init__(self):
        self.purposes = []
        self.unhandled = []

    async def complete(self, request):
        purpose = str(request.metadata.get("purpose"))
        self.purposes.append(purpose)
        if purpose == "need_detection":
            output = {"needs": [], "temporal_range": None, "sub_queries": ["marker"],
                      "sparse_query_hints": [{"sub_query_text": "marker", "fts_phrase": "marker"}],
                      "query_type": "default", "retrieval_levels": [0]}
        elif purpose == "context_cache_signal_detection":
            output = {"contradiction_detected": False, "high_stakes_topic": False,
                      "sensitive_content": False, "mode_shift_target": None,
                      "short_followup": False, "ambiguous_wording": False}
        elif purpose == "applicability_scoring":
            prompt = "\n".join(str(message.content) for message in request.messages)
            keys = re.findall(r'<candidate[^>]*score_key="([^\"]+)"', prompt)
            output = {"scores": [{"score_key": key, "llm_applicability": 1.0} for key in keys]}
        else:
            self.unhandled.append(purpose)
            raise AssertionError(f"Unconfigured deterministic model purpose: {purpose}")
        return LLMCompletionResponse(provider=self.name, model=request.model,
                                     output_text=json.dumps(output))

    async def embed(self, request):
        raise AssertionError("Embeddings are disabled in this controlled engine test")


def settings_for(path: Path, *, service=False):
    return Settings(sqlite_path=str(path),
        migrations_path=default_resource_path("migrations"),
        manifests_path=default_resource_path("manifests"),
        operational_profiles_path=default_resource_path("operational_profiles"),
        storage_backend="inprocess", redis_url="redis://unused.invalid/0",
        openai_api_key=None, openrouter_api_key=None, openrouter_site_url="http://synthetic.test",
        openrouter_app_name="Atagia engine test", llm_chat_model="openai/test-memory",
        llm_ingest_model="openai/test-memory", llm_retrieval_model="openai/test-memory",
        llm_component_models={"intent_classifier": "openai/test-memory"},
        service_mode=service, service_api_key="synthetic-service" if service else None,
        admin_api_key="synthetic-admin" if service else None, workers_enabled=False,
        lifecycle_worker_enabled=False, lifecycle_lazy_enabled=False, debug=False,
        embedding_backend="none", context_cache_enabled=True,
        artifact_blob_storage_kind="sqlite_blob")


@pytest.fixture
def deterministic_models(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    provider = DeterministicRetrievalProvider()
    monkeypatch.setattr("atagia.app.build_llm_client", lambda settings:
        LLMClient(provider_name=provider.name, providers=[provider]))
    async def forbidden_transport(*args, **kwargs):
        raise AssertionError("External HTTP transport is forbidden in engine acceptance")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_transport)
    return provider


@pytest_asyncio.fixture
async def real_engine(tmp_path, monkeypatch, deterministic_models):
    settings = settings_for(tmp_path / "real-engine.sqlite")
    engine = Atagia(db_path=settings.sqlite_path)
    monkeypatch.setattr(engine, "_build_settings", lambda: settings)
    await engine.setup()
    try:
        assert Path(engine.runtime.database_path).is_relative_to(tmp_path)
        assert not engine.runtime.worker_tasks
        yield engine, deterministic_models
    finally:
        await engine.close()


def spaces():
    shared = MemoryNamespace("demo", "context-a", "beneficiary:subject-a", "shared")
    return [shared,
        MemoryNamespace("demo", "context-a", "beneficiary:subject-a", "private", "assistant-a"),
        MemoryNamespace("demo", "context-a", "beneficiary:subject-a", "private", "assistant-b"),
        replace(shared, app_id="other-app"), replace(shared, context_id="context-b"),
        replace(shared, subject="beneficiary:subject-b")]


async def seed_real_memory(engine, namespace, marker, *, conversation_id=1):
    """Seed canonical extracted state using the real repository, not fake retrieval."""
    await engine.create_user(namespace.provider_user_id)
    await engine.create_conversation(user_id=namespace.provider_user_id,
        conversation_id=namespace.provider_conversation_id(conversation_id), platform_id=namespace.platform_id,
        user_persona_id=namespace.persona_id, character_id=namespace.character_id,
        mode="personal_assistant", incognito=False)
    runtime = await engine._require_runtime()
    connection = await runtime.open_connection()
    try:
        await MemoryObjectRepository(connection, runtime.clock).create_memory_object(
            user_id=namespace.provider_user_id, object_type=MemoryObjectType.EVIDENCE,
            scope=MemoryScope.USER, canonical_text=f"The marker is {marker}.",
            source_kind=MemorySourceKind.VERBATIM, confidence=0.99, privacy_level=0,
            stability=1.0, vitality=1.0, maya_score=1.0, sensitivity=MemorySensitivity.PUBLIC,
            user_persona_id=namespace.persona_id, platform_id=namespace.platform_id,
            scope_canonical="user", memory_id=f"test-memory-{namespace.key}")
    finally:
        await connection.close()


async def source_counts(runtime):
    connection = await runtime.open_connection()
    try:
        counts = {}
        for table in ("messages", "worker_job_runs", "memory_objects"):
            cursor = await connection.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = (await cursor.fetchone())[0]
        return counts
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_real_engine_scoped_read_isolates_user_memory_without_query_ingest(real_engine, monkeypatch):
    engine, provider = real_engine
    all_spaces = spaces()
    for index, namespace in enumerate(all_spaces):
        await seed_real_memory(engine, namespace, f"ISOLATED_{index}")
    before = await source_counts(engine.runtime)
    config = AtagiaBridgeConfig(enabled=True, transport="local", platform_id=all_spaces[0].platform_id)
    for index, namespace in enumerate(all_spaces):
        # Another native conversation recalls the authorized scope's seeded fact.
        result = await read_application_atagia_context(engine, config=config, namespace=namespace,
            conversation_id=2, message_text="Recall the marker. QUERY_MUST_NOT_BE_SAVED")
        assert f"ISOLATED_{index}" in result["system_prompt"]
        assert all(f"ISOLATED_{other}" not in result["system_prompt"] for other in range(len(all_spaces)) if other != index)
        assert result["namespace_key"] == namespace.key
    assert await source_counts(engine.runtime) == before
    assert not provider.unhandled
    # Exercise the bridge's actual local-engine creation path too, with explicit
    # isolated settings rather than the host's environment-derived settings.
    monkeypatch.setattr(Atagia, "_build_settings", lambda self: settings_for(Path(self._db_path)))
    bridge = AtagiaBridge(replace(config, db_path=engine.runtime.database_path))
    bridged = await bridge.get_application_context(namespace=all_spaces[0], conversation_id=2,
        message_text="Recall the marker. SECOND_QUERY_MUST_NOT_BE_SAVED")
    assert bridged is not None, bridge.last_error
    assert "ISOLATED_0" in bridged["system_prompt"] and "ISOLATED_1" not in bridged["system_prompt"]
    assert await source_counts(engine.runtime) == before


@pytest.mark.asyncio
async def test_real_asgi_auth_and_bridge_http_local_parity(tmp_path, monkeypatch, deterministic_models):
    assert Path(atagia_application_api.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    settings = settings_for(tmp_path / "real-asgi.sqlite", service=True)
    app = atagia_application_api.create_app(settings)
    namespace, private, *_ = spaces()
    async with app.router.lifespan_context(app):
        runtime = app.state.runtime
        facade = atagia_application_api._RuntimeFacade(runtime)
        await seed_real_memory(facade, namespace, "HTTP_SHARED")
        await seed_real_memory(facade, private, "PRIVATE_EXCLUDED")
        before = await source_counts(runtime)
        body = {"namespace": namespace.as_dict(), "conversation_id": 2, "message": "Recall the marker.",
                "mode": "personal_assistant", "platform_id": namespace.platform_id}
        headers = {"Authorization": "Bearer synthetic-service", "X-Atagia-User-Id": namespace.provider_user_id}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://synthetic.test") as client:
            assert (await client.post("/v1/context/read-only", json=body)).status_code == 401
            assert (await client.post("/v1/context/read-only", json=body,
                headers={**headers, "X-Atagia-User-Id": private.provider_user_id})).status_code == 403
            response = await client.post("/v1/context/read-only", json=body, headers=headers)
            assert response.status_code == 200, response.text
            remote = response.json()
        local = await read_application_atagia_context(facade,
            config=AtagiaBridgeConfig(enabled=True, platform_id=namespace.platform_id),
            namespace=namespace, conversation_id=2, message_text=body["message"])
        assert "HTTP_SHARED" in remote["system_prompt"]
        assert "PRIVATE_EXCLUDED" not in remote["system_prompt"]
        assert remote["system_prompt"] == local["system_prompt"]
        original_client = httpx.AsyncClient
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
            original_client(transport=transport, **kwargs))
        bridge = AtagiaBridge(AtagiaBridgeConfig(enabled=True, transport="http",
            base_url="http://synthetic.test", api_key="synthetic-service"))
        bridged = await bridge.get_application_context(namespace=namespace,
            conversation_id=2, message_text=body["message"])
        assert bridged is not None, bridge.last_error
        assert bridged["system_prompt"] == local["system_prompt"]
        assert await source_counts(runtime) == before
        assert not deterministic_models.unhandled


@pytest.mark.asyncio
async def test_real_sidecar_ingest_keeps_namespace_message_ids_and_read_does_not_add_turns(tmp_path, monkeypatch, deterministic_models):
    monkeypatch.setattr(Atagia, "_build_settings", lambda self: settings_for(Path(self._db_path)))
    namespace = spaces()[0]
    bridge = AtagiaBridge(AtagiaBridgeConfig(enabled=True, transport="local",
        db_path=str(tmp_path / "real-sidecar.sqlite")))
    try:
        assert await bridge.ensure_user_and_conversation(1, 9, namespace=namespace)
        assert await bridge.ingest_message(1, 9, "user", "The shared marker is SIDECAR_SOURCE.",
            message_id=101, source_seq=101, namespace=namespace,
            ingest_origin="live_turn", confirmation_strategy="live_prompt_allowed"), bridge.last_error
        assert await bridge.record_assistant_response(1, 9, "I understand the marker.",
            message_id=102, source_seq=102, namespace=namespace,
            ingest_origin="live_turn", confirmation_strategy="live_prompt_allowed"), bridge.last_error
        inspector = Atagia(db_path=str(tmp_path / "real-sidecar.sqlite"))
        await inspector.setup()
        try:
            before = await source_counts(inspector.runtime)
            assert before["messages"] == 2
            assert before["worker_job_runs"] > 0  # Ingestion queued real work; workers remain disabled.
            connection = await inspector.runtime.open_connection()
            try:
                cursor = await connection.execute("SELECT id,conversation_id,role FROM messages ORDER BY seq")
                rows = await cursor.fetchall()
                assert [(row["id"], row["conversation_id"], row["role"]) for row in rows] == [
                    (namespace.provider_message_id(101), namespace.provider_conversation_id(9), "user"),
                    (namespace.provider_message_id(102), namespace.provider_conversation_id(9), "assistant")]
            finally:
                await connection.close()
            result = await bridge.get_application_context(namespace=namespace, conversation_id=9,
                message_text="QUERY_ONLY_DO_NOT_INGEST")
            assert result is not None, bridge.last_error
            assert await source_counts(inspector.runtime) == before
            assert not deterministic_models.unhandled
        finally:
            await inspector.close()
    finally:
        await bridge.close()
