# tools/generate_images.py

import os
import orjson
import asyncio
import aiohttp
import dramatiq
import time
import re
import secrets
import base64
import io
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Union
from dotenv import load_dotenv
from datetime import datetime
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Response, HTTPException, Depends, Request, Form, status, UploadFile, File
from urllib.parse import urlparse

# Own libraries
from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    claim_fixed_usage_provider,
    mark_fixed_usage_provider_succeeded,
    refund_fixed_usage,
    reserve_fixed_usage,
    settle_fixed_usage,
)
from rediscfg import redis_client, broker
from common import Cost, get_runtime_request_url
from database import get_db_connection
from storage_quota import StorageQuotaExceededError, ensure_generation_headroom
from log_config import logger
from models import User, ConnectionManager
from integrations.conversations import is_whatsapp_conversation
from tools import register_tool, register_dramatiq_task, register_function_handler
from tools.delivery_ack import publish_result_with_ack
from save_images import save_image_locally, get_or_generate_img_token, resize_image
from auth import hash_password, verify_password, get_user_by_username, get_current_user, create_access_token, get_user_by_id, get_user_from_phone_number
from auth import get_current_user_from_websocket, get_user_id_from_conversation, get_user_by_token, create_user_info, create_login_response, generate_magic_link

load_dotenv()

# =============================================================================
# API KEYS
# =============================================================================
OPENAI_API_KEY = os.getenv('OPENAI_KEY')
IDEOGRAM_API_KEY = os.getenv('IDEOGRAM_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_KEY')
POE_API_KEY = os.getenv('POE_API_KEY')

# =============================================================================
# ENGINE CONFIGURATION
# =============================================================================
IMAGE_GENERATION_ENGINE = os.getenv('IMAGE_GENERATION_ENGINE', 'dall-e').lower()
IMAGE_GENERATION_TIMEOUT = int(os.getenv('IMAGE_GENERATION_TIMEOUT', 120))

# Model configuration
GEMINI_IMAGE_MODEL = os.getenv('GEMINI_IMAGE_MODEL', 'gemini-2.5-flash-image')
OPENAI_IMAGE_MODEL = os.getenv('OPENAI_IMAGE_MODEL', 'gpt-image-1')
POE_IMAGE_MODEL = os.getenv('POE_IMAGE_MODEL', 'flux2pro')

_IMAGE_SERVICE_BY_ENGINE = {
    'ideogram': 'IMAGE-IDEOGRAM-V2',
    'poe': 'IMAGE-POE',
    'openai': 'IMAGE-OPENAI-GPT-IMAGE',
}

_active_image_billing: ContextVar[dict | None] = ContextVar(
    "active_image_billing",
    default=None,
)


async def _mark_active_image_provider_succeeded() -> None:
    """Mark remote success before fallible local image processing."""
    billing = _active_image_billing.get()
    if not billing:
        return
    # Set this first: if durable marking itself fails after a billable response,
    # the task must not refund known external work.
    billing["provider_completed"] = True
    if billing.get("provider_marked"):
        return
    marked = await mark_fixed_usage_provider_succeeded(
        billing["reservation_id"],
        purpose="image",
        user_id=billing["user_id"],
    )
    if not marked:
        raise BillingReservationError(
            "Image billing reservation is no longer active"
        )
    billing["provider_marked"] = True

# =============================================================================
# MODEL CAPABILITIES (max reference images supported)
# =============================================================================
POE_MODEL_MAX_REFS = {
    "nanobananapro": 14,      # Gemini 3 Pro via Poe - 14 refs, 5 faces
    "flux2pro": 8,            # Best quality
    "flux2flex": 8,           # High resolution (14MP)
    "fluxkontextpro": 1,      # Best prompt following (editing model)
    "seedream40": 8,          # Good for combining references
    "Ideogram-v3": 3,         # Best for text/logos
}

