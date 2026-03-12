# common.py

import os
import re
import html
import time
import hashlib
import asyncio
import secrets
import string
import unicodedata
from pathlib import Path
import jwt
from jwt import PyJWTError as JWTError
from dotenv import load_dotenv
from functools import lru_cache
from typing import Dict, Optional
import sqlite3
from fastapi.templating import Jinja2Templates
from datetime import date, datetime, timezone, timedelta

from urllib.parse import urlencode, quote, urlparse
import hmac
import ipaddress

# Own libraries
from database import get_db_connection, DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, is_lock_error
from log_config import logger

load_dotenv()

# Critical environment variables - application will not start without these
PEPPER = os.getenv('PEPPER')
if not PEPPER:
    raise RuntimeError("CRITICAL: PEPPER environment variable is required for password hashing. Set it in .env file.")

SECRET_KEY = os.getenv('APP_SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("CRITICAL: APP_SECRET_KEY environment variable is required for JWT signing. Set it in .env file.")

# Optional API keys - application can start but features may be limited
elevenlabs_key = os.getenv('ELEVEN_KEY')
openai_key = os.getenv('OPENAI_KEY')
claude_key = os.getenv('ANTHROPIC_API_KEY')
gemini_key = os.getenv('GEMINI_KEY')
xai_key =  os.getenv('XAI_KEY')
openrouter_key = os.getenv('OPENROUTER_API_KEY')

# Security: cookie secure flag (requires HTTPS in production)
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").lower() == "true"

# Failover read-only mode: blocks all write operations (POST/PUT/DELETE/PATCH)
READONLY_MODE = os.getenv("READONLY_MODE", "false").lower() == "true"

# Google OAuth configuration
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')

# Stripe configuration
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')

tts_engine = os.getenv('TTS_ENGINE')
stt_engine = os.getenv('STT_ENGINE')

service_sid = os.getenv('SERVICE_SID')
twilio_sid = os.getenv('TWILIO_SID')
twilio_auth = os.getenv('TWILIO_AUTH')
twilio_messaging_service_sid = os.getenv('TWILIO_MESSAGING_SERVICE_SID')

# Twilio security: allowed domains for media URLs (anti-SSRF)
TWILIO_ALLOWED_MEDIA_DOMAINS = frozenset([
    "api.twilio.com",
    "media.twiliocdn.com",
    "s3.amazonaws.com",  # Twilio sometimes uses S3 for media
    "s3-external-1.amazonaws.com",
])

def validate_twilio_media_url(url: str) -> bool:
    """
    Validate that a media URL is from an allowed Twilio domain.
    Prevents SSRF attacks by ensuring we only fetch from trusted sources.

    Args:
        url: The media URL to validate

    Returns:
        True if URL is valid and from allowed domain, False otherwise
    """
    if not url:
        return False

    try:
        parsed = urlparse(url)

        # Must be HTTPS
        if parsed.scheme != "https":
            logger.warning(f"Rejected non-HTTPS media URL: {url[:100]}")
            return False

        # Must be from allowed domain
        if parsed.netloc not in TWILIO_ALLOWED_MEDIA_DOMAINS:
            logger.warning(f"Rejected media URL from untrusted domain: {parsed.netloc}")
            return False

        return True

    except Exception as e:
        logger.error(f"Error validating media URL: {e}")
        return False

# Telegram Bot API
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_WEBHOOK_SECRET = os.getenv('TELEGRAM_WEBHOOK_SECRET')
if TELEGRAM_BOT_TOKEN and not TELEGRAM_WEBHOOK_SECRET:
    raise RuntimeError(
        "CRITICAL: TELEGRAM_WEBHOOK_SECRET is required when TELEGRAM_BOT_TOKEN is set. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\" "
        "and add it to .env"
    )
TELEGRAM_RATE_LIMIT_PER_USER = int(os.getenv('TELEGRAM_RATE_LIMIT_PER_USER', '20'))
TELEGRAM_RATE_LIMIT_GLOBAL = int(os.getenv('TELEGRAM_RATE_LIMIT_GLOBAL', '200'))

# ============================================================================
# Packs System Constants
# ============================================================================
MAX_PACKS_PER_USER = 20
MAX_PACK_ITEMS = 50
MIN_PACK_ITEMS_TO_PUBLISH = 2
PACK_CREATION_RATE_LIMIT = 5  # per day
MAX_PACK_TAGS = 10
MAX_TAG_LENGTH = 30
MAX_PACK_PRICE = 999.99
MIN_PACK_PAID_PRICE = 1.99  # Minimum price for paid packs (prevents near-zero pricing abuse)
MAX_FREE_INITIAL_BALANCE = 5.00  # Max initial_balance for free packs/prompts (platform absorbs cost)
MAX_COVER_FULLSIZE_WIDTH = 2560  # Cap fullsize cover output to prevent DoS via aspect ratio inflation

MODERATION_COST_FIXED = 0.03  # Fixed cost charged to creator per moderation check
MODERATION_MIN_BALANCE = 0.05  # Minimum balance required to attempt moderation
BYOK_MIN_BALANCE_PAID_PROMPT = 0.10  # Minimum balance required for BYOK users on paid prompts (creator markup)

PACK_STATUSES = ['draft', 'pending_review', 'published', 'rejected', 'suspended']
VALID_NOTICE_PERIODS = [0, 90, 180, 365, 730]

ALGORITHM = "HS256"

MAX_TOKENS = int(os.getenv('MAX_TOKENS', 4096))
MAX_MESSAGE_SIZE = int(os.getenv('MAX_MESSAGE_SIZE', 5120))

# Image upload security limits
MAX_IMAGE_UPLOAD_SIZE = int(os.getenv('MAX_IMAGE_UPLOAD_SIZE', 10 * 1024 * 1024))  # 10MB default
MAX_IMAGE_PIXELS = int(os.getenv('MAX_IMAGE_PIXELS', 50_000_000))  # 50 megapixels (e.g., 7000x7000)

# Image token expiration (hours)
# AVATAR: User profile pictures and bot/prompt avatars (change rarely, can be longer)
# MEDIA: Conversation images, videos, generated content (more sensitive, shorter)
AVATAR_TOKEN_EXPIRE_HOURS = int(os.getenv('AVATAR_TOKEN_EXPIRE_HOURS', 8))
MEDIA_TOKEN_EXPIRE_HOURS = int(os.getenv('MEDIA_TOKEN_EXPIRE_HOURS', 1))

PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')

CLOUDFLARE_API_KEY = os.getenv('CLOUDFLARE_API_KEY')
CLOUDFLARE_EMAIL = os.getenv('CLOUDFLARE_EMAIL')
CLOUDFLARE_ZONE_ID = os.getenv('CLOUDFLARE_ZONE_ID') 
CLOUDFLARE_API_URL = os.getenv('CLOUDFLARE_API_URL') 

CLOUDFLARE_FOR_IMAGES = os.getenv("CLOUDFLARE_FOR_IMAGES", "false").lower() == "true"
CLOUDFLARE_SECRET = os.getenv("CLOUDFLARE_SECRET")
CLOUDFLARE_IMAGE_SUBDOMAIN = os.getenv("CLOUDFLARE_IMAGE_SUBDOMAIN")
CLOUDFLARE_BASE_URL = os.getenv("CLOUDFLARE_BASE_URL")

# Cloudflare DNS Management (for auto-creating user subdomains)
CLOUDFLARE_DOMAIN = os.getenv("CLOUDFLARE_DOMAIN")
CLOUDFLARE_CNAME_TARGET = os.getenv("CLOUDFLARE_CNAME_TARGET")

# Image Auth IP Whitelist
AUTH_IMAGE_ALLOWED_IPS = [ip.strip() for ip in os.getenv("AUTH_IMAGE_ALLOWED_IPS", "127.0.0.1").split(",") if ip.strip()]
AUTH_IMAGE_ALLOWED_PREFIXES = [p.strip() for p in os.getenv("AUTH_IMAGE_ALLOWED_PREFIXES", "").split(",") if p.strip()]

# CDN Configuration
CDN_BASE_URL = os.getenv("CDN_BASE_URL", "")  # For static files (/static/)
CDN_FILES_URL = os.getenv("CDN_FILES_URL", "")  # For user files (/users/)
ENABLE_CDN = os.getenv("ENABLE_CDN", "false").lower() == "true"

def get_static_url(path: str) -> str:
    """
    Generate URL for static content (CSS, JS, images)
    If CDN is enabled, returns CDN URL, otherwise returns local FastAPI URL
    """
    if ENABLE_CDN and CDN_BASE_URL:
        # Ensure path starts with /
        if not path.startswith('/'):
            path = '/' + path
        
        # Remove /static from path if present since CDN_BASE_URL already includes it
        if path.startswith('/static/'):
            path = path[7:]  # Remove '/static' (7 characters)
        elif path.startswith('/static'):
            path = path[7:]  # Remove '/static' (7 characters)
        
        # Ensure path starts with / 
        if not path.startswith('/'):
            path = '/' + path
            
        return f"{CDN_BASE_URL.rstrip('/')}{path}"
    else:
        # Return local FastAPI static URL
        if not path.startswith('/'):
            path = '/' + path
        return path

JWT_CACHE_SIZE = int(os.getenv('JWT_CACHE_SIZE', '100000'))

# Folder for audio cache
cache_directory = Path("data/cache")

# Folder for user data
users_directory = os.path.join("data", "users")

# Templates folder
templates = Jinja2Templates(directory="templates")

# Add helper functions to template context
templates.env.globals['get_static_url'] = get_static_url


async def get_template_context(request, current_user, branding_context=None):
    """Generate base context for templates that include navbar.html"""
    is_manager = await current_user.is_manager if current_user else False
    is_admin = await current_user.is_admin if current_user else False

    navbar_avatar_url = ""
    navbar_initials = ""

    if current_user:
        # Fetch profile picture, checking alter-ego first
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """SELECT u.username, u.profile_picture,
                          ud.current_alter_ego_id, ae.name AS alter_ego_name,
                          ae.profile_picture AS alter_ego_profile_picture
                   FROM USERS u
                   JOIN USER_DETAILS ud ON u.id = ud.user_id
                   LEFT JOIN USER_ALTER_EGOS ae ON ud.current_alter_ego_id = ae.id
                   WHERE u.id = ?""",
                (current_user.id,)
            )
            row = await cursor.fetchone()

        if row and row["current_alter_ego_id"]:
            display_name = row["alter_ego_name"] or current_user.username
            profile_picture = row["alter_ego_profile_picture"]
        else:
            display_name = current_user.username
            profile_picture = row["profile_picture"] if row else None

        navbar_initials = display_name[0].upper() if display_name else ""

        if profile_picture and CLOUDFLARE_BASE_URL:
            expiration = datetime.now(timezone.utc) + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
            payload = {"exp": expiration, "username": current_user.username}
            token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
            navbar_avatar_url = f"{CLOUDFLARE_BASE_URL}{profile_picture}_32.webp?token={token}"

    # Contextual branding (defaults to platform if no context passed)
    branding = await get_branding_for_context(context=branding_context)
    branding["is_custom_domain"] = getattr(request.state, "custom_domain", False)
    if not branding["is_custom_domain"]:
        branding["hide_platform_branding"] = False

    return {
        "request": request,
        "username": current_user.username if current_user else "",
        "is_manager": is_manager,
        "is_admin": is_admin,
        "get_static_url": get_static_url,
        "navbar_avatar_url": navbar_avatar_url,
        "navbar_initials": navbar_initials,
        "branding": branding,
        "readonly_mode": READONLY_MODE
    }

