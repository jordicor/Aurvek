# save_images.py

import os
import io
import asyncio
import hashlib
import posixpath
import redis
import aiosqlite
from typing import Optional
import jwt
from jwt import PyJWTError as JWTError
from urllib.parse import unquote, urlparse
from datetime import date, datetime, timezone, timedelta
from PIL import Image as PilImage, UnidentifiedImageError
from fastapi import FastAPI, Response, HTTPException, Depends, Request, Form, status, UploadFile, File
from dotenv import load_dotenv

# own libraries
from models import User, ConnectionManager
from log_config import logger
from auth import hash_password, verify_password, get_user_by_username, get_current_user, create_access_token, get_user_by_id, get_user_from_phone_number
from auth import get_current_user_from_websocket, get_user_id_from_conversation, get_user_by_token, create_user_info, create_login_response, generate_magic_link
from common import (
    CLOUDFLARE_FOR_IMAGES,
    CLOUDFLARE_IMAGE_SUBDOMAIN,
    CLOUDFLARE_BASE_URL,
    generate_signed_url_cloudflare,
    get_runtime_request_url,
)
from common import Cost, generate_user_hash, has_sufficient_balance, cost_tts, cache_directory, users_directory, elevenlabs_key, openai_key, tts_engine, get_balance, deduct_balance, load_service_costs, SECRET_KEY, ALGORITHM, MEDIA_TOKEN_EXPIRE_HOURS
from database import get_db_connection
from storage_quota import record_generated_file

# Load environment variables
load_dotenv()

# Token storage configuration
USE_REDIS = os.getenv('REDIS_IMG_TOKEN', '0') == '1'

# Variables globales
redis_client = None
conn_mem = None

if USE_REDIS:
    # Initialize Redis
    redis_client = redis.Redis(
        host='localhost',
        port=6379,
        db=0,
        decode_responses=True
    )

def _save_image_to_disk(
    image_data: bytes, username: str, conversation_id: int,
    source: str, format: str = 'webp', pre_compressed_webp: bool = False
) -> tuple[str, str, str, str, str, str, int, int]:
    """Sync helper: hash, create dirs, open/resize/save image to disk.

    Args:
        pre_compressed_webp: If True, image_data is already WebP — write fullsize
            directly to disk without Pillow re-encode. Only thumbnail uses Pillow.

    Returns:
        (file_hash, ext, base_url_256, base_url_fullsize,
         file_path_256, file_path_fullsize, size_256, size_fullsize)
    """
    # User hash-based path
    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)

    # Conversation ID path components (7-digit zero-padded)
    conversation_id_str = f"{conversation_id:07d}"
    conversation_id_prefix1 = conversation_id_str[:3]
    conversation_id_prefix2 = conversation_id_str[3:]

    # Build directory path
    file_location = os.path.join(
        users_directory, hash_prefix1, hash_prefix2, user_hash,
        "files", conversation_id_prefix1, conversation_id_prefix2, "img", source
    )
    os.makedirs(file_location, exist_ok=True)

    # Hash + filenames
    ext = format.lower()
    file_hash = hashlib.sha1(image_data).hexdigest()
    filename_256 = f"{file_hash}_256.{ext}"
    filename_fullsize = f"{file_hash}_fullsize.{ext}"
    file_path_256 = os.path.join(file_location, filename_256)
    file_path_fullsize = os.path.join(file_location, filename_fullsize)

    # Open image for thumbnail (always needed for resize)
    image = PilImage.open(io.BytesIO(image_data))

    # Normalize mode for WebP compatibility (CMYK, P, LA, I, etc.)
    if ext == 'webp' and image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA") if (
            image.mode in ("PA", "LA") or image.info.get("transparency") is not None
        ) else image.convert("RGB")

    # Thumbnail
    width, height = image.size
    if width <= 256 and height <= 256:
        image_256 = image
    else:
        image_256 = resize_image(image, 256)
    image_256.save(file_path_256, ext.upper())

    # Fullsize: write directly if pre-compressed, otherwise re-encode via Pillow
    if pre_compressed_webp and ext == 'webp':
        with open(file_path_fullsize, 'wb') as f:
            f.write(image_data)
    else:
        image.save(file_path_fullsize, ext.upper())

    # File sizes on disk feed the storage-quota ledger (one row per file).
    size_256 = os.path.getsize(file_path_256)
    size_fullsize = os.path.getsize(file_path_fullsize)

    # Return base URLs for token/Cloudflare URL generation
    base_url_256 = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/files/{conversation_id_prefix1}/{conversation_id_prefix2}/img/{source}/{filename_256}"
    base_url_fullsize = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/files/{conversation_id_prefix1}/{conversation_id_prefix2}/img/{source}/{filename_fullsize}"

    return (
        file_hash, ext, base_url_256, base_url_fullsize,
        file_path_256, file_path_fullsize, size_256, size_fullsize,
    )


