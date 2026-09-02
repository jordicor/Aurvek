# tools/generate_videos.py

import os
import orjson
import asyncio
import aiohttp
import dramatiq
import time
import re
import secrets
import base64
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Response, HTTPException, Depends, Request, Form, status, UploadFile, File
from urllib.parse import urlparse

# Own libraries
from rediscfg import redis_client, broker
from common import estimate_message_tokens, Cost, get_runtime_request_url
from models import User, ConnectionManager
from integrations.conversations import is_whatsapp_conversation
from tools import register_tool, register_dramatiq_task, register_function_handler
from tools.delivery_ack import publish_result_with_ack
from database import get_db_connection
from storage_quota import (
    StorageQuotaExceededError,
    delete_generated_file_rows,
    ensure_generation_headroom,
    record_generated_file,
)
from save_images import save_image_locally, generate_img_token, resize_image
from auth import hash_password, verify_password, get_user_by_username, get_current_user, create_access_token, get_user_by_id, get_user_from_phone_number
from auth import get_current_user_from_websocket, get_user_id_from_conversation, get_user_by_token, create_user_info, create_login_response, generate_magic_link
from common import Cost, generate_user_hash, has_sufficient_balance, cost_tts, cache_directory, users_directory, elevenlabs_key, openai_key, tts_engine, get_balance, deduct_balance, record_daily_usage, load_service_costs, ALGORITHM, estimate_message_tokens, CLOUDFLARE_BASE_URL, MEDIA_TOKEN_EXPIRE_HOURS
from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    claim_fixed_usage_provider,
    mark_fixed_usage_provider_succeeded,
    refund_fixed_usage,
    reserve_fixed_usage,
    settle_fixed_usage,
)

load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_KEY')
VIDEO_GENERATION_ENGINE = os.getenv('VIDEO_GENERATION_ENGINE', 'veo-3').lower()
VIDEO_GENERATION_TIMEOUT = int(os.getenv('VIDEO_GENERATION_TIMEOUT', 600))  # Timeout in seconds, default 10 minutes

# VEO Model Configuration
# Available models (Gemini API):
#   - veo-3.1-fast-generate-preview (fast, good for most use cases)
#   - veo-3.1-generate-preview (best quality, slower)
#   - veo-3.1-lite-generate-preview (lowest-cost VEO 3.1 variant)
VEO_MODEL = os.getenv('VEO_MODEL', 'veo-3.1-fast-generate-preview')
VEO_DURATION_SECONDS = 8
VEO_RESOLUTION = '720p'


def _video_billing_service_name() -> str:
    """Resolve a SERVICES name for the concrete VEO model and output plan."""
    if VIDEO_GENERATION_ENGINE != 'veo-3':
        return f'VIDEO-{VIDEO_GENERATION_ENGINE.upper()}'
    normalized_model = VEO_MODEL.lower()
    if 'veo-3.1' in normalized_model and 'lite' in normalized_model:
        return 'VIDEO-VEO-3.1-LITE-8S-720P'
    if 'veo-3.1' in normalized_model and 'fast' in normalized_model:
        return 'VIDEO-VEO-3.1-FAST-8S-720P'
    if 'veo-3.1' in normalized_model:
        return 'VIDEO-VEO-3.1-STANDARD-8S-720P'
    model_component = re.sub(r'[^A-Z0-9]+', '-', VEO_MODEL.upper()).strip('-')
    return f'VIDEO-{model_component}-8S-720P'


async def _publish_video_event(channel_name: str, payload) -> None:
    """Treat progress/error delivery as best effort, never as provider state."""
    try:
        await redis_client.publish(channel_name, payload)
    except Exception as exc:
        print(f"WARNING: Could not publish video event: {exc}")