# Get the absolute path of the current script
SCRIPT_DIR = Path(__file__).parent.absolute()

# Path for data and nginx
DATA_DIR = SCRIPT_DIR / "data"


class Cost:
    TTS_COST_PER_CHARACTER = 0.0002  # Default values, in case of failure
    STT_COST_PER_MINUTE = 0.0059  # Deepgram default
    DALLE_COST = 0.016
    IMAGE_GENERATION_COST = DALLE_COST

    TTS_SERVICE_ID = None
    STT_SERVICE_ID = None
    DALLE_SERVICE_ID = None

    @classmethod
    async def initialize(cls):
        costs = await load_service_costs()
        if tts_engine == 'elevenlabs':
            cls.TTS_COST_PER_CHARACTER = costs.get('TTS_COST_PER_CHARACTER_ELEVENLABS', cls.TTS_COST_PER_CHARACTER)
            cls.TTS_SERVICE_ID = costs.get('TTS_SERVICE_ID_ELEVENLABS')
        elif tts_engine == 'openai':
            cls.TTS_COST_PER_CHARACTER = costs.get('TTS_COST_PER_CHARACTER_OPENAI', cls.TTS_COST_PER_CHARACTER)
            cls.TTS_SERVICE_ID = costs.get('TTS_SERVICE_ID_OPENAI')

        if stt_engine == 'elevenlabs':
            cls.STT_COST_PER_MINUTE = costs.get('STT_COST_PER_MINUTE_ELEVENLABS', 0.005)  # $0.30/hour = $0.005/minute
            cls.STT_SERVICE_ID = costs.get('STT_SERVICE_ID_ELEVENLABS') or costs.get('STT_SERVICE_ID')
        elif stt_engine == 'deepgram':
            cls.STT_COST_PER_MINUTE = costs.get('STT_COST_PER_MINUTE_DEEPGRAM', 0.0059)  # Deepgram Nova-2
            cls.STT_SERVICE_ID = costs.get('STT_SERVICE_ID_DEEPGRAM') or costs.get('STT_SERVICE_ID')
        else:
            cls.STT_COST_PER_MINUTE = costs.get('STT_COST_PER_MINUTE', cls.STT_COST_PER_MINUTE)
            cls.STT_SERVICE_ID = costs.get('STT_SERVICE_ID')
            
        cls.DALLE_COST = costs.get('DALLE_COST', cls.DALLE_COST)
        cls.DALLE_SERVICE_ID = costs.get('DALLE_SERVICE_ID')



def generate_user_hash(username: str) -> tuple:
    # Convert user_id to bytes and concatenate with PEPPER
    data_to_hash = str(username) + PEPPER
    #logger.debug(f"data to hash: {data_to_hash}")
    hash_obj = hashlib.sha1(data_to_hash.encode())
    hash_str = hash_obj.hexdigest()
    return hash_str[:3], hash_str[3:7], hash_str

async def has_sufficient_balance(user_id: int, amount: float) -> bool:
    async with get_db_connection(readonly=True) as conn:
        try:
            cursor = await conn.cursor()
            await cursor.execute('''
                SELECT balance FROM USER_DETAILS WHERE user_id = ?
            ''', (user_id,))
            result = await cursor.fetchone()
            current_balance = result[0] if result else 0
            return current_balance >= amount
        except Exception as e:
            logger.error(f"Error checking balance: {e}")
            return False

async def cost_tts(user_id: int, characters_used: int) -> bool:
    """Charge user for TTS usage in a single atomic transaction.
    Returns True on success, False on failure (insufficient balance or DB error)."""
    total_tts_cost = Cost.TTS_COST_PER_CHARACTER * characters_used
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute('BEGIN IMMEDIATE')
                transaction_started = True

                # Atomic balance check + deduction (replaces separate deduct_balance call)
                result = await conn.execute('''
                    UPDATE USER_DETAILS
                    SET balance = balance - ?
                    WHERE user_id = ? AND balance >= ?
                    RETURNING balance
                ''', (total_tts_cost, user_id, total_tts_cost))
                new_balance = await result.fetchone()

                if new_balance is None:
                    await conn.rollback()
                    return False  # Insufficient balance

                await conn.execute('''
                    INSERT INTO SERVICE_USAGE (user_id, service_id, usage_quantity, cost)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, Cost.TTS_SERVICE_ID, characters_used, total_tts_cost))

                await conn.execute('''
                    UPDATE USER_DETAILS
                    SET total_cost = total_cost + ?, total_tts_cost = total_tts_cost + ?
                    WHERE user_id = ?
                ''', (total_tts_cost, total_tts_cost, user_id))

                daily_ok = await record_daily_usage(
                    user_id=user_id,
                    usage_type='tts',
                    cost=total_tts_cost,
                    units=characters_used,
                    conn=conn
                )
                if not daily_ok:
                    logger.warning("Daily usage record failed for cost_tts user_id=%s, proceeding with commit", user_id)

                await conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    logger.warning(
                        "Lock detected in cost_tts for user_id=%s (retry %s/%s, wait %.2fs)",
                        user_id,
                        attempt + 1,
                        DB_MAX_RETRIES,
                        wait_time,
                    )
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error(f"Error in cost_tts: {exc}")
                    return False
            except Exception as e:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                logger.error(f"Error in cost_tts: {e}")
                return False

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "Failed cost_tts for user_id=%s after %s retries: %s",
            user_id,
            DB_MAX_RETRIES,
            last_lock_error,
        )
    return False


async def refund_tts(user_id: int, characters_used: int) -> bool:
    """Reverse a cost_tts charge when TTS generation fails after billing.
    Returns True on success, False on failure."""
    total_tts_cost = Cost.TTS_COST_PER_CHARACTER * characters_used
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute('BEGIN IMMEDIATE')
                transaction_started = True

                # Restore balance
                await conn.execute('''
                    UPDATE USER_DETAILS SET balance = balance + ? WHERE user_id = ?
                ''', (total_tts_cost, user_id))

                # Compensating usage record (negative cost for audit trail)
                await conn.execute('''
                    INSERT INTO SERVICE_USAGE (user_id, service_id, usage_quantity, cost)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, Cost.TTS_SERVICE_ID, -characters_used, -total_tts_cost))

                # Reverse totals with floor guard (never go negative)
                await conn.execute('''
                    UPDATE USER_DETAILS
                    SET total_cost = MAX(0, total_cost - ?),
                        total_tts_cost = MAX(0, total_tts_cost - ?)
                    WHERE user_id = ?
                ''', (total_tts_cost, total_tts_cost, user_id))

                await conn.commit()
                logger.info("TTS refund for user_id=%s: %.6f (%d chars)",
                            user_id, total_tts_cost, characters_used)
                return True
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    logger.warning(
                        "Lock detected in TTS refund for user_id=%s (retry %s/%s, wait %.2fs)",
                        user_id,
                        attempt + 1,
                        DB_MAX_RETRIES,
                        wait_time,
                    )
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error(f"Error in TTS refund: {exc}")
                    return False
            except Exception as e:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                logger.error(f"Error in TTS refund: {e}")
                return False

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "Failed TTS refund for user_id=%s after %s retries: %s",
            user_id,
            DB_MAX_RETRIES,
            last_lock_error,
        )
    return False