async def save_image_locally(
    request: Optional[Request],
    image_data: bytes,
    current_user,
    conversation_id: int,
    filename: str,
    source: str,
    format: str = 'webp',
    scheme: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    pre_compressed_webp: bool = False
) -> tuple:
    """Save image to disk (in thread) and generate access URLs.

    Args:
        pre_compressed_webp: If True, skip Pillow re-encode for fullsize (write bytes directly).
    """
    # All Pillow + filesystem work runs in a thread
    (
        file_hash, ext, base_url_256, base_url_fullsize,
        file_path_256, file_path_fullsize, size_256, size_fullsize,
    ) = await asyncio.to_thread(
        _save_image_to_disk,
        image_data, current_user.username, conversation_id, source, format,
        pre_compressed_webp
    )

    # Ledger both files (thumbnail + fullsize) so generated images count against
    # the owner's storage quota -- ONE row per file on disk. This same hook also
    # covers QR codes (qr_code.py) and map renders (execution.py): those are tiny
    # chat-tool byproducts and are NOT pre-check-gated, but they ARE ledgered
    # here. Fail fast: the files were written first, so we ledger them
    # immediately after; if the ledger insert fails we delete both files and
    # re-raise -- an artifact that cannot be accounted must not exist.
    try:
        async with get_db_connection() as conn:
            await record_generated_file(conn, conversation_id, 'image', base_url_256, size_256)
            await record_generated_file(conn, conversation_id, 'image', base_url_fullsize, size_fullsize)
            await conn.commit()
    except Exception:
        for orphan_path in (file_path_256, file_path_fullsize):
            try:
                os.remove(orphan_path)
            except OSError:
                pass
        raise

    # URL generation (lightweight, stays on event loop)
    if CLOUDFLARE_FOR_IMAGES:
        # Generate signed Cloudflare URLs
        image_link_token_256 = generate_signed_url_cloudflare(base_url_256, expiration_seconds=3600)
        image_link_token_fullsize = generate_signed_url_cloudflare(base_url_fullsize, expiration_seconds=3600)

        image_link_base_256 = f"{CLOUDFLARE_BASE_URL}{base_url_256}"
        image_link_base_fullsize = f"{CLOUDFLARE_BASE_URL}{base_url_fullsize}"
    else:
        # Generate local tokens
        current_time = datetime.now(timezone.utc)
        new_expiration = current_time + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)

        token_256 = generate_img_token(base_url_256, new_expiration, current_user)
        token_fullsize = generate_img_token(base_url_fullsize, new_expiration, current_user)

        token_url_256 = f"{base_url_256}?token={token_256}"
        token_url_fullsize = f"{base_url_fullsize}?token={token_fullsize}"

        # Use the explicit origin when supplied. Request-free channels (phone)
        # resolve the configured canonical application origin instead of
        # fabricating a Starlette Request or trusting an arbitrary host.
        if scheme is None or host is None:
            runtime_url = urlparse(get_runtime_request_url(request))
            scheme = runtime_url.scheme
            host = runtime_url.hostname
            port = runtime_url.port
            if not scheme or not host:
                raise ValueError("Cannot determine a valid runtime image origin.")

        image_link_token_256 = f'{CLOUDFLARE_BASE_URL}{token_url_256}'
        image_link_token_fullsize = f'{CLOUDFLARE_BASE_URL}{token_url_fullsize}'

        image_link_base_256 = f'{CLOUDFLARE_BASE_URL}{base_url_256}'
        image_link_base_fullsize = f'{CLOUDFLARE_BASE_URL}{base_url_fullsize}'

    return image_link_base_256, image_link_token_256, image_link_base_fullsize, image_link_token_fullsize


def normalize_image_path(path_or_url: str) -> str:
    """Return a canonical URL path suitable for binding an image token."""
    if not isinstance(path_or_url, str):
        raise TypeError(f"path_or_url must be a string, got {type(path_or_url)}")
    if "\x00" in path_or_url:
        raise ValueError("Invalid image path")

    parsed = urlparse(path_or_url)
    raw_path = unquote(parsed.path).replace("\\", "/").lstrip("/")
    normalized = posixpath.normpath(raw_path)
    if normalized in ("", "."):
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("Invalid image path")
    return normalized


