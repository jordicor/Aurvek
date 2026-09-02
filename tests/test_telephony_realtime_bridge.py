import asyncio

import pytest

from integrations.telephony.openai_realtime import (
    OpenAIFunctionCallEvent,
    OpenAIOutputAudioEvent,
    OpenAIOutputTextEvent,
    OpenAIRealtimeUsage,
    OpenAIResponseDoneEvent,
)
from integrations.telephony.realtime_bridge import (
    RealtimeBridgeHandle,
    RealtimeDoneEvent,
    RealtimeErrorEvent,
    RealtimeStatusEvent,
    RealtimeToolCallEvent,
)


_CLOSE = object()


class FakeRealtimeClient:
    instances = []

    def __init__(self, *, api_key_provider, options):
        self.api_key_provider = api_key_provider
        self.options = options
        self.connected = False
        self.events_queue = asyncio.Queue()
        self.commands = []
        self.__class__.instances.append(self)

    async def connect(self):
        value = self.api_key_provider()
        if asyncio.iscoroutine(value):
            value = await value
        assert value == "secret"
        self.connected = True
        self.commands.append(("connect",))

    async def update_session(self, **kwargs):
        self.commands.append(("update_session", kwargs))

    async def create_conversation_item(self, text, *, role="user"):
        self.commands.append(("item", role, text))

    async def append_audio(self, chunk):
        self.commands.append(("audio", bytes(chunk)))

    async def commit_audio(self):
        self.commands.append(("commit",))

    async def create_response(self, instructions=None):
        self.commands.append(("response", instructions))

    async def send_function_output(self, call_id, output):
        self.commands.append(("tool_output", call_id, output))

    async def cancel_response(self, response_id=None):
        self.commands.append(("cancel", response_id))

    async def truncate_item(self, item_id, played_ms, *, content_index=0):
        self.commands.append(("truncate", item_id, played_ms, content_index))

    async def events(self):
        while True:
            event = await self.events_queue.get()
            if event is _CLOSE:
                return
            yield event

    async def close(self):
        self.connected = False
        await self.events_queue.put(_CLOSE)


def usage(input_tokens=5, output_tokens=3):
    return OpenAIRealtimeUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_input_tokens=0,
        text_input_tokens=1,
        audio_input_tokens=input_tokens - 1,
        text_output_tokens=1,
        audio_output_tokens=output_tokens - 1,
        reasoning_output_tokens=0,
    )


@pytest.mark.asyncio
async def test_handle_binds_lazily_and_bridge_routes_audio_and_transcript():
    FakeRealtimeClient.instances.clear()
    key_calls = 0

    def key_provider():
        nonlocal key_calls
        key_calls += 1
        return "secret"

    handle = RealtimeBridgeHandle(
        audio_input=[b"one", b"two"], client_factory=FakeRealtimeClient
    )
    assert handle.captured_input_pcmu_bytes == 6
    assert key_calls == 0
    bridge = await handle.bind_provider(
        api_key_provider=key_provider, model="gpt-realtime-2.1-mini"
    )
    assert key_calls == 0
    await bridge.start_turn(
        [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
            {"role": "user", "content": "current transcript is replaced by audio"},
        ],
        instructions="Be concise",
        reasoning_effort="minimal",
    )
    assert key_calls == 1
    client = FakeRealtimeClient.instances[-1]
    assert ("item", "user", "older") in client.commands
    assert ("item", "assistant", "answer") in client.commands
    assert not any("current transcript" in str(command) for command in client.commands)
    assert ("audio", b"one") in client.commands
    assert ("audio", b"two") in client.commands
    assert client.options.vad is None

    audio_task = asyncio.create_task(_collect(handle.output_pcmu()))
    event_task = asyncio.create_task(_collect(bridge.runtime_events()))
    await client.events_queue.put(
        OpenAIOutputAudioEvent("r1", "i1", 0, b"pcmu", False)
    )
    await client.events_queue.put(
        OpenAIOutputTextEvent("audio_transcript", "r1", "i1", 0, "hello", False)
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))

    assert await audio_task == [b"pcmu"]
    events = await event_task
    assert any(getattr(event, "text", None) == "hello" for event in events)
    done = next(event for event in events if isinstance(event, RealtimeDoneEvent))
    assert done.transcript == "hello"
    assert done.usage.total_tokens == 8
    await handle.close()


