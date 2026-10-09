import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from integrations.live.gemini import GeminiLiveClient
from integrations.telephony.gemini_live_billing import calculate_api_cost, parse_usage

USAGE = {
    "prompt_token_count": 1271, "response_token_count": 38,
    "thoughts_token_count": 78, "total_token_count": 1309,
    "prompt_tokens_details": [{"modality": "TEXT", "token_count": 731}, {"modality": "AUDIO", "token_count": 444}],
    "response_tokens_details": [{"modality": "AUDIO", "token_count": 30}],
}

class FakeSession:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.histories = []
        self.tools = []

    async def send_client_content(self, **value):
        self.histories.append(value)

    async def send_realtime_input(self, **value):
        self.sent.append(value)
        if value.get("audio_stream_end"):
            self.event(server_content={"turn_complete": True}, usage_metadata=USAGE)

    async def send_tool_response(self, **value):
        self.tools.append(value)

    def event(self, **value):
        self.incoming.put_nowait(types.LiveServerMessage(**value))

    async def receive(self):
        while True:
            event = await self.incoming.get()
            yield event
            if event.server_content and event.server_content.turn_complete:
                return

class FakeClient:
    def __init__(self, session):
        self.session, self.connections = session, 0
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=self.connect), aclose=AsyncMock())

    @asynccontextmanager
    async def connect(self, **kwargs):
        self.connections += 1
        self.configuration = kwargs
        yield self.session

@pytest.mark.asyncio
async def test_native_audio_multiturn_video_tools_and_interruption_share_one_socket():
    socket = FakeSession()
    sdk = FakeClient(socket)
    client = GeminiLiveClient(api_key_provider=lambda: "test", client_factory=lambda **_: sdk,
        session_loader=AsyncMock(return_value={"instructions": "Test", "input": [], "audio": {"output": {"voice": "Kore"}}}))
    await client.connect()
    events = client.events().__aiter__()
    try:
        await client.send_audio(b"\x01\x02" * 320)
        await client.send_video(b"jpeg-fixture")
        assert socket.sent[0]["audio"].data == b"\x01\x02" * 320
        assert socket.sent[0]["audio"].mime_type == "audio/pcm;rate=16000"
        assert socket.sent[1]["video"].data == b"jpeg-fixture"
        assert not socket.histories
        for _ in range(2):
            socket.event(server_content={"input_transcription": {"text": "Hello"},
                "output_transcription": {"text": "Hi"}, "turn_complete": True}, usage_metadata=USAGE)
            received = [await asyncio.wait_for(anext(events), 1) for _ in range(4)]
            assert received[-1]["type"] == "session.turn.completed"
        assert sdk.connections == 1
        assert sdk.configuration["config"].speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
        socket.event(tool_call={"function_calls": [{"id": "a", "name": "ask_aurvek", "args": {"request": "Search memory"}}]})
        delegation = await asyncio.wait_for(anext(events), 1)
        assert delegation["delegation"]["request"] == "Search memory"
        await client.commentary("Found it", delegation_id="a")
        assert socket.tools[0]["function_responses"][0].response == {"result": "Found it"}
        socket.event(server_content={"interrupted": True})
        assert (await asyncio.wait_for(anext(events), 1))["type"] == "session.output_audio.interrupted"
        socket.event(tool_call_cancellation={"ids": ["a"]})
        assert (await asyncio.wait_for(anext(events), 1))["type"] == "session.delegation.canceled"
        assert await client.commentary("Late", delegation_id="a") is False
    finally:
        await client.close()
    assert client.final_usage_confirmed
    assert sdk.aio.aclose.await_count == 1


def test_video_cost_is_not_charged_at_text_or_audio_rate():
    metadata = types.LiveServerMessage(usage_metadata={"prompt_token_count": 300,
        "response_token_count": 10, "prompt_tokens_details": [
            {"modality": "TEXT", "token_count": 100}, {"modality": "VIDEO", "token_count": 200}],
        "response_tokens_details": [{"modality": "AUDIO", "token_count": 10}]}).usage_metadata
    usage = parse_usage(metadata)
    assert (usage.text_input_tokens, usage.visual_input_tokens) == (100, 200)
    assert calculate_api_cost(usage) == pytest.approx((75 + 200 + 120) / 1e6)

def test_modality_cost_includes_internal_text_and_thinking_once():
    usage = parse_usage(types.LiveServerMessage(usage_metadata=USAGE).usage_metadata)
    assert usage.text_input_tokens == 827
    assert usage.output_tokens == 116
    assert calculate_api_cost(usage) == pytest.approx((827 * .75 + 444 * 3 + 86 * 4.5 + 30 * 12) / 1_000_000)


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [None, "Kore", "Puck", "marin"])
async def test_phone_editor_accepts_only_the_selected_provider_voices(voice):
    from integrations.realtime.configuration import validate_settings
    from tests.test_telephony_phone_model import _database, _insert_model

    conn = await _database()
    try:
        await _insert_model(conn, 1, machine="Gemini", provider="google", model="gemini-3.8-live")
        await conn.execute("INSERT INTO PROMPTS(id) VALUES(1)")
        selection = dict(llm_id=1, realtime_voice=voice)
        if voice == "marin":
            with pytest.raises(ValueError, match="Gemini Live voice"):
                await validate_settings(conn, **selection)
        else:
            result = await validate_settings(conn, **selection)
            assert result["realtime_voice"] == (voice or "Kore")
            assert result["realtime_backend_llm_id"] is None
    finally:
        await conn.close()