async def generate_video_veo3(prompt: str, aspect_ratio: str = "16:9", negative_prompt: str = None) -> tuple:
    """Generate video using Google's VEO-3 model with official google-genai library"""
    try:
        # Import google-genai library
        from google import genai
        from google.genai import types
        
        # Create client
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # Create generation prompt
        generation_prompt = f"Create a high-quality 8-second video: {prompt}"
        
        # Create config with available parameters
        config_params = {}
        if negative_prompt:
            config_params["negative_prompt"] = negative_prompt
        config_params["aspect_ratio"] = aspect_ratio
        config_params["duration_seconds"] = VEO_DURATION_SECONDS
        config_params["resolution"] = VEO_RESOLUTION
            
        config = types.GenerateVideosConfig(**config_params)
        
        print(f"Starting VEO-3 video generation with prompt: {generation_prompt}")
        
        # Start video generation operation
        operation = client.models.generate_videos(
            model=VEO_MODEL,
            prompt=generation_prompt,
            config=config
        )
        
        print(f"Video generation operation started: {operation.name}")
        
        # Poll for completion with progress updates
        max_wait_time = 360  # 6 minutes in seconds
        poll_interval = 10   # 10 seconds between polls
        elapsed_time = 0
        
        while not operation.done:
            await asyncio.sleep(poll_interval)
            elapsed_time += poll_interval
            
            # Update operation status
            operation = client.operations.get(operation)
            
            print(f"Video generation in progress... {elapsed_time}s elapsed (max {max_wait_time}s)")
            
            if elapsed_time >= max_wait_time:
                raise Exception("Video generation timed out after 6 minutes")
        
        # Get the generated video
        if hasattr(operation, 'result') and hasattr(operation.result, 'generated_videos'):
            generated_video = operation.result.generated_videos[0]
            
            # Download video data
            video_file = generated_video.video
            video_data = client.files.download(file=video_file)
            
            # Determine mime type
            mime_type = "video/mp4"  # VEO-3 typically generates MP4
            
            print(f"Video generation completed! Size: {len(video_data)} bytes")
            
            return video_data, prompt, aspect_ratio, mime_type
        else:
            raise Exception("No video generated in operation result")
            
    except ImportError:
        raise Exception("google-genai library not installed. Install with: pip install google-genai")
    except Exception as e:
        if "google-genai" in str(e):
            raise Exception(f"Google GenAI library error: {str(e)}")
        else:
            raise Exception(f"VEO-3 generation failed: {str(e)}")

async def generate_video(prompt: str) -> tuple:
    print(f"Generating video using {VIDEO_GENERATION_ENGINE}")
    
    # Extract aspect ratio from prompt if specified
    aspect_ratio_match = re.search(r'\b(\d+:\d+)\b', prompt)
    if aspect_ratio_match:
        aspect_ratio = aspect_ratio_match.group(1)
        prompt = re.sub(r'\b\d+:\d+\b', '', prompt).strip()
    else:
        aspect_ratio = "16:9"

    # Extract negative prompt if specified
    negative_prompt = None
    negative_match = re.search(r'(?:without|avoid|exclude|no)\s+([^.!?]+)', prompt, re.IGNORECASE)
    if negative_match:
        negative_prompt = negative_match.group(1).strip()

    if VIDEO_GENERATION_ENGINE == 'veo-3':
        return await generate_video_veo3(prompt, aspect_ratio, negative_prompt)
    else:
        raise ValueError(f"Unsupported video generation engine: {VIDEO_GENERATION_ENGINE}")