GEMINI_MODEL_MAX_REFS = {
    "gemini-3-pro-image-preview": 14,  # Up to 6 objects + 5 humans
    "gemini-2.5-flash-image": 3,       # Recommended for best results
}

OPENAI_MODEL_MAX_REFS = {
    "gpt-image-1.5": 16,
    "gpt-image-1": 16,
    "gpt-image-1-mini": 16,
    "dall-e-3": 0,  # Does NOT support references
}


def _extract_image_ratio(prompt: str) -> str:
    ratio_match = re.search(r'\b(\d+:\d+)\b', prompt or '')
    return ratio_match.group(1) if ratio_match else '1:1'


def _dalle_size_for_ratio(ratio: str) -> str:
    if ratio in {'16:9', '4:3'}:
        return '1792x1024'
    if ratio in {'9:16', '3:4'}:
        return '1024x1792'
    return '1024x1024'


def _image_billing_service_name(prompt: str) -> str:
    """Resolve the configured service that matches the provider call."""
    selected_engine = IMAGE_GENERATION_ENGINE
    if selected_engine == 'dall-e':
        dalle_size = _dalle_size_for_ratio(_extract_image_ratio(prompt))
        if dalle_size != '1024x1024':
            return 'IMAGE-DALL-E-3-STANDARD-WIDE'
        return 'IMAGE-DALL-E-3-STANDARD-SQUARE'
    if selected_engine in ('nano-banana', 'gemini'):
        if GEMINI_IMAGE_MODEL == 'gemini-2.5-flash-image':
            return 'IMAGE-GEMINI-2.5-FLASH'
        model_component = re.sub(
            r'[^A-Z0-9]+',
            '-',
            GEMINI_IMAGE_MODEL.upper(),
        ).strip('-')
        return f'IMAGE-GEMINI-{model_component}'
    return _IMAGE_SERVICE_BY_ENGINE.get(
        selected_engine,
        f'IMAGE-{selected_engine.upper()}',
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _prepare_reference_image(image_source: Union[bytes, str, Path], max_size: int = 1024) -> Optional[str]:
    """
    Prepare a reference image for API consumption.

    Args:
        image_source: Image bytes, base64 string, file path, or URL
        max_size: Maximum dimension (width or height) for resizing

    Returns:
        Base64 encoded JPEG string, or None if failed
    """
    try:
        from PIL import Image

        img = None

        if isinstance(image_source, bytes):
            img = Image.open(io.BytesIO(image_source))
        elif isinstance(image_source, (str, Path)):
            path = Path(image_source)
            if path.exists():
                img = Image.open(path)
            elif isinstance(image_source, str) and image_source.startswith('data:image'):
                # Base64 data URL
                header, data = image_source.split(',', 1)
                img = Image.open(io.BytesIO(base64.b64decode(data)))
            elif isinstance(image_source, str) and image_source.startswith('http'):
                # URL - would need to download, skip for now
                return None

        if img is None:
            return None

        # Convert to RGB if necessary
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        # Resize if too large
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size))

        # Convert to base64 JPEG
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=85)
        return base64.b64encode(buffer.getvalue()).decode()

    except Exception as e:
        print(f"Error preparing reference image: {e}")
        return None


# =============================================================================
# DALL-E 3 (NO REFERENCE SUPPORT)
# =============================================================================

