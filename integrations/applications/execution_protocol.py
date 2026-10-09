"""Bounded text wire protocols and content-free provider usage observations."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import unquote, urlsplit


def endpoint(url: str) -> tuple[str, str, str | None]:
    """Identify supported HTTPS endpoints; return provider, protocol, path model."""
    target = urlsplit(url)
    if target.scheme != "https" or target.username or target.password or target.port not in (None, 443):
        raise ValueError("Unsupported provider endpoint")
    routes = {
        ("api.openai.com", "/v1/chat/completions"): ("openai", "chat"),
        ("api.openai.com", "/v1/responses"): ("openai", "responses"),
        ("api.anthropic.com", "/v1/messages"): ("anthropic", "anthropic"),
        ("api.x.ai", "/v1/chat/completions"): ("xai", "chat"),
        ("api.x.ai", "/v1/responses"): ("xai", "responses"),
        ("openrouter.ai", "/api/v1/chat/completions"): ("openrouter", "chat"),
    }
    route = routes.get((target.hostname, target.path))
    if route:
        return *route, None
    if target.hostname == "generativelanguage.googleapis.com":
        for prefix in ("/v1beta/models/", "/v1/models/"):
            if target.path.startswith(prefix):
                model, separator, action = unquote(target.path[len(prefix):]).rpartition(":")
                if separator and model and "/" not in model and action in {"generateContent", "streamGenerateContent"}:
                    return "gemini", "gemini_stream" if action == "streamGenerateContent" else "gemini", model
    raise ValueError("Unsupported provider endpoint")


def describe(url: str, payload: dict) -> dict:
    """Reject unpriced capabilities and bound serialized text before dispatch."""
    provider, protocol, path_model = endpoint(url)
    model = path_model or payload.get("model")
    if not isinstance(model, str) or not 1 <= len(model) <= 128:
        raise ValueError("An explicit model is required")
    if protocol.startswith("gemini"):
        config = payload.get("generationConfig") or {}
        maximum = config.get("maxOutputTokens")
        if (config.get("candidateCount", 1) != 1 or config.get("responseModalities", ["TEXT"]) != ["TEXT"]
                or "cachedContent" in payload):
            raise ValueError("Only one text completion without external cache storage is supported")
        messages = payload.get("contents")
        for message in [*(messages or []), payload.get("systemInstruction", {})]:
            for part in message.get("parts", []):
                if set(part) - {"text", "thought", "thoughtSignature", "functionCall", "functionResponse"}:
                    raise ValueError("Multimodal input is outside text coverage")
        if any(set(tool) - {"functionDeclarations"} for tool in payload.get("tools", [])):
            raise ValueError("Hosted tools require a separate tariff")
    else:
        maximum = payload.get("max_completion_tokens", payload.get("max_tokens", payload.get("max_output_tokens")))
        messages = payload.get("input") if protocol == "responses" else payload.get("messages")
        if (payload.get("n", 1) != 1 or any(key in payload for key in (
                "audio", "modalities", "web_search_options", "models", "previous_response_id"))):
            raise ValueError("Only one explicit text completion is supported")
        if any(plugin.get("enabled") is not False for plugin in payload.get("plugins", [])):
            raise ValueError("Active gateway plugins require a separate tariff")
        standard_tier = "standard_only" if provider == "anthropic" else "default"
        if payload.get("service_tier", standard_tier) != standard_tier or payload.get("prompt_cache_retention") == "24h":
            raise ValueError("Special service tiers and cache storage require a separate tariff")
        allowed_parts = {"text", "input_text", "output_text", "thinking", "redacted_thinking", "tool_use", "tool_result"}

        def check_content(content):
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") not in allowed_parts:
                        raise ValueError("Multimodal input is outside text coverage")
                    if part.get("type") == "tool_result":
                        check_content(part.get("content"))
            elif content is not None and not isinstance(content, str):
                raise ValueError("Unsupported text content")
        for message in messages or []:
            if not isinstance(message, dict):
                raise ValueError("Structured text messages are required")  # noqa: TRY004 -- invalid wire payload
            if protocol == "responses" and message.get("type", "message") not in {
                    "message", "function_call", "function_call_output", "reasoning"}:
                raise ValueError("Unsupported response input item")
            check_content(message.get("content"))
        check_content(payload.get("system"))
        for tool in payload.get("tools", []):
            if tool.get("type", "custom" if protocol == "anthropic" else "") not in {"function", "custom"}:
                raise ValueError("Hosted tools require a separate tariff")
        if protocol == "chat" and payload.get("stream") and payload.get("stream_options", {}).get("include_usage") is not True:
            raise ValueError("Streaming usage is required")
    if not isinstance(messages, list) or not messages or type(maximum) is not int or not 0 < maximum <= 2_000_000_000:
        raise ValueError("Text messages and an explicit output bound are required")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"provider": provider, "model": model, "input_bound": len(encoded) + 1024 + 64 * len(messages),
            "output_bound": maximum, "fingerprint": hashlib.sha256(encoded).hexdigest()}


def constrain_routing(payload: dict, limits: dict) -> None:
    """Apply authority-issued gateway ceilings without hidden retry or fallback routes."""
    identity = f"openrouter:{payload.get('model')}"
    if identity not in limits:
        raise ValueError("OpenRouter model has no frozen routing ceiling")
    routing = dict(payload.get("provider") or {})
    requested = routing.get("max_price") or {}
    routing["max_price"] = {key: min(value, requested.get(key, value)) for key, value in limits[identity].items()}
    routing.update(require_parameters=True, allow_fallbacks=False)
    payload["provider"] = routing


def usage_evidence(value: dict, protocol: str) -> dict:
    """Keep raw usage categories only; the SaaS owns interpretation and pricing."""
    usage = value.get("usageMetadata" if protocol.startswith("gemini") else "usage")
    if not isinstance(usage, dict):
        raise ValueError("Provider usage is missing")  # noqa: TRY004 -- incomplete provider evidence
    fields = ({"promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount", "thoughtsTokenCount",
               "totalTokenCount", "toolUsePromptTokenCount", "promptTokensDetails", "candidatesTokensDetails"}
              if protocol.startswith("gemini") else
              {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
               "cache_creation", "server_tool_use", "input_tokens_details", "output_tokens_details"}
              if protocol in {"anthropic", "responses"} else
              {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_tokens_details",
               "completion_tokens_details", "cost", "cost_details", "is_byok"})
    return {"response_id": str(value.get("responseId", value.get("id")) or "")[:128],
            "usage": {key: item for key, item in usage.items() if key in fields}}


class UsageStream:
    """Observe fragmented SSE, cumulative usage, and each protocol's terminal event."""

    def __init__(self, protocol: str):
        self.protocol = protocol
        self.buffer = b""
        self.evidence = None
        self.complete = False
        self.finish_seen = False
        self.event_type = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        if len(self.buffer) > 16 * 1024 * 1024:
            raise ValueError("Provider accounting response exceeds its limit")
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"event:"):
                self.event_type = line[6:].strip().decode("ascii")
                continue
            if not line:
                self.event_type = None
                continue
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                if self.protocol == "chat":
                    self.complete = True
                continue
            if not data:
                continue
            value = json.loads(data)
            kind = value.get("type", self.event_type)
            if value.get("error") or kind in {"error", "response.failed"}:
                raise ValueError("Provider stream failed before completion")
            if self.protocol == "anthropic":
                if kind == "message_start":
                    self.evidence = usage_evidence(value["message"], self.protocol)
                elif kind == "message_delta" and isinstance(value.get("usage"), dict) and self.evidence is not None:
                    self.evidence["usage"].update(usage_evidence(value, self.protocol)["usage"])
                elif kind == "message_stop":
                    self.complete = True
            elif self.protocol.startswith("gemini"):
                self.finish_seen |= any(item.get("finishReason") for item in value.get("candidates", []))
                if value.get("usageMetadata") is not None:
                    self.evidence = usage_evidence(value, self.protocol)
            elif self.protocol == "responses":
                if kind in {"response.completed", "response.incomplete"}:
                    self.evidence = usage_evidence(value["response"], self.protocol)
                    self.complete = True
            elif value.get("usage") is not None:
                self.evidence = usage_evidence(value, self.protocol)

    def finish(self) -> None:
        """Gemini ends at clean HTTP EOF, after a candidate finish and measured usage."""
        if self.protocol.startswith("gemini"):
            self.complete = self.finish_seen and not self.buffer.strip()


def rejected_evidence(status: int) -> dict | None:
    """Definite admission rejection has no usage; server failure is unknown."""
    return {"response_id": f"http-{status}", "usage": {"rejected": status}} if status in {400, 401, 403, 404, 422, 429} else None
