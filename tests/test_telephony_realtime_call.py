import asyncio

import pytest

from integrations.telephony.openai_realtime import (
    OpenAIFunctionCallEvent,
    OpenAIInputTranscriptFailedEvent,
    OpenAIInputTranscriptEvent,
    OpenAIOutputAudioEvent,
    OpenAIOutputTextEvent,
    OpenAIProviderErrorEvent,
    OpenAIRealtimeError,
    OpenAIRealtimeUsage,
    OpenAIResponseDoneEvent,
    OpenAISpeechEvent,
    SemanticVadOptions,
)
from integrations.telephony.realtime_bridge import (
    RealtimeDoneEvent,
    RealtimeErrorEvent,
    RealtimeStatusEvent,
    RealtimeToolCallEvent,
)
from integrations.telephony.realtime_call import (
    OpenAIRealtimeCallBridge,
    RealtimeCallSpeechStartedEvent,
    RealtimeCallTranscriptEvent,
)


_CLOSE = object()


class FakeRealtimeClient:
    instances = []

    def __init__(self, *, api_key_provider, options):
        self.api_key_provider = api_key_provider
        self.options = options
        self.connected = False
        self.commands = []
        self.events_queue = asyncio.Queue()
        self.create_response_error = None
        self.function_output_error = None
        self.__class__.instances.append(self)

    async def connect(self):
        value = self.api_key_provider()
        if asyncio.iscoroutine(value):
            value = await value
        assert value == "secret"
        self.connected = True
        self.commands.append(("connect",))
        return self

    async def append_audio(self, audio):
        self.commands.append(("audio", bytes(audio)))

    async def commit_audio(self):
        self.commands.append(("commit",))

    async def update_session(self, **kwargs):
        self.commands.append(("update_session", kwargs))

    async def create_response(self, instructions=None):
        self.commands.append(("response", instructions))
        if self.create_response_error is not None:
            raise self.create_response_error

    async def send_function_output(self, call_id, output):
        self.commands.append(("tool_output", call_id, output))
        if self.function_output_error is not None:
            raise self.function_output_error

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
        self.commands.append(("close",))
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


async def emit_input(client, item_id, text, *, start_ms=100, end_ms=300):
    await client.events_queue.put(OpenAISpeechEvent(True, item_id, start_ms))
    await client.events_queue.put(OpenAISpeechEvent(False, item_id, end_ms))
    await client.events_queue.put(
        OpenAIInputTranscriptEvent(item_id, text, True)
    )


async def next_final(events):
    started = await anext(events)
    final = await anext(events)
    assert isinstance(started, RealtimeCallSpeechStartedEvent)
    assert isinstance(final, RealtimeCallTranscriptEvent)
    assert final.is_final and final.speech_final
    assert final.turn_handle is not None
    return final


async def collect_runtime(iterator):
    result = []
    async for event in iterator:
        result.append(event)
        if isinstance(event, RealtimeStatusEvent):
            event.acknowledge_accounting()
    return result


@pytest.mark.asyncio
async def test_audio_is_forwarded_before_transcript_and_vad_is_provider_owned():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    await bridge.send_audio(b"pcmu")
    client = FakeRealtimeClient.instances[-1]
    assert client.commands[:2] == [("connect",), ("audio", b"pcmu")]
    assert isinstance(client.options.vad, SemanticVadOptions)
    assert client.options.vad.create_response is False
    assert client.options.vad.interrupt_response is False
    assert client.options.input_transcription_model == "gpt-live-transcribe"

    events = bridge.events()
    await emit_input(client, "input-1", "hello", start_ms=50, end_ms=150)
    final = await next_final(events)
    assert final.text == "hello"
    assert final.turn_handle.captured_input_pcmu_bytes == 800
    await bridge.finalize()
    assert client.commands[-1] == ("commit",)
    await bridge.close()