async def generate_image_dalle(prompt: str, ratio: str = "1:1") -> tuple:
    """Generate image using DALL-E 3 (no reference support)"""
    # DALL-E 3 only accepts these native square/landscape/portrait sizes.
    size = _dalle_size_for_ratio(ratio)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }
    data = {
        "model": "dall-e-3",
        "prompt": prompt,
        "n": 1,
        "size": size
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.openai.com/v1/images/generations", headers=headers, json=data, timeout=IMAGE_GENERATION_TIMEOUT) as response:
                if response.status == 200:
                    result = await response.json()
                    image_url = result['data'][0]['url']
                    await _mark_active_image_provider_succeeded()
                    revised_prompt = result['data'][0].get('revised_prompt', prompt)
                    async with session.get(image_url, timeout=IMAGE_GENERATION_TIMEOUT) as img_response:
                        return await img_response.read(), revised_prompt, ratio
                else:
                    raise Exception(f"Error generating image with DALL-E: {await response.text()}")
        except asyncio.TimeoutError:
            raise Exception("Timeout occurred while generating image with DALL-E")


# =============================================================================
# IDEOGRAM (NO REFERENCE SUPPORT)
# =============================================================================

async def generate_image_ideogram(prompt: str, ratio: str = "1:1") -> tuple:
    """Generate image using Ideogram V2 (no reference support)"""
    url = "https://api.ideogram.ai/generate"

    aspect_ratio_map = {
        "1:1": "ASPECT_1_1",
        "16:9": "ASPECT_16_9",
        "9:16": "ASPECT_9_16",
        "4:3": "ASPECT_4_3",
        "3:4": "ASPECT_3_4"
    }
    aspect_ratio = aspect_ratio_map.get(ratio, "ASPECT_1_1")

    payload = {
        "image_request": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "model": "V_2",
            "magic_prompt_option": "AUTO"
        }
    }
    headers = {
        "Api-Key": IDEOGRAM_API_KEY,
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers=headers, timeout=IMAGE_GENERATION_TIMEOUT) as response:
                if response.status == 200:
                    result = await response.json()
                    image_url = result['data'][0]['url']
                    await _mark_active_image_provider_succeeded()
                    print(f"image url: {image_url}")
                    async with session.get(image_url, timeout=IMAGE_GENERATION_TIMEOUT) as img_response:
                        return await img_response.read(), prompt, ratio
                else:
                    raise Exception(f"Error generating image with Ideogram: {await response.text()}")
        except asyncio.TimeoutError:
            raise Exception("Timeout occurred while generating image with Ideogram")


# =============================================================================
# GEMINI (WITH REFERENCE SUPPORT)
# =============================================================================

async def generate_image_gemini(
    prompt: str,
    ratio: str = "1:1",
    reference_images: list = None,
    model: str = None
) -> tuple:
    """
    Generate image using Google Gemini.

    Args:
        prompt: Text prompt for image generation
        ratio: Aspect ratio (1:1, 16:9, 9:16, 4:3, 3:4)
        reference_images: Optional list of reference images (bytes, paths, or base64)
        model: Model to use (default: GEMINI_IMAGE_MODEL from env)

    Returns:
        tuple: (image_bytes, prompt, ratio)
    """
    selected_model = model or GEMINI_IMAGE_MODEL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent"

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    # Build content parts
    parts = []

    # Add reference images if provided
    if reference_images:
        max_refs = GEMINI_MODEL_MAX_REFS.get(selected_model, 3)
        refs_added = 0

        for ref in reference_images[:max_refs]:
            b64_data = _prepare_reference_image(ref)
            if b64_data:
                parts.append({
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": b64_data
                    }
                })
                refs_added += 1

        if refs_added > 0:
            print(f"Added {refs_added} reference images to Gemini request")

    # Build the text prompt
    ratio_instruction = ""
    if ratio != "1:1":
        ratio_instruction = f" Make sure the image has an aspect ratio of {ratio}."

    if reference_images and len(reference_images) > 0:
        generation_prompt = f"""Use the reference images above to understand the visual style, characters, or elements to include.

Create a high-quality image: {prompt}{ratio_instruction}

Preserve the identity and appearance of any people shown in the references."""
    else:
        generation_prompt = f"Create a high-quality image: {prompt}{ratio_instruction}"

    parts.append({"text": generation_prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"]
        }
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers=headers, timeout=IMAGE_GENERATION_TIMEOUT) as response:
                if response.status == 200:
                    result = await response.json()

                    # Check for prompt blocking
                    if result.get('promptFeedback', {}).get('blockReason'):
                        block_reason = result['promptFeedback']['blockReason']
                        block_message = result['promptFeedback'].get('blockReasonMessage', '')
                        raise Exception(f"Request blocked by Gemini. Reason: {block_reason}. {block_message}")

                    # Extract image data from response
                    candidates = result.get('candidates', [])
                    if not candidates:
                        raise Exception("No candidates returned from Gemini")

                    parts = candidates[0].get('content', {}).get('parts', [])
                    image_part = None

                    for part in parts:
                        if 'inlineData' in part:
                            image_part = part['inlineData']
                            break

                    if not image_part:
                        finish_reason = candidates[0].get('finishReason', 'Unknown')
                        if finish_reason != 'STOP':
                            raise Exception(f"Image generation stopped unexpectedly. Reason: {finish_reason}")
                        raise Exception("No image data returned from Gemini model")

                    encoded_image = image_part.get('data')
                    if not encoded_image:
                        raise Exception("No image data returned from Gemini model")
                    await _mark_active_image_provider_succeeded()
                    image_data = base64.b64decode(encoded_image)
                    return image_data, prompt, ratio

                else:
                    error_text = await response.text()
                    raise Exception(f"Error generating image with Gemini: {error_text}")

        except asyncio.TimeoutError:
            raise Exception("Timeout occurred while generating image with Gemini")


