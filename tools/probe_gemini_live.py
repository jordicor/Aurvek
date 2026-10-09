"""Bounded native Gemini probe using synthetic WAVs; no DB or PSTN writes."""
import argparse
import asyncio
import audioop
import base64
import io
import json
import os
from pathlib import Path
import sys
import wave
from dotenv import load_dotenv
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def probe(paths):
    from integrations.live.gemini import GeminiLiveClient
    from integrations.telephony.gemini_live_billing import calculate_api_cost
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    async def configuration():
        return {'instructions': 'This is a synthetic voice test. Answer briefly in English. Use ask_aurvek when asked for the synthetic code. Report the actual tool result. You can see the supplied camera frame.',
                'input': [], 'audio': {'output': {'voice': 'Kore'}}}
    client = GeminiLiveClient(api_key_provider=lambda: os.environ.get('GEMINI_KEY'), session_loader=configuration)
    results = {'turns': 0, 'tools': 0, 'audio_bytes': 0, 'interruptions': 0}
    usages, transcript = [], []
    completed = asyncio.Event()
    async def receive():
        async for event in client.events():
            kind = event['type']
            if kind == 'session.output_audio.delta':
                results['audio_bytes'] += len(base64.b64decode(event['delta']))
            elif kind == 'session.delegation.created':
                results['tools'] += 1
                await client.commentary('The synthetic code is seven.', delegation_id=event['delegation']['id'])
            elif kind == 'session.usage.updated': usages.append(event['usage'])
            elif kind == 'session.output_transcript.delta': transcript.append(event['delta'])
            elif kind == 'session.output_audio.interrupted': results['interruptions'] += 1
            elif kind == 'session.turn.completed':
                results['turns'] += 1
                completed.set()
    reader = None
    try:
        async with asyncio.timeout(90):
            await client.connect()
            reader = asyncio.create_task(receive())
            frame = io.BytesIO()
            Image.new('RGB', (320, 240), 'blue').save(frame, format='JPEG')
            for path in paths:
                await client.send_video(frame.getvalue())
                with wave.open(str(path), 'rb') as wav:
                    if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getnframes() > wav.getframerate() * 20:
                        raise ValueError('Use mono PCM16 synthetic speech shorter than 20 seconds')
                    pcm, _ = audioop.ratecv(wav.readframes(wav.getnframes()), 2, 1, wav.getframerate(), 16000, None)
                completed.clear()
                for offset in range(0, len(pcm), 640):
                    await client.send_audio(pcm[offset:offset + 640])
                    await asyncio.sleep(.02)
                for _ in range(75):
                    await client.send_audio(b'\0' * 640)
                    await asyncio.sleep(.02)
                waiter = asyncio.create_task(completed.wait())
                try:
                    done, _ = await asyncio.wait([reader, waiter], timeout=30, return_when=asyncio.FIRST_COMPLETED)
                    if reader in done: await reader
                    if not completed.is_set(): raise RuntimeError('Native audio response timed out')
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            if not results['audio_bytes'] or results['turns'] < len(paths) or not any(u.audio_input_tokens for u in usages):
                raise RuntimeError('Native audio acceptance failed')
            if not any(u.visual_input_tokens for u in usages): raise RuntimeError('Camera frame was not accounted for')
            print(json.dumps({**results, 'provider_cost_usd': sum(map(calculate_api_cost, usages)), 'transcript': ''.join(transcript)}, ensure_ascii=True))
    finally:
        await client.close()
        if reader:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', type=Path, required=True, action='append')
    asyncio.run(probe(parser.parse_args().audio))