@pytest.mark.asyncio
async def test_finalize_publishes_pending_final_without_speech_stopped():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    await bridge.send_audio(b"a" * 1_600)
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await client.events_queue.put(OpenAISpeechEvent(True, "partial", 100))
    await client.events_queue.put(
        OpenAIInputTranscriptEvent("partial", "last words", True)
    )
    assert isinstance(await anext(input_events), RealtimeCallSpeechStartedEvent)
    for _ in range(100):
        if "partial" in bridge._pending_finals:
            break
        await asyncio.sleep(0)
    assert "partial" in bridge._pending_finals

    await bridge.finalize()
    final = await asyncio.wait_for(anext(input_events), timeout=1)

    assert final.text == "last words"
    assert final.start_seconds == 0.1
    assert final.duration_seconds == 0.1
    assert final.turn_handle.captured_input_pcmu_bytes == 800
    assert client.commands[-1] == ("commit",)
    await bridge.close()


@pytest.mark.asyncio
async def test_final_arriving_after_finalize_uses_admitted_audio_frontier_once():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    await bridge.send_audio(b"a" * 1_603)
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await client.events_queue.put(OpenAISpeechEvent(True, "late-final", 150))
    assert isinstance(await anext(input_events), RealtimeCallSpeechStartedEvent)

    await bridge.finalize()
    await client.events_queue.put(
        OpenAIInputTranscriptEvent("late-final", "still here", True)
    )
    final = await asyncio.wait_for(anext(input_events), timeout=1)
    await client.events_queue.put(OpenAISpeechEvent(False, "late-final", 250))
    await asyncio.sleep(0)

    assert final.text == "still here"
    assert final.start_seconds == 0.15
    assert final.duration_seconds == 0.051
    assert final.turn_handle.captured_input_pcmu_bytes == 408
    assert bridge._input_queue.empty()
    await bridge.close()


@pytest.mark.asyncio
async def test_two_turns_share_one_connection_and_handle_close_keeps_socket_open():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()

    await emit_input(client, "input-1", "first")
    first = (await next_final(input_events)).turn_handle
    await first.start_turn(
        [
            {"role": "user", "content": "older"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "first"},
        ],
        instructions="prompt",
    )
    first_update = next(
        command[1]
        for command in client.commands
        if command[0] == "update_session"
    )
    assert "user: older" in first_update["instructions"]
    assert "assistant: answer" in first_update["instructions"]
    assert "user: first" not in first_update["instructions"]

    audio_task = asyncio.create_task(_collect_audio(first.output_pcmu()))
    runtime_task = asyncio.create_task(collect_runtime(first.runtime_events()))
    await client.events_queue.put(
        OpenAIOutputAudioEvent("r1", "out-1", 0, b"audio", False)
    )
    await client.events_queue.put(
        OpenAIOutputTextEvent(
            "audio_transcript", "r1", "out-1", 0, "reply", False
        )
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))
    assert await audio_task == [b"audio"]
    assert any(
        isinstance(event, RealtimeDoneEvent)
        for event in await runtime_task
    )
    await first.close()
    assert client.connected is True

    await emit_input(client, "input-2", "second", start_ms=400, end_ms=500)
    second = (await next_final(input_events)).turn_handle
    await second.start_turn(
        [
            {"role": "user", "content": "older duplicate"},
            {"role": "user", "content": "second"},
        ],
        instructions="updated prompt",
    )
    updates = [command[1] for command in client.commands if command[0] == "update_session"]
    assert "user: older" in updates[-1]["instructions"]
    assert "assistant: answer" in updates[-1]["instructions"]
    assert "older duplicate" not in updates[-1]["instructions"]
    runtime_task = asyncio.create_task(collect_runtime(second.runtime_events()))
    await client.events_queue.put(OpenAIResponseDoneEvent("r2", "completed", usage()))
    await runtime_task

    assert sum(command[0] == "connect" for command in client.commands) == 1
    assert sum(command[0] == "response" for command in client.commands) == 2
    await bridge.close()


@pytest.mark.asyncio
async def test_completed_input_item_is_deduplicated():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    events = bridge.events()
    await emit_input(client, "same-item", "once")
    await next_final(events)
    await client.events_queue.put(
        OpenAIInputTranscriptEvent("same-item", "duplicate", True)
    )
    await asyncio.sleep(0)
    assert bridge._input_queue.empty()
    assert "same-item" in bridge._handles
    await client.events_queue.put(
        OpenAIInputTranscriptFailedEvent(
            "same-item",
            0,
            "late_failure",
            "transcription_error",
            "late provider failure",
        )
    )
    await asyncio.sleep(0)
    assert bridge._input_queue.empty()
    assert client.connected is True
    await bridge.close()