# Alias for backward compatibility
async def generate_image_nano_banana(prompt: str, ratio: str = "1:1") -> tuple:
    """Generate image using Gemini (nano-banana alias for backward compatibility)"""
    return await generate_image_gemini(prompt, ratio)


# =============================================================================
# POE / FLUX (WITH REFERENCE SUPPORT)
# =============================================================================

async def generate_image_poe(
    prompt: str,
    ratio: str = "16:9",
    reference_images: list = None,
    model: str = None
) -> tuple:
    """
    Generate image using Poe API (FLUX/Ideogram models).

    Supports reference images for character/face consistency.
    Available models:
        - flux2pro: Best quality, up to 8 references
        - flux2flex: High resolution (14MP)
        - fluxkontextpro: Best prompt following (1 ref only)
        - seedream40: Good for combining references
        - nanobananapro: Gemini 3 Pro via Poe (14 refs)
        - Ideogram-v3: Best for text/logos

    Args:
        prompt: Text prompt for image generation
        ratio: Aspect ratio (16:9, 1:1, 9:16, 4:3, 3:4)
        reference_images: Optional list of reference images (bytes, paths, or base64)
        model: Model to use (default: POE_IMAGE_MODEL from env)

    Returns:
        tuple: (image_bytes, prompt, ratio)
    """
    if not POE_API_KEY:
        raise Exception("POE_API_KEY not configured. Add POE_API_KEY to your .env file.")

    try:
        from openai import OpenAI
        import requests
    except ImportError:
        raise Exception("openai package required for POE. Install with: pip install openai")

    # Initialize Poe client (OpenAI-compatible)
    client = OpenAI(
        api_key=POE_API_KEY,
        base_url="https://api.poe.com/v1",
    )

    selected_model = model or POE_IMAGE_MODEL
    print(f"Generating image with POE ({selected_model})...")

    # Build message content
    content = []

    # Add reference images
    has_refs = reference_images and len(reference_images) > 0
    if reference_images:
        max_images = POE_MODEL_MAX_REFS.get(selected_model, 8)
        refs_added = 0

        for ref in reference_images[:max_images]:
            b64_data = _prepare_reference_image(ref)
            if b64_data:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64_data}"
                    }
                })
                refs_added += 1

        if refs_added > 0:
            print(f"Added {refs_added} reference images to POE request")

    # Build the prompt
    if has_refs:
        full_prompt = f"""REFERENCE IMAGES PROVIDED ABOVE - Use them to maintain visual consistency.

SCENE TO CREATE:
{prompt}

REQUIREMENTS:
- Aspect ratio: {ratio}
- High quality, professional image
- If people are shown in references, preserve their exact appearance
- DO NOT include any text in the image"""
    else:
        full_prompt = f"""{prompt}

Requirements:
- Aspect ratio: {ratio}
- High quality, professional image
- DO NOT include any text in the image"""

    content.append({
        "type": "text",
        "text": full_prompt
    })

    # Make API call
    try:
        response = client.chat.completions.create(
            model=selected_model,
            messages=[{"role": "user", "content": content}],
            stream=False,
            extra_body={
                "aspect": ratio,
                "quality": "high"
            }
        )
    except Exception as e:
        raise Exception(f"POE API call failed: {e}")
    await _mark_active_image_provider_succeeded()

    # Extract image from response
    if response.choices and len(response.choices) > 0:
        message = response.choices[0].message

        if hasattr(message, 'content') and message.content:
            response_content = message.content
            image_url = None

            # Handle different response formats
            if isinstance(response_content, list):
                for block in response_content:
                    if isinstance(block, dict):
                        if block.get('type') == 'image_url':
                            image_url = block.get('image_url', {}).get('url')
                        elif block.get('type') == 'image':
                            image_url = block.get('url') or block.get('image_url')
                    elif hasattr(block, 'image_url'):
                        image_url = block.image_url.url if hasattr(block.image_url, 'url') else block.image_url

            elif isinstance(response_content, str):
                # Look for markdown image: ![...](URL)
                match = re.search(r'!\[.*?\]\((https?://[^\)]+)\)', response_content)
                if match:
                    image_url = match.group(1)
                elif response_content.startswith('http'):
                    image_url = response_content.strip()
                elif response_content.startswith('data:image'):
                    image_url = response_content

            # Download/decode the image
            if image_url:
                if image_url.startswith('data:image'):
                    # Base64 encoded image
                    header, data = image_url.split(',', 1)
                    img_bytes = base64.b64decode(data)
                else:
                    # URL - download image
                    img_response = requests.get(image_url, timeout=60)
                    if img_response.status_code != 200:
                        raise Exception(f"Failed to download image: HTTP {img_response.status_code}")
                    img_bytes = img_response.content

                print(f"POE image generated successfully ({len(img_bytes)} bytes)")
                return img_bytes, prompt, ratio
            else:
                raise Exception(f"No image URL found in POE response: {str(response_content)[:200]}")

    raise Exception("No image in POE response")