async def get_balance(user_id: int) -> float:
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute('SELECT balance FROM USER_DETAILS WHERE user_id = ?', (user_id,)) as cursor:
            result = await cursor.fetchone()
            return result[0] if result else 0.0


async def deduct_balance(user_id: int, amount: float):
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute('BEGIN IMMEDIATE')
                transaction_started = True
                result = await conn.execute('''
                    UPDATE USER_DETAILS
                    SET balance = balance - ?
                    WHERE user_id = ? AND balance >= ?
                    RETURNING balance
                ''', (amount, user_id, amount))
                new_balance = await result.fetchone()

                if new_balance is not None:
                    await conn.commit()
                    return True

                await conn.rollback()
                return False
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    logger.warning(
                        "Lock detected while deducting balance (user_id=%s, retry %s/%s, wait %.2fs)",
                        user_id,
                        attempt + 1,
                        DB_MAX_RETRIES,
                        wait_time,
                    )
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error(f"Error executing balance update: {exc}")
                    return False
            except Exception as e:
                if transaction_started:
                    try:
                        await conn.rollback()
                    except Exception:
                        pass
                logger.error(f"Error executing balance update: {e}")
                return False

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "Failed to deduct balance for user_id=%s after %s retries: %s",
            user_id,
            DB_MAX_RETRIES,
            last_lock_error,
        )
    return False


async def record_daily_usage(
    user_id: int,
    usage_type: str,
    cost: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    units: float = 0,
    conn=None,
    cursor=None
):
    """
    Record or update daily usage summary for a user.
    Uses UPSERT to accumulate multiple operations in the same day.

    Args:
        user_id: The user ID
        usage_type: Type of usage ('ai_tokens', 'tts', 'stt', 'image', 'video', 'domain')
        cost: Cost of this operation
        tokens_in: Input tokens (for AI calls)
        tokens_out: Output tokens (for AI calls)
        units: Units consumed (chars for TTS, mins for STT, count for images/videos)
        conn: Optional existing connection (for transaction reuse)
        cursor: Optional existing cursor (for transaction reuse)
    """
    upsert_query = '''
        INSERT INTO USAGE_DAILY (user_id, date, type, operations, tokens_in, tokens_out, units, total_cost, updated_at)
        VALUES (?, date('now'), ?, 1, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id, date, type) DO UPDATE SET
            operations = operations + 1,
            tokens_in = tokens_in + excluded.tokens_in,
            tokens_out = tokens_out + excluded.tokens_out,
            units = units + excluded.units,
            total_cost = total_cost + excluded.total_cost,
            updated_at = datetime('now')
    '''

    # If connection provided, use it directly (caller manages transaction)
    if conn is not None:
        try:
            if cursor is not None:
                await cursor.execute(upsert_query, (user_id, usage_type, tokens_in, tokens_out, units, cost))
            else:
                await conn.execute(upsert_query, (user_id, usage_type, tokens_in, tokens_out, units, cost))
            return True
        except Exception as e:
            logger.error(f"Error recording daily usage (with provided conn): {e}")
            return False

    # Otherwise, manage our own connection with retries
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as db_conn:
            try:
                await db_conn.execute(upsert_query, (user_id, usage_type, tokens_in, tokens_out, units, cost))
                await db_conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    logger.warning(
                        "Lock detected recording daily usage (user_id=%s, type=%s, retry %s/%s, wait %.2fs)",
                        user_id, usage_type, attempt + 1, DB_MAX_RETRIES, wait_time
                    )
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error(f"Error recording daily usage: {exc}")
                    return False
            except Exception as e:
                logger.error(f"Error recording daily usage: {e}")
                return False

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(
            "Failed to record daily usage for user_id=%s after %s retries: %s",
            user_id, DB_MAX_RETRIES, last_lock_error
        )
    return False


async def load_service_costs():
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute('''
                SELECT id, name, cost_per_unit 
                FROM SERVICES
            ''')
            costs = await cursor.fetchall()
            cost_dict = {}
            for row in costs:
                service_id, service_name, cost_per_unit = row
                if service_name == 'TTS-ELEVENLABS':
                    cost_dict['TTS_COST_PER_CHARACTER_ELEVENLABS'] = cost_per_unit
                    cost_dict['TTS_SERVICE_ID_ELEVENLABS'] = service_id
                elif service_name == 'TTS-OPENAI':
                    cost_dict['TTS_COST_PER_CHARACTER_OPENAI'] = cost_per_unit
                    cost_dict['TTS_SERVICE_ID_OPENAI'] = service_id
                elif service_name == 'STT-ELEVENLABS':
                    cost_dict['STT_COST_PER_MINUTE_ELEVENLABS'] = cost_per_unit
                    cost_dict['STT_SERVICE_ID_ELEVENLABS'] = service_id
                elif service_name == 'STT-DEEPGRAM':
                    cost_dict['STT_COST_PER_MINUTE_DEEPGRAM'] = cost_per_unit
                    cost_dict['STT_SERVICE_ID_DEEPGRAM'] = service_id
                elif service_name == 'STT':  # Maintain backward compatibility
                    cost_dict['STT_COST_PER_MINUTE'] = cost_per_unit
                    cost_dict['STT_SERVICE_ID'] = service_id
                elif service_name == 'DALLE-2-256':
                    cost_dict['DALLE_COST'] = cost_per_unit
                    cost_dict['DALLE_SERVICE_ID'] = service_id
                    
            return cost_dict
        except Exception as e:
            logger.error(f"Error loading service costs: {e}")
            return {}
        finally:
            await conn.close()

def estimate_message_tokens(text: str, token_ratio: float = 4.0, margin: float = 1.1) -> int:
    total_chars = len(text)
    estimated_tokens = total_chars / token_ratio
    rounded_tokens = int(estimated_tokens * margin + 0.5)
    return rounded_tokens