@pytest.mark.asyncio
async def test_pending_completed_transcription_wins_over_late_failure():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    events = bridge.events()
    await client.events_queue.put(OpenAISpeechEvent(True, "pending-input", 100))
    assert isinstance(await anext(events), RealtimeCallSpeechStartedEvent)
    await client.events_queue.put(
        OpenAIInputTranscriptEvent("pending-input", "valid final", True)
    )
    await asyncio.sleep(0)
    assert "pending-input" in bridge._pending_finals

    await client.events_queue.put(
        OpenAIInputTranscriptFailedEvent(
            "pending-input",
            0,
            "late_failure",
            "transcription_error",
            "late provider failure",
        )
    )
    await asyncio.sleep(0)
    assert client.connected is True
    assert "pending-input" in bridge._pending_finals

    await client.events_queue.put(OpenAISpeechEvent(False, "pending-input", 300))
    final = await asyncio.wait_for(anext(events), timeout=1)
    assert final.text == "valid final"
    assert final.turn_handle is not None
    assert client.connected is True
    await bridge.close()


@pytest.mark.asyncio
async def test_input_transcription_failure_closes_call_stream():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        input_transcription_timeout_seconds=0.2,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await client.events_queue.put(OpenAISpeechEvent(True, "failed-input", 100))
    await client.events_queue.put(OpenAISpeechEvent(False, "failed-input", 300))
    assert isinstance(await anext(input_events), RealtimeCallSpeechStartedEvent)
    await client.events_queue.put(
        OpenAIInputTranscriptFailedEvent(
            "failed-input",
            0,
            "audio_unintelligible",
            "transcription_error",
            "sensitive provider detail",
        )
    )

    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await asyncio.wait_for(anext(input_events), timeout=1)
    assert client.connected is False
    assert bridge._input_transcription_watchdogs == {}
    await bridge.close()


@pytest.mark.asyncio
async def test_input_transcription_watchdog_fails_silent_item_and_closes_socket():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        input_transcription_timeout_seconds=0.01,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await client.events_queue.put(OpenAISpeechEvent(True, "stalled-input", 100))
    await client.events_queue.put(OpenAISpeechEvent(False, "stalled-input", 300))
    assert isinstance(await anext(input_events), RealtimeCallSpeechStartedEvent)

    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await asyncio.wait_for(anext(input_events), timeout=1)
    assert client.connected is False
    assert bridge._input_transcription_watchdogs == {}
    await bridge.close()


@pytest.mark.asyncio
async def test_late_transcript_cannot_publish_after_timeout_claims_session():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        input_transcription_timeout_seconds=0.01,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    failure_claimed = asyncio.Event()
    release_failure = asyncio.Event()
    original_cancel = bridge._cancel_input_transcription_watchdogs

    async def pause_after_failure_claim():
        failure_claimed.set()
        await release_failure.wait()
        await original_cancel()

    bridge._cancel_input_transcription_watchdogs = pause_after_failure_claim
    await client.events_queue.put(OpenAISpeechEvent(True, "late-input", 100))
    await client.events_queue.put(OpenAISpeechEvent(False, "late-input", 300))
    assert isinstance(await anext(input_events), RealtimeCallSpeechStartedEvent)
    await asyncio.wait_for(failure_claimed.wait(), timeout=1)
    assert bridge._consumer_error is not None

    await client.events_queue.put(
        OpenAIInputTranscriptEvent("late-input", "too late", True)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert "late-input" not in bridge._handles
    assert "late-input" not in bridge._pending_finals

    release_failure.set()
    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await asyncio.wait_for(anext(input_events), timeout=1)
    await bridge.close()


@pytest.mark.asyncio
async def test_completed_transcription_and_finalize_drain_item_watchdogs():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        input_transcription_timeout_seconds=0.02,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "completed-input", "hello")
    await next_final(input_events)
    assert bridge._input_transcription_watchdogs == {}

    await client.events_queue.put(OpenAISpeechEvent(True, "stop-input", 400))
    await client.events_queue.put(OpenAISpeechEvent(False, "stop-input", 500))
    assert isinstance(await anext(input_events), RealtimeCallSpeechStartedEvent)
    for _ in range(100):
        if "stop-input" in bridge._input_transcription_watchdogs:
            break
        await asyncio.sleep(0)
    assert "stop-input" in bridge._input_transcription_watchdogs
    await bridge.finalize()
    assert "stop-input" in bridge._input_transcription_watchdogs
    await client.events_queue.put(
        OpenAIInputTranscriptEvent("stop-input", "last words", True)
    )
    final = await asyncio.wait_for(anext(input_events), timeout=1)

    assert final.text == "last words"
    assert bridge._input_transcription_watchdogs == {}
    assert client.connected is True
    await bridge.close()