# =============================================================================
# OPENAI GPT-IMAGE (WITH REFERENCE SUPPORT)
# =============================================================================

async def generate_image_openai(
    prompt: str,
    ratio: str = "16:9",
    reference_images: list = None,
    model: str = None
) -> tuple:
    """
    Generate image using OpenAI gpt-image models.

    Supports reference images for gpt-image-1.x models (NOT dall-e-3).

    Args:
        prompt: Text prompt for image generation
        ratio: Aspect ratio (16:9, 1:1, 9:16, 4:3, 3:4)
        reference_images: Optional list of reference images (bytes, paths, or base64)
        model: Model to use (default: OPENAI_IMAGE_MODEL from env)

    Returns:
        tuple: (image_bytes, prompt, ratio)
    """
    try:
        from openai import OpenAI
        import requests
    except ImportError:
        raise Exception("openai package required. Install with: pip install openai")

    client = OpenAI(api_key=OPENAI_API_KEY)
    selected_model = model or OPENAI_IMAGE_MODEL

    # Determine size based on ratio
    size_map = {
        "1:1": "1024x1024",
        "16:9": "1536x1024",
        "9:16": "1024x1536",
        "4:3": "1536x1024",  # Closest available
        "3:4": "1024x1536",  # Closest available
    }
    size = size_map.get(ratio, "1536x1024")

    is_gpt_image = selected_model.startswith("gpt-image")
    max_refs = OPENAI_MODEL_MAX_REFS.get(selected_model, 0)
    has_refs = reference_images and len(reference_images) > 0 and max_refs > 0

    print(f"Generating image with OpenAI ({selected_model})...")

    response = None

    # Try with references first (only for gpt-image models)
    if is_gpt_image and has_refs:
        try:
            # Prepare image tuples for the edit endpoint
            image_files = []
            for ref in reference_images[:max_refs]:
                b64_data = _prepare_reference_image(ref)
                if b64_data:
                    # Convert to bytes for the API
                    img_bytes = base64.b64decode(b64_data)
                    image_files.append(img_bytes)

            if image_files:
                # Build prompt with reference context
                full_prompt = f"""Using the provided reference images for visual consistency:

{prompt}

Preserve the appearance and style from the references."""

                # Use images.edit for reference-based generation
                response = client.images.edit(
                    model=selected_model,
                    image=image_files,
                    prompt=full_prompt,
                    size=size,
                )
                await _mark_active_image_provider_succeeded()
                print(f"OpenAI edit API called with {len(image_files)} reference images")
        except BillingReservationError:
            raise
        except Exception as e:
            print(f"OpenAI edit with references failed, falling back to generate: {e}")
            response = None

    # Fallback to standard generation
    if response is None:
        response = client.images.generate(
            model=selected_model,
            prompt=prompt,
            size=size,
            quality="high" if is_gpt_image else "hd",
            n=1,
        )
        await _mark_active_image_provider_succeeded()

    # Extract image
    image_data = response.data[0]

    if hasattr(image_data, 'b64_json') and image_data.b64_json:
        img_bytes = base64.b64decode(image_data.b64_json)
    elif hasattr(image_data, 'url') and image_data.url:
        import requests
        img_response = requests.get(image_data.url, timeout=60)
        img_bytes = img_response.content
    else:
        raise Exception("No image data in OpenAI response")

    print(f"OpenAI image generated successfully ({len(img_bytes)} bytes)")
    return img_bytes, prompt, ratio