def custom_unescape(text):
    replacements = {
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
        "&nbsp;": " ",
        "&ndash;": "–",
        "&mdash;": "—",
        "&cent;": "¢",
        "&pound;": "£",
        "&yen;": "¥",
        "&euro;": "€",
        "&copy;": "©",
        "&reg;": "®",
        "&sect;": "§",
        "&bull;": "•",
        "&hellip;": "…",
        "&prime;": "′",
        "&Prime;": "″",
        "&deg;": "°",
        "&permil;": "‰",
        "&lsaquo;": "‹",
        "&rsaquo;": "›",
        "&laquo;": "«",
        "&raquo;": "»",
        "&trade;": "™",
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return html.unescape(text)


def generate_cloudflare_signature(path: str, expires: int, secret: str) -> str:
    """
    Generate an HMAC signature for Cloudflare signed URL.
    """
    string_to_sign = f"{path}{expires}"
    signature = hmac.new(
        secret.encode(),
        string_to_sign.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature

def generate_signed_url_cloudflare(path: str, expiration_seconds: int = 3600) -> str:
    """
    Generate a signed URL for Cloudflare.
    """
    expires = int(time.time()) + expiration_seconds
    signature = generate_cloudflare_signature(path, expires, CLOUDFLARE_SECRET)
    query_params = urlencode({
        'expires': expires,
        'signature': signature
    })
    logger.debug(f"Path before encoding: {path}")
    logger.debug(f"Path after encoding: {quote(path)}")
    signed_url = f"{CLOUDFLARE_BASE_URL}{quote(path)}?{query_params}"
    logger.debug(f"Signed URL generated: {signed_url}")
    return signed_url


@lru_cache(maxsize=JWT_CACHE_SIZE)
def decode_jwt_cached(token: str, secret_key: str) -> Dict:
    """
    Cached version of jwt.decode
    The secret_key is included as part of the cache key for security
    """
    try:
        return jwt.decode(token, secret_key, algorithms=[ALGORITHM], options={"verify_exp": False})  # Disable exp verification here
    except JWTError as e:
        logger.error(f"Error decoding token: {e}")
        raise
  
  
def verify_token_expiration(payload: Dict) -> bool:
    """
    Simple version using timestamps directly
    """
    try:
        exp = payload.get('exp')
        if not exp:
            return False
        
        # Use timestamp directly to avoid creating datetime objects
        return int(time.time()) < exp

    except Exception as e:
        return False


# Function to sanitize prompt name
def sanitize_name(name: str) -> str:
    name = re.sub(r'<[^>]+>', '', name)
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    # Remove path traversal sequences
    while '..' in name:
        name = name.replace('..', '')
    name = name[:120]
    name = name.lower().replace(' ', '_')
    return name


def validate_path_within_directory(user_path: str, base_directory: Path) -> Path:
    """
    Validates that a user-provided path resolves within the allowed base directory.
    Prevents path traversal attacks using ../ sequences.

    Args:
        user_path: The path provided by the user (potentially malicious)
        base_directory: The directory the path must stay within

    Returns:
        The resolved absolute path if valid

    Raises:
        ValueError if path escapes the base directory
    """
    from fastapi import HTTPException

    # Resolve base to absolute path
    base_resolved = base_directory.resolve()

    # Build and resolve the full path
    # Using Path() normalizes and resolves ../ sequences
    full_path = (base_directory / user_path).resolve()

    # Check that resolved path is within base directory
    # is_relative_to() is the secure method (Python 3.9+)
    if not full_path.is_relative_to(base_resolved):
        raise HTTPException(
            status_code=403,
            detail="Access denied - path outside allowed directory"
        )

    return full_path


# ============================================================================
# API Key Encryption Functions
# ============================================================================

def get_encryption_key():
    """
    Derive an encryption key from SECRET_KEY using PBKDF2.
    Returns a Fernet instance for encryption/decryption.
    """
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        import base64

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=PEPPER.encode() if PEPPER else b'default_salt',
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(SECRET_KEY.encode() if SECRET_KEY else b'default_key'))
        return Fernet(key)
    except ImportError:
        logger.error("cryptography library not installed. Run: pip install cryptography")
        return None


def encrypt_api_key(plain_key: str) -> Optional[str]:
    """
    Encrypt an API key for storage in the database.

    Args:
        plain_key: The plaintext API key to encrypt

    Returns:
        Encrypted key as a string, or None if encryption fails
    """
    if not plain_key:
        return None

    fernet = get_encryption_key()
    if fernet is None:
        logger.error("Could not get encryption key")
        return None

    try:
        encrypted = fernet.encrypt(plain_key.encode())
        return encrypted.decode()
    except Exception as e:
        logger.error(f"Error encrypting API key: {e}")
        return None


def decrypt_api_key(encrypted_key: str) -> Optional[str]:
    """
    Decrypt an API key from the database.

    Args:
        encrypted_key: The encrypted API key string

    Returns:
        Decrypted plaintext key, or None if decryption fails
    """
    if not encrypted_key:
        return None

    fernet = get_encryption_key()
    if fernet is None:
        logger.error("Could not get encryption key")
        return None

    try:
        decrypted = fernet.decrypt(encrypted_key.encode())
        return decrypted.decode()
    except Exception as e:
        logger.error(f"Error decrypting API key: {e}")
        return None


def mask_api_key(key: str) -> str:
    """
    Mask an API key for display purposes.
    Shows first 8 and last 4 characters.

    Args:
        key: The API key to mask

    Returns:
        Masked key string (e.g., "sk-proj-...abcd")
    """
    if not key or len(key) < 16:
        return "****"
    return f"{key[:8]}...{key[-4:]}"


# ============================================
# API Key Mode Configuration
# ============================================

# Valid API key modes
API_KEY_MODE_SYSTEM_ONLY = 'system_only'
API_KEY_MODE_OWN_ONLY = 'own_only'
API_KEY_MODE_BOTH_PREFER_OWN = 'both_prefer_own'
API_KEY_MODE_BOTH_PREFER_SYSTEM = 'both_prefer_system'

VALID_API_KEY_MODES = [
    API_KEY_MODE_SYSTEM_ONLY,
    API_KEY_MODE_OWN_ONLY,
    API_KEY_MODE_BOTH_PREFER_OWN,
    API_KEY_MODE_BOTH_PREFER_SYSTEM
]

DEFAULT_API_KEY_MODE = API_KEY_MODE_BOTH_PREFER_OWN

# Human-readable labels for UI
API_KEY_MODE_LABELS = {
    API_KEY_MODE_SYSTEM_ONLY: 'System Keys Only',
    API_KEY_MODE_OWN_ONLY: 'Own Keys Only (BYOK)',
    API_KEY_MODE_BOTH_PREFER_OWN: 'Both (Prefer Own)',
    API_KEY_MODE_BOTH_PREFER_SYSTEM: 'Both (Prefer System)'
}

# Descriptions for UI tooltips
API_KEY_MODE_DESCRIPTIONS = {
    API_KEY_MODE_SYSTEM_ONLY: 'User can only use platform API keys. Cannot configure their own.',
    API_KEY_MODE_OWN_ONLY: 'User MUST configure their own API keys to use AI services.',
    API_KEY_MODE_BOTH_PREFER_OWN: 'User keys take priority if configured, otherwise uses platform keys.',
    API_KEY_MODE_BOTH_PREFER_SYSTEM: 'Platform keys by default. User can optionally use their own.'
}


async def get_user_api_key_mode(user_id: int) -> str:
    """
    Get the API key mode for a user.

    Args:
        user_id: The user's ID

    Returns:
        The user's api_key_mode or DEFAULT_API_KEY_MODE if not set
    """
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT api_key_mode FROM USER_DETAILS WHERE user_id = ?",
            (user_id,)
        )
        result = await cursor.fetchone()

    if result and result[0]:
        return result[0]
    return DEFAULT_API_KEY_MODE


async def user_can_configure_own_keys(user_id: int) -> bool:
    """
    Check if a user is allowed to configure their own API keys.

    Returns:
        True if user can configure own keys (not system_only mode)
    """
    mode = await get_user_api_key_mode(user_id)
    return mode != API_KEY_MODE_SYSTEM_ONLY


async def user_requires_own_keys(user_id: int) -> bool:
    """
    Check if a user MUST have their own API keys configured.

    Returns:
        True if user is in own_only mode
    """
    mode = await get_user_api_key_mode(user_id)
    return mode == API_KEY_MODE_OWN_ONLY


async def user_has_valid_api_keys(user_id: int, provider: str = None) -> bool:
    """
    Check if a user has valid API keys configured.

    Args:
        user_id: The user's ID
        provider: Optional specific provider to check (openai, anthropic, google, xai)

    Returns:
        True if user has at least one API key configured (or specific provider if specified)
    """
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
            (user_id,)
        )
        result = await cursor.fetchone()

    if not result or not result[0]:
        return False

    try:
        keys_json = decrypt_api_key(result[0])
        if not keys_json:
            return False

        import orjson
        keys = orjson.loads(keys_json)

        if provider:
            return provider in keys and bool(keys[provider])

        # Check if at least one key is configured
        return any(bool(v) for v in keys.values())
    except Exception:
        return False


def resolve_api_key_for_provider(
    user_api_keys: dict,
    api_key_mode: str,
    provider: str
) -> tuple:
    """
    Determine which API key to use based on mode and availability.

    Args:
        user_api_keys: Dict of user's configured API keys
        api_key_mode: The user's api_key_mode setting
        provider: The provider name (openai, anthropic, google, xai, openrouter)

    Returns:
        Tuple of (api_key_to_use or None, should_use_system_key)
        - If api_key_to_use is not None, use that key
        - If api_key_to_use is None and should_use_system_key is True, use system key
        - If both are None/False, user cannot proceed (own_only without keys)
    """
    # Map machine names to provider keys
    provider_map = {
        "GPT": "openai",
        "O1": "openai",
        "Claude": "anthropic",
        "Gemini": "google",
        "xAI": "xai",
        "OpenRouter": "openrouter"
    }

    provider_key = provider_map.get(provider, provider.lower())
    user_has_key = user_api_keys and provider_key in user_api_keys and bool(user_api_keys[provider_key])

    if api_key_mode == API_KEY_MODE_SYSTEM_ONLY:
        # Always use system key, ignore user keys
        return (None, True)

    elif api_key_mode == API_KEY_MODE_OWN_ONLY:
        # Must use own key, no system fallback
        if user_has_key:
            return (user_api_keys[provider_key], False)
        else:
            return (None, False)  # Error condition - no key available

    elif api_key_mode == API_KEY_MODE_BOTH_PREFER_OWN:
        # Prefer user key, fall back to system
        if user_has_key:
            return (user_api_keys[provider_key], False)
        else:
            return (None, True)  # Use system key

    elif api_key_mode == API_KEY_MODE_BOTH_PREFER_SYSTEM:
        # Prefer system key, but user can override
        # For now, always use system (user override would need UI flag)
        return (None, True)

    # Default fallback
    return (None, True)


# ============================================================================
# Public Profile URL Functions
# ============================================================================

# Base62 character set for public IDs
BASE62_CHARS = string.ascii_letters + string.digits  # a-zA-Z0-9

# Public profile configuration
PUBLIC_PROFILE_DOMAIN = os.getenv("PUBLIC_PROFILE_DOMAIN", "localhost:7789")


def generate_public_id(length: int = 8) -> str:
    """
    Generate a random base62 public ID for prompts.

    8 chars base62 = 62^8 = ~218 trillion combinations (~48 bits entropy).
    At 1000 requests/second, enumeration would take ~6,900 years.

    Args:
        length: Number of characters (default 8)

    Returns:
        Random base62 string (e.g., 'k9F3aZ2p')
    """
    return ''.join(secrets.choice(BASE62_CHARS) for _ in range(length))