@pytest.mark.asyncio
async def test_tool_continuation_stays_on_the_same_call_session():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-tool", "look it up")
    handle = (await next_final(input_events)).turn_handle

    async def durable_marker():
        client.commands.append(("provider_started",))

    handle.set_usage_uncertain_handler(durable_marker)
    await handle.start_turn(
        [{"role": "user", "content": "look it up"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    runtime = handle.runtime_events()
    await client.events_queue.put(
        OpenAIFunctionCallEvent("r1", "tool-item", "call-1", "lookup", "{}", True)
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))
    assert isinstance(await anext(runtime), RealtimeToolCallEvent)
    status = await anext(runtime)
    assert isinstance(status, RealtimeStatusEvent)
    status.acknowledge_accounting()

    await handle.continue_function_call("call-1", {"value": 7})
    assert ("tool_output", "call-1", {"value": 7}) in client.commands
    assert sum(command[0] == "response" for command in client.commands) == 2
    response_indexes = [
        index
        for index, command in enumerate(client.commands)
        if command[0] == "response"
    ]
    marker_indexes = [
        index
        for index, command in enumerate(client.commands)
        if command[0] == "provider_started"
    ]
    assert len(response_indexes) == len(marker_indexes) == 2
    assert all(
        marker < response
        for marker, response in zip(marker_indexes, response_indexes, strict=True)
    )
    second_marker = marker_indexes[1]
    tool_output = next(
        index
        for index, command in enumerate(client.commands)
        if command[0] == "tool_output"
    )
    assert second_marker < tool_output
    remaining_task = asyncio.create_task(collect_runtime(runtime))
    await client.events_queue.put(
        OpenAIOutputTextEvent(
            "audio_transcript", "r2", "out-2", 0, "seven", False
        )
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r2", "completed", usage()))
    remaining = await remaining_task
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.transcript == "seven"
        for event in remaining
    )
    assert sum(command[0] == "connect" for command in client.commands) == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_pending_tool_timeout_fails_handle_and_invalidates_call_socket():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1",
        client_factory=FakeRealtimeClient,
        pending_tool_timeout_seconds=0.01,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-tool-timeout", "look it up")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn(
        [{"role": "user", "content": "look it up"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    runtime = handle.runtime_events()
    await client.events_queue.put(
        OpenAIFunctionCallEvent(
            "r-tool-timeout",
            "tool-item",
            "call-timeout",
            "lookup",
            "{}",
            True,
        )
    )
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-tool-timeout", "completed", usage())
    )
    assert isinstance(await anext(runtime), RealtimeToolCallEvent)
    status = await anext(runtime)
    assert isinstance(status, RealtimeStatusEvent)
    status.acknowledge_accounting()

    remaining = await asyncio.wait_for(collect_runtime(runtime), timeout=1)
    assert [
        event.code
        for event in remaining
        if isinstance(event, RealtimeErrorEvent)
    ] == ["realtime_tool_timeout"]
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "failed"
        for event in remaining
    )
    assert client.connected is False
    assert not any(command[0] == "tool_output" for command in client.commands)
    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await asyncio.wait_for(anext(input_events), timeout=1)
    await bridge.close()


@pytest.mark.asyncio
async def test_tool_continuation_revalidates_after_timeout_claims_session():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1",
        client_factory=FakeRealtimeClient,
        pending_tool_timeout_seconds=0.05,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-tool-race", "look it up")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn(
        [{"role": "user", "content": "look it up"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )

    continuation_disarming = asyncio.Event()
    timeout_claimed = asyncio.Event()
    release_timeout = asyncio.Event()
    disarm_calls = 0
    original_disarm = bridge._disarm_pending_tool_watchdog
    original_cancel = bridge._cancel_input_transcription_watchdogs

    async def pause_continuation_disarm(target_handle):
        nonlocal disarm_calls
        disarm_calls += 1
        if disarm_calls == 2:
            continuation_disarming.set()
            await timeout_claimed.wait()
        await original_disarm(target_handle)

    async def pause_after_timeout_claim():
        timeout_claimed.set()
        await release_timeout.wait()
        await original_cancel()

    bridge._disarm_pending_tool_watchdog = pause_continuation_disarm
    bridge._cancel_input_transcription_watchdogs = pause_after_timeout_claim
    runtime = handle.runtime_events()
    await client.events_queue.put(
        OpenAIFunctionCallEvent(
            "r-tool-race",
            "tool-item",
            "call-race",
            "lookup",
            "{}",
            True,
        )
    )
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-tool-race", "completed", usage())
    )
    assert isinstance(await anext(runtime), RealtimeToolCallEvent)
    status = await anext(runtime)
    assert isinstance(status, RealtimeStatusEvent)
    status.acknowledge_accounting()

    continuation = asyncio.create_task(
        handle.continue_function_call("call-race", {"value": 7})
    )
    await asyncio.wait_for(continuation_disarming.wait(), timeout=1)
    await asyncio.wait_for(timeout_claimed.wait(), timeout=1)
    with pytest.raises(RuntimeError, match="no longer active"):
        await asyncio.wait_for(continuation, timeout=1)
    assert not any(command[0] == "tool_output" for command in client.commands)

    release_timeout.set()
    remaining = await asyncio.wait_for(collect_runtime(runtime), timeout=1)
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "failed"
        for event in remaining
    )
    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await asyncio.wait_for(anext(input_events), timeout=1)
    await bridge.close()


@pytest.mark.asyncio
async def test_completed_response_truncates_playback_without_response_cancel():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-completed-barge", "hello")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn([{"role": "user", "content": "hello"}])
    runtime_task = asyncio.create_task(collect_runtime(handle.runtime_events()))
    await client.events_queue.put(
        OpenAIOutputAudioEvent("r-complete", "out-complete", 0, b"audio", False)
    )
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-complete", "completed", usage())
    )
    await runtime_task

    await handle.cancel_output()
    await handle.truncate_output(played_ms=0)

    assert not any(command[0] == "cancel" for command in client.commands)
    assert ("truncate", "out-complete", 0, 0) in client.commands
    await bridge.close()


