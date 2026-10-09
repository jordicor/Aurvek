"""Bounded local voice acceptance with synthetic WAVs and simulated playback ACKs.

Requires an already provisioned acceptance conversation. This invokes real
providers through Aurvek, writes its normal history, and incurs normal charges.
It never opens a microphone/camera, loads .env, or prints media/transcripts.
"""

import argparse
import asyncio
import audioop
import base64
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
import wave

import aiohttp


class ProbeError(RuntimeError):
    """A fixed, content-free diagnostic safe to include in the report."""


def local_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise argparse.ArgumentTypeError("Use a loopback HTTP(S) origin without credentials or a path")
    return value.rstrip("/")


def synthetic_audio(path):
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE"
                or not 8000 <= wav.getframerate() <= 48000
                or not 0 < wav.getnframes() <= wav.getframerate() * 20):
            raise ProbeError("invalid_synthetic_wav")
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != wav.getnframes() * 2:
            raise ProbeError("truncated_synthetic_wav")
        pcm, _ = audioop.ratecv(pcm, 2, 1, wav.getframerate(), 16000, None)
        return pcm


class CSRFParser(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "aurvek-csrf-token":
            self.token = attrs.get("content")


async def authenticate(http, origin):
    username = os.environ.get("AURVEK_LIVE_USERNAME")
    password = os.environ.get("AURVEK_LIVE_PASSWORD")
    if not username or not password:
        raise ProbeError("missing_acceptance_credentials")
    async with http.post(origin + "/login", data={"username": username, "password": password},
                         allow_redirects=False) as response:
        # Do not follow authentication redirects to an unexpected destination.
        if response.status not in {302, 303}:
            raise ProbeError("acceptance_login_failed")


async def receive_events(socket, state):
    async for raw in socket:
        if raw.type != aiohttp.WSMsgType.TEXT:
            raise ProbeError("unexpected_websocket_message")
        event = raw.json()
        kind = event.get("event")
        if kind == "audio":
            if state["ending"]:
                raise ProbeError("audio_after_ending")
            data = base64.b64decode(event["audio"], validate=True)
            if not data or len(data) % 2 or len(data) > 1024 * 1024:
                raise ProbeError("invalid_output_audio")
            state["audio_bytes"] += len(data)
            # GPT Live keeps sending PCM silence between spoken turns.
            if audioop.rms(data, 2) > 64:
                state["last_output"] = max(time.monotonic(), state["last_output"]) + len(data) / 48000
            # Receipt ACK only: nothing has been played through a speaker.
            await socket.send_json({"event": "played", "id": event["id"],
                                    "generation": event["generation"]})
            state["simulated_acks"] += 1
        elif kind == "transcript":
            role = event.get("role")
            if role not in {"bot", "user"} or not isinstance(event.get("text"), str):
                raise ProbeError("invalid_transcript_event")
            state[role + "_chars"] += len(event["text"])
            if role == "bot":
                state["last_output"] = max(time.monotonic(), state["last_output"])
        elif kind == "interrupted":
            state["interruptions"] += 1
            state["last_output"] = time.monotonic()
        elif kind == "backend":
            active = event.get("active")
            if not isinstance(active, bool):
                raise ProbeError("invalid_backend_state")
            if state["backend_active"] and not active:
                state["backend_completed"] += 1
            state["backend_active"] = active
        elif kind == "ending":
            if state["ending"] or state["ended"]:
                raise ProbeError("duplicate_ending")
            state["ending"] = True
        elif kind == "ended":
            if not state["ending"] or state["ended"]:
                raise ProbeError("invalid_ended_order")
            state["ended"] = True
        elif kind == "error":
            # Provider/server details can contain private data. Never echo them.
            raise ProbeError("server_reported_error")
        else:
            raise ProbeError("unknown_live_event")


def new_state():
    return {"audio_bytes": 0, "simulated_acks": 0, "bot_chars": 0, "user_chars": 0,
            "interruptions": 0, "last_output": 0.0, "ending": False, "ended": False,
            "backend_active": False, "backend_completed": 0}


async def send_stage(socket, reader, state, pcm, *, expect_end=False, expect_backend=False):
    before_audio, before_text = state["audio_bytes"], state["bot_chars"]
    before_backend = state["backend_completed"]
    started = time.monotonic()
    for offset in range(0, len(pcm), 640):
        if reader.done():
            await reader
            raise ProbeError("session_closed_during_input")
        if state["ending"]:
            raise ProbeError("session_ended_during_input")
        await socket.send_bytes(pcm[offset:offset + 640])
        await asyncio.sleep(.02)
    deadline = time.monotonic() + (90 if expect_end else 45)
    while time.monotonic() < deadline:
        if reader.done():
            await reader
            if expect_end and state["ended"]:
                break
            raise ProbeError("session_closed_before_expected_end")
        if state["ended"]:
            if not expect_end:
                raise ProbeError("session_ended_before_goodbye_stage")
            break
        if (not expect_end and state["audio_bytes"] > before_audio and state["bot_chars"] > before_text
                and not state["backend_active"]
                and (not expect_backend or state["backend_completed"] > before_backend)
                and time.monotonic() - state["last_output"] >= 4):
            break  # Heuristic quiet boundary, not proof of provider turn completion.
        if not state["ending"]:
            await socket.send_bytes(b"\0" * 640)
        await asyncio.sleep(.02)
    else:
        raise ProbeError("voice_stage_timed_out")
    if state["audio_bytes"] <= before_audio or state["bot_chars"] <= before_text:
        raise ProbeError("stage_missing_audio_or_assistant_transcript")
    return {"audio_bytes": state["audio_bytes"] - before_audio,
            "assistant_transcript_chars": state["bot_chars"] - before_text,
            "backend_delegations": state["backend_completed"] - before_backend,
            "elapsed_seconds": round(time.monotonic() - started, 2)}


async def probe(args):
    # Validate every input before authentication or provider admission.
    stages = ("end",) if args.only_end else ("identity", "kb", "web", "end")
    clips = []
    for stage in stages:
        path = getattr(args, stage + "_audio")
        if path is None:
            raise ProbeError("missing_" + stage + "_audio")
        clips.append((stage, synthetic_audio(path)))
    origin = local_origin(args.base_url)
    ws_origin = origin.replace("http", "ws", 1)
    report = {"status": "protocol_only", "conversation_id": args.conversation_id,
              "model_id": args.model_id, "started_at": datetime.now(timezone.utc).isoformat(),
              "playback_ack": "simulated_on_receipt", "physical_listening_verified": False,
              "tool_execution_verified": False, "history_verified": False, "billing_verified": False,
              "stages": {}}
    jar = aiohttp.CookieJar(unsafe=True, treat_as_secure_origin=[origin, ws_origin])
    async with asyncio.timeout(300), aiohttp.ClientSession(cookie_jar=jar,
            timeout=aiohttp.ClientTimeout(total=20)) as http:
        await authenticate(http, origin)
        async with http.get(origin + f"/chat?conversation_id={args.conversation_id}",
                            allow_redirects=False) as response:
            if response.status != 200:
                raise ProbeError("acceptance_conversation_unavailable")
            parser = CSRFParser()
            parser.feed(await response.text())
        if not isinstance(parser.token, str) or not 32 <= len(parser.token) <= 256:
            raise ProbeError("csrf_token_missing")
        async with http.ws_connect(ws_origin + f"/ws/conversations/{args.conversation_id}/live",
                                   headers={"Origin": origin}, max_msg_size=2 * 1024 * 1024) as socket:
            await socket.send_json({"event": "start", "csrf_token": parser.token,
                                    "model_id": args.model_id, "camera": False})
            ready = await socket.receive_json(timeout=30)
            if (ready.get("event") != "ready" or ready.get("input_rate") != 16000
                    or ready.get("output_rate") != 24000):
                raise ProbeError("live_session_not_ready")
            state = new_state()
            reader = asyncio.create_task(receive_events(socket, state))
            try:
                for stage, pcm in clips:
                    try:
                        report["stages"][stage] = await send_stage(socket, reader, state, pcm,
                            expect_end=stage == "end", expect_backend=stage in {"kb", "web"})
                    except ProbeError as exc:
                        raise ProbeError(stage + ":" + str(exc)) from None
                await asyncio.wait_for(reader, 15)
                if not state["ending"] or not state["ended"]:
                    raise ProbeError("missing_server_end_confirmation")
                if socket.close_code != 1000:
                    raise ProbeError("abnormal_websocket_close")
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            report.update({key: value for key, value in state.items() if key != "last_output"})
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def positive_id(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Use a positive ID")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=local_origin, default="http://127.0.0.1:7789")
    parser.add_argument("--conversation-id", type=positive_id, required=True)
    parser.add_argument("--model-id", type=positive_id, required=True)
    parser.add_argument("--only-end", action="store_true", help="Verify only the final voice action")
    for stage in ("identity", "kb", "web", "end"):
        parser.add_argument("--" + stage + "-audio", type=Path, required=stage == "end",
                            help="Synthetic mono PCM16 WAV, at most 20 seconds")
    args = parser.parse_args()
    try:
        report = asyncio.run(probe(args))
    except ProbeError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    except (Exception, KeyboardInterrupt):
        # File paths, HTTP bodies, tokens and websocket details stay out of logs.
        print(json.dumps({"status": "failed", "error": "probe_failed"}))
        return 1
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