def slugify(name: str) -> str:
    """
    Convert a prompt name to a URL-friendly slug.

    Examples:
        'Ava AI Companion' -> 'ava-ai-companion'
        'Coach de Productividad' -> 'coach-de-productividad'
        'Test   Name!!!' -> 'test-name'

    Args:
        name: The prompt name to slugify

    Returns:
        URL-safe lowercase slug with hyphens
    """
    if not name:
        return ''

    # Normalize unicode characters (accents -> base letters for URL safety)
    # Use NFKD to decompose, then encode to ASCII ignoring non-ASCII
    normalized = unicodedata.normalize('NFKD', name)
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')

    # Convert to lowercase
    slug = ascii_text.lower()

    # Replace spaces and underscores with hyphens
    slug = re.sub(r'[\s_]+', '-', slug)

    # Remove any character that isn't alphanumeric or hyphen
    slug = re.sub(r'[^a-z0-9-]', '', slug)

    # Collapse multiple hyphens into one
    slug = re.sub(r'-+', '-', slug)

    # Remove leading/trailing hyphens
    slug = slug.strip('-')

    # Limit length (for very long names)
    return slug[:64]


def get_public_profile_url(
    public_id: str,
    slug: str,
    page: str = None
) -> str:
    """
    Generate the public URL for a landing page.

    Args:
        public_id: The prompt's public_id (8 chars base62)
        slug: The URL slug (from prompt name)
        page: Optional page name (None or 'home' returns base URL)

    Returns:
        Full URL string

    Example:
        https://example.com/p/k9F3aZ2p/ava/
    """
    domain = PUBLIC_PROFILE_DOMAIN
    protocol = "http" if "localhost" in domain else "https"
    base = f"{protocol}://{domain}/p/{public_id}/{slug}/"

    if page and page != "home":
        return f"{base}{page}"
    return base


# ============================================================================
# Internal IP Validation (for nginx internal endpoints)
# ============================================================================

# Private IP ranges (RFC 1918 + loopback)
INTERNAL_IP_NETWORKS = [
    ipaddress.ip_network('127.0.0.0/8'),      # Loopback
    ipaddress.ip_network('10.0.0.0/8'),       # Class A private
    ipaddress.ip_network('172.16.0.0/12'),    # Class B private
    ipaddress.ip_network('192.168.0.0/16'),   # Class C private
    ipaddress.ip_network('::1/128'),          # IPv6 loopback
    ipaddress.ip_network('fc00::/7'),         # IPv6 private
]


def is_internal_ip(ip_str: str) -> bool:
    """
    Check if an IP address is from an internal/private network.

    Used to restrict internal endpoints (like /internal/resolve-landing)
    to only accept requests from localhost, nginx, or internal services.

    Args:
        ip_str: IP address as string (e.g., '127.0.0.1', '192.168.1.100')

    Returns:
        True if IP is internal/private, False otherwise
    """
    if not ip_str:
        return False

    try:
        # Handle IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
        if ip_str.startswith('::ffff:'):
            ip_str = ip_str[7:]

        ip = ipaddress.ip_address(ip_str)

        return any(ip in network for network in INTERNAL_IP_NETWORKS)
    except ValueError:
        logger.warning(f"Invalid IP address format: {ip_str}")
        return False


# ============================================================================
# Pricing & Earnings Functions
# ============================================================================

# Cache for pricing config to avoid repeated DB queries
_pricing_config_cache = {}
_pricing_config_cache_time = 0
PRICING_CONFIG_CACHE_TTL = 300  # 5 minutes

async def get_pricing_config() -> dict:
    """
    Get pricing configuration from SYSTEM_CONFIG table.
    Returns dict with keys: margin_free, margin_paid, margin_personal, commission, min_payout
    Values are already converted to decimals (e.g., 20% -> 0.20)
    """
    global _pricing_config_cache, _pricing_config_cache_time

    current_time = time.time()
    if _pricing_config_cache and (current_time - _pricing_config_cache_time) < PRICING_CONFIG_CACHE_TTL:
        return _pricing_config_cache

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT key, value FROM SYSTEM_CONFIG WHERE key LIKE 'pricing_%' OR key = 'min_payout_amount'"
        )
        rows = await cursor.fetchall()

        config = {
            'margin_free': 0.20,      # 20% default
            'margin_paid': 0.10,      # 10% default
            'margin_personal': 0.15,  # 15% default
            'commission': 0.30,       # 30% default
            'min_payout': 50.0        # $50 default
        }

        for row in rows:
            key, value = row[0], float(row[1])
            if key == 'pricing_margin_free':
                config['margin_free'] = value / 100
            elif key == 'pricing_margin_paid':
                config['margin_paid'] = value / 100
            elif key == 'pricing_margin_personal':
                config['margin_personal'] = value / 100
            elif key == 'pricing_commission':
                config['commission'] = value / 100
            elif key == 'min_payout_amount':
                config['min_payout'] = value

        _pricing_config_cache = config
        _pricing_config_cache_time = current_time
        return config


async def add_pending_earnings(user_id: int, amount: float, conn=None, cursor=None) -> bool:
    """
    Increment pending_earnings for a user (creator or referral).
    If conn/cursor provided, uses them (for transaction). Otherwise creates new connection.
    """
    if amount <= 0:
        return True

    if conn and cursor:
        # Use provided connection (within transaction)
        await cursor.execute('''
            UPDATE USER_DETAILS
            SET pending_earnings = COALESCE(pending_earnings, 0) + ?
            WHERE user_id = ?
        ''', (amount, user_id))
        return True

    # Create new connection
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as new_conn:
            try:
                await new_conn.execute('BEGIN IMMEDIATE')
                await new_conn.execute('''
                    UPDATE USER_DETAILS
                    SET pending_earnings = COALESCE(pending_earnings, 0) + ?
                    WHERE user_id = ?
                ''', (amount, user_id))
                await new_conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                try:
                    await new_conn.rollback()
                except Exception:
                    pass
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error(f"[add_pending_earnings] Error: {exc}")
                    return False
            except Exception as e:
                try:
                    await new_conn.rollback()
                except Exception:
                    pass
                logger.error(f"[add_pending_earnings] Error: {e}")
                return False

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(f"[add_pending_earnings] Failed after retries: {last_lock_error}")
    return False


async def record_creator_earnings(
    creator_id: int,
    prompt_id: int,
    consumer_id: int,
    tokens_consumed: int,
    gross_amount: float,
    platform_commission: float,
    net_earnings: float,
    referral_id: int = None,
    conn=None,
    cursor=None
) -> bool:
    """
    Record a creator earnings transaction in CREATOR_EARNINGS table.
    If conn/cursor provided, uses them (for transaction). Otherwise creates new connection.
    """
    if net_earnings <= 0:
        return True

    insert_sql = '''
        INSERT INTO CREATOR_EARNINGS
        (creator_id, prompt_id, consumer_id, referral_id, tokens_consumed,
         gross_amount, platform_commission, net_earnings, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    '''
    params = (creator_id, prompt_id, consumer_id, referral_id, tokens_consumed,
              gross_amount, platform_commission, net_earnings)

    if conn and cursor:
        # Use provided connection (within transaction)
        await cursor.execute(insert_sql, params)
        return True

    # Create new connection
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        wait_time = 0.0
        async with get_db_connection() as new_conn:
            try:
                await new_conn.execute('BEGIN IMMEDIATE')
                await new_conn.execute(insert_sql, params)
                await new_conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                try:
                    await new_conn.rollback()
                except Exception:
                    pass
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                    last_lock_error = exc
                    retry_needed = True
                else:
                    logger.error(f"[record_creator_earnings] Error: {exc}")
                    return False
            except Exception as e:
                try:
                    await new_conn.rollback()
                except Exception:
                    pass
                logger.error(f"[record_creator_earnings] Error: {e}")
                return False

        if retry_needed:
            await asyncio.sleep(wait_time)
            continue
        break

    if last_lock_error:
        logger.error(f"[record_creator_earnings] Failed after retries: {last_lock_error}")
    return False


async def get_prompt_pricing_info(prompt_id: int, conn=None) -> dict:
    """
    Get pricing information for a prompt.
    Returns dict with: is_paid, markup_per_mtokens, created_by_user_id
    """
    query = '''
        SELECT is_paid, markup_per_mtokens, created_by_user_id
        FROM PROMPTS
        WHERE id = ?
    '''

    if conn:
        cursor = await conn.execute(query, (prompt_id,))
        row = await cursor.fetchone()
    else:
        async with get_db_connection(readonly=True) as new_conn:
            cursor = await new_conn.execute(query, (prompt_id,))
            row = await cursor.fetchone()

    if not row:
        return {'is_paid': False, 'markup_per_mtokens': 0.0, 'created_by_user_id': None}

    return {
        'is_paid': bool(row[0]),
        'markup_per_mtokens': float(row[1] or 0),
        'created_by_user_id': row[2]
    }