@pytest.mark.asyncio
async def test_late_cancelled_response_events_cannot_contaminate_next_turn():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        cancel_accounting_seconds=0.2,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()

    await emit_input(client, "input-cancelled", "first")
    first = (await next_final(input_events)).turn_handle
    await first.start_turn([{"role": "user", "content": "first"}])
    first_runtime = asyncio.create_task(collect_runtime(first.runtime_events()))
    await client.events_queue.put(
        OpenAIOutputAudioEvent("r-old", "out-old", 0, b"old", False)
    )
    cancel_task = asyncio.create_task(first.cancel_output())
    await _wait_for_command(client, "cancel")
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-old", "cancelled", usage(4, 1))
    )
    await first_runtime
    await cancel_task

    await emit_input(client, "input-next", "second", start_ms=400, end_ms=500)
    second = (await next_final(input_events)).turn_handle
    await second.start_turn([{"role": "user", "content": "second"}])
    second_audio = asyncio.create_task(_collect_audio(second.output_pcmu()))
    second_runtime = asyncio.create_task(collect_runtime(second.runtime_events()))

    # Delayed duplicates from the cancelled response remain owned by ``first``.
    await client.events_queue.put(
        OpenAIOutputAudioEvent("r-old", "out-old", 0, b"stale", False)
    )
    await client.events_queue.put(
        OpenAIOutputTextEvent(
            "audio_transcript", "r-old", "out-old", 0, "stale", False
        )
    )
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-old", "cancelled", usage(99, 9))
    )
    await client.events_queue.put(
        OpenAIOutputAudioEvent("r-new", "out-new", 0, b"new", False)
    )
    await client.events_queue.put(
        OpenAIOutputTextEvent(
            "audio_transcript", "r-new", "out-new", 0, "new", False
        )
    )
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-new", "completed", usage(6, 2))
    )

    assert await second_audio == [b"new"]
    events = await second_runtime
    done = next(event for event in events if isinstance(event, RealtimeDoneEvent))
    assert done.transcript == "new"
    assert done.usage.total_tokens == 8
    await bridge.close()


