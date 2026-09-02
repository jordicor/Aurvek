from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest

from ai_runtime.channel_turns import ChannelContext, bind_channel_turn
from ai_runtime.tooling import execution
from ai_runtime.watchdog import takeover as watchdog_takeover
from integrations.telephony.foreground import ForegroundCommitGuard
from integrations.telephony.phone_context import create_phone_channel_turn
from integrations.telephony.tooling import (
    CallStartController,
    PHONE_END_CALL_TOOL,
    phone_tools_for_context,
)


def _phone_turn(*, openai_realtime_bridge=None):
    return create_phone_channel_turn(
        ForegroundCommitGuard(
            conversation_id=7,
            epoch=2,
            expected_owner="phone",
            call_id="call-tool",
            lease_owner="media-tool",
        ),
        turn_id="turn-tool",
        openai_realtime_bridge=openai_realtime_bridge,
    )


class _PendingRealtimeBridge:
    _aurvek_internal_realtime_bridge = True

    def __init__(self):
        self.finish_pending_output = AsyncMock(return_value=True)


class _TakeoverUser:
    id = 4

    @property
    async def is_admin(self):
        return False

    @property
    async def is_user(self):
        return True


def _patch_takeover(monkeypatch, provider):
    async def get_llm(_llm_id):
        return {
            "machine": "Kimi",
            "model": "kimi-test",
            "max_output_tokens": 100,
            "input_token_cost": 0,
            "output_token_cost": 0,
        }

    async def empty_blocks():
        return []

    async def empty_messages(*_args, **_kwargs):
        return []

    async def system_mode(_user_id):
        return "system"

    async def reserve(**_kwargs):
        return 100, None

    async def finalize(*_args, **_kwargs):
        return None

    monkeypatch.setattr(watchdog_takeover, "get_llm_info", get_llm)
    monkeypatch.setattr(watchdog_takeover, "get_effective_blocks", empty_blocks)
    monkeypatch.setattr(
        watchdog_takeover, "_format_messages_for_provider", empty_messages
    )
    monkeypatch.setattr(watchdog_takeover, "get_user_api_key_mode", system_mode)
    monkeypatch.setattr(
        watchdog_takeover,
        "resolve_api_key_for_provider",
        lambda *_args: ("synthetic-key", False),
    )
    monkeypatch.setattr(
        watchdog_takeover,
        "assert_billable_claude_system_key",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(watchdog_takeover, "_reserve_takeover_usage", reserve)
    monkeypatch.setattr(watchdog_takeover, "call_kimi_api", provider)
    monkeypatch.setattr("tools.watchdog._finalize_takeover", finalize)


async def _collect_takeover():
    return [
        chunk
        async for chunk in watchdog_takeover.watchdog_takeover_response(
            conversation_id=7,
            prompt_id=2,
            user_id=4,
            watchdog_config={"llm_id": 99},
            original_prompt="original",
            directive="respond naturally",
            context_messages=[],
            user_message="latest message",
            message="latest message",
            should_lock=False,
            current_user=_TakeoverUser(),
            request=None,
            user_api_keys={},
            machine="Kimi",
            model="kimi-test",
        )
    ]


@pytest.mark.asyncio
async def test_takeover_settlement_never_refunds_observed_or_unsettled_usage(
    monkeypatch,
):
    settlement = AsyncMock(
        side_effect=watchdog_takeover.BillingReservationError("temporary failure")
    )
    refund = AsyncMock()
    monkeypatch.setattr(
        watchdog_takeover,
        "settle_accumulated_ai_reservation_usage",
        settlement,
    )
    monkeypatch.setattr(watchdog_takeover, "refund_fixed_usage", refund)

    values = dict(
        reservation_id="takeover-reservation",
        user_id=4,
        input_cost_per_million=2.0,
        output_cost_per_million=8.0,
        prompt_id=2,
        byok=False,
    )
    await watchdog_takeover._settle_or_refund_takeover_reservation(**values)
    refund.assert_not_awaited()

    settlement.side_effect = None
    settlement.return_value = False
    await watchdog_takeover._settle_or_refund_takeover_reservation(
        **values,
        provider_usage_observed=True,
    )
    refund.assert_not_awaited()

    await watchdog_takeover._settle_or_refund_takeover_reservation(**values)
    refund.assert_awaited_once_with("takeover-reservation")


@pytest.mark.asyncio
async def test_requestfree_takeover_accumulates_usage_before_caller_persistence(
    monkeypatch,
):
    async def get_llm(_llm_id):
        return {
            "machine": "Kimi",
            "model": "kimi-test",
            "max_output_tokens": 100,
            "input_token_cost": 2.0,
            "output_token_cost": 8.0,
        }

    async def provider(**_kwargs):
        yield 'data: {"content":"safe answer"}\n\n'
        yield (
            'data: {"token_info":true,"input_tokens":12,'
            '"output_tokens":3}\n\n'
        )

    async def reserve(**_kwargs):
        return 100, "takeover-requestfree"

    accumulation = AsyncMock()
    _patch_takeover(monkeypatch, provider)
    monkeypatch.setattr(watchdog_takeover, "get_llm_info", get_llm)
    monkeypatch.setattr(watchdog_takeover, "_reserve_takeover_usage", reserve)
    monkeypatch.setattr(
        watchdog_takeover,
        "accumulate_ai_reservation_usage",
        accumulation,
    )
    monkeypatch.setattr(
        watchdog_takeover,
        "resolve_api_key_for_provider",
        lambda *_args: (None, True),
    )
    monkeypatch.setattr(
        "tools.watchdog._read_user_api_keys",
        AsyncMock(return_value={}),
    )
    billing_context = {}

    chunks = [
        chunk
        async for chunk in watchdog_takeover.watchdog_takeover_response_requestfree(
            directive="respond naturally",
            watchdog_config={"llm_id": 99},
            context_messages=[{"type": "user", "message": "latest"}],
            user_id=4,
            conversation_id=7,
            prompt_id=2,
            original_prompt="original",
            billing_context=billing_context,
        )
    ]

    assert len(chunks) == 2
    accumulation.assert_awaited_once_with(
        reservation_id="takeover-requestfree",
        user_id=4,
        input_tokens=12,
        output_tokens=3,
        component={
            "input_tokens": 12,
            "output_tokens": 3,
            "input_cost_per_million": 2.0,
            "output_cost_per_million": 8.0,
            "prompt_id": 2,
            "byok": False,
            "idempotency_key": "watchdog-requestfree:takeover-requestfree",
        },
    )
    assert billing_context["usage_accumulated"] is True
    assert billing_context["provider_usage_observed"] is True
    assert billing_context["input_tokens"] == 12
    assert billing_context["output_tokens"] == 3


def test_end_call_tool_is_exposed_only_to_phone_contexts():
    phone_turn = _phone_turn()

    assert phone_tools_for_context(phone_turn.context) == [PHONE_END_CALL_TOOL]
    assert phone_tools_for_context(ChannelContext(channel="web")) == []
    assert phone_tools_for_context(None) == []
    assert PHONE_END_CALL_TOOL["function"]["parameters"]["required"] == [
        "final_message"
    ]


def test_start_call_tool_is_scoped_to_the_prompt_mode_and_current_turn():
    on_request = CallStartController("on_request")
    proactive = CallStartController("proactive")
    requested_tools = phone_tools_for_context(
        ChannelContext(
            channel="whatsapp",
            provenance={"call_start_controller": on_request},
        )
    )
    proactive_tools = phone_tools_for_context(
        ChannelContext(
            channel="telegram",
            provenance={"call_start_controller": proactive},
        )
    )

    assert requested_tools[0]["function"]["name"] == "start_phone_call"
    assert "explicitly asks" in requested_tools[0]["function"]["description"]
    assert "current user turn" in proactive_tools[0]["function"]["description"]
    assert phone_tools_for_context(ChannelContext(channel="web")) == []


@pytest.mark.asyncio
async def test_end_call_tool_persists_farewell_and_arms_hangup_after_audio(monkeypatch):
    phone_turn = _phone_turn()
    saved = []

    async def save_content(*args, **kwargs):
        saved.append((args, kwargs))
        return 501, 502

    monkeypatch.setattr(execution, "save_content_to_db", save_content)
    user = SimpleNamespace(id=4)
    chunks = []

    with bind_channel_turn(phone_turn.context, handle=SimpleNamespace()):
        async for chunk in execution.handle_function_call(
            "end_call",
            {"final_message": "Thanks for calling. Talk soon!"},
            [],
            "model",
            0.7,
            1000,
            "",
            7,
            user,
            None,
            10,
            5,
            15,
            None,
            4,
            "GPT",
            "prompt",
            user_message="bye",
        ):
            chunks.append(chunk)

    payloads = [
        orjson.loads(chunk[6:].strip())
        for chunk in chunks
        if isinstance(chunk, str) and chunk.startswith("data: ")
    ]
    assert payloads[0] == {
        "content": "Thanks for calling. Talk soon!",
        "action": "phone_end_call_requested",
    }
    assert payloads[-1] == {"message_ids": {"user": 501, "bot": 502}}
    assert saved[0][0][0] == "Thanks for calling. Talk soon!"
    assert phone_turn.end_controller.pending.final_message == (
        "Thanks for calling. Talk soon!"
    )
    # The media session, not the tool, consumes this only after a playback mark.
    assert phone_turn.end_controller.audio_confirmed().final_message.endswith("soon!")


@pytest.mark.asyncio
async def test_realtime_end_call_returns_result_to_same_provider_before_hangup(
    monkeypatch,
):
    phone_turn = _phone_turn()
    provider_calls = []
    direct_saves = []

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        yield 'data: {"content":"Thanks for calling. Talk soon!"}\n\n'
        yield 'data: {"message_ids":{"user":801,"bot":802}}\n\n'
        yield "Thanks for calling. Talk soon!"

    async def save_content(*args, **kwargs):
        direct_saves.append((args, kwargs))
        return 701, 702

    async def billing_ready(*_args, **_kwargs):
        return True

    monkeypatch.setattr(execution, "save_content_to_db", save_content)
    monkeypatch.setattr(execution, "revalidate_user_billing", billing_ready)
    messages = []
    chunks = []
    with bind_channel_turn(phone_turn.context, handle=SimpleNamespace()):
        async for chunk in execution.handle_function_call(
            "end_call",
            {"final_message": "Thanks for calling. Talk soon!"},
            messages,
            "gpt-realtime-2.1-mini",
            0.7,
            1000,
            "",
            7,
            SimpleNamespace(id=4),
            None,
            10,
            5,
            15,
            None,
            4,
            "GPT",
            "prompt",
            user_message="bye",
            tool_call={
                "id": "call-end",
                "name": "end_call",
                "arguments": {"final_message": "Thanks for calling. Talk soon!"},
            },
            api_func_override=provider,
        ):
            chunks.append(chunk)

    assert direct_saves == []
    assert provider_calls[0]["messages"] is messages
    function_output = messages[-1]
    assert function_output["type"] == "function_call_output"
    assert function_output["call_id"] == "call-end"
    result = orjson.loads(function_output["output"])
    assert result["status"] == "end_call_requested"
    assert result["final_message"] == "Thanks for calling. Talk soon!"
    assert phone_turn.end_controller.pending.final_message.endswith("soon!")
    assert not any("phone_end_call_requested" in chunk for chunk in chunks)
    assert any("Thanks for calling" in chunk for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_error", [False, True])
async def test_realtime_generic_tool_result_continues_through_provider(
    monkeypatch,
    tool_error,
):
    async def handler(*_args, **_kwargs):
        payload = {"content": "lookup failed", "is_error": True} if tool_error else {
            "content": "private lookup result"
        }
        yield f"data: {orjson.dumps(payload).decode()}\n\n"

    provider_calls = []

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        yield 'data: {"content":"Natural spoken answer"}\n\n'
        yield "Natural spoken answer"

    async def billing_ready(*_args, **_kwargs):
        return True

    monkeypatch.setitem(execution.function_handlers, "lookup_test", handler)
    monkeypatch.setattr(execution, "revalidate_user_billing", billing_ready)
    messages = []
    chunks = [
        chunk
        async for chunk in execution.handle_function_call(
            "lookup_test",
            {"query": "x"},
            messages,
            "gpt-realtime-2.1-mini",
            0.7,
            1000,
            "",
            7,
            SimpleNamespace(id=4),
            None,
            10,
            5,
            15,
            None,
            4,
            "GPT",
            "prompt",
            user_message="lookup",
            tool_call={
                "id": "call-lookup",
                "name": "lookup_test",
                "arguments": {"query": "x"},
            },
            api_func_override=provider,
        )
    ]

    assert len(provider_calls) == 1
    function_output = messages[-1]
    assert function_output["type"] == "function_call_output"
    assert function_output["call_id"] == "call-lookup"
    if tool_error:
        assert function_output["output"] == "Error: lookup failed"
    else:
        result = orjson.loads(function_output["output"])
        assert result == {"status": "success", "result": "private lookup result"}
    assert not any("private lookup result" in chunk for chunk in chunks)
    assert any("Natural spoken answer" in chunk for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extension_error", "billing_ready", "expected_error"),
    (
        (
            execution.InsufficientBalanceError("no balance"),
            True,
            "Insufficient balance for the tool follow-up",
        ),
        (None, False, "Insufficient balance"),
    ),
)
async def test_realtime_tool_billing_failure_finishes_pending_output(
    monkeypatch,
    extension_error,
    billing_ready,
    expected_error,
):
    async def handler(*_args, **_kwargs):
        yield 'data: {"content":"private result"}\n\n'

    provider_calls = []

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        yield 'data: {"content":"must not run"}\n\n'

    bridge = _PendingRealtimeBridge()
    phone_turn = _phone_turn(openai_realtime_bridge=bridge)
    extension = AsyncMock()
    if extension_error is not None:
        extension.side_effect = extension_error
    monkeypatch.setitem(execution.function_handlers, "billing_test", handler)
    monkeypatch.setattr(execution, "extend_ai_reservation", extension)
    monkeypatch.setattr(
        execution,
        "revalidate_user_billing",
        AsyncMock(return_value=billing_ready),
    )

    hold_amount = 0.25 if extension_error is not None else 0.0
    with bind_channel_turn(phone_turn.context, handle=SimpleNamespace()):
        chunks = [
            chunk
            async for chunk in execution.handle_function_call(
                "billing_test",
                {"query": "x"},
                [],
                "gpt-realtime-2.1-mini",
                0.7,
                1000,
                "",
                7,
                SimpleNamespace(id=4),
                None,
                10,
                5,
                15,
                None,
                4,
                "GPT",
                "prompt",
                billing_preflight_amount=0.1,
                billing_reservation_id="reservation-tool",
                billing_followup_hold_amount=hold_amount,
                tool_call={
                    "id": "call-billing",
                    "name": "billing_test",
                    "arguments": {"query": "x"},
                },
                api_func_override=provider,
            )
        ]

    assert provider_calls == []
    assert any(expected_error in chunk for chunk in chunks)
    bridge.finish_pending_output.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_realtime_at_field_extends_followup_reservation_once(monkeypatch):
    async def fake_at_field(*_args, **_kwargs):
        yield 'data: {"content":"safety review result"}\n\n'

    provider_calls = []

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        yield 'data: {"content":"Natural spoken follow-up"}\n\n'

    extension = AsyncMock()
    bridge = _PendingRealtimeBridge()
    phone_turn = _phone_turn(openai_realtime_bridge=bridge)
    monkeypatch.setattr(execution, "atFieldActivate", fake_at_field)
    monkeypatch.setattr(execution, "extend_ai_reservation", extension)
    monkeypatch.setattr(
        execution,
        "revalidate_user_billing",
        AsyncMock(return_value=True),
    )
    messages = [{"role": "user", "content": "suspicious input"}]

    with bind_channel_turn(phone_turn.context, handle=SimpleNamespace()):
        chunks = [
            chunk
            async for chunk in execution.handle_function_call(
                "atFieldActivate",
                {"text": "suspicious input"},
                messages,
                "gpt-realtime-2.1-mini",
                0.7,
                1000,
                "",
                7,
                SimpleNamespace(id=4),
                None,
                10,
                5,
                15,
                None,
                4,
                "GPT",
                "prompt",
                billing_reservation_id="reservation-at-field",
                billing_followup_hold_amount=0.25,
                tool_call={
                    "id": "call-at-field",
                    "name": "atFieldActivate",
                    "arguments": {"text": "suspicious input"},
                },
                api_func_override=provider,
            )
        ]

    extension.assert_awaited_once_with(
        reservation_id="reservation-at-field",
        user_id=4,
        additional_amount=0.25,
    )
    assert len(provider_calls) == 1
    assert any("Natural spoken follow-up" in chunk for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function_name", "arguments", "expected_status"),
    (
        ("dream_of_consciousness", {}, "error"),
        ("atFieldActivate", {}, "error"),
        ("start_phone_call", {"reply_message": "Call me"}, "error"),
        ("zipItDrEvil", {}, "error"),
        ("pass_turn", {"reason_code": "OTHER"}, "success"),
        ("advanceExtension", {"target_extension_id": "invalid"}, "error"),
        ("changeResponseMode", {}, "error"),
        (
            "get_directions",
            {"origin": "Miami", "destination": "Orlando"},
            "error",
        ),
    ),
)
async def test_realtime_explicit_tools_always_continue_same_socket(
    monkeypatch,
    function_name,
    arguments,
    expected_status,
):
    phone_turn = _phone_turn()
    provider_calls = []

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        yield 'data: {"content":"Natural spoken follow-up"}\n\n'
        yield "Natural spoken follow-up"

    async def billing_ready(*_args, **_kwargs):
        return True

    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    monkeypatch.setattr(execution, "revalidate_user_billing", billing_ready)
    messages = []
    with bind_channel_turn(phone_turn.context, handle=SimpleNamespace()):
        chunks = [
            chunk
            async for chunk in execution.handle_function_call(
                function_name,
                arguments,
                messages,
                "gpt-realtime-2.1-mini",
                0.7,
                1000,
                "",
                7,
                SimpleNamespace(id=4),
                None,
                10,
                5,
                15,
                None,
                4,
                "GPT",
                "prompt",
                user_message="tool request",
                tool_call={
                    "id": f"call-{function_name}",
                    "name": function_name,
                    "arguments": arguments,
                },
                api_func_override=provider,
            )
        ]

    assert len(provider_calls) == 1
    assert messages[-1]["type"] == "function_call_output"
    assert messages[-1]["call_id"] == f"call-{function_name}"
    result = orjson.loads(messages[-1]["output"])
    assert result["status"] == expected_status
    assert result["result"]
    assert any("Natural spoken follow-up" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_end_call_fails_closed_outside_phone_context():
    chunks = []
    with bind_channel_turn(ChannelContext(channel="web")):
        async for chunk in execution.handle_function_call(
            "end_call",
            {"final_message": "Not available here"},
            [],
            "model",
            0.7,
            1000,
            "",
            7,
            SimpleNamespace(id=4),
            None,
            1,
            1,
            2,
            None,
            4,
            "GPT",
            "prompt",
            user_message="bye",
        ):
            chunks.append(chunk)

    assert any("end_call is unavailable" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_start_call_saves_reply_before_the_controller_can_enqueue(monkeypatch):
    controller = CallStartController("on_request")
    context = ChannelContext(
        channel="whatsapp",
        provenance={"call_start_controller": controller},
    )
    saved = []

    async def save_content(*args, **kwargs):
        saved.append((args, kwargs))
        return 601, 602

    monkeypatch.setattr(execution, "save_content_to_db", save_content)
    chunks = []
    with bind_channel_turn(context):
        async for chunk in execution.handle_function_call(
            "start_phone_call",
            {"reply_message": "Claro, te llamo ahora."},
            [],
            "model",
            0.7,
            1000,
            "",
            7,
            SimpleNamespace(id=4),
            None,
            10,
            5,
            15,
            None,
            4,
            "GPT",
            "prompt",
            user_message="llamame",
        ):
            chunks.append(chunk)

    payloads = [
        orjson.loads(chunk[6:].strip())
        for chunk in chunks
        if isinstance(chunk, str) and chunk.startswith("data: ")
    ]
    assert payloads[0] == {
        "content": "Claro, te llamo ahora.",
        "action": "phone_call_requested",
    }
    assert payloads[-1] == {"message_ids": {"user": 601, "bot": 602}}
    assert saved[0][0][0] == "Claro, te llamo ahora."
    assert controller.directive.reply_message == "Claro, te llamo ahora."


@pytest.mark.asyncio
async def test_start_call_fails_closed_without_a_turn_capability():
    chunks = []
    with bind_channel_turn(ChannelContext(channel="web")):
        async for chunk in execution.handle_function_call(
            "start_phone_call",
            {"reply_message": "Te llamo."},
            [],
            "model",
            0.7,
            1000,
            "",
            7,
            SimpleNamespace(id=4),
            None,
            1,
            1,
            2,
            None,
            4,
            "GPT",
            "prompt",
            user_message="hola",
        ):
            chunks.append(chunk)

    assert any("start_phone_call is unavailable" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_phone_watchdog_takeover_can_end_only_the_bound_call(
    monkeypatch,
):
    phone_turn = _phone_turn()
    saved = []
    provider_tools = []

    async def provider(**kwargs):
        provider_tools.extend(kwargs.get("tools") or [])
        yield (
            'data: {"tool_call":{"name":"end_call","arguments":'
            '{"final_message":"Hasta pronto."},"id":"call-end"},'
            '"pre_tool_content":""}\n\n'
        )
        yield 'data: {"tool_call_pending":true}\n\n'

    async def save_content(*args, **kwargs):
        saved.append((args, kwargs))
        return 701, 702

    _patch_takeover(monkeypatch, provider)
    monkeypatch.setattr(execution, "save_content_to_db", save_content)

    with bind_channel_turn(phone_turn.context, handle=SimpleNamespace()):
        chunks = await _collect_takeover()

    assert provider_tools[0]["function"]["name"] == "end_call"
    assert phone_turn.end_controller.pending.final_message == "Hasta pronto."
    assert saved[0][0][0] == "Hasta pronto."
    assert any("phone_end_call_requested" in chunk for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["on_request", "proactive"])
async def test_non_phone_watchdog_takeover_uses_current_turn_call_outbox_controller(
    monkeypatch,
    mode,
):
    controller = CallStartController(mode)
    context = ChannelContext(
        channel="telegram",
        provenance={"call_start_controller": controller},
    )
    saved = []
    provider_tools = []

    async def provider(**kwargs):
        provider_tools.extend(kwargs.get("tools") or [])
        yield (
            'data: {"tool_call":{"name":"start_phone_call","arguments":'
            '{"reply_message":"Te llamo ahora."},"id":"call-start"},'
            '"pre_tool_content":""}\n\n'
        )

    async def save_content(*args, **kwargs):
        saved.append((args, kwargs))
        return 801, 802

    _patch_takeover(monkeypatch, provider)
    monkeypatch.setattr(execution, "save_content_to_db", save_content)

    with bind_channel_turn(context):
        chunks = await _collect_takeover()

    assert provider_tools[0]["function"]["name"] == "start_phone_call"
    assert controller.directive.reply_message == "Te llamo ahora."
    assert saved[0][0][0] == "Te llamo ahora."
    assert any("phone_call_requested" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_watchdog_takeover_without_current_capability_has_no_phone_tool(
    monkeypatch,
):
    captured = {}

    async def provider(**kwargs):
        captured.update(kwargs)
        yield 'data: {"content":"Sigo por aquí."}\n\n'

    _patch_takeover(monkeypatch, provider)
    with bind_channel_turn(ChannelContext(channel="web")):
        chunks = await _collect_takeover()

    assert "tools" not in captured
    assert chunks == ['data: {"content":"Sigo por aquí."}\n\n']