async def get_user_referral_info(user_id: int, conn=None) -> dict:
    """
    Get referral information for a user.
    Returns dict with: created_by (referral_id), referral_markup_per_mtokens
    """
    query = '''
        SELECT created_by, referral_markup_per_mtokens
        FROM USER_DETAILS
        WHERE user_id = ?
    '''

    if conn:
        cursor = await conn.execute(query, (user_id,))
        row = await cursor.fetchone()
    else:
        async with get_db_connection(readonly=True) as new_conn:
            cursor = await new_conn.execute(query, (user_id,))
            row = await cursor.fetchone()

    if not row:
        return {'created_by': None, 'referral_markup_per_mtokens': 0.0}

    return {
        'created_by': row[0],
        'referral_markup_per_mtokens': float(row[1] or 0)
    }


async def get_user_billing_info(user_id: int, conn=None) -> dict:
    """
    Get team billing configuration for a user.
    Returns dict with billing fields for team billing.
    """
    query = '''
        SELECT billing_account_id, billing_limit, billing_limit_action,
               billing_current_month_spent, billing_month_reset_date,
               billing_auto_refill_amount, billing_max_limit, billing_auto_refill_count
        FROM USER_DETAILS
        WHERE user_id = ?
    '''

    if conn:
        cursor = await conn.execute(query, (user_id,))
        row = await cursor.fetchone()
    else:
        async with get_db_connection(readonly=True) as new_conn:
            cursor = await new_conn.execute(query, (user_id,))
            row = await cursor.fetchone()

    if not row:
        return {
            'billing_account_id': None,
            'billing_limit': None,
            'billing_limit_action': 'block',
            'billing_current_month_spent': 0.0,
            'billing_month_reset_date': None,
            'billing_auto_refill_amount': 10.0,
            'billing_max_limit': None,
            'billing_auto_refill_count': 0
        }

    return {
        'billing_account_id': row[0],
        'billing_limit': float(row[1]) if row[1] is not None else None,
        'billing_limit_action': row[2] or 'block',
        'billing_current_month_spent': float(row[3] or 0),
        'billing_month_reset_date': row[4],
        'billing_auto_refill_amount': float(row[5]) if row[5] is not None else 10.0,
        'billing_max_limit': float(row[6]) if row[6] is not None else None,
        'billing_auto_refill_count': int(row[7] or 0)
    }


async def reset_monthly_billing_if_needed(user_id: int, conn, cursor) -> bool:
    """
    Reset billing_current_month_spent if we're in a new month.
    Returns True if reset was performed, False otherwise.
    """
    from datetime import datetime

    current_month = datetime.now().strftime('%Y-%m')

    # Get current billing info
    await cursor.execute('''
        SELECT billing_month_reset_date, billing_current_month_spent
        FROM USER_DETAILS
        WHERE user_id = ?
    ''', (user_id,))
    row = await cursor.fetchone()

    if not row:
        return False

    last_reset_month = row[0]

    # If we're in a new month, reset the counters
    if last_reset_month != current_month:
        await cursor.execute('''
            UPDATE USER_DETAILS
            SET billing_current_month_spent = 0.00,
                billing_month_reset_date = ?,
                billing_auto_refill_count = 0
            WHERE user_id = ?
        ''', (current_month, user_id))
        return True

    return False


async def get_llm_info(llm_id: int) -> dict | None:
    """Lookup machine/model from LLM table by ID. Returns dict or None."""
    if llm_id is None:
        return None
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT id, machine, model FROM LLM WHERE id = ?", (llm_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {"id": row[0], "machine": row[1], "model": row[2]}


def extract_post_watchdog_config(config: dict | None) -> dict | None:
    """Normalize flat/nested watchdog config and return the post-watchdog block.
    Canonical version - import from here instead of duplicating."""
    if not isinstance(config, dict):
        return None
    post_watchdog = config.get("post_watchdog")
    if isinstance(post_watchdog, dict):
        return post_watchdog
    return config


def extract_pre_watchdog_config(config: dict | None) -> dict | None:
    """Extract the pre-watchdog sub-config from nested watchdog config."""
    if not isinstance(config, dict):
        return None
    pre_watchdog = config.get("pre_watchdog")
    if isinstance(pre_watchdog, dict):
        return pre_watchdog
    return None


async def get_llm_token_costs(model: str, conn=None) -> tuple[float, float]:
    """Return (input_token_cost, output_token_cost) per million tokens for a model."""
    query = "SELECT input_token_cost, output_token_cost FROM LLM WHERE model = ?"
    if conn:
        cursor = await conn.execute(query, (model,))
        row = await cursor.fetchone()
    else:
        async with get_db_connection(readonly=True) as new_conn:
            cursor = await new_conn.execute(query, (model,))
            row = await cursor.fetchone()

    if not row:
        return 0.0, 0.0
    return float(row[0] or 0.0), float(row[1] or 0.0)