async def save_video_locally(video_data, filename, user, conversation_id, source="bot", format="mp4"):
    """Save video file locally using the same structure as images"""
    file_path = None
    try:
        # Use the same directory structure as images
        from common import users_directory
        
        # Generate the user hash
        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(user.username)

        # Create the conversation prefixes according to the specified format
        conversation_id_str = f"{conversation_id:07d}"
        conversation_id_prefix1 = conversation_id_str[:3]
        conversation_id_prefix2 = conversation_id_str[3:]
        
        # Build the directory path (video instead of img)
        file_location = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "files", conversation_id_prefix1, conversation_id_prefix2, "video", source)
        
        # Create the directory if it doesn't exist
        os.makedirs(file_location, exist_ok=True)
        
        base_filename = filename
        
        # Save the video file
        file_path = os.path.join(file_location, f"{base_filename}.{format}")
        with open(file_path, 'wb') as f:
            f.write(video_data)

        # Ledger the generated video so it counts against the owner's storage
        # quota (one row per file on disk). Fail fast: the file is written first,
        # then ledgered immediately after -- if the ledger insert fails, the
        # except block below removes the file and re-raises, so an unaccounted
        # artifact never exists.
        async with get_db_connection() as conn:
            await record_generated_file(conn, conversation_id, 'video', file_path, len(video_data))
            await conn.commit()

        # Generate video path (same structure as images)
        video_path = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/files/{conversation_id_prefix1}/{conversation_id_prefix2}/video/{source}/{base_filename}.{format}"
        
        # Generate URLs (same as images - no token in database)
        base_url = f"{CLOUDFLARE_BASE_URL}{video_path}"
        token_url = base_url  # Same as base_url, process_message will add token when serving
        
        print(f"Video saved: {file_path}")
        print(f"Video URL: {token_url}")
        
        return base_url, token_url, file_path
        
    except Exception as e:
        if file_path:
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass
        print(f"Error saving video: {e}")
        raise e


async def _delete_generated_video(file_path: str) -> None:
    media_root = Path(users_directory).resolve()
    resolved_path = Path(file_path).resolve()
    if not resolved_path.is_relative_to(media_root):
        raise ValueError("Refusing to delete video outside the user media root")
    await asyncio.to_thread(resolved_path.unlink, missing_ok=True)
    # Drop the ledger row together with the file so a refunded video stops
    # counting against the owner's storage quota.
    async with get_db_connection() as conn:
        await delete_generated_file_rows(conn, [str(resolved_path)])
        await conn.commit()