# =============================================================================
# UNIFIED GENERATION FUNCTION
# =============================================================================

async def generate_image(
    prompt: str,
    reference_images: list = None,
    engine: str = None,
    model: str = None
) -> tuple:
    """
    Generate an image using the configured or specified engine.

    Args:
        prompt: Text prompt (can include aspect ratio like "16:9")
        reference_images: Optional list of reference images for supported engines
        engine: Override the default engine (dall-e, ideogram, nano-banana, gemini, poe, openai)
        model: Override the default model for the engine

    Returns:
        tuple: (image_bytes, revised_prompt, ratio)
    """
    selected_engine = (engine or IMAGE_GENERATION_ENGINE).lower()
    print(f"Generating image using {selected_engine}")

    # Extract ratio from prompt
    ratio_match = re.search(r'\b(\d+:\d+)\b', prompt)
    if ratio_match:
        ratio = ratio_match.group(1)
        prompt = re.sub(r'\b\d+:\d+\b', '', prompt).strip()
    else:
        ratio = "1:1"

    # Route to appropriate engine
    if selected_engine == 'dall-e':
        return await generate_image_dalle(prompt, ratio)

    elif selected_engine == 'ideogram':
        return await generate_image_ideogram(prompt, ratio)

    elif selected_engine in ('nano-banana', 'gemini'):
        return await generate_image_gemini(prompt, ratio, reference_images, model)

    elif selected_engine == 'poe':
        return await generate_image_poe(prompt, ratio, reference_images, model)

    elif selected_engine == 'openai':
        return await generate_image_openai(prompt, ratio, reference_images, model)

    else:
        raise ValueError(f"Unsupported image generation engine: {selected_engine}")


# =============================================================================
# CONVENIENCE FUNCTIONS FOR REFERENCE-BASED GENERATION
# =============================================================================