async def consume_token(
    user_id,
    input_tokens,
    output_tokens,
    input_token_cost_per_million,
    output_token_cost_per_million,
    conn,
    cursor,
    reasoning_tokens=0,
    prompt_id=None,
    byok=False,
):
    """
    Consume tokens and apply pricing logic based on prompt configuration.

    Pricing Scenarios:
    A) FREE Prompt: API cost * (1 + margin_free)
    B) PAID Prompt (external user): API cost * (1 + margin_paid) + creator_markup + referral_markup
    C) Creator uses OWN prompt: API cost * (1 + margin_personal)
    D) Same as B but with referral markup added
    """
    try:
        # Convert to float and calculate API cost
        input_token_cost_per_million = float(input_token_cost_per_million)
        output_token_cost_per_million = float(output_token_cost_per_million)

        input_token_cost = input_token_cost_per_million / 1_000_000
        output_token_cost = output_token_cost_per_million / 1_000_000

        # Calculate base API cost
        input_cost_total = input_tokens * input_token_cost
        output_cost_total = (output_tokens + reasoning_tokens) * output_token_cost
        api_cost = input_cost_total + output_cost_total
        total_tokens = input_tokens + output_tokens + reasoning_tokens

        # BYOK: user provides their own API key, so platform incurs no API cost
        if byok:
            api_cost = 0
            input_cost_total = 0
            output_cost_total = 0

        # Get pricing configuration
        pricing_config = await get_pricing_config()
        margin_free = pricing_config['margin_free']
        margin_paid = pricing_config['margin_paid']
        margin_personal = pricing_config['margin_personal']
        commission_rate = pricing_config['commission']

        # Initialize earnings tracking
        creator_earnings = 0.0
        referral_earnings = 0.0
        creator_id = None
        referral_id = None

        # Determine pricing scenario
        if prompt_id:
            # Get prompt pricing info
            prompt_info = await get_prompt_pricing_info(prompt_id, conn)
            is_paid = prompt_info['is_paid']
            creator_markup_per_mtokens = prompt_info['markup_per_mtokens']
            creator_id = prompt_info['created_by_user_id']

            is_creator = (creator_id == user_id)

            if not is_paid:
                # SCENARIO A: Free prompt - apply free margin
                total_cost = api_cost * (1 + margin_free)
                logger.debug(f"[consume_token] Scenario A (FREE): API={api_cost:.6f}, margin={margin_free}, total={total_cost:.6f}")

            elif is_creator:
                # SCENARIO C: Creator using own prompt - apply personal margin, no markup
                total_cost = api_cost * (1 + margin_personal)
                logger.debug(f"[consume_token] Scenario C (PERSONAL): API={api_cost:.6f}, margin={margin_personal}, total={total_cost:.6f}")

            else:
                # SCENARIO B/D: Paid prompt by external user
                base_cost = api_cost * (1 + margin_paid)

                # Calculate creator markup
                creator_markup = creator_markup_per_mtokens * total_tokens / 1_000_000

                # Check for referral markup
                user_referral_info = await get_user_referral_info(user_id, conn)
                referral_markup_per_mtokens = user_referral_info['referral_markup_per_mtokens']
                potential_referral_id = user_referral_info['created_by']

                referral_markup = 0.0
                if referral_markup_per_mtokens > 0 and potential_referral_id:
                    referral_id = potential_referral_id
                    referral_markup = referral_markup_per_mtokens * total_tokens / 1_000_000

                total_cost = base_cost + creator_markup + referral_markup

                # Calculate earnings (70% of markup goes to creator/referral)
                if creator_markup > 0 and creator_id:
                    platform_commission = creator_markup * commission_rate
                    creator_earnings = creator_markup - platform_commission

                    # Record creator earnings
                    await record_creator_earnings(
                        creator_id=creator_id,
                        prompt_id=prompt_id,
                        consumer_id=user_id,
                        tokens_consumed=total_tokens,
                        gross_amount=creator_markup,
                        platform_commission=platform_commission,
                        net_earnings=creator_earnings,
                        referral_id=referral_id,
                        conn=conn,
                        cursor=cursor
                    )

                    # Add to creator's pending earnings
                    await add_pending_earnings(creator_id, creator_earnings, conn, cursor)

                if referral_markup > 0 and referral_id:
                    referral_platform_commission = referral_markup * commission_rate
                    referral_earnings = referral_markup - referral_platform_commission

                    # Add to referral's pending earnings
                    await add_pending_earnings(referral_id, referral_earnings, conn, cursor)

                logger.debug(
                    f"[consume_token] Scenario B/D (PAID): API={api_cost:.6f}, base={base_cost:.6f}, "
                    f"creator_markup={creator_markup:.6f}, referral_markup={referral_markup:.6f}, "
                    f"total={total_cost:.6f}, creator_earnings={creator_earnings:.6f}, referral_earnings={referral_earnings:.6f}"
                )
        else:
            # No prompt_id - fallback to free pricing (shouldn't happen normally)
            total_cost = api_cost * (1 + margin_free)
            logger.debug(f"[consume_token] No prompt_id - using free margin: API={api_cost:.6f}, total={total_cost:.6f}")

        # Check for team billing
        billing_info = await get_user_billing_info(user_id, conn)
        billing_account_id = billing_info['billing_account_id']

        if billing_account_id:
            # TEAM BILLING: charge manager's account instead of user's
            manager_id = billing_account_id

            # Reset monthly billing counter if new month
            await reset_monthly_billing_if_needed(user_id, conn, cursor)

            # Get fresh billing info after potential reset
            await cursor.execute('''
                SELECT billing_limit, billing_limit_action, billing_current_month_spent,
                       billing_auto_refill_amount, billing_max_limit
                FROM USER_DETAILS WHERE user_id = ?
            ''', (user_id,))
            user_billing = await cursor.fetchone()
            billing_limit = float(user_billing[0]) if user_billing[0] is not None else None
            billing_limit_action = user_billing[1] or 'block'
            current_month_spent = float(user_billing[2] or 0)
            auto_refill_amount = float(user_billing[3]) if user_billing[3] is not None else 10.0
            max_limit = float(user_billing[4]) if user_billing[4] is not None else None

            # Check monthly spending limit
            if billing_limit is not None:
                if current_month_spent + total_cost > billing_limit:
                    if billing_limit_action == 'block':
                        logger.info(f"[consume_token] Team-billed user {user_id} blocked - monthly limit reached. Limit: {billing_limit}, Spent: {current_month_spent}, Requested: {total_cost}")
                        return False
                    elif billing_limit_action == 'auto_refill':
                        # Auto-refill: increase the limit automatically
                        new_limit = billing_limit + auto_refill_amount

                        # Check if we would exceed the maximum limit cap
                        if max_limit is not None and new_limit > max_limit:
                            logger.info(f"[consume_token] Team-billed user {user_id} blocked - auto_refill would exceed max limit. Current: {billing_limit}, Max: {max_limit}")
                            return False

                        # Update the billing limit and increment auto_refill_count
                        await cursor.execute('''
                            UPDATE USER_DETAILS
                            SET billing_limit = ?,
                                billing_auto_refill_count = billing_auto_refill_count + 1
                            WHERE user_id = ?
                        ''', (new_limit, user_id))

                        logger.info(f"[consume_token] Team-billed user {user_id} auto-refill triggered. Limit: {billing_limit} -> {new_limit}")
                        billing_limit = new_limit  # Update local variable for subsequent checks
                    else:
                        # 'notify' - log warning but continue
                        logger.warning(f"[consume_token] Team-billed user {user_id} over monthly limit (action=notify). Limit: {billing_limit}, Spent: {current_month_spent + total_cost}")

            # Check manager's balance
            await cursor.execute('SELECT balance FROM USER_DETAILS WHERE user_id = ?', (manager_id,))
            manager_result = await cursor.fetchone()
            if not manager_result:
                logger.info(f"Manager with ID {manager_id} not found in USER_DETAILS")
                return False

            manager_balance = manager_result[0]
            if total_cost > manager_balance:
                logger.info(f"Insufficient manager balance. Required: {total_cost:.6f}, Available: {manager_balance:.6f}")
                return False

            # Deduct from manager's balance
            await cursor.execute('''
                UPDATE USER_DETAILS
                SET balance = balance - ?
                WHERE user_id = ?
            ''', (total_cost, manager_id))

            # Update user's billing spent (for limit tracking) and token stats (no balance deduction)
            await cursor.execute('''
                UPDATE USER_DETAILS
                SET billing_current_month_spent = billing_current_month_spent + ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    input_token_cost = input_token_cost + ?,
                    output_token_cost = output_token_cost + ?,
                    total_cost = total_cost + ?,
                    tokens_spent = tokens_spent + ?
                WHERE user_id = ?
            ''', (total_cost, input_tokens, output_tokens + reasoning_tokens, input_cost_total, output_cost_total, total_cost, total_tokens, user_id))

            # Record daily usage summary (under actual user, not manager)
            await record_daily_usage(
                user_id=user_id,
                usage_type='ai_tokens',
                cost=total_cost,
                tokens_in=input_tokens,
                tokens_out=output_tokens + reasoning_tokens,
                conn=conn,
                cursor=cursor
            )

            logger.debug(f"[consume_token] Team billing: charged manager {manager_id} ${total_cost:.6f} for user {user_id}")

        else:
            # STANDARD MODE: charge user's own balance
            await cursor.execute('SELECT balance FROM USER_DETAILS WHERE user_id = ?', (user_id,))
            result = await cursor.fetchone()
            if not result:
                logger.info(f"User with ID {user_id} not found in USER_DETAILS")
                return False

            current_balance = result[0]
            if total_cost > current_balance:
                logger.info(f"Insufficient balance to consume tokens. Required: {total_cost:.6f}, Available: {current_balance:.6f}")
                return False

            # Update USER_DETAILS with tokens spent and total cost
            await cursor.execute('''
                UPDATE USER_DETAILS
                SET balance = balance - ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    input_token_cost = input_token_cost + ?,
                    output_token_cost = output_token_cost + ?,
                    total_cost = total_cost + ?,
                    tokens_spent = tokens_spent + ?
                WHERE user_id = ?
            ''', (total_cost, input_tokens, output_tokens + reasoning_tokens, input_cost_total, output_cost_total, total_cost, total_tokens, user_id))

            # Record daily usage summary
            await record_daily_usage(
                user_id=user_id,
                usage_type='ai_tokens',
                cost=total_cost,
                tokens_in=input_tokens,
                tokens_out=output_tokens + reasoning_tokens,
                conn=conn,
                cursor=cursor
            )

        # Don't commit here since transaction is managed by caller's transaction
        return True
    except Exception as e:
        logger.error(f"[consume_token] - Error executing balance update query: {e}")
        return False


# ============================================================================
# Phase 5: White-Label Branding Functions
# ============================================================================

async def get_manager_branding(manager_id: int, conn=None) -> dict:
    """
    Get white-label branding configuration for a manager.
    Returns default values if no custom branding is configured.
    """
    query = '''
        SELECT company_name, logo_url, brand_color_primary, brand_color_secondary,
               footer_text, email_signature, hide_platform_branding, forced_theme,
               disable_theme_selector
        FROM MANAGER_BRANDING
        WHERE manager_id = ?
    '''

    if conn:
        cursor = await conn.execute(query, (manager_id,))
        row = await cursor.fetchone()
    else:
        async with get_db_connection(readonly=True) as new_conn:
            cursor = await new_conn.execute(query, (manager_id,))
            row = await cursor.fetchone()

    if not row:
        return {
            'company_name': None,
            'logo_url': None,
            'brand_color_primary': '#6366f1',
            'brand_color_secondary': '#10B981',
            'footer_text': None,
            'email_signature': None,
            'hide_platform_branding': False,
            'forced_theme': None,
            'disable_theme_selector': False
        }

    return {
        'company_name': row[0],
        'logo_url': row[1],
        'brand_color_primary': row[2] or '#6366f1',
        'brand_color_secondary': row[3] or '#10B981',
        'footer_text': row[4],
        'email_signature': row[5],
        'hide_platform_branding': bool(row[6]),
        'forced_theme': row[7],
        'disable_theme_selector': bool(row[8])
    }


async def get_branding_for_user(user_id: int, conn=None) -> dict:
    """
    Get white-label branding for a user based on their creator/manager.
    If the user was created by a manager with custom branding, return that.
    Otherwise return default branding.
    """
    query = '''
        SELECT ucr.creator_id
        FROM USER_CREATOR_RELATIONSHIPS ucr
        WHERE ucr.user_id = ? AND ucr.is_primary = 1
    '''

    if conn:
        cursor = await conn.execute(query, (user_id,))
        row = await cursor.fetchone()
        creator_id = row[0] if row else None

        if creator_id:
            return await get_manager_branding(creator_id, conn)
    else:
        async with get_db_connection(readonly=True) as new_conn:
            cursor = await new_conn.execute(query, (user_id,))
            row = await cursor.fetchone()
            creator_id = row[0] if row else None

            if creator_id:
                return await get_manager_branding(creator_id, new_conn)

    # Return default branding
    return {
        'company_name': 'Aurvek',
        'logo_url': None,
        'brand_color_primary': '#6366f1',
        'brand_color_secondary': '#10B981',
        'footer_text': None,
        'email_signature': None,
        'hide_platform_branding': False,
        'forced_theme': None,
        'disable_theme_selector': False
    }


