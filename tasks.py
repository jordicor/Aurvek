# tasks.py

from rediscfg import broker, redis_client
import dramatiq
import asyncio

# Import functions to generate and save PDF and MP3
#from tools import *
from tools import dramatiq_tasks

#from tools.perplexity import query_perplexity
from tools.download_pdf import generate_and_save_pdf
from tools.download_mp3 import generate_and_save_mp3

# Define task to generate PDFs
@dramatiq.actor
def generate_pdf_task(conversation_id: int, user_id: int, is_admin: bool):
    import asyncio
    asyncio.run(generate_and_save_pdf(conversation_id, user_id, is_admin))

# Define task to generate MP3s
@dramatiq.actor
def generate_mp3_task(conversation_id: int, user_id: int, is_admin: bool):
    import asyncio
    asyncio.run(generate_and_save_mp3(conversation_id, user_id, is_admin))


@dramatiq.actor(max_retries=5, min_backoff=10_000, max_backoff=300_000)
def download_elevenlabs_audio_task(conversation_id: int, session_id: str, user_id: int):
    import asyncio
    from integrations.elevenlabs.service import service as elevenlabs_service
    asyncio.run(elevenlabs_service.download_conversation_audio(conversation_id, session_id, user_id))


@dramatiq.actor(queue_name="gransabio", max_retries=0, max_age=None)
def gransabio_external_task(
    conversation_id: int,
    user_message_json: str,
    platform: str,
    platform_context_json: str,
    estimated_timeout: int,
):
    """Durable background task for GranSabio on external channels.

    max_age=None disables Dramatiq's AgeLimit middleware for this actor.
    The global AgeLimit (5 min) would silently discard messages if workers
    are temporarily busy. GranSabio pipelines are long-running.

    time_limit is set per-message via .send_with_options() to
    SESSION_TIMEOUT_CAP (28_800_000 ms = 8 hours).
    """
    import asyncio
    import orjson
    asyncio.run(_run_gransabio_external(
        conversation_id,
        orjson.loads(user_message_json),
        platform,
        orjson.loads(platform_context_json),
        estimated_timeout,
    ))


async def _run_gransabio_external(conversation_id, user_message, platform, platform_context, estimated_timeout):
    """Wrapper that creates a fresh httpx client for the Dramatiq worker.

    Dramatiq runs asyncio.run() per job, creating a NEW event loop each time.
    The module-level cached httpx client in gransabio_service is bound to the
    FastAPI event loop and MUST NOT be used here.
    """
    import httpx
    from gransabio_service import process_gransabio_external, _HTTP_CLIENT_KWARGS

    async with httpx.AsyncClient(**_HTTP_CLIENT_KWARGS) as client:
        await process_gransabio_external(
            conversation_id=conversation_id,
            user_id=0,  # Resolved inside process_gransabio_external from conversation
            user_message=user_message,
            platform=platform,
            platform_context=platform_context,
            http_client=client,
            estimated_timeout=estimated_timeout,
        )