@pytest.mark.asyncio
async def test_provider_started_marker_precedes_every_response_create():
    FakeRealtimeClient.instances.clear()
    handle = RealtimeBridgeHandle(
        audio_input=b"caller", client_factory=FakeRealtimeClient
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1-mini"
    )
    client = FakeRealtimeClient.instances[-1]

    async def mark_started():
        client.commands.append(("provider_started",))

    bridge.set_usage_uncertain_handler(mark_started)
    await bridge.start_turn(
        [{"role": "user", "content": "use a tool"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    events = bridge.runtime_events()
    await client.events_queue.put(
        OpenAIFunctionCallEvent("r1", "i1", "c1", "lookup", "{}", True)
    )
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r1", "completed", usage())
    )
    assert isinstance(await anext(events), RealtimeToolCallEvent)
    status = await anext(events)
    assert isinstance(status, RealtimeStatusEvent)
    status.acknowledge_accounting()
    await bridge.continue_function_call("c1", {"value": 7})

    response_indexes = [
        index for index, command in enumerate(client.commands)
        if command[0] == "response"
    ]
    marker_indexes = [
        index for index, command in enumerate(client.commands)
        if command[0] == "provider_started"
    ]
    assert len(response_indexes) == len(marker_indexes) == 2
    assert all(
        marker_index < response_index
        for marker_index, response_index in zip(
            marker_indexes,
            response_indexes,
            strict=True,
        )
    )
    await handle.close()


@pytest.mark.asyncio
async def test_provider_marker_failure_never_sends_response_create():
    FakeRealtimeClient.instances.clear()
    handle = RealtimeBridgeHandle(
        audio_input=b"caller", client_factory=FakeRealtimeClient
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1-mini"
    )

    async def fail_marker():
        raise RuntimeError("billing marker unavailable")

    bridge.set_usage_uncertain_handler(fail_marker)
    with pytest.raises(RuntimeError, match="billing marker unavailable"):
        await bridge.start_turn([{"role": "user", "content": "hello"}])

    client = FakeRealtimeClient.instances[-1]
    assert not any(command[0] == "response" for command in client.commands)
    await handle.close()


@pytest.mark.asyncio
async def test_tool_continuation_waits_for_previous_response_done():
    FakeRealtimeClient.instances.clear()
    handle = RealtimeBridgeHandle(
        audio_input=b"caller", client_factory=FakeRealtimeClient
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1"
    )
    await bridge.start_turn(
        [{"role": "user", "content": "use a tool"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    client = FakeRealtimeClient.instances[-1]
    events = bridge.runtime_events()
    pending_event = asyncio.create_task(anext(events))
    await client.events_queue.put(
        OpenAIFunctionCallEvent("r1", "i1", "c1", "lookup", "{}", True)
    )
    call = await pending_event
    assert isinstance(call, RealtimeToolCallEvent)

    continuation = asyncio.create_task(
        bridge.continue_function_call("c1", {"value": 7})
    )
    await asyncio.sleep(0)
    assert not continuation.done()
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))
    await continuation
    assert ("tool_output", "c1", {"value": 7}) in client.commands
    assert client.commands[-1] == ("response", None)

    await client.events_queue.put(
        OpenAIOutputTextEvent("audio_transcript", "r2", "i2", 0, "seven", False)
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r2", "completed", usage(2, 2)))
    remaining = await _collect(events)
    done = next(event for event in remaining if isinstance(event, RealtimeDoneEvent))
    assert done.transcript == "seven"
    assert done.usage.total_tokens == 12
    await handle.close()


@pytest.mark.asyncio
async def test_response_timeout_cancels_provider_and_closes_both_streams():
    FakeRealtimeClient.instances.clear()
    handle = RealtimeBridgeHandle(
        audio_input=b"caller",
        client_factory=FakeRealtimeClient,
        response_timeout_seconds=0.02,
        cancel_accounting_timeout_seconds=0.02,
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1-mini"
    )
    await bridge.start_turn([{"role": "user", "content": "hello"}])
    client = FakeRealtimeClient.instances[-1]

    events, audio = await asyncio.gather(
        asyncio.wait_for(_collect(bridge.runtime_events()), timeout=1),
        asyncio.wait_for(_collect(handle.output_pcmu()), timeout=1),
    )

    error = next(event for event in events if isinstance(event, RealtimeErrorEvent))
    done = next(event for event in events if isinstance(event, RealtimeDoneEvent))
    assert error.code == "realtime_response_timeout"
    assert done.status == "failed"
    assert audio == []
    assert ("cancel", None) in client.commands
    await handle.close()


@pytest.mark.asyncio
async def test_timeout_marks_usage_uncertain_when_done_never_arrives():
    FakeRealtimeClient.instances.clear()
    uncertain = []
    handle = RealtimeBridgeHandle(
        audio_input=b"caller",
        client_factory=FakeRealtimeClient,
        response_timeout_seconds=0.02,
        cancel_accounting_timeout_seconds=0.01,
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1-mini"
    )
    bridge.set_usage_uncertain_handler(lambda: uncertain.append(True))
    await bridge.start_turn([{"role": "user", "content": "hello"}])

    await asyncio.wait_for(_collect(bridge.runtime_events()), timeout=1)

    # One durable mark precedes response.create; the idempotent second call
    # records that final usage never arrived.
    assert uncertain == [True, True]
    await handle.close()


@pytest.mark.asyncio
async def test_response_timeout_disarms_for_tool_and_rearms_for_continuation():
    FakeRealtimeClient.instances.clear()
    handle = RealtimeBridgeHandle(
        audio_input=b"caller",
        client_factory=FakeRealtimeClient,
        response_timeout_seconds=0.03,
        cancel_accounting_timeout_seconds=0.02,
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1"
    )
    await bridge.start_turn(
        [{"role": "user", "content": "use a tool"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    client = FakeRealtimeClient.instances[-1]
    events = bridge.runtime_events()

    await client.events_queue.put(
        OpenAIFunctionCallEvent("r1", "i1", "c1", "lookup", "{}", True)
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))
    assert isinstance(await anext(events), RealtimeToolCallEvent)
    assert isinstance(await anext(events), RealtimeStatusEvent)

    # Local tool execution is outside the provider response deadline.
    await asyncio.sleep(0.07)
    assert not any(command[0] == "cancel" for command in client.commands)

    await bridge.continue_function_call("c1", {"value": 7})
    remaining = await asyncio.wait_for(_collect(events), timeout=1)
    assert any(
        isinstance(event, RealtimeErrorEvent)
        and event.code == "realtime_response_timeout"
        for event in remaining
    )
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "failed"
        for event in remaining
    )
    assert sum(command[0] == "response" for command in client.commands) == 2
    assert ("cancel", None) in client.commands
    await handle.close()


@pytest.mark.asyncio
async def test_useful_progress_renews_response_inactivity_timeout():
    FakeRealtimeClient.instances.clear()
    handle = RealtimeBridgeHandle(
        audio_input=b"caller",
        client_factory=FakeRealtimeClient,
        response_timeout_seconds=0.03,
        cancel_accounting_timeout_seconds=0.01,
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1-mini"
    )
    await bridge.start_turn([{"role": "user", "content": "hello"}])
    client = FakeRealtimeClient.instances[-1]
    async def collect_and_account():
        observed = []
        async for event in bridge.runtime_events():
            observed.append(event)
            if isinstance(event, RealtimeStatusEvent):
                event.acknowledge_accounting()
        return observed

    events_task = asyncio.create_task(collect_and_account())
    audio_task = asyncio.create_task(_collect(handle.output_pcmu()))

    for index in range(3):
        await client.events_queue.put(
            OpenAIOutputAudioEvent("r1", "i1", 0, bytes([index + 1]), False)
        )
        while bridge._response_progress_generation < index + 1:
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)
    assert not any(command[0] == "cancel" for command in client.commands)

    await client.events_queue.put(
        OpenAIResponseDoneEvent("r1", "completed", usage())
    )
    events = await asyncio.wait_for(events_task, timeout=1)
    await asyncio.wait_for(audio_task, timeout=1)
    assert any(isinstance(event, RealtimeStatusEvent) for event in events)
    assert not any(command[0] == "cancel" for command in client.commands)
    await handle.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_done_accounting_before_terminal_end():
    FakeRealtimeClient.instances.clear()
    uncertain = []
    handle = RealtimeBridgeHandle(
        audio_input=b"caller",
        client_factory=FakeRealtimeClient,
        cancel_accounting_timeout_seconds=0.2,
    )
    bridge = await handle.bind_provider(
        api_key_provider=lambda: "secret", model="gpt-realtime-2.1-mini"
    )
    bridge.set_usage_uncertain_handler(lambda: uncertain.append(True))
    await bridge.start_turn([{"role": "user", "content": "hello"}])
    client = FakeRealtimeClient.instances[-1]
    observed = []

    async def account_events():
        async for event in bridge.runtime_events():
            observed.append(event)
            if isinstance(event, RealtimeStatusEvent):
                event.acknowledge_accounting()

    consumer = asyncio.create_task(account_events())
    cancel = asyncio.create_task(bridge.cancel_output())
    await asyncio.sleep(0)
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r1", "cancelled", usage(7, 1))
    )
    await asyncio.wait_for(cancel, timeout=1)
    await asyncio.wait_for(consumer, timeout=1)

    status_index = next(
        index for index, event in enumerate(observed)
        if isinstance(event, RealtimeStatusEvent)
    )
    done_index = next(
        index for index, event in enumerate(observed)
        if isinstance(event, RealtimeDoneEvent)
    )
    assert status_index < done_index
    assert observed[done_index].usage.total_tokens == 8
    # The only marker is the mandatory pre-provider fence; no uncertainty
    # fallback was needed after the accounted response.done.
    assert uncertain == [True]
    await handle.close()


async def _collect(iterator):
    return [item async for item in iterator]
