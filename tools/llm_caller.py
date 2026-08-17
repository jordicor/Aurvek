"""
Non-streaming LLM caller for background tasks (watchdog, future plugins).
Avoids circular imports with the streaming AI runtime.
Uses aiohttp direct HTTP calls to each provider.
"""

import logging
import os
import aiohttp
import orjson
from dataclasses import dataclass
from typing import Optional
from ai_runtime.providers.claude_capabilities import claude_omits_temperature

logger = logging.getLogger("llm_caller")

# API keys from environment (same env vars as common.py)
_openai_key = os.getenv("OPENAI_KEY", "")
_claude_key = os.getenv("ANTHROPIC_API_KEY", "")
_gemini_key = os.getenv("GEMINI_KEY", "")
_xai_key = os.getenv("XAI_KEY", "")
_openrouter_key = os.getenv("OPENROUTER_API_KEY", "")

# Provider timeout (seconds) for the aiohttp request
_DEFAULT_TIMEOUT = 30


@dataclass
class LLMCallResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


def extract_json_from_llm_response(text: str) -> Optional[dict]:
    """Extract a JSON object from an LLM response that may contain extra text.

    Handles markdown fences, preamble text, trailing notes, etc.
    Returns the parsed dict or None if no valid JSON found.
    """
    if not text:
        return None

    stripped = text.strip()
    start_idx = stripped.find("{")
    end_idx = stripped.rfind("}")

    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None

    json_str = stripped[start_idx:end_idx + 1]

    try:
        return orjson.loads(json_str)
    except orjson.JSONDecodeError:
        return None


async def call_llm_non_streaming_with_usage(
    machine: str,
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int = _DEFAULT_TIMEOUT,
    max_tokens: int = 500,
    api_key_override: Optional[str] = None,
) -> LLMCallResult:
    """Call any configured LLM provider and return text + token usage.

    Args:
        machine: Provider name as stored in LLM.machine (Claude, GPT, O1, Gemini, xAI, OpenRouter).
        model: Model identifier as stored in LLM.model.
        system_prompt: System-level instructions.
        user_message: The user/evaluation message to send.
        timeout: HTTP request timeout in seconds.
        max_tokens: Maximum tokens for the response.
        api_key_override: Optional BYOK API key. If None, uses system key.

    Returns:
        LLMCallResult with text and token usage.

    Raises:
        ValueError: If the provider is unsupported or its API key is missing.
        Exception: On HTTP/provider errors (caller should handle).
    """
    if machine == "Claude":
        return await _call_claude(
            model, system_prompt, user_message, timeout, max_tokens, api_key_override
        )
    elif machine in ("GPT", "O1"):
        return await _call_openai(
            model, system_prompt, user_message, timeout, max_tokens, api_key_override
        )
    elif machine == "Gemini":
        return await _call_gemini(
            model, system_prompt, user_message, timeout, max_tokens, api_key_override
        )
    elif machine == "xAI":
        return await _call_xai(
            model, system_prompt, user_message, timeout, max_tokens, api_key_override
        )
    elif machine == "OpenRouter":
        return await _call_openrouter(
            model, system_prompt, user_message, timeout, max_tokens, api_key_override
        )
    else:
        raise ValueError(f"Unsupported LLM provider: {machine}")


# ---------------------------------------------------------------------------
# Provider-specific implementations (non-streaming, aiohttp)
# ---------------------------------------------------------------------------

async def _call_claude(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_tokens: int,
    api_key_override: Optional[str],
) -> LLMCallResult:
    api_key = api_key_override or _claude_key
    if not api_key:
        raise ValueError("Anthropic API key not configured")

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    data = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }
    if not claude_omits_temperature(model):
        data["temperature"] = 0.0

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                content = result.get("content", [])
                usage = result.get("usage", {}) or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
                if content:
                    text = content[0].get("text", "")
                    return LLMCallResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
                return LLMCallResult("", input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
            error = await resp.text()
            raise Exception(f"Claude API error {resp.status}: {error[:300]}")


async def _call_openai(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_tokens: int,
    api_key_override: Optional[str],
) -> LLMCallResult:
    api_key = api_key_override or _openai_key
    if not api_key:
        raise ValueError("OpenAI API key not configured")

    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.0,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                choices = result.get("choices", [])
                usage = result.get("usage", {}) or {}
                input_tokens = int(usage.get("prompt_tokens") or 0)
                output_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
                if choices:
                    text = choices[0].get("message", {}).get("content", "")
                    return LLMCallResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
                return LLMCallResult("", input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
            error = await resp.text()
            raise Exception(f"OpenAI API error {resp.status}: {error[:300]}")


async def _call_gemini(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_tokens: int,
    api_key_override: Optional[str],
) -> LLMCallResult:
    api_key = api_key_override or _gemini_key
    if not api_key:
        raise ValueError("Gemini API key not configured")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={api_key}"
    )
    headers = {"Content-Type": "application/json"}
    combined_prompt = f"{system_prompt}\n\n{user_message}"
    data = {
        "contents": [{"parts": [{"text": combined_prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens},
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                candidates = result.get("candidates", [])
                usage = result.get("usageMetadata", {}) or {}
                input_tokens = int(usage.get("promptTokenCount") or 0)
                output_tokens = int(usage.get("candidatesTokenCount") or 0)
                total_tokens = int(usage.get("totalTokenCount") or (input_tokens + output_tokens))
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        text = parts[0].get("text", "")
                        return LLMCallResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
                return LLMCallResult("", input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
            error = await resp.text()
            raise Exception(f"Gemini API error {resp.status}: {error[:300]}")


async def _call_xai(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_tokens: int,
    api_key_override: Optional[str],
) -> LLMCallResult:
    api_key = api_key_override or _xai_key
    if not api_key:
        raise ValueError("xAI API key not configured")

    url = "https://api.x.ai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.0,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                choices = result.get("choices", [])
                usage = result.get("usage", {}) or {}
                input_tokens = int(usage.get("prompt_tokens") or 0)
                output_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
                if choices:
                    text = choices[0].get("message", {}).get("content", "")
                    return LLMCallResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
                return LLMCallResult("", input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
            error = await resp.text()
            raise Exception(f"xAI API error {resp.status}: {error[:300]}")


async def _call_openrouter(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout: int,
    max_tokens: int,
    api_key_override: Optional[str],
) -> LLMCallResult:
    api_key = api_key_override or _openrouter_key
    if not api_key:
        raise ValueError("OpenRouter API key not configured")

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    data = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    if not claude_omits_temperature(model):
        data["temperature"] = 0.0

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status == 200:
                result = await resp.json()
                choices = result.get("choices", [])
                usage = result.get("usage", {}) or {}
                input_tokens = int(usage.get("prompt_tokens") or 0)
                output_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
                if choices:
                    text = choices[0].get("message", {}).get("content", "")
                    return LLMCallResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
                return LLMCallResult("", input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)
            error = await resp.text()
            raise Exception(f"OpenRouter API error {resp.status}: {error[:300]}")
