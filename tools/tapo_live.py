"""Connect a local go2rtc/Tapo stream to Aurvek's authenticated Live endpoint.

Requires the existing go2rtc camera/backchannel configuration and FFmpeg.
Credentials come from AURVEK_DEVICE_TOKEN, never from command-line arguments.
"""

import argparse
import asyncio
import audioop
import base64
import contextlib
import os
import secrets
import struct
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from aiohttp import web
from dotenv import load_dotenv


SERVER_END_TIMEOUT_SECONDS = 15


def local_url(value, *, scheme):
    parsed = urlsplit(value)
    if parsed.scheme not in scheme or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise ValueError("go2rtc must run on this computer with a loopback URL")
    return value.rstrip("/")


def aurvek_url(base):
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Aurvek requires HTTPS, or HTTP on localhost")
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, "/ws/devices/live", "", ""))


class TapoLive:
    def __init__(self, args):
        self.args = args
        self.audio = asyncio.Queue(maxsize=1500)
        self.generation = 0
        self.rate_state = None
        self.socket = None
        self.output_connected = asyncio.Event()
        self.stopping = False
        self.processes = []
        self.output_active = False
        self.server_ending = False

    async def ffmpeg(self, *output):
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        process = await asyncio.create_subprocess_exec(
            self.args.ffmpeg, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", self.args.rtsp, *output, "pipe:1", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, **kwargs)
        self.processes.append(process)
        return process

    async def input_audio(self):
        process = await self.ffmpeg("-vn", "-ac", "1", "-ar", "16000", "-f", "s16le")
        while True:
            chunk = await process.stdout.readexactly(640)
            await self.socket.send_bytes(chunk)

    async def input_video(self):
        process = await self.ffmpeg("-an", "-vf", "fps=0.8,scale=640:-2", "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg")
        pending = bytearray()
        while True:
            chunk = await process.stdout.read(16384)
            if not chunk:
                raise RuntimeError("Camera video stopped")
            pending.extend(chunk)
            if len(pending) > 512 * 1024:
                raise ValueError("Camera frame is too large")
            end = pending.find(b"\xff\xd9")
            if end >= 0:
                jpeg = bytes(pending[:end + 2])
                del pending[:end + 2]
                await self.socket.send_json({"event": "frame", "jpeg": base64.b64encode(jpeg).decode("ascii")})

    async def output_audio(self, request):
        if self.output_active:
            raise web.HTTPConflict()
        self.output_active = True
        response = web.StreamResponse(headers={"Content-Type": "audio/wav", "Cache-Control": "no-store"})
        await response.prepare(request)
        # PCMA/8 kHz WAV, with unknown length for a continuous HTTP stream.
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ",
                             16, 6, 1, 8000, 8000, 1, 8, b"data", 0xFFFFFFFF)
        await response.write(header)
        self.output_connected.set()
        deadline = asyncio.get_running_loop().time()
        try:
            while not self.stopping:
                try:
                    generation, audio, mark = self.audio.get_nowait()
                except asyncio.QueueEmpty:
                    generation, audio, mark = self.generation, b"\xd5" * 160, None
                if generation != self.generation:
                    continue
                await response.write(audio)
                deadline = max(deadline + len(audio) / 8000, asyncio.get_running_loop().time())
                await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
                if mark is not None and generation == self.generation and not self.stopping:
                    # This confirms paced delivery to go2rtc, not a microphone measurement.
                    await self.socket.send_json({"event": "played", "id": mark, "generation": generation})
        except (ConnectionResetError, asyncio.CancelledError):
            if not self.stopping:
                await self.socket.close()
        return response

    async def receive(self):
        async for raw in self.socket:
            if raw.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError("Aurvek connection closed")
            event = raw.json()
            kind = event.get("event")
            if kind == "error":
                raise RuntimeError("Aurvek rejected the live session: " + str(event.get("message")))
            if kind == "backend":
                continue  # Backend activity is status, never a device action.
            if kind in {"ending", "ended"}:
                self.server_ending = self.stopping = True
                while not self.audio.empty():
                    self.audio.get_nowait()
                for process in self.processes:
                    if process.returncode is None:
                        process.terminate()
                if kind == "ended":
                    return
                continue
            if kind == "audio":
                if event["generation"] != self.generation:
                    continue
                pcm = base64.b64decode(event["audio"], validate=True)
                pcm, self.rate_state = audioop.ratecv(pcm, 2, 1, 24000, 8000, self.rate_state)
                audio = audioop.lin2alaw(pcm, 2)
                for offset in range(0, len(audio), 160):
                    mark = event["id"] if offset + 160 >= len(audio) else None
                    self.audio.put_nowait((self.generation, audio[offset:offset + 160], mark))
            elif kind == "interrupted":
                self.generation = event["generation"]
                self.rate_state = None
                while not self.audio.empty():
                    self.audio.get_nowait()
            elif kind == "transcript":
                # Do not print or retain private conversation content by default.
                print("Transcript received:", event["role"], flush=True)
        raise RuntimeError("Aurvek connection closed before session completion")

    async def run(self):
        token = os.environ.get("AURVEK_DEVICE_TOKEN")
        if not token:
            raise ValueError("AURVEK_DEVICE_TOKEN is required")
        application = web.Application()
        path = "/" + secrets.token_urlsafe(24) + "/audio.wav"
        application.router.add_get(path, self.output_audio)
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        timeout = aiohttp.ClientTimeout(total=None, connect=15)
        tasks = []
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.ws_connect(aurvek_url(self.args.aurvek),
                    headers={"Authorization": "Bearer " + token}, max_msg_size=2 * 1024 * 1024, heartbeat=20) as socket:
                    self.socket = socket
                    await socket.send_json({"event": "start", "model_id": self.args.model_id, "camera": True})
                    ready = await asyncio.wait_for(socket.receive_json(), 30)
                    if ready.get("event") != "ready":
                        raise RuntimeError("Aurvek did not accept the device session")
                    try:
                        async with http.post(self.args.go2rtc + "/api/streams", params={
                            "dst": self.args.stream, "src": f"http://127.0.0.1:{port}{path}"},
                            timeout=aiohttp.ClientTimeout(total=20)) as response:
                            response.raise_for_status()
                            await response.read()
                        await asyncio.wait_for(self.output_connected.wait(), 10)
                        print("Tapo live session connected. Press Ctrl+C to stop.", flush=True)
                        tasks = [asyncio.create_task(method()) for method in (self.input_audio, self.input_video, self.receive)]
                        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        if self.server_ending:
                            # FFmpeg EOF can win FIRST_COMPLETED after ending.
                            # Preserve the terminal receiver and propagate final
                            # errors instead of treating socket close as success.
                            await asyncio.wait_for(tasks[-1], SERVER_END_TIMEOUT_SECONDS)
                        else:
                            for task in done:
                                task.result()
                    finally:
                        self.stopping = True
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        with contextlib.suppress(aiohttp.ClientError):
                            async with http.post(self.args.go2rtc + "/api/streams", params={"dst": self.args.stream, "src": ""},
                                                 timeout=aiohttp.ClientTimeout(total=5)) as response:
                                await response.read()
                        if not socket.closed and not self.server_ending:
                            await socket.send_json({"event": "stop"})
                            with contextlib.suppress(TimeoutError):
                                async with asyncio.timeout(5):
                                    async for _ in socket:
                                        pass
        finally:
            self.stopping = True
            for process in self.processes:
                if process.returncode is None:
                    process.terminate()
                await process.wait()
            await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", help="Path to the existing Tapo bridge .env")
    parser.add_argument("--aurvek")
    parser.add_argument("--go2rtc")
    parser.add_argument("--stream")
    parser.add_argument("--rtsp")
    parser.add_argument("--model-id", type=int)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    load_dotenv(args.env) if args.env else load_dotenv()
    args.aurvek = args.aurvek or os.environ.get("AURVEK_BASE_URL", "")
    args.go2rtc = local_url(args.go2rtc or os.environ.get("TAPO_GO2RTC_URL", "http://127.0.0.1:1984"), scheme={"http"})
    args.stream = args.stream or os.environ.get("TAPO_GO2RTC_STREAM")
    if not args.stream:
        raise ValueError("TAPO_GO2RTC_STREAM is required")
    args.rtsp = local_url(args.rtsp or "rtsp://127.0.0.1:8554/" + quote(args.stream, safe=""), scheme={"rtsp"})
    aurvek_url(args.aurvek)
    try:
        asyncio.run(TapoLive(args).run())
    except KeyboardInterrupt:
        print("Tapo live session stopped.")


if __name__ == "__main__":
    main()