def generate_img_token(string_to_use: str, expiration: datetime, current_user: User = Depends(get_current_user)) -> str:
    if not isinstance(string_to_use, str):
        raise TypeError(f"string_to_use must be a string, got {type(string_to_use)}")

    payload = {
        "exp": expiration,
        "username": current_user.username
    }
    media_path = normalize_image_path(string_to_use)
    if media_path.startswith("users/"):
        payload["media_path"] = media_path
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    return token


async def get_or_generate_img_token(current_user: User = Depends(get_current_user)):
    user_id = current_user.id
    current_time = datetime.now(timezone.utc).replace(tzinfo=None)
    new_expiration = current_time + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)
    token_lifetime = timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)
    min_remaining = timedelta(minutes=10)

    if USE_REDIS:
        redis_key = f"img_token:{user_id}"
        token = redis_client.get(redis_key)

        if token:
            remaining_ttl = redis_client.ttl(redis_key)
            if remaining_ttl > int(min_remaining.total_seconds()):
                return token

        new_token = generate_img_token(f"new token for {user_id}", new_expiration, current_user)
        redis_client.setex(
            redis_key,
            token_lifetime,
            new_token
        )
        return new_token
    else:
        cursor_mem = await conn_mem.cursor()
        await cursor_mem.execute("SELECT last_access, token FROM last_access WHERE user_id = ?", (user_id,))
        row = await cursor_mem.fetchone()

        if row:
            last_access, token = row
            last_access = datetime.strptime(last_access, '%Y-%m-%d %H:%M:%S')
            token_age = current_time - last_access
            if token_age < timedelta(0):
                token_age = token_lifetime
            if token_age < token_lifetime - min_remaining:
                return token

        new_token = generate_img_token(f"new token for {user_id}", new_expiration, current_user)
        await cursor_mem.execute(
            "INSERT OR REPLACE INTO last_access (user_id, last_access, token) VALUES (?, ?, ?)",
            (user_id, current_time.strftime('%Y-%m-%d %H:%M:%S'), new_token)
        )
        await conn_mem.commit()
        return new_token


def resize_image(image: PilImage.Image, size: int) -> PilImage.Image:
    """Resize an image to a square with the given side length."""
    if image.width != image.height:
        # Crop to square
        min_dimension = min(image.width, image.height)
        left = (image.width - min_dimension) / 2
        top = (image.height - min_dimension) / 2
        right = (image.width + min_dimension) / 2
        bottom = (image.height + min_dimension) / 2
        image = image.crop((left, top, right, bottom))

    return image.resize((size, size), PilImage.LANCZOS)


def resize_image_cover(image: PilImage.Image, width: int) -> PilImage.Image:
    """Resize an image to 16:9 aspect ratio with the given width."""
    target_height = int(width * 9 / 16)
    current_ratio = image.width / image.height
    target_ratio = 16 / 9

    if current_ratio != target_ratio:
        # Crop to 16:9 ratio (center crop)
        if current_ratio > target_ratio:
            # Image is wider than 16:9, crop width
            crop_width = int(image.height * target_ratio)
            left = (image.width - crop_width) / 2
            top = 0
            right = (image.width + crop_width) / 2
            bottom = image.height
        else:
            # Image is taller than 16:9, crop height
            crop_height = int(image.width / target_ratio)
            left = 0
            top = (image.height - crop_height) / 2
            right = image.width
            bottom = (image.height + crop_height) / 2
        image = image.crop((left, top, right, bottom))

    return image.resize((width, target_height), PilImage.LANCZOS)


async def initialize_memory_db():
    global conn_mem
    if not USE_REDIS:
        conn_mem = await aiosqlite.connect(':memory:')
        cursor_mem = await conn_mem.cursor()
        await cursor_mem.execute('''
            CREATE TABLE last_access (
                user_id INTEGER PRIMARY KEY,
                last_access TIMESTAMP,
                token TEXT
            )
        ''')
        await conn_mem.commit()
        logger.info("In-memory SQLite database initialized")
    
    # Initialize cost system (non-blocking)
    try:
        await Cost.initialize()
    except Exception as e:
        logger.warning(f"Could not initialize Cost system during startup: {e}")
        logger.info("Cost system will use default values")


async def close_memory_db():
    global conn_mem
    if not USE_REDIS and conn_mem:
        await conn_mem.close()
        conn_mem = None
        logger.info("In-memory SQLite connection closed")