async def generate_video_task(channel_name: str, prompt: str, conversation_id: int, user_id: int, is_whatsapp: bool, request_url: str, reservation_id: str):
    saved_video_path = None
    provider_succeeded = False
    try:
        print("Entering generate_video_task")
        if not await claim_fixed_usage_provider(
            reservation_id,
            purpose="video",
            user_id=user_id,
        ):
            return
        
        # Send initial status update
        await _publish_video_event(channel_name, orjson.dumps({
            'progress_update': 'Starting video generation...'
        }).decode())
        
        # Custom generate_video_veo3_with_progress function
        try:
            from google import genai
            from google.genai import types
            
            client = genai.Client(api_key=GEMINI_API_KEY)
            generation_prompt = f"Create a high-quality 8-second video: {prompt}"
            
            config = types.GenerateVideosConfig(
                duration_seconds=VEO_DURATION_SECONDS,
                resolution=VEO_RESOLUTION,
            )
            
            print(f"Starting VEO-3 video generation with prompt: {generation_prompt}")
            
            # Start video generation operation
            operation = client.models.generate_videos(
                model=VEO_MODEL,
                prompt=generation_prompt,
                config=config
            )
            
            await _publish_video_event(channel_name, orjson.dumps({
                'progress_update': 'Video generation started'
            }).decode())
            
            # Poll for completion with progress updates
            max_wait_time = 360  # 6 minutes in seconds
            poll_interval = 10   # 10 seconds between polls
            elapsed_time = 0
            
            while not operation.done:
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval
                
                # Update operation status
                operation = client.operations.get(operation)
                
                # Send progress update to chat
                progress_msg = f"Generating video... {elapsed_time}s elapsed (up to 6min)"
                await _publish_video_event(channel_name, orjson.dumps({
                    'progress_update': progress_msg
                }).decode())
                
                print(f"Video generation in progress... {elapsed_time}s elapsed")
                
                if elapsed_time >= max_wait_time:
                    raise Exception("Video generation timed out after 6 minutes")
            
            # Get the generated video
            if hasattr(operation, 'result') and hasattr(operation.result, 'generated_videos'):
                generated_video = operation.result.generated_videos[0]
                provider_succeeded = True
                if not await mark_fixed_usage_provider_succeeded(
                    reservation_id,
                    purpose="video",
                    user_id=user_id,
                ):
                    raise BillingReservationError(
                        "Video billing reservation is no longer active"
                    )
                if not await settle_fixed_usage(reservation_id):
                    raise BillingReservationError(
                        "Video billing reservation is no longer active"
                    )
                video_file = generated_video.video
                video_data = client.files.download(file=video_file)
                mime_type = "video/mp4"
                
                print(f"Video generation completed! Size: {len(video_data)} bytes")
            else:
                raise Exception("No video generated in operation result")
            
        except Exception as e:
            raise Exception(f"VEO-3 generation failed: {str(e)}")
        
        filename = f"generated_video_{secrets.token_hex(24)}"
        source = "bot"
        format = "mp4"

        user = await get_user_by_id(user_id)

        await _publish_video_event(channel_name, orjson.dumps({
            'progress_update': 'Saving video...'
        }).decode())

        video_link_base, video_link_token, saved_video_path = await save_video_locally(
            video_data=video_data,
            filename=filename,
            user=user,
            conversation_id=conversation_id,
            source=source,
            format=format
        )

        # Generate token for immediate display (same as process_message does for DB loads)
        from datetime import datetime, timezone, timedelta
        expiration = datetime.now(timezone.utc) + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)
        token = generate_img_token(video_link_base, expiration, user)
        video_url_with_token = f"{video_link_base}?token={token}"

        # Use the same JSON structure for both display and save
        video_content = orjson.dumps([
            {
                "type": "video_url",
                "video_url": {
                    "url": video_url_with_token,  # Use URL with token for display
                    "alt": prompt,
                    "mime_type": mime_type
                }
            }
        ]).decode()

        content_to_show = video_content
        content_to_save = orjson.dumps([
            {
                "type": "video_url",
                "video_url": {
                    "url": video_link_base,  # Use base URL for storage
                    "alt": prompt,
                    "mime_type": mime_type
                }
            }
        ]).decode()

        await publish_result_with_ack(
            redis_client,
            channel_name=channel_name,
            payload={
                'content_to_show': content_to_show,
                'content_to_save': content_to_save,
            },
            reservation_id=reservation_id,
        )

    except Exception as e:
        print(f"Error in generate_video_task: {e}")
        refunded = False
        if not provider_succeeded:
            try:
                refunded = await refund_fixed_usage(reservation_id)
            except BillingReservationError:
                print(f"WARNING: Failed to refund video reservation {reservation_id}")
        if refunded and saved_video_path and not provider_succeeded:
            try:
                await _delete_generated_video(saved_video_path)
            except Exception as cleanup_error:
                print(
                    "WARNING: Failed to remove refunded video "
                    f"{saved_video_path}: {cleanup_error}"
                )
        await _publish_video_event(
            channel_name,
            orjson.dumps({'error': str(e)}).decode(),
        )
        await _publish_video_event(channel_name, 'END')

@dramatiq.actor(max_retries=0, max_age=None, time_limit=600_000)
def generate_video_task_actor(channel_name: str, prompt: str, conversation_id: int, user_id: int, is_whatsapp: bool, request_url: str, reservation_id: str):
    asyncio.run(generate_video_task(channel_name, prompt, conversation_id, user_id, is_whatsapp, request_url, reservation_id))

