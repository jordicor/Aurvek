import asyncio
from types import SimpleNamespace

import pytest

from ai_runtime.providers import openai_realtime_phone as provider
from integrations.telephony.openai_realtime import OpenAIRealtimeUsage
from integrations.telephony.realtime_bridge import (
    RealtimeDoneEvent,
    RealtimeStatusEvent,
    RealtimeToolCallEvent,
    RealtimeTranscriptEvent,
    RealtimeUsage,
)


class FakeBridge:
    _aurvek_internal_realtime_bridge = True

    def __init__(self, event_batches):
        self.event_batches = list(event_batches)
        self.started = None
        self.continuations = []
        self.usage_uncertain_handler = None

    def set_usage_uncertain_handler(self, handler):
        self.usage_uncertain_handler = handler

    async def start_turn(self, messages, **kwargs):
        self.started = (messages, kwargs)

    async def runtime_events(self):
        for event in self.event_batches.pop(0):
            yield event

    async def continue_function_call(self, call_id, output):
        self.continuations.append((call_id, output))


def raw_usage(input_tokens=3, output_tokens=2):
    return OpenAIRealtimeUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_input_tokens=0,
        text_input_tokens=0,
        audio_input_tokens=input_tokens,
        text_output_tokens=0,
        audio_output_tokens=output_tokens,
        reasoning_output_tokens=0,
    )


@pytest.mark.asyncio
async def test_provider_leaves_tool_to_runtime_then_continues_and_saves(monkeypatch):
    bridge = FakeBridge(
        [
            [
                RealtimeToolCallEvent("call-1", "lookup", '{"q":"x"}', "r1", "i1"),
                RealtimeStatusEvent(
                    "r1", "completed", RealtimeUsage(), raw_usage()
                ),
            ],
            [
                RealtimeTranscriptEvent("Result", "r2", "i2", "audio_transcript"),
                RealtimeStatusEvent(
                    "r2", "completed", RealtimeUsage(), raw_usage(8, 2)
                ),
                RealtimeDoneEvent(
                    "Result",
                    RealtimeUsage(input_tokens=11, output_tokens=4, total_tokens=15),
                    "completed",
                ),
            ],
        ]
    )
    bridge.item_id = "input-item-1"
    bridge.captured_input_pcmu_bytes = 800
    bridge.transcription_usage = None
    saved = {}
    billed = []

    async def fake_save(*args, **kwargs):
        saved["args"] = args
        saved["kwargs"] = kwargs
        return 101, 102

    async def noop(*args, **kwargs):
        return None

    async def fake_bill(**kwargs):
        billed.append(kwargs)
        return 0, 0, 0.0

    monkeypatch.setattr(provider, "save_content_to_db", fake_save)
    monkeypatch.setattr(provider, "record_provider_success_for_label", noop)
    monkeypatch.setattr(provider, "record_provider_error_for_label", noop)
    monkeypatch.setattr(
        provider, "accumulate_openai_realtime_response_usage", fake_bill
    )

    first_chunks = [
        chunk
        async for chunk in provider.call_openai_realtime_phone_api(
            [{"role": "user", "content": "hello"}],
            "gpt-realtime-2.1-mini",
            0,
            500,
            "Prompt instructions",
            77,
            SimpleNamespace(id=9),
            None,
            user_message="hello",
            user_api_key="key",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {}},
                }
            ],
            realtime_bridge=bridge,
            billing_reservation_id="reservation-1",
        )
    ]
    assert any('"tool_call"' in chunk for chunk in first_chunks)
    assert not saved
    assert bridge.started[1]["instructions"].startswith("Prompt instructions\n\n")
    assert "without speaking a preamble" in bridge.started[1]["instructions"]
    assert "answer naturally in speech" in bridge.started[1]["instructions"]
    assert bridge.started[1]["reasoning_effort"] == "minimal"
    assert bridge.started[1]["tools"] == [
        {"type": "function", "name": "lookup", "description": "", "parameters": {}}
    ]

    second_chunks = [
        chunk
        async for chunk in provider.call_openai_realtime_phone_api(
            [
                {"role": "user", "content": "hello"},
                {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call-1", "output": '{"answer":42}'},
            ],
            "gpt-realtime-2.1-mini",
            0,
            500,
            "Prompt instructions",
            77,
            SimpleNamespace(id=9),
            None,
            user_message="hello",
            user_api_key="key",
            realtime_bridge=bridge,
            billing_reservation_id="reservation-1",
        )
    ]
    assert bridge.continuations == [("call-1", '{"answer":42}')]
    assert any('"content":"Result"' in chunk for chunk in second_chunks)
    assert saved["args"][:7] == ("Result", 11, 4, 15, 77, 9, "gpt-realtime-2.1-mini")
    assert saved["kwargs"]["user_message"] == "hello"
    assert [entry["response_id"] for entry in billed] == ["r1", "r2"]
    assert all(entry["reservation_id"] == "reservation-1" for entry in billed)
    assert bridge.usage_uncertain_handler is not None
    assert second_chunks[-1] == "Result"


@pytest.mark.asyncio
async def test_provider_without_database_finishes_with_token_info(monkeypatch):
    bridge = FakeBridge(
        [
            [
                RealtimeTranscriptEvent("Hi", "r1", "i1", "audio_transcript"),
                RealtimeDoneEvent(
                    "Hi",
                    RealtimeUsage(input_tokens=2, output_tokens=1, total_tokens=3),
                    "completed",
                ),
            ],
        ]
    )

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(provider, "record_provider_success_for_label", noop)
    chunks = [
        chunk
        async for chunk in provider.call_openai_realtime_phone_api(
            [{"role": "user", "content": "hello"}],
            "gpt-realtime-2.1-mini",
            0,
            100,
            "",
            1,
            SimpleNamespace(id=2),
            None,
            user_api_key="key",
            save_to_db=False,
            realtime_bridge=bridge,
        )
    ]
    assert any('"token_info":true' in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_billing_failure_marks_usage_uncertain_before_ack(monkeypatch):
    accounting_done = asyncio.Event()
    bridge = FakeBridge(
        [[
            RealtimeStatusEvent(
                "r1",
                "cancelled",
                RealtimeUsage(),
                raw_usage(),
                accounting_done,
            ),
            RealtimeDoneEvent("", RealtimeUsage(), "cancelled"),
        ]]
    )
    uncertain = []

    async def fail_bill(**_kwargs):
        raise RuntimeError("billing unavailable")

    async def mark_uncertain(**kwargs):
        uncertain.append(kwargs)
        return True

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        provider, "accumulate_openai_realtime_response_usage", fail_bill
    )
    monkeypatch.setattr(
        provider, "mark_openai_realtime_usage_uncertain", mark_uncertain
    )
    monkeypatch.setattr(provider, "record_provider_error_for_label", noop)

    chunks = [
        chunk
        async for chunk in provider.call_openai_realtime_phone_api(
            [{"role": "user", "content": "hello"}],
            "gpt-realtime-2.1-mini",
            0,
            100,
            "",
            1,
            SimpleNamespace(id=2),
            None,
            user_api_key="key",
            realtime_bridge=bridge,
            billing_reservation_id="reservation-uncertain",
        )
    ]

    assert accounting_done.is_set()
    assert uncertain == [
        {"reservation_id": "reservation-uncertain", "user_id": 2}
    ]
    assert any('"error"' in chunk for chunk in chunks)