async def generate_image_with_references(
    prompt: str,
    reference_images: list,
    engine: str = "poe",
    model: str = None,
    ratio: str = "16:9"
) -> tuple:
    """
    Convenience function to generate an image using references.

    Args:
        prompt: Text description of the image to generate
        reference_images: List of reference images (paths, bytes, or base64)
        engine: Engine to use (poe, gemini, openai)
        model: Specific model to use
        ratio: Aspect ratio

    Returns:
        tuple: (image_bytes, prompt, ratio)
    """
    # Add ratio to prompt if not already there
    if ratio and not re.search(r'\b\d+:\d+\b', prompt):
        prompt = f"{prompt} {ratio}"

    return await generate_image(
        prompt=prompt,
        reference_images=reference_images,
        engine=engine,
        model=model
    )


# =============================================================================
# DRAMATIQ TASK FOR BACKGROUND PROCESSING
# =============================================================================

async def generate_image_task(
    channel_name: str,
    prompt: str,
    conversation_id: int,
    user_id: int,
    is_whatsapp: bool,
    request_url: str,
    reservation_id: str,
):
    billing_context = {
        "reservation_id": reservation_id,
        "user_id": int(user_id),
        "provider_completed": False,
        "provider_marked": False,
    }
    billing_token = _active_image_billing.set(billing_context)
    try:
        print("Entering generate_image_task")
        if not reservation_id:
            raise BillingReservationError("Image billing reservation is required")
        if not await claim_fixed_usage_provider(
            reservation_id,
            purpose="image",
            user_id=user_id,
        ):
            return
        image_bytes, revised_prompt, ratio = await generate_image(prompt)
        # Compatibility fallback for direct/mocked generators that return the
        # final bytes without calling one of the provider-specific adapters.
        if not billing_context["provider_completed"]:
            await _mark_active_image_provider_succeeded()
        settled = await settle_fixed_usage(reservation_id)
        if not settled:
            raise BillingReservationError(
                "Image billing reservation was not active"
            )

        filename = f"generated_image_{conversation_id}_{int(time.time())}.png"
        source = "bot"

        file_format = 'png' if is_whatsapp else 'webp'

        user = await get_user_by_id(user_id)

        parsed_url = urlparse(request_url)
        scheme = parsed_url.scheme
        host = parsed_url.hostname
        port = parsed_url.port

        image_link_base_256, image_link_token_256, image_link_base_fullsize, image_link_token_fullsize = await save_image_locally(
            request=None,
            image_data=image_bytes,
            current_user=user,
            conversation_id=conversation_id,
            filename=filename,
            source=source,
            format=file_format,
            scheme=scheme,
            host=host,
            port=port
        )

        content_to_show = f"![Generated Image]({image_link_token_fullsize})"

        content_to_save = orjson.dumps([
            {
                "type": "image_url",
                "image_url": {
                    "url": image_link_base_256,
                    "alt": revised_prompt
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
        print(f"Error in generate_image_task: {e}")
        if reservation_id and not billing_context["provider_completed"]:
            try:
                await refund_fixed_usage(reservation_id)
            except BillingReservationError:
                logger.exception(
                    "Could not refund image reservation %s",
                    reservation_id,
                )
        await redis_client.publish(channel_name, orjson.dumps({'error': str(e)}).decode())
        await redis_client.publish(channel_name, 'END')
    finally:
        _active_image_billing.reset(billing_token)

@dramatiq.actor(max_age=None, max_retries=0, time_limit=600_000)
def generate_image_task_actor(
    channel_name: str,
    prompt: str,
    conversation_id: int,
    user_id: int,
    is_whatsapp: bool,
    request_url: str,
    reservation_id: str,
):
    asyncio.run(
        generate_image_task(
            channel_name,
            prompt,
            conversation_id,
            user_id,
            is_whatsapp,
            request_url,
            reservation_id,
        )
    )

async def handle_generate_image(function_arguments, messages, model, temperature, max_tokens, content, conversation_id, current_user, request, input_tokens, output_tokens, total_tokens, message_id, user_id, client, prompt, user_message=None):
    reservation_id = None
    reservation_transferred = False
    try:
        print("Entering handle_generate_image")
        if (
            current_user is None
            or int(getattr(current_user, "id", -1)) != int(user_id)
            or not bool(getattr(current_user, "can_generate_images", False))
        ):
            yield f"data: {orjson.dumps({'content': 'Image generation is not enabled for this account.', 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
            return
        image_prompt = function_arguments['prompt']
        channel_name = (
            f"generate_image_response_{conversation_id}_{user_id}_"
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

        try:
            image_service_name = _image_billing_service_name(image_prompt)
            image_cost, image_service_id = Cost.get_media_generation_service(
                image_service_name
            )
            reservation_id = await reserve_fixed_usage(
                user_id=user_id,
                purpose="image",
                amount=image_cost,
                service_id=image_service_id,
                usage_quantity=1,
            )
        except InsufficientBalanceError:
            yield f"data: {orjson.dumps({'content': 'Insufficient balance to generate image.', 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
            return
        except BillingReservationError:
            logger.exception("Could not reserve image generation usage")
            yield f"data: {orjson.dumps({'content': 'Image billing is temporarily unavailable.', 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
            return

        async with redis_client.pubsub() as pubsub:
            await pubsub.subscribe(channel_name)
            generate_image_task_actor.send(
                channel_name,
                image_prompt,
                conversation_id,
                user_id,
                is_whatsapp,
                request_url,
                reservation_id,
            )
            reservation_transferred = True

            yield f"data: {orjson.dumps({'content': 'Generating image...', 'save_to_db': False, 'yield': True}).decode()}\n\n"

            start_time = time.time()
            while True:
                if time.time() - start_time > IMAGE_GENERATION_TIMEOUT:
                    yield f"data: {orjson.dumps({'content': 'Image generation timed out. Please try again.', 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
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
                        yield f"data: {orjson.dumps({'content': json_data['error'], 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
                    elif 'content_to_show' in json_data and 'content_to_save' in json_data:
                        yield f"data: {orjson.dumps({'content': json_data['content_to_show'], 'save_to_db': False, 'yield': True, '_delivery_ack': json_data.get('_delivery_ack')}).decode()}\n\n"
                        yield f"data: {orjson.dumps({'content': json_data['content_to_save'], 'save_to_db': True, 'yield': False, '_delivery_ack': json_data.get('_delivery_ack')}).decode()}\n\n"
                else:
                    await asyncio.sleep(0.1)

    except Exception as e:
        print(f"Error in handle_generate_image: {e}")
        yield f"data: {orjson.dumps({'content': f'Error generating image: {str(e)}', 'save_to_db': True, 'yield': True, 'is_error': True}).decode()}\n\n"
    finally:
        if reservation_id and not reservation_transferred:
            try:
                await refund_fixed_usage(reservation_id)
            except BillingReservationError:
                logger.exception(
                    "Could not refund image reservation before task handoff"
                )

# Tool definition for the semantic router
register_tool({
    "type": "function",
    "function": {
        "name": "generateImage",
        "description": f"Generate an image using {IMAGE_GENERATION_ENGINE.upper().replace('NANO-BANANA', 'NANO-BANANA (Google Gemini 2.5 Flash Image)')} based on the provided prompt. You can specify the aspect ratio (e.g., 16:9, 4:3, 1:1) in the prompt.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The prompt to generate the image, optionally including the desired aspect ratio"
                }
            },
            "required": ["prompt"],
            "additionalProperties": False
        }
    },
    "strict": True
})

# Register the task for Dramatiq
register_dramatiq_task("generate_image_task_actor", generate_image_task_actor)

# Register the function handler
register_function_handler("generateImage", handle_generate_image)

# For CLI usage, use the standalone script: tools/generate_images_cli.py
