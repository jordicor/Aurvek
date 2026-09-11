"""Opt-in, isolated acceptance of real Atagia extraction and scoped retrieval.

Without --run-real this script never reads credentials or initializes Atagia.
--self-check exercises only the budget arithmetic, without model/network I/O.
Real runs use two synthetic sources, native workers/providers, and temporary
SQLite. Retrieval uses Atagia's explicit ablations for need detection and LLM
applicability scoring; this checks real extracted memory and namespace isolation,
not semantic ranking, a natural final answer, or the production HTTP adapter.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import sys
import tempfile


MAX_REQUESTS = 8
MAX_ESTIMATED_INPUT = 30_000
MAX_OUTPUT = 2_048
MAX_COST = Decimal("0.50")
MODELS = ("gpt-4.1-mini", "gpt-4o-mini")


class BudgetExceeded(RuntimeError):
    """A request was denied before its network transport ran."""


@dataclass
class Budget:
    input_price: Decimal
    output_price: Decimal
    requests: int = 0
    estimated_input: int = 0
    reserved_cost: Decimal = Decimal(0)
    denied: int = 0
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def reserve(self, body: dict) -> None:
        # Include tool/schema text and serialization overhead. The display/input
        # limit uses a conservative English estimate; cost reserves one token per
        # UTF-8 byte plus framing, so a failed request is never refunded.
        prompt = {key: value for key, value in body.items()
                  if key not in {"max_tokens", "max_completion_tokens", "stream", "stream_options"}}
        byte_count = len(json.dumps(prompt, ensure_ascii=False).encode("utf-8"))
        estimate = (byte_count + 2) // 3 + 256
        cost_token_bound = byte_count + 1024
        output = body.get("max_tokens")
        async with self.lock:
            cost = (Decimal(cost_token_bound) * self.input_price
                    + Decimal(MAX_OUTPUT) * self.output_price) / Decimal(1_000_000)
            if (self.closed or type(output) is not int or not 1 <= output <= MAX_OUTPUT
                    or "max_completion_tokens" in body
                    or self.requests >= MAX_REQUESTS
                    or self.estimated_input + estimate > MAX_ESTIMATED_INPUT
                    or self.reserved_cost + cost > MAX_COST):
                self.denied += 1
                self.closed = True
                raise BudgetExceeded("Global provider budget denied the request")
            self.requests += 1
            self.estimated_input += estimate
            self.reserved_cost += cost

    def summary(self) -> dict:
        return {"provider_requests": self.requests,
                "estimated_input_tokens": self.estimated_input,
                "reserved_cost_usd": str(self.reserved_cost),
                "denied_requests": self.denied}


async def self_check() -> None:
    body = {"model": MODELS[0], "messages": [{"role": "user", "content": "synthetic"}],
            "max_tokens": MAX_OUTPUT}
    budget = Budget(Decimal("0.40"), Decimal("1.60"))
    outcomes = await asyncio.gather(*(budget.reserve(body) for _ in range(12)),
                                    return_exceptions=True)
    assert budget.requests == MAX_REQUESTS
    assert sum(isinstance(result, BudgetExceeded) for result in outcomes) == 4
    for test_body, input_price in (
        ({**body, "max_tokens": MAX_OUTPUT + 1}, Decimal("0.40")),
        ({**body, "messages": [{"role": "user", "content": "x" * 100_000}]}, Decimal("0.40")),
        (body, Decimal("1000")),
    ):
        blocked = Budget(input_price, Decimal("1.60"))
        try:
            await blocked.reserve(test_body)
        except BudgetExceeded:
            pass
        else:
            raise AssertionError("Budget guard did not reject an unsafe request")
        assert blocked.requests == 0
    print(json.dumps({"self_check": "passed", "network_requests": 0,
                      "guards": ["concurrent_request_cap", "input_cap", "output_cap", "cost_cap"]}))


async def run_real(args, budget: Budget, evidence: dict) -> dict:
    # This is set before importing any project code; Settings never reads either
    # repository's environment. Only the explicitly named Aurvek key is parsed.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    logging.disable(logging.CRITICAL)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root.parent / "atagia" / "src"))
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from dotenv import dotenv_values
    import httpx
    from openai import AsyncOpenAI
    import atagia.app as atagia_app
    from atagia.engine import Atagia
    from atagia.models.schemas_replay import AblationConfig
    from atagia.services.context_cache_service import ContextCacheService
    from atagia.services.llm_client import LLMClient, LLMProvider, RetryPolicy
    from atagia.services.providers.openai import OpenAIProvider
    from integrations.applications.memory import MemoryNamespace
    from test_application_atagia_engine import settings_for, source_counts
    import memory

    assert Path(memory.__file__).resolve() == root / "memory" / "__init__.py"
    evidence["phase"] = "credential_selection"

    values = dotenv_values(root / ".env", interpolate=False)
    key = values.get(args.key_name)
    values.clear()
    if not key:
        raise ValueError("The explicitly selected credential is missing")

    class CappedTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

        async def handle_async_request(self, request):
            if (request.url.scheme != "https" or request.url.host != "api.openai.com"
                    or request.url.port not in (None, 443)
                    or request.url.path != "/v1/chat/completions" or request.method != "POST"):
                raise BudgetExceeded("Only the selected completion endpoint is permitted")
            body = json.loads(await request.aread())
            if body.get("model") != args.model:
                raise BudgetExceeded("Unpriced model requested")
            await budget.reserve(body)
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    # Fixed origin, no SDK retry, redirect, inherited proxy, project or organization.
    http = httpx.AsyncClient(transport=CappedTransport(), trust_env=False,
                            follow_redirects=False, timeout=45)
    sdk = AsyncOpenAI(api_key=key, base_url="https://api.openai.com/v1",
                      organization="", project="", max_retries=0, http_client=http)
    key = None
    native = OpenAIProvider(api_key="unused-client-already-supplied", client=sdk)

    class BoundedNativeProvider(LLMProvider):
        name = "openai"
        supports_embeddings = False

        def limited(self, request):
            # Native structured-schema retries and streaming all traverse the
            # capped HTTP transport. Discard provider overrides that could raise
            # output limits or enable reasoning in extra_body.
            metadata = {name: value for name, value in request.metadata.items()
                        if name not in {"provider_extra_body", "reasoning_effort", "verbosity"}}
            return request.model_copy(update={"max_output_tokens": min(
                request.max_output_tokens or MAX_OUTPUT, MAX_OUTPUT),
                "include_thinking": False, "metadata": metadata})

        async def complete(self, request):
            async with asyncio.timeout(50):
                return await native.complete(self.limited(request))

        async def stream(self, request):
            async with asyncio.timeout(50):
                async for event in native.stream(self.limited(request)):
                    yield event

        async def embed(self, request):
            raise BudgetExceeded("Embeddings are disabled in this acceptance")

    models = LLMClient(providers=[BoundedNativeProvider()],
                       retry_policy=RetryPolicy(attempts=1))
    original_factory = atagia_app.build_llm_client
    atagia_app.build_llm_client = lambda settings: models
    try:
        with tempfile.TemporaryDirectory(prefix="aurvek-atagia-provider-") as directory:
            settings = replace(settings_for(Path(directory) / "acceptance.sqlite"),
                workers_enabled=True, llm_forced_global_model=f"openai/{args.model}",
                llm_chat_model=f"openai/{args.model}", llm_ingest_model=f"openai/{args.model}",
                llm_retrieval_model=f"openai/{args.model}",
                llm_component_models={"intent_classifier": f"openai/{args.model}"},
                topic_working_set_enabled=False, graph_projection_enabled=False,
                skip_belief_revision=True, skip_compaction=True,
                disable_chunking_extraction=True, extraction_watchdog_enabled=False)
            engine = Atagia(db_path=settings.sqlite_path)
            engine._build_settings = lambda: settings
            try:
                evidence["phase"] = "native_engine_setup"
                await engine.setup()
                runtime = await engine._require_runtime()
                assert Path(runtime.database_path).is_relative_to(Path(directory))
                assert runtime.worker_tasks, "Native workers did not start"
                shared_a = MemoryNamespace("synthetic-app", "synthetic-context", "beneficiary:synthetic", "shared")
                # Shared identity deliberately excludes the assistant alias.
                shared_b = MemoryNamespace("synthetic-app", "synthetic-context", "beneficiary:synthetic", "shared")
                private_a = replace(shared_a, space="private", assistant_id="assistant-a")
                private_b = replace(shared_a, space="private", assistant_id="assistant-b")

                async def ensure(namespace, conversation_id):
                    await engine.create_user(namespace.provider_user_id)
                    await engine.create_conversation(user_id=namespace.provider_user_id,
                        conversation_id=namespace.provider_conversation_id(conversation_id),
                        user_persona_id=namespace.persona_id, platform_id=namespace.platform_id,
                        character_id=namespace.character_id, mode="personal_assistant", incognito=False)

                for namespace, text in (
                    (shared_a, "My favorite hobby has been watercolor painting for many years. "
                               "I regularly paint watercolor landscapes for fun."),
                    (private_a, "My favorite hobby has been chess for many years. "
                                "I regularly play chess for fun."),
                ):
                    evidence["phase"] = f"native_{namespace.space}_extraction"
                    await ensure(namespace, 1)
                    await engine.ingest_message(user_id=namespace.provider_user_id,
                        conversation_id=namespace.provider_conversation_id(1), role="user", text=text,
                        message_id=namespace.provider_message_id(1), source_seq=1,
                        occurred_at="2021-03-14T10:00:00+00:00", mode="personal_assistant",
                        user_persona_id=namespace.persona_id, platform_id=namespace.platform_id,
                        character_id=namespace.character_id, cross_chat_memory=True,
                        ingest_origin="live_turn", confirmation_strategy="live_prompt_allowed")
                    assert await engine.flush(timeout_seconds=180), "Native workers did not drain"
                    assert not budget.denied, "Provider budget was exhausted during extraction"

                connection = await runtime.open_connection()
                try:
                    cursor = await connection.execute("SELECT status, COUNT(*) FROM worker_job_runs GROUP BY status")
                    job_status = {row[0]: row[1] for row in await cursor.fetchall()}
                    evidence["native_worker_statuses"] = job_status
                    assert job_status.get("succeeded", 0) > 0, "No native job completed"
                    assert set(job_status) <= {"succeeded", "skipped"}, "Native jobs failed or remain pending"
                finally:
                    await connection.close()

                async def retrieve(namespace):
                    # A new conversation has no copied source transcript. Native
                    # retrieval reads only real worker-produced memory objects.
                    await ensure(namespace, 2)
                    connection = await runtime.open_connection()
                    try:
                        result = await ContextCacheService(runtime).resolve_with_connection(
                            connection, user_id=namespace.provider_user_id,
                            conversation_id=namespace.provider_conversation_id(2),
                            message_text="What is my favorite hobby?", assistant_mode_id="personal_assistant",
                            ablation=AblationConfig(skip_need_detection=True,
                                skip_applicability_scoring=True, disable_context_cache=True))
                        composed = result.composed_context
                        return "\n".join(str(getattr(composed, field, "")) for field in
                            ("contract_block", "workspace_block", "memory_block", "state_block")).lower()
                    finally:
                        await connection.close()

                before = await source_counts(runtime)
                evidence["source_counts"] = before
                evidence["phase"] = "scoped_read_only_retrieval"
                calls_before = budget.requests
                shared_text = await retrieve(shared_b)
                private_text = await retrieve(private_a)
                assert "watercolor" in shared_text, "Shared fact was not extracted and recalled"
                assert "chess" in private_text, "Private control fact was not extracted and recalled"
                assert "chess" not in shared_text and "watercolor" not in private_text
                for namespace in (private_b, replace(shared_b, app_id="other-app"),
                                  replace(shared_b, context_id="other-context"),
                                  replace(shared_b, subject="beneficiary:other")):
                    text = await retrieve(namespace)
                    assert "watercolor" not in text and "chess" not in text, "Namespace leak"
                assert await source_counts(runtime) == before, "Retrieval ingested messages or jobs"
                assert budget.requests == calls_before, "Bounded retrieval unexpectedly called a provider"
                assert not budget.denied
                evidence["phase"] = "completed"
                return {"acceptance": "passed", "real_extraction": True,
                        "native_worker_statuses": job_status, "source_counts": before,
                        "shared_recall": True, "private_app_context_subject_isolation": True,
                        "read_only_retrieval": True, "semantic_ranking": "disabled by explicit ablation",
                        "final_answer_generation": "not exercised"}
            finally:
                # Close admission first: shutdown cannot start another paid call.
                budget.closed = True
                await engine.close()
    finally:
        atagia_app.build_llm_client = original_factory
        await sdk.close()


def positive_decimal(value):
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Price must be a decimal number") from None
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("Price must be finite and positive")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true", help="Explicitly permit capped provider I/O")
    parser.add_argument("--self-check", action="store_true", help="Validate budget guards without network")
    parser.add_argument("--model", choices=MODELS)
    parser.add_argument("--key-name", default="OPENAI_API_KEY", help="Key name in Aurvek .env; never a key value")
    parser.add_argument("--input-price-per-million", type=positive_decimal)
    parser.add_argument("--output-price-per-million", type=positive_decimal)
    args = parser.parse_args()
    if args.self_check:
        if args.run_real:
            parser.error("--self-check cannot be combined with --run-real")
        asyncio.run(self_check())
        return 0
    plan = {"mode": "real" if args.run_real else "preparation-only", "model": args.model,
            "hard_limits": {"requests": MAX_REQUESTS, "estimated_input_tokens": MAX_ESTIMATED_INPUT,
                            "output_tokens_per_request": MAX_OUTPUT, "cost_usd": str(MAX_COST)},
            "synthetic_sources": 2, "temporary_sqlite": True,
            "retrieval": "native; need detection and LLM applicability scoring disabled"}
    print(json.dumps(plan))
    if not args.run_real:
        return 0
    if sys.version_info < (3, 12):
        parser.error("Real Atagia requires Python 3.12 or newer")
    if not args.model or args.input_price_per_million is None or args.output_price_per_million is None:
        parser.error("Real runs require an explicit model and both per-million USD prices")
    budget = Budget(args.input_price_per_million, args.output_price_per_million)
    evidence = {"phase": "imports"}
    try:
        result = asyncio.run(run_real(args, budget, evidence))
    except BaseException as exc:
        # Never print exception text, requests, model output, environment, or keys.
        print(json.dumps({"acceptance": "failed", "error_type": type(exc).__name__,
                          **evidence, **budget.summary()}))
        return 1
    print(json.dumps({**result, **budget.summary()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