@pytest.mark.asyncio
async def test_inactivity_cancel_waits_for_response_usage_accounting():
    FakeRealtimeClient.instances.clear()
    uncertain = []
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        response_inactivity_seconds=0.02,
        cancel_accounting_seconds=0.2,
        usage_uncertain_handler=lambda: uncertain.append(True),
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-timeout", "hello")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn([{"role": "user", "content": "hello"}])
    runtime_task = asyncio.create_task(collect_runtime(handle.runtime_events()))

    await _wait_for_command(client, "cancel")
    await client.events_queue.put(
        OpenAIResponseDoneEvent("r-timeout", "cancelled", usage(9, 2))
    )
    events = await asyncio.wait_for(runtime_task, timeout=1)
    status = next(event for event in events if isinstance(event, RealtimeStatusEvent))
    done = next(event for event in events if isinstance(event, RealtimeDoneEvent))
    assert status.response_usage.total_tokens == 11
    assert done.usage.total_tokens == 11
    assert done.status == "cancelled"
    # The one call is the durable pre-response fence.  No second uncertainty
    # mark is needed because cancelled response usage was accounted exactly.
    assert uncertain == [True]
    assert not any(isinstance(event, RealtimeErrorEvent) for event in events)
    await bridge.close()


@pytest.mark.asyncio
async def test_inactivity_without_provider_done_claims_one_failed_terminal():
    FakeRealtimeClient.instances.clear()
    uncertain = []
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        response_inactivity_seconds=0.01,
        cancel_accounting_seconds=0.01,
        usage_uncertain_handler=lambda: uncertain.append(True),
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-stalled", "hello")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn([{"role": "user", "content": "hello"}])

    events = await asyncio.wait_for(
        collect_runtime(handle.runtime_events()), timeout=1
    )
    errors = [event for event in events if isinstance(event, RealtimeErrorEvent)]
    dones = [event for event in events if isinstance(event, RealtimeDoneEvent)]
    assert [event.code for event in errors] == ["realtime_response_timeout"]
    assert [event.status for event in dones] == ["failed"]
    assert sum(command[0] == "cancel" for command in client.commands) == 1
    # Durable pre-response fence plus the final missing-usage mark.
    assert uncertain == [True, True]
    assert client.connected is False
    assert isinstance(bridge._consumer_error, OpenAIRealtimeError)
    await bridge.close()


@pytest.mark.asyncio
async def test_ambiguous_response_create_failure_invalidates_call_socket():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-create-failure", "hello")
    handle = (await next_final(input_events)).turn_handle
    client.create_response_error = RuntimeError("ambiguous create failure")

    with pytest.raises(RuntimeError, match="ambiguous create failure"):
        await handle.start_turn([{"role": "user", "content": "hello"}])

    runtime = await asyncio.wait_for(
        collect_runtime(handle.runtime_events()), timeout=1
    )
    assert any(isinstance(event, RealtimeErrorEvent) for event in runtime)
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "failed"
        for event in runtime
    )
    assert client.connected is False
    assert client.commands[-1] == ("close",)
    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await anext(input_events)
    await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["function_output", "create_response"])
async def test_ambiguous_continuation_failure_invalidates_call_socket(
    failure_point,
):
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-continuation-failure", "lookup")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn(
        [{"role": "user", "content": "lookup"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    runtime = handle.runtime_events()
    await client.events_queue.put(
        OpenAIFunctionCallEvent("r1", "tool-item", "call-1", "lookup", "{}", True)
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))
    assert isinstance(await anext(runtime), RealtimeToolCallEvent)
    status = await anext(runtime)
    assert isinstance(status, RealtimeStatusEvent)
    status.acknowledge_accounting()
    error = RuntimeError(f"ambiguous {failure_point} failure")
    if failure_point == "function_output":
        client.function_output_error = error
    else:
        client.create_response_error = error

    with pytest.raises(RuntimeError, match=f"ambiguous {failure_point} failure"):
        await handle.continue_function_call("call-1", {"value": 7})

    remaining = await asyncio.wait_for(collect_runtime(runtime), timeout=1)
    assert any(isinstance(event, RealtimeErrorEvent) for event in remaining)
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "failed"
        for event in remaining
    )
    assert client.connected is False
    assert client.commands[-1] == ("close",)
    await bridge.close()