async def handle_generate_video(function_arguments, messages, model, temperature, max_tokens, content, conversation_id, current_user, request, input_tokens, output_tokens, total_tokens, message_id, user_id, client, prompt, user_message=None):
    reservation_id = None
    reservation_handed_off = False
    try:
        print("Entering handle_generate_video")
        if (
            current_user is None
            or int(getattr(current_user, "id", -1)) != int(user_id)
            or not bool(getattr(current_user, "can_generate_images", False))
        ):
            yield f"data: {orjson.dumps({'content': 'Video generation is not enabled for this account.', 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
            return
        video_prompt = function_arguments['prompt']
        channel_name = (
            f"generate_video_response_{conversation_id}_{user_id}_"
            f"{secrets.token_urlsafe(12)}"
        )
        
        is_whatsapp = await is_whatsapp_conversation(conversation_id)
        request_url = get_runtime_request_url(request)

        # Storage-quota soft pre-check: a generation may START only while the
        # owner is strictly under quota. Runs BEFORE any billing reservation so
        # we never charge for an operation the quota check then kills. The
        # requester is verified to be the conversation owner (user_id) above.
        try:
            async with get_db_connection(readonly=True) as quota_conn:
                await ensure_generation_headroom(quota_conn, user_id)
        except StorageQuotaExceededError as quota_exc:
            yield f"data: {orjson.dumps({'content': quota_exc.message, 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
            return

        video_service_name = _video_billing_service_name()
        video_cost, video_service_id = Cost.get_media_generation_service(
            video_service_name
        )
        reservation_id = await reserve_fixed_usage(
            user_id=user_id,
            purpose="video",
            amount=video_cost,
            service_id=video_service_id,
            usage_quantity=1,
        )

        async with redis_client.pubsub() as pubsub:
            await pubsub.subscribe(channel_name)
            generate_video_task_actor.send(
                channel_name,
                video_prompt,
                conversation_id,
                user_id,
                is_whatsapp,
                request_url,
                reservation_id,
            )
            reservation_handed_off = True

            yield f"data: {orjson.dumps({'content': 'Generating video... This may take up to 6 minutes.', 'save_to_db': False, 'yield': True}).decode()}\n\n"

            start_time = time.time()
            while True:
                if time.time() - start_time > VIDEO_GENERATION_TIMEOUT:
                    yield f"data: {orjson.dumps({'content': 'Video generation timed out. Please try again.', 'save_to_db': True, 'yield': True}).decode()}\n\n"
                    break
                
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message:
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    if data == 'END':
                        break
                    json_data = orjson.loads(data)
                    if 'error' in json_data:
                        yield f"data: {orjson.dumps({'content': json_data['error'], 'save_to_db': True, 'yield': True}).decode()}\n\n"
                    elif 'progress_update' in json_data:
                        # Send progress update without saving to DB
                        yield f"data: {orjson.dumps({'content': json_data['progress_update'], 'save_to_db': False, 'yield': True, 'replace_last': True}).decode()}\n\n"
                    elif 'content_to_show' in json_data and 'content_to_save' in json_data:
                        # Send video as separate type for proper rendering
                        yield f"data: {orjson.dumps({'video_content': json_data['content_to_show'], 'save_to_db': False, 'yield': True, '_delivery_ack': json_data.get('_delivery_ack')}).decode()}\n\n"
                        yield f"data: {orjson.dumps({'content': json_data['content_to_save'], 'save_to_db': True, 'yield': False, '_delivery_ack': json_data.get('_delivery_ack')}).decode()}\n\n"
                else:
                    await asyncio.sleep(0.1)
        
    except InsufficientBalanceError:
        yield f"data: {orjson.dumps({'content': 'Insufficient balance to generate video.', 'save_to_db': True, 'yield': True}).decode()}\n\n"
    except BillingReservationError:
        yield f"data: {orjson.dumps({'content': 'Video billing is temporarily unavailable.', 'save_to_db': True, 'yield': True}).decode()}\n\n"
    except Exception as e:
        print(f"Error in handle_generate_video: {e}")
        yield f"data: {orjson.dumps({'content': f'Error generating video: {str(e)}', 'save_to_db': True, 'yield': True}).decode()}\n\n"
    finally:
        if reservation_id and not reservation_handed_off:
            try:
                await refund_fixed_usage(reservation_id)
            except BillingReservationError:
                print(f"WARNING: Failed to refund video reservation {reservation_id}")

# Register the tool for semantic router
register_tool({
    "type": "function",
    "function": {
        "name": "generateVideo",
        "description": f"Generate a high-quality 8-second video with audio using {VIDEO_GENERATION_ENGINE.upper()} (Google VEO-3) based on the provided prompt. You can specify aspect ratio (e.g., 16:9, 9:16, 1:1) and negative prompts in the description.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt to generate the video, optionally including desired aspect ratio and elements to avoid"
                }
            },
            "required": ["prompt"],
            "additionalProperties": False
        }
    },
    "strict": True
})

# Register the Dramatiq task
register_dramatiq_task("generate_video_task_actor", generate_video_task_actor)

# Register the function handler
register_function_handler("generateVideo", handle_generate_video)