def _safe_color(value, fallback='#6366f1'):
    """Validate hex color to prevent CSS injection. Returns fallback if invalid."""
    if value and re.fullmatch(r'#[0-9a-fA-F]{3,8}', value):
        return value
    return fallback


async def get_branding_for_context(context=None, conn=None) -> dict:
    """
    Resolve branding based on navigation context.
    - context=None -> AURVEK platform defaults (0 queries)
    - context={"creator_id": X} -> that creator's branding
    - context={"storefront_slug": "john"} -> lookup creator by slug -> branding
    - context={"prompt_id": X} -> lookup creator by prompt -> branding
    - context={"pack_id": X} -> lookup creator by pack -> branding
    """
    PLATFORM_DEFAULTS = {
        'company_name': 'Aurvek',
        'logo_url': None,
        'brand_color_primary': '#6366f1',
        'brand_color_secondary': '#10B981',
        'footer_text': None,
        'email_signature': None,
        'hide_platform_branding': False,
        'forced_theme': None,
        'disable_theme_selector': False,
        'context_type': 'platform',
        'is_custom_domain': False
    }

    if context is None:
        return PLATFORM_DEFAULTS

    creator_id = None
    context_type = 'platform'

    async def _resolve(connection):
        nonlocal creator_id, context_type

        if 'creator_id' in context:
            creator_id = context['creator_id']
            context_type = 'storefront'
        elif 'storefront_slug' in context:
            cursor = await connection.execute(
                "SELECT user_id FROM CREATOR_PROFILES WHERE slug = ?",
                (context['storefront_slug'],)
            )
            row = await cursor.fetchone()
            if row:
                creator_id = row[0]
                context_type = 'storefront'
        elif 'prompt_id' in context:
            cursor = await connection.execute(
                "SELECT created_by_user_id FROM PROMPTS WHERE id = ?",
                (context['prompt_id'],)
            )
            row = await cursor.fetchone()
            if row:
                creator_id = row[0]
                context_type = 'product'
        elif 'pack_id' in context:
            cursor = await connection.execute(
                "SELECT created_by_user_id FROM PACKS WHERE id = ?",
                (context['pack_id'],)
            )
            row = await cursor.fetchone()
            if row:
                creator_id = row[0]
                context_type = 'product'

    if conn:
        await _resolve(conn)
    else:
        async with get_db_connection(readonly=True) as new_conn:
            await _resolve(new_conn)

    if not creator_id:
        return PLATFORM_DEFAULTS

    branding = await get_manager_branding(creator_id, conn)
    branding['brand_color_primary'] = _safe_color(branding.get('brand_color_primary'), '#6366f1')
    branding['brand_color_secondary'] = _safe_color(branding.get('brand_color_secondary'), '#10B981')
    branding['context_type'] = context_type
    branding['is_custom_domain'] = False

    return branding


async def upsert_creator_relationship(cursor, user_id: int, creator_id: int, rel_type: str, source_type: str, source_id: int = None):
    """
    Insert or update a creator relationship.
    PK is (user_id, creator_id, relationship_type) -- allows multiple types per pair.
    Sets is_primary=1 only if no existing primary (enforced by UNIQUE index).
    Self-relationships are silently skipped.
    """
    if user_id == creator_id:
        return

    existing = await (await cursor.execute(
        "SELECT 1 FROM USER_CREATOR_RELATIONSHIPS WHERE user_id = ? AND creator_id = ? AND relationship_type = ?",
        (user_id, creator_id, rel_type)
    )).fetchone()
    if existing:
        await cursor.execute(
            "UPDATE USER_CREATOR_RELATIONSHIPS SET last_interaction_at = CURRENT_TIMESTAMP WHERE user_id = ? AND creator_id = ? AND relationship_type = ?",
            (user_id, creator_id, rel_type))
        return

    try:
        await cursor.execute("""
            INSERT INTO USER_CREATOR_RELATIONSHIPS
            (user_id, creator_id, relationship_type, source_type, source_id, is_primary)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (user_id, creator_id, rel_type, source_type, source_id))
    except sqlite3.IntegrityError:
        # UNIQUE constraint on is_primary=1 fired -- insert as non-primary
        await cursor.execute("""
            INSERT INTO USER_CREATOR_RELATIONSHIPS
            (user_id, creator_id, relationship_type, source_type, source_id, is_primary)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (user_id, creator_id, rel_type, source_type, source_id))


# ---------------------------------------------------------------------------
# Landing page SEO metadata fixer (save-time)
# ---------------------------------------------------------------------------

def fix_landing_seo_tags(html: str, canonical_url: str, static_base_url: str) -> str:
    """
    Fix/inject core SEO meta tags in landing HTML so files on disk have
    correct absolute URLs. Call at save-time (editor, AI wizard, migration).

    Handles: canonical, og:url, og:image, twitter:card, twitter:image.
    Skips files without a <head> tag (not proper HTML documents).
    """
    if not re.search(r'<head[\s>]', html, re.IGNORECASE):
        return html

    def _make_absolute(url):
        if not url:
            return url
        # Resolve template placeholders left by landing page generator
        url = url.replace('{{LANDING_BASE_URL}}/', '').replace('{{LANDING_BASE_URL}}', '')
        # Clean double slashes (preserve ://)
        url = re.sub(r'(?<!:)//', '/', url)
        if url.startswith(('https://', 'http://')):
            return url
        base = static_base_url.rstrip('/') + '/'
        # Strip leading ./ or ../ prefixes without being greedy
        while url.startswith(('./','../')):
            url = url[2:] if url.startswith('./') else url[3:]
        return base + url

    # Helper: build regex that matches a <meta> tag regardless of attribute order.
    # Returns compiled pattern with group(1) capturing the value of `val_attr`.
    def _meta_pat(key_attr, key_name, val_attr, val_name):
        # Order A: key="name" val="value"  |  Order B: val="value" key="name"
        a = rf'<meta\s+{key_attr}=["\']{ re.escape(key_name) }["\']\s+{val_attr}=["\']([^"\']*)["\'][^>]*/?\s*>'
        b = rf'<meta\s+{val_attr}=["\']([^"\']*)["\'\s]+{key_attr}=["\']{ re.escape(key_name) }["\'][^>]*/?\s*>'
        return re.compile(f'(?:{a}|{b})', re.IGNORECASE)

    def _meta_match_value(m):
        """Return the captured value from whichever alternation matched."""
        return m.group(1) if m.group(1) is not None else m.group(2)

    inject_tags = []

    # 1. Canonical — fix existing or inject (attribute order is fixed for <link>)
    canon_pat = re.compile(
        r'<link\s+rel=["\']canonical["\']\s+href=["\']([^"\']*)["\'][^>]*/?\s*>'
        r'|<link\s+href=["\']([^"\']*)["\'\s]+rel=["\']canonical["\'][^>]*/?\s*>',
        re.IGNORECASE,
    )
    m = canon_pat.search(html)
    if m:
        val = m.group(1) if m.group(1) is not None else m.group(2)
        if not val.startswith('https://'):
            html = html.replace(m.group(0), f'<link rel="canonical" href="{canonical_url}">', 1)
    else:
        inject_tags.append(f'<link rel="canonical" href="{canonical_url}">')

    # 2. og:url — fix existing or inject
    og_url_pat = _meta_pat('property', 'og:url', 'content', 'content')
    m = og_url_pat.search(html)
    if m:
        val = _meta_match_value(m)
        if not val.startswith('https://'):
            html = html.replace(m.group(0), f'<meta property="og:url" content="{canonical_url}">', 1)
    else:
        inject_tags.append(f'<meta property="og:url" content="{canonical_url}">')

    # 3. og:image — make relative paths absolute
    og_image_pat = _meta_pat('property', 'og:image', 'content', 'content')
    og_image_abs = None
    m = og_image_pat.search(html)
    if m:
        val = _meta_match_value(m)
        if val:
            og_image_abs = _make_absolute(val)
            if og_image_abs != val:
                html = html.replace(
                    m.group(0),
                    f'<meta property="og:image" content="{og_image_abs}">',
                    1,
                )
            else:
                og_image_abs = val if val.startswith('https://') else None

    # 4. twitter:card — inject if missing
    tw_card_pat = _meta_pat('name', 'twitter:card', 'content', 'content')
    if not tw_card_pat.search(html):
        inject_tags.append('<meta name="twitter:card" content="summary_large_image">')

    # 5. twitter:image — fix existing or inject (mirrors og:image)
    tw_image_pat = _meta_pat('name', 'twitter:image', 'content', 'content')
    m = tw_image_pat.search(html)
    if m:
        val = _meta_match_value(m)
        if val:
            abs_img = _make_absolute(val)
            if abs_img != val:
                html = html.replace(
                    m.group(0),
                    f'<meta name="twitter:image" content="{abs_img}">',
                    1,
                )
    elif og_image_abs:
        inject_tags.append(f'<meta name="twitter:image" content="{og_image_abs}">')

    # Inject any missing tags before </head>
    if inject_tags:
        inject_block = '\n    '.join(inject_tags)
        html = re.sub(
            r'(</head>)',
            f'    {inject_block}\n\\1',
            html,
            count=1,
            flags=re.IGNORECASE,
        )

    return html