@pytest.mark.asyncio
async def test_cancel_accounting_timeout_terminates_handle_queues():
    FakeRealtimeClient.instances.clear()
    uncertain = []
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
        cancel_accounting_seconds=0.01,
        usage_uncertain_handler=lambda: uncertain.append(True),
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-cancel-accounting-timeout", "hello")
    handle = (await next_final(input_events)).turn_handle
    await handle.start_turn([{"role": "user", "content": "hello"}])
    runtime_task = asyncio.create_task(collect_runtime(handle.runtime_events()))
    audio_task = asyncio.create_task(_collect_audio(handle.output_pcmu()))

    await asyncio.wait_for(handle.cancel_output(), timeout=1)

    runtime = await asyncio.wait_for(runtime_task, timeout=1)
    assert await asyncio.wait_for(audio_task, timeout=1) == []
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "cancelled"
        for event in runtime
    )
    assert handle._finished is True
    assert bridge._active_handle is None
    assert client.connected is False
    assert uncertain == [True, True]
    await bridge.close()


@pytest.mark.asyncio
async def test_durable_marker_failure_prevents_response_create():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-marker", "hello")
    handle = (await next_final(input_events)).turn_handle

    async def fail_marker():
        raise RuntimeError("billing marker unavailable")

    handle.set_usage_uncertain_handler(fail_marker)
    with pytest.raises(RuntimeError, match="billing marker unavailable"):
        await handle.start_turn([{"role": "user", "content": "hello"}])
    assert not any(command[0] == "response" for command in client.commands)
    await bridge.close()


@pytest.mark.asyncio
async def test_continuation_marker_failure_prevents_function_output():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await emit_input(client, "input-continuation-marker", "lookup")
    handle = (await next_final(input_events)).turn_handle
    handle.set_usage_uncertain_handler(lambda: None)
    await handle.start_turn(
        [{"role": "user", "content": "lookup"}],
        tools=[{"type": "function", "name": "lookup", "parameters": {}}],
    )
    runtime = handle.runtime_events()
    await client.events_queue.put(
        OpenAIFunctionCallEvent("r1", "tool-item", "call-1", "lookup", "{}", True)
    )
    await client.events_queue.put(OpenAIResponseDoneEvent("r1", "completed", usage()))
    assert isinstance(await anext(runtime), RealtimeToolCallEvent)
    status = await anext(runtime)
    assert isinstance(status, RealtimeStatusEvent)
    status.acknowledge_accounting()

    async def fail_marker():
        raise RuntimeError("billing continuation marker unavailable")

    handle.set_usage_uncertain_handler(fail_marker)
    with pytest.raises(RuntimeError, match="continuation marker unavailable"):
        await handle.continue_function_call("call-1", {"value": 7})
    assert not any(command[0] == "tool_output" for command in client.commands)
    remaining = await collect_runtime(runtime)
    assert any(isinstance(event, RealtimeErrorEvent) for event in remaining)
    assert any(
        isinstance(event, RealtimeDoneEvent) and event.status == "failed"
        for event in remaining
    )
    await bridge.close()


@pytest.mark.asyncio
async def test_provider_error_without_active_turn_fails_input_stream():
    FakeRealtimeClient.instances.clear()
    bridge = OpenAIRealtimeCallBridge(
        api_key_provider=lambda: "secret",
        model="gpt-realtime-2.1-mini",
        client_factory=FakeRealtimeClient,
    )
    await bridge.connect()
    client = FakeRealtimeClient.instances[-1]
    input_events = bridge.events()
    await client.events_queue.put(
        OpenAIProviderErrorEvent(
            "provider_error",
            "server_error",
            "sensitive provider detail",
            None,
            "event-id",
        )
    )
    with pytest.raises(OpenAIRealtimeError, match="event stream failed"):
        await anext(input_events)
    await bridge.close()


async def _wait_for_command(client, name):
    for _ in range(100):
        if any(command[0] == name for command in client.commands):
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"command {name} was not sent")


async def _collect_audio(iterator):
    return [item async for item in iterator]
