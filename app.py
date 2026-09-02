import io
import os
import re
import sys
import json
import math
import uuid
import html
import pytz
import zlib
import time
import httpx
import openai
import base64
import ffmpeg
import orjson
import string
import random
import shutil
import psutil
import qrcode
import hashlib
import secrets
import asyncio
import aiohttp
import aiofiles
import sqlite3
import logging
import uvicorn
import requests
import aiosqlite
import traceback
import markdown2
import mimetypes
import subprocess
import tracemalloc
import aiofiles.os
import urllib.parse
from io import BytesIO
from pathlib import Path
from functools import wraps
from dotenv import load_dotenv
from pydub import AudioSegment
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
import jwt
from jwt import PyJWTError as JWTError
from pydantic import BaseModel
from cachetools import TTLCache
from reportlab.lib import colors
from html import escape, unescape
from unicodedata import normalize
from fastapi import Query, Header, BackgroundTasks
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from mutagen.oggopus import OggOpus
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from passlib.context import CryptContext
from contextlib import asynccontextmanager
from reportlab.lib.pagesizes import letter
from fastapi import UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder
from fastapi.templating import Jinja2Templates
from typing import Annotated, Any, Union, Optional, List, Dict, Tuple
from starlette.background import BackgroundTask
from starlette.status import HTTP_401_UNAUTHORIZED
from fastapi.middleware.cors import CORSMiddleware
from urllib.parse import urljoin, urlparse, urlencode, quote
from fastapi import WebSocket, WebSocketDisconnect
from datetime import date, datetime, timezone, timedelta
from PIL import Image as PilImage, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, HRFlowable, PageBreak
from fastapi import FastAPI, Response, HTTPException, Depends, Request, Form, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse, FileResponse

#import imghdr

# Librerias propias
from tools import *
from admin_audit import log_admin_action
import storage_quota
from log_config import logger
from maintenance_tasks import (
    MaintenanceTaskBusy,
    MaintenanceTaskTimedOut,
    clear_audio_cache as run_audio_cache_cleanup,
    disable_cloudflare_cache as run_cloudflare_cache_disable,
)
from tools import dramatiq_tasks
from database import get_db_connection, DB_MAX_RETRIES, DB_RETRY_DELAY_BASE, is_lock_error
from models import User, ConnectionManager
from tasks import generate_pdf_task, generate_mp3_task
from rediscfg import broker, redis_client, add_revoked_user, remove_revoked_user, RedisManager, get_metrics, get_active_users_count
from save_images import (
    generate_img_token,
    get_or_generate_img_token,
    normalize_image_path,
    resize_image,
    save_image_locally,
)
from auth import (
    bump_session_version,
    create_access_token,
    get_current_user,
    get_user_by_id,
    get_user_by_username,
    has_recent_authentication,
    hash_password,
    verify_password,
)
from auth_constants import SESSION_COOKIE_NAME
from auth import get_current_user_from_websocket, get_user_id_from_conversation, get_user_by_token, create_user_info, create_login_response, generate_magic_link
from auth import get_user_by_google_id, get_user_by_email, update_user_google_id
from auth_flows import (
    generate_username_from_email,
    generate_unique_username,
    get_after_login_redirect,
    handle_login_request,
    username_exists,
)
from auth import ACCESS_TOKEN_EXPIRE_MINUTES, unauthenticated_response
from email_service import email_service
from email_validation import validate_email_robust
from ultra_admin import (
    generate_elevation_code, verify_elevation_code, is_elevated,
    revoke_elevation, get_elevation_ttl, get_active_lock_owner, ELEVATION_TTL
)
from user_accounts import (
    InitialBalanceError,
    InsufficientInitialBalanceError,
    InvalidInitialBalanceError,
    apply_initial_balance,
    get_live_user_role,
    get_user_management_access,
    validate_managed_balance,
)
from billing.usage_reservations import reconcile_stale_usage_reservations
from phone_verification import (
    PURPOSE_CREATE_USER,
    PURPOSE_PROFILE_PHONE_CHANGE,
    PhoneVerificationError,
    PhoneVerificationRateLimitError,
    consume_phone_verification,
    normalize_phone_number,
    request_phone_verification,
    verify_phone_code,
)
from common import Cost, generate_user_hash, has_sufficient_balance, cost_tts, cache_directory, users_directory, tts_engine, get_balance, deduct_balance, record_daily_usage, load_service_costs, estimate_message_tokens, custom_unescape, templates, validate_path_within_directory, slugify, is_internal_ip, generate_public_id, get_template_context, fix_landing_seo_tags, get_auth_base_url, get_request_base_url, _get_marketplace_template_flags
from common import SCRIPT_DIR, DATA_DIR, CLOUDFLARE_API_KEY, CLOUDFLARE_EMAIL, CLOUDFLARE_ZONE_ID, CLOUDFLARE_API_URL, CLOUDFLARE_FOR_IMAGES, CLOUDFLARE_SECRET, CLOUDFLARE_IMAGE_SUBDOMAIN, CLOUDFLARE_BASE_URL, generate_cloudflare_signature, generate_signed_url_cloudflare, CLOUDFLARE_DOMAIN, CLOUDFLARE_CNAME_TARGET
from common import ALGORITHM, MAX_TOKENS, MAX_MESSAGE_SIZE, MAX_IMAGE_UPLOAD_SIZE, MAX_IMAGE_PIXELS, PERPLEXITY_API_KEY, elevenlabs_key, openai_key, claude_key, gemini_key, openrouter_key, service_sid, decode_jwt_cached, verify_token_expiration, AVATAR_TOKEN_EXPIRE_HOURS, MEDIA_TOKEN_EXPIRE_HOURS
from common import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from common import encrypt_api_key, decrypt_api_key, mask_api_key
from common import CDN_FILES_URL, ENABLE_CDN
from common import SECURE_COOKIES
from common import READONLY_MODE
from common import MAX_API_IMAGE_SIZE_MB, MAX_CHAT_IMAGE_DIMENSION
from common import compute_static_hashes
from common import upsert_creator_relationship
import nh3
from chat.registration import router as chat_router
from chat.services.attachment_uploads import (
    ATTACHMENT_UPLOAD_MAX_CHUNK_SIZE_BYTES,
    prune_stale_attachment_upload_chunks,
)
from chat.services.privacy import (
    ensure_conversation_privacy_schema,
)
from file_storage import (
    delete_attachment_and_rewrite_message,
    discard_stale_pending_attachments,
    ensure_file_storage_schema,
    prune_unreferenced_blobs,
)
from welcome_service import build_world, user_has_pack_access, user_has_prompt_access, serve_welcome_world
from tools.tts import handle_tts_request, resolve_playback_voice
from ai_runtime.voice_resolution import CanonicalVoiceResolutionError
from system_prompt_defaults import (
    MANDATORY_SYSTEM_KEYS, SYSTEM_BLOCK_METADATA, DEFAULT_SYSTEM_BLOCKS, MAX_BLOCK_CONTENT_SIZE
)
from llm_catalog import (
    LlmCatalogError,
    build_manual_insert_metadata,
    fetch_remote_models,
    get_catalog as get_llm_catalog,
    get_provider_catalog_view,
    get_selector_llms,
    is_sync_managed,
    merge_manual_overrides,
    normalize_provider_key,
    set_model_enabled,
    sync_all_providers,
    sync_provider,
)

from billing.routes import router as billing_router
from integrations.runtime import ensure_integration_schema
from integrations.routes import router as integrations_router
from prompt_access import can_user_access_pack, get_user_accessible_prompts

from prompts import router as prompts_router
from marketplace.routes.packs import router as packs_router
from marketplace.routes.packs import warmup_pack_landing_cache, _pack_landing_cache_stats, _pack_landing_cache, PACK_LANDING_CACHE_SIZE
from prompts import get_user_accessible_prompts as get_user_role_accessible_prompts, get_user_owned_prompts, create_prompt_directory, get_prompt_info, get_prompt_path, get_pack_path, get_prompt_templates_dir, get_prompt_components_dir, can_manage_prompt, get_manageable_prompts
from prompts import get_user_directory, get_user_prompts_directory, list_prompts, process_prompt_image_upload, create_prompt, create_prompt_post, edit_prompt, update_prompt, delete_prompt, delete_prompt_image
from prompts import get_prompt_owner_id
from prompts import can_user_access_prompt
import user_deletion
from marketplace.landing.cache import get_landing_cache_stats, warmup_landing_cache
from marketplace.landing.rendering import landing_404_response
from marketplace.landing.jobs import cleanup_old_jobs
from security_guard_llm import check_security, is_security_guard_enabled
from ranking import get_ranking_config, start_scheduled_ranking_loop, recalculate_ranking_scores
from rate_limiter import (
    check_rate_limits, check_failure_limit, record_failure,
    RateLimitConfig as RLC, get_client_ip, rate_limiter
)
from captcha_service import verify_captcha, get_captcha_config, set_captcha_enabled, get_captcha_runtime_status
from cloudflare_geo import get_all_geo_data, validate_country_codes, validate_continent_codes, geo_sync_engine, CloudflareGeoClient, get_countries_for_continent
from wellbeing_service import (
    ensure_wellbeing_schema,
    get_admin_events as get_wellbeing_admin_events,
    get_admin_live_sessions,
    get_admin_overview as get_wellbeing_admin_overview,
    get_active_pause,
    get_status as get_wellbeing_status,
    get_user_preferences as get_wellbeing_user_preferences,
    get_user_wellbeing_summary,
    get_wellbeing_config,
    record_activity as record_wellbeing_activity,
    record_user_action as record_wellbeing_user_action,
    reset_user_session as reset_wellbeing_user_session,
    update_user_preferences as update_wellbeing_user_preferences,
    update_wellbeing_config,
)

# Custom domains for landing pages
from marketplace.middleware.custom_domains import CustomDomainMiddleware, set_primary_domains
# Security middleware for scanner/bot protection
from middleware.security import (
    SecurityMiddleware,
    get_security_stats_async,
    get_security_events_async,
    get_security_blocked_ips_async,
    manually_block_ip_async,
    manually_unblock_ip_async,
    is_ip_blocked_async,
    retry_cloudflare_sync_async,
)
# Security config for forbidden names
from security_config import is_forbidden_username
from marketplace.routes.admin import create_router as create_marketplace_admin_router
from marketplace.routes.analytics import router as marketplace_analytics_router
from marketplace.routes.checkout import router as marketplace_checkout_router
from marketplace.routes.custom_domains import router as custom_domains_router, admin_router as custom_domains_admin_router
from marketplace.routes.discovery import router as marketplace_discovery_router
from marketplace.routes.geo import router as marketplace_geo_router
from marketplace.routes.marketing import router as marketplace_marketing_router
from marketplace.routes.acquisition import router as marketplace_acquisition_router
from auth_apple import router as apple_auth_router
from content_reports.routes import router as content_reports_router
from legal.routes import router as legal_router
from mobile.routes import router as mobile_router
from marketplace.routes.prompt_landing_builder import router as prompt_landing_builder_router
from marketplace.routes.prompt_landings import (
    custom_domain_router as prompt_custom_domain_router,
    router as prompt_landings_router,
    serve_custom_domain_home,
)
from marketplace.routes.ranking import router as marketplace_ranking_router
from marketplace.routes.storefronts import router as marketplace_storefronts_router
from memory.routes import router as memory_router
from marketplace.services.acquisition_context import (
    handle_pack_for_existing_user,
    render_custom_domain_login,
    render_custom_domain_register,
    resolve_pack_oauth_context,
)
from marketplace.services.entitlements import active_entitlement_condition, grant_pack_entitlement
from marketplace.services.landing_registration import (
    DEFAULT_LANDING_REGISTRATION_CONFIG,
    get_landing_registration_config,
    strip_personal_subscription_default,
)
from marketplace.services.pending_entitlements import send_entitlement_claim_email
from marketplace.services.pending_registrations import (
    cleanup_expired_registrations,
    create_pending_registration,
    delete_pending_registration,
    get_pending_registration,
    get_user_by_email_record,
)
from marketplace.config import (
    get_marketplace_flags,
    marketplace_checkout_enabled,
    marketplace_discovery_enabled,
    marketplace_public_landings_enabled,
    require_checkout_enabled,
    require_creator_tools_enabled,
    require_public_landings_enabled,
)
from marketplace.runtime import load_marketplace_config_from_db, refresh_marketplace_config_loop


TTS_BILLING_MISSING_PROVIDERS: tuple[str, ...] = ()


async def _initialize_tts_billing() -> None:
    """Load provider prices before any runtime backend can accept TTS work."""
    global TTS_BILLING_MISSING_PROVIDERS
    await Cost.initialize()
    TTS_BILLING_MISSING_PROVIDERS = tuple(sorted(
        provider
        for provider, config in Cost.TTS_PROVIDER_SERVICES.items()
        if config.get("service_id") is None
    ))
    if TTS_BILLING_MISSING_PROVIDERS:
        logger.critical(
            "TTS billing unavailable for providers: %s; those providers will fail closed",
            ", ".join(TTS_BILLING_MISSING_PROVIDERS),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _marketplace_refresh_task = None
    _nginx_blocklist_reconcile_task = None
    _telephony_runtime = None
    _telephony_unregister = None
    await _initialize_tts_billing()

    # Check which system to use based on configuration
    use_redis = os.getenv('REDIS_IMG_TOKEN', '0') == '1'

    if use_redis:
        # Initialize Redis at startup
        logger.info("Initializing Redis connection...")
        redis_manager = RedisManager.get_instance()

        # Verify connections
        try:
            # Verify sync client
            sync_client = await redis_manager.get_sync_client()

            # Verify async client
            async_client = await redis_manager.get_async_client()

            logger.info("Redis connections established successfully")
        except Exception as e:
            logger.error("Error connecting to Redis: %s", e)
            raise
    else:
        # Initialize in-memory SQLite
        logger.info("Initializing in-memory SQLite...")
        try:
            from save_images import initialize_memory_db
            await initialize_memory_db()
            logger.info("In-memory SQLite initialized successfully")
        except Exception as e:
            logger.error("Error initializing in-memory SQLite: %s", e)
            logger.warning("Continuing without memory DB initialization...")
            # Don't raise - continue startup

    # Set WAL journal mode once (persistent across connections)
    from database import ensure_wal_mode
    await ensure_wal_mode()
    # Voice-note assets extend the attachment kind checks. Production deploys
    # run this migration before startup; this guard also upgrades local/legacy
    # databases before the file-storage schema is cached for the process.
    try:
        from migration_messaging_voice_notes import migrate as migrate_messaging_voice_notes

        await asyncio.to_thread(migrate_messaging_voice_notes)
    except ModuleNotFoundError:
        logger.info("Messaging voice-note migration module not found; assuming schema is current")
    except Exception:
        logger.exception("Failed to apply messaging voice-note migration")
        raise
    await ensure_file_storage_schema()
    await discard_stale_pending_attachments()
    pruned_upload_dirs = await prune_stale_attachment_upload_chunks()
    if pruned_upload_dirs:
        logger.info("Pruned %d stale attachment upload chunk directorie(s)", pruned_upload_dirs)
    await ensure_conversation_privacy_schema()
    await ensure_wellbeing_schema()

    # Keep additive schema migrations available for existing installs. The
    # standalone runner still handles deployment migrations; this guard keeps
    # local/legacy startup from reaching routes with missing LLM catalog columns.
    try:
        from migration_llm_catalog_metadata import migrate as migrate_llm_catalog_metadata
        await asyncio.to_thread(migrate_llm_catalog_metadata)
    except ModuleNotFoundError:
        logger.info("LLM catalog migration module not found; assuming schema is current")
    except Exception:
        logger.exception("Failed to apply LLM catalog metadata migration")
        raise

    try:
        from migration_llm_reasoning_capabilities import migrate as migrate_llm_reasoning_capabilities
        await asyncio.to_thread(migrate_llm_reasoning_capabilities)
    except ModuleNotFoundError:
        logger.info("LLM reasoning capabilities migration module not found; assuming catalog is current")
    except Exception:
        logger.exception("Failed to apply LLM reasoning capabilities migration")
        raise

    try:
        from migration_entitlements import migrate as migrate_entitlements
        await asyncio.to_thread(migrate_entitlements)
    except ModuleNotFoundError:
        logger.info("Entitlements migration module not found; assuming schema is current")
    except Exception:
        logger.exception("Failed to apply entitlements migration")
        raise

    # Compute static asset hashes for cache-busting
    compute_static_hashes()

    # Initialize IP Reputation system
    from middleware.ip_reputation import reputation_manager
    await reputation_manager.initialize()

    # Initialize nginx blocklist sync
    from middleware.nginx_blocklist import nginx_blocklist_manager
    await nginx_blocklist_manager.initialize()
    try:
        await nginx_blocklist_manager.reconcile_once()
    except Exception:
        logger.exception("Initial nginx blocklist reconciliation failed")
    _nginx_blocklist_reconcile_task = asyncio.create_task(
        nginx_blocklist_manager.reconciliation_loop()
    )

    await ensure_integration_schema()

    # Create system tables if not exists
    async with get_db_connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS SYSTEM_CONFIG (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.commit()

    await load_marketplace_config_from_db()
    _marketplace_refresh_task = asyncio.create_task(refresh_marketplace_config_loop())

    if marketplace_public_landings_enabled():
        # Warm up landing page cache with most visited prompts
        await warmup_landing_cache()

        # Warm up pack landing page cache with most visited packs
        await warmup_pack_landing_cache()
    else:
        logger.info("Marketplace public landing cache warmup skipped (public landings disabled)")

    # Ranking system startup — elect ONE worker as leader via atomic file lock.
    # Without this, all N workers redundantly recalculate rankings at startup
    # and run parallel scheduled loops, wasting DB writes.
    import tempfile as _tempfile
    _ranking_lock = os.path.join(
        _tempfile.gettempdir(), f"aurvek_ranking_{os.getppid()}.lock"
    )
    _is_ranking_leader = False

    # Clean stale lock from a previous crashed run (handles OS PID reuse)
    try:
        if os.path.exists(_ranking_lock):
            if time.time() - os.path.getmtime(_ranking_lock) > 300:
                os.unlink(_ranking_lock)
    except OSError:
        pass

    try:
        fd = os.open(_ranking_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        _is_ranking_leader = True
    except FileExistsError:
        pass

    if _is_ranking_leader:
        ranking_config = await get_ranking_config()
        if ranking_config['mode'] == 'scheduled':
            asyncio.create_task(start_scheduled_ranking_loop())
        # Initial recalculation at startup
        asyncio.create_task(recalculate_ranking_scores())

    # GranSabio worker config validation
    from gransabio_service import check_gransabio_worker_config
    await check_gransabio_worker_config(dual_mode_active=_dual_mode_active)

    # Best-effort sweep of stale GPTSub (ChatGPT subscription) runtime dirs left by
    # a prior crash: per-user homes (u_*) and per-turn work dirs
    # (gptsub_cwd_*/turn_*) are ephemeral, and a fresh single-worker start has no
    # live turns, so orphans are safe to remove. Only runs if GPTSUB_RUNTIME_DIR
    # is set; fully defensive -- it must never crash startup.
    _gptsub_runtime_dir = None
    _gptsub_runtime_sweep_ok = True
    if os.environ.get("GPTSUB_RUNTIME_DIR"):
        try:
            from subscription_auth.config import resolve_codex_home_runtime_dir

            _gptsub_runtime_dir = resolve_codex_home_runtime_dir()
        except Exception:
            _gptsub_runtime_sweep_ok = False
            logger.exception("GPTSub runtime directory validation failed at startup")
    if _gptsub_runtime_dir and os.path.isdir(_gptsub_runtime_dir):
        _gptsub_stale_prefixes = (
            "u_",
            "gptsub_cwd_",
            "turn_",
            "appserver_",
        )
        _gptsub_swept = 0
        try:
            for _entry in os.scandir(_gptsub_runtime_dir):
                if not _entry.name.startswith(_gptsub_stale_prefixes):
                    continue
                # Direct, real directories only. Never follow a symlink/reparse point
                # supplied inside the runtime directory during a crash recovery
                # sweep. A matching non-directory is itself an unsafe residue and
                # must block later enablement; silently skipping it would let the
                # initialize-only doctor approve an unclean runtime.
                if not _entry.is_dir(follow_symlinks=False):
                    raise RuntimeError("unsafe stale GPTSub runtime entry")
                _target = _entry.path
                shutil.rmtree(_target, ignore_errors=False)
                if os.path.lexists(_target):
                    raise RuntimeError("stale GPTSub runtime path survived cleanup")
                _gptsub_swept += 1
            if _gptsub_swept:
                logger.info("Swept %d stale GPTSub runtime director(ies) at startup", _gptsub_swept)
        except Exception:
            _gptsub_runtime_sweep_ok = False
            logger.exception(
                "GPTSub runtime dir sweep failed; readiness will remain disabled"
            )

    # A failed validation/sweep is a plaintext-lifecycle safety failure even when
    # the durable switch currently says OFF. Latch now so an administrator cannot
    # enable later in the same process after a basic initialize-only doctor passes.
    if not _gptsub_runtime_sweep_ok and _SUBSCRIPTION_AUTH_MODULE_AVAILABLE:
        try:
            from subscription_auth.gate import latch_subscription_auth_off

            latch_subscription_auth_off()
        except Exception:
            logger.critical(
                "GPTSub runtime sweep failure could not engage the local OFF latch",
                exc_info=True,
            )

    # A deployment must never remain globally enabled when its exact binary,
    # dedicated key, isolation directory, or initialize contract is broken. This
    # silent readiness probe contains no account/model data and automatically trips
    # the persistent kill switch on a bad restart.
    try:
        from common import (
            get_subscription_auth_enabled,
            set_subscription_auth_enabled,
        )

        if await get_subscription_auth_enabled():
            ready = False
            try:
                from subscription_auth.doctor import runtime_ready

                ready = _gptsub_runtime_sweep_ok and await runtime_ready()
            except Exception:
                # An exception is itself a failed readiness check. Persist OFF
                # below; logging alone would leave a previously-enabled flag live.
                logger.exception("GPTSub startup readiness probe failed")
            if not ready:
                # The database write may fail while the previously-read TRUE value
                # remains cached. Trip the process-local irreversible latch first so
                # no request can materialize a credential on a rejected runtime.
                from subscription_auth.gate import latch_subscription_auth_off

                latch_subscription_auth_off()
                await set_subscription_auth_enabled(False)
                logger.critical(
                    "GPTSub failed startup readiness; subscription_auth_enabled was disabled"
                )
    except Exception:
        # The first switch read can itself fail before runtime_ready() runs. Do not
        # rely only on that transient read failure: if the DB recovers with a
        # persisted TRUE value later in this process, inference would otherwise be
        # admitted on a runtime that this startup never approved. Latch locally
        # before a best-effort durable OFF write.
        if _SUBSCRIPTION_AUTH_MODULE_AVAILABLE:
            try:
                from subscription_auth.gate import latch_subscription_auth_off

                latch_subscription_auth_off()
            except Exception:
                logger.critical(
                    "GPTSub startup failure could not engage the local OFF latch",
                    exc_info=True,
                )
            try:
                from common import set_subscription_auth_enabled

                await asyncio.wait_for(
                    set_subscription_auth_enabled(False), timeout=5.0
                )
            except Exception:
                logger.critical(
                    "GPTSub startup failure could not persist the OFF switch; "
                    "the local latch remains authoritative",
                    exc_info=True,
                )
        logger.exception("GPTSub startup readiness enforcement failed")

    try:
        from integrations.telephony.admin_service import (
            register_runtime_lifecycle,
            unregister_runtime_lifecycle,
        )
        from integrations.telephony.runtime import get_telephony_runtime

        _telephony_runtime = get_telephony_runtime()
        register_runtime_lifecycle(_telephony_runtime)
        _telephony_unregister = unregister_runtime_lifecycle
        _telephony_status = await _telephony_runtime.start()
        if _telephony_status.enabled and not _telephony_status.ready:
            logger.warning(
                "Native telephone channel is fail-closed: %s",
                _telephony_status.reason,
            )
    except Exception:
        logger.exception("Native telephone lifecycle failed closed during startup")

    try:
        yield
    finally:
        if _telephony_runtime is not None:
            try:
                await _telephony_runtime.stop()
            except Exception:
                logger.exception("Native telephone lifecycle failed during shutdown")
            finally:
                if _telephony_unregister is not None:
                    _telephony_unregister(_telephony_runtime)

    if _marketplace_refresh_task:
        _marketplace_refresh_task.cancel()
        try:
            await _marketplace_refresh_task
        except asyncio.CancelledError:
            pass

    # Shutdown IP Reputation system
    from middleware.ip_reputation import reputation_manager
    await reputation_manager.shutdown()

    # Shutdown nginx blocklist sync
    from middleware.nginx_blocklist import nginx_blocklist_manager
    if _nginx_blocklist_reconcile_task:
        _nginx_blocklist_reconcile_task.cancel()
        try:
            await _nginx_blocklist_reconcile_task
        except asyncio.CancelledError:
            pass
    await nginx_blocklist_manager.shutdown()

    # Cleanup ranking leader lock file
    if _is_ranking_leader:
        try:
            os.unlink(_ranking_lock)
        except OSError:
            pass

    # GranSabio HTTP client cleanup
    try:
        from gransabio_service import shutdown_http_client
        await shutdown_http_client()
    except Exception:
        pass

    try:
        from integrations.elevenlabs.service import (
            shutdown_http_client as shutdown_elevenlabs_http_client,
        )
        await shutdown_elevenlabs_http_client()
    except Exception:
        logger.exception("Error closing ElevenLabs HTTP client")

    try:
        from atagia_bridge import close_atagia_bridge
        await close_atagia_bridge()
    except Exception:
        pass

    if async_twilio is not None:
        try:
            await async_twilio.close()
        except Exception:
            logger.exception("Error closing async Twilio client")

    # Tear down any held GPTSub (ChatGPT subscription) pending-link login state
    # so its worker subprocesses do not leak on restart. Private
    # module -- guard the import; ImportError just means the feature is absent.
    try:
        from subscription_auth import shutdown_pending_links
        await shutdown_pending_links()
    except ImportError:
        pass
    except Exception:
        logger.exception("subscription_auth: error tearing down pending links on shutdown")

    # Cleanup on shutdown
    if use_redis:
        logger.info("Closing Redis connections...")
        await RedisManager.close()
    else:
        logger.info("Closing in-memory SQLite connection...")
        from save_images import close_memory_db
        await close_memory_db()

# Disable Swagger/ReDoc/OpenAPI schema in production (set ENABLE_API_DOCS=1 in .env to enable)
# All three must be disabled: leaving openapi_url exposed leaks the full API schema,
# especially via custom domains where nginx proxies ALL requests to FastAPI.
_enable_docs = os.getenv("ENABLE_API_DOCS", "").lower() in ("1", "true", "yes")
docs_url = "/docs" if _enable_docs else None
redoc_url = "/redoc" if _enable_docs else None
openapi_url = "/openapi.json" if _enable_docs else None
app = FastAPI(lifespan=lifespan, docs_url=docs_url, redoc_url=redoc_url, openapi_url=openapi_url)

app.include_router(chat_router)
app.include_router(prompts_router)
app.include_router(packs_router)
app.include_router(integrations_router)
app.include_router(billing_router)
app.include_router(apple_auth_router)
app.include_router(legal_router)
app.include_router(mobile_router)
app.include_router(content_reports_router)
app.include_router(memory_router)
app.include_router(custom_domains_router)
app.include_router(custom_domains_admin_router)
app.include_router(marketplace_discovery_router)
app.include_router(marketplace_marketing_router)
app.include_router(marketplace_storefronts_router)
app.include_router(marketplace_analytics_router)
app.include_router(marketplace_ranking_router)
app.include_router(marketplace_checkout_router)
app.include_router(marketplace_geo_router)
app.include_router(prompt_landings_router)
app.include_router(prompt_landing_builder_router)
app.include_router(marketplace_acquisition_router)

# GPTSub (ChatGPT subscription) account-linking router. Private module,
# excluded from the public mirror -- guard the import so the open-source build
# simply lacks the feature.
_SUBSCRIPTION_AUTH_MODULE_AVAILABLE = False
try:
    from subscription_auth import router as subscription_auth_router
    app.include_router(subscription_auth_router)
    _SUBSCRIPTION_AUTH_MODULE_AVAILABLE = True
except ModuleNotFoundError as exc:
    # The public mirror deliberately omits this private package. A missing
    # dependency *inside* an installed package is a broken private deployment and
    # must still fail startup instead of silently hiding the feature.
    if exc.name != "subscription_auth":
        raise

# CORS configuration - use ALLOWED_ORIGINS env var (comma-separated) or default to same-origin only
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
allowed_origins = [origin.strip() for origin in allowed_origins_env.split(",") if origin.strip()] if allowed_origins_env else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True if allowed_origins else False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

model_token_cost_cache = {}

# Get number of physical CPUs
num_cpus = psutil.cpu_count(logical=False)

# Calculate max_pool based on formula
max_pool = num_cpus * 2 + 1

if os.getenv("APP_DEBUG", "false").lower() == "true":
    tracemalloc.start()
load_dotenv()

app.secret_key = os.getenv('APP_SECRET_KEY')
SECRET_KEY = os.getenv('APP_SECRET_KEY')

# Define the file system path where static files are located
static_directory = Path("data/static")

# Mount static files at the "/static" path of our application
app.mount("/static", StaticFiles(directory=static_directory), name="static")

# Voice samples to choose from
VOICE_SAMPLES_DIR = os.path.join(static_directory, 'audio', 'voice_samples')

manager = ConnectionManager()

default_lang = "es"

# External service clients — in a separate module so Python's sys.modules
# caching prevents double-execution per worker on Windows (multiprocessing
# spawn + uvicorn import string causes app.py to run 2x per worker process).
# NOTE: OpenAI/Anthropic/Gemini clients are initialized in ai_runtime.
from clients import (
    async_twilio,
    deepgram, stt_engine, stt_fallback_enabled,
)

user_costs_cache = TTLCache(maxsize=1024, ttl=3600)

PEPPER = os.getenv('PEPPER')

rol = "santa"
role_file_path = f"rols/{rol}.txt"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="_oauth_state",
    https_only=SECURE_COOKIES,
    same_site="lax",
)

# Custom Domain Middleware - configure primary domains that skip DB lookup
PRIMARY_APP_DOMAIN = os.getenv("PRIMARY_APP_DOMAIN", "")
set_primary_domains([
    PRIMARY_APP_DOMAIN,
    CLOUDFLARE_DOMAIN,
    "localhost",
    "127.0.0.1"
])
app.add_middleware(CustomDomainMiddleware)

# Security middleware - MUST be last (executes first in request chain)
# Blocks scanners/bots by pattern matching and 404 accumulation
app.add_middleware(SecurityMiddleware)


# ---------------------------------------------------------------------------
# SEO: noindex header for non-public routes
# ---------------------------------------------------------------------------
# Adds X-Robots-Tag: noindex to responses for authenticated, admin, API, and
# internal routes so search engines only index public-facing pages.
_PUBLIC_EXACT_PATHS = frozenset(("/", "/sitemap.xml", "/robots.txt", "/favicon.ico"))
_PUBLIC_PREFIXES = ("/p/", "/store/", "/pack/", "/static/", "/.well-known/")

# ---------------------------------------------------------------------------
# Read-only mode: blocks write operations for failover instances
# ---------------------------------------------------------------------------
if READONLY_MODE:
    _READONLY_WHITELIST = frozenset((
        "/magic-link-recovery",
        "/api/refresh-session",
        "/api/ultra-admin/request-code",
        "/api/ultra-admin/verify",
    ))

    @app.middleware("http")
    async def readonly_middleware(request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        if request.url.path in _READONLY_WHITELIST:
            return await call_next(request)
        if request.url.path.endswith("/login"):
            return await call_next(request)
        return JSONResponse(
            status_code=503,
            content={
                "error": "readonly",
                "message": "Service is in read-only mode. Normal service will resume shortly."
            }
        )


ATTACHMENT_UPLOAD_CHUNK_REQUEST_RE = re.compile(r"^/api/conversations/\d+/attachments/chunk$")
ATTACHMENT_UPLOAD_MULTIPART_OVERHEAD_BYTES = 64 * 1024
ATTACHMENT_UPLOAD_MAX_REQUEST_BYTES = (
    ATTACHMENT_UPLOAD_MAX_CHUNK_SIZE_BYTES + ATTACHMENT_UPLOAD_MULTIPART_OVERHEAD_BYTES
)


@app.middleware("http")
async def reject_oversized_attachment_chunk_requests(request: Request, call_next):
    if (
        request.method == "POST"
        and ATTACHMENT_UPLOAD_CHUNK_REQUEST_RE.match(request.url.path)
    ):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                request_size = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "message": "Invalid Content-Length"},
                )
            if request_size > ATTACHMENT_UPLOAD_MAX_REQUEST_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"success": False, "message": "Attachment upload chunk is too large"},
                )
    return await call_next(request)


@app.middleware("http")
async def add_noindex_header(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path

    is_public = (
        path in _PUBLIC_EXACT_PATHS
        or any(path.startswith(pfx) for pfx in _PUBLIC_PREFIXES)
        or (path.endswith(".html") and "/" not in path[1:])  # root-level .html files
    )

    if not is_public:
        response.headers["X-Robots-Tag"] = "noindex"

    return response


class PhoneVerificationRequest(BaseModel):
    phone: str
    purpose: str


class VerificationCodeRequest(BaseModel):
    challenge_id: str
    phone: str
    purpose: str
    code: str
class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None


class SecurityManualBlockRequest(BaseModel):
    ip: str
    hours: int = 24
    reason: str = "Manual block"


class SecurityManualUnblockRequest(BaseModel):
    ip: str


class SecurityRetrySyncRequest(BaseModel):
    ip: str
    reason: str = "Manual sync retry from admin panel"


class SecurityResetScoreRequest(BaseModel):
    ip: str


class TextToSpeechRequest(BaseModel):
    text: str
    user_id: int
    conversation_id: int


class ChangePasswordRequest(BaseModel):
    username: str
    password: str
class TextToSpeechRequest(BaseModel):
    text: str
    user_id: int
    conversation_id: int


# Websockets for TTS
connected_websocket = None
current_task = None


def read_role_prompt(file_path):
    with open(file_path, "r", encoding='utf-8') as file:
        content = file.read().strip()
    return content

async def get_user_prompt(user_id):
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute("SELECT p.prompt FROM USER_DETAILS u JOIN PROMPTS p ON u.current_prompt_id = p.id WHERE u.user_id = ?", (user_id,))
            result = await cursor.fetchone()
            if result:
                return result[0]
            else:
                initial_prompt = read_role_prompt(role_file_path)
                return initial_prompt
        except sqlite3.Error as e:
            logger.error(f"Error {e}")

async def get_user_llm_cost(user_id):
    if user_id in user_costs_cache:
        return user_costs_cache[user_id]

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute('''
                SELECT L.model, L.input_token_cost, L.output_token_cost
                FROM USER_DETAILS UD
                JOIN LLM L ON UD.llm_id = L.id
                WHERE UD.user_id = ?
            ''', (user_id,))
            row = await cursor.fetchone()
            if row:
                model, input_token_cost, output_token_cost = row
                user_costs_cache[user_id] = (model, input_token_cost, output_token_cost)
                return model, input_token_cost, output_token_cost
            else:
                logger.info(f"No LLM costs found for user {user_id}")
                return None
        except Exception as e:
            logger.error(f"Error loading LLM cost for user {user_id}: {e}")
            return None

def handle_error(request: Request, error_code: int, error_message: str):
    context = {
        "request": request,
        "error_code": error_code,
        "error_message": error_message,
        "marketplace": _get_marketplace_template_flags(),
    }
    return templates.TemplateResponse("error.html", context, status_code=error_code)

def parse_optional_float(value, default=None):
    """Parse optional float from form input. Returns default for None/empty strings."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid numeric value provided.")



async def is_admin(user_id):
    async with get_db_connection(readonly=True) as conn:
        query = """
        SELECT u.role_id, r.role_name
        FROM USERS u
        JOIN USER_ROLES r ON u.role_id = r.id
        WHERE u.id = ?
        """
        try:
            async with conn.execute(query, (user_id,)) as cursor:
                result = await cursor.fetchone()
                return bool(result and result[1].lower() == 'admin')
        except sqlite3.Error as e:
            logger.error(f"Error verifying if user is admin: {e}")
            return False

async def have_vision(user_id):
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute("SELECT allow_file_upload FROM user_details WHERE user_id=?", (user_id,)) as cursor:
            result = await cursor.fetchone()
    return bool(result and result[0])


app.include_router(create_marketplace_admin_router(log_admin_action))


async def add_user(
    username,
    prompt_id,
    all_prompts_access,
    public_prompts_access,
    llm_id,
    allow_file_upload,
    allow_image_generation,
    balance,
    phone,
    role_name,
    authentication_mode="magic_link_only",
    initial_password=None,
    can_change_password=False,
    email=None,
    company_id=None,
    current_user=None,
    api_key_mode="both_prefer_own",
    category_access=None,
    billing_account_id=None,
    billing_limit=None,
    billing_limit_action="block",
    billing_auto_refill_amount=10.0,
    billing_max_limit=None,
    initial_balance_funder_id=None,
    allow_platform_balance_grant=False,
    phone_verification_id=None,
    phone_verification_actor_id=None,
    allow_unverified_phone=False,
):
    try:
        async with get_db_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            async with conn.cursor() as c:
                # Get the role_ids
                await c.execute("SELECT id, role_name FROM USER_ROLES")
                roles = {row[1].lower(): row[0] for row in await c.fetchall()}

                # Try to get the role_id for the provided role_name
                role_id = roles.get(role_name.lower())
                if role_id is None:
                    logger.info(f"Role '{role_name}' not found")
                    return None

                # Check if the current user has permission to create this type of user
                if current_user:
                    actor_role = await get_live_user_role(conn, current_user.id)
                    if not (
                        actor_role == "admin"
                        or (actor_role == "user" and role_name.lower() == "customer")
                    ):
                        logger.info("User does not have permission to create this type of user")
                        return None

                # Hash password if provided
                hashed_password = None
                if initial_password:
                    hashed_password = hash_password(initial_password)

                phone_verified = False
                if phone:
                    phone = normalize_phone_number(phone)
                    await c.execute(
                        "SELECT id FROM USERS WHERE phone_number = ?",
                        (phone,),
                    )
                    if await c.fetchone():
                        raise HTTPException(
                            status_code=400,
                            detail="Phone number already in use.",
                        )

                    if phone_verification_id:
                        if not phone_verification_actor_id:
                            raise HTTPException(
                                status_code=400,
                                detail="Phone verification is required.",
                            )
                        await consume_phone_verification(
                            conn,
                            actor_user_id=phone_verification_actor_id,
                            challenge_id=phone_verification_id,
                            phone_number=phone,
                            purpose=PURPOSE_CREATE_USER,
                        )
                        phone_verified = True
                    elif not allow_unverified_phone:
                        raise HTTPException(
                            status_code=400,
                            detail="Phone verification is required.",
                        )

                # Insert user
                await c.execute("""
                    INSERT INTO USERS (
                        username, password, role_id, is_enabled,
                        phone_number, phone_verified, email
                    )
                    VALUES (?, ?, ?, 1, ?, ?, ?)
                    RETURNING id
                """, (
                    username,
                    hashed_password,
                    role_id,
                    phone,
                    phone_verified,
                    email,
                ))

                user_id = await c.fetchone()
                user_id = user_id[0] if user_id else None

                if user_id:
                    await c.execute("""
                        INSERT INTO USER_DETAILS (
                            user_id,
                            current_prompt_id,
                            all_prompts_access,
                            public_prompts_access,
                            llm_id,
                            allow_file_upload,
                            allow_image_generation,
                            balance,
                            created_by,
                            current_alter_ego_id,
                            authentication_mode,
                            can_change_password,
                            api_key_mode,
                            category_access,
                            billing_account_id,
                            billing_limit,
                            billing_limit_action,
                            billing_auto_refill_amount,
                            billing_max_limit,
                            web_search_mode
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'native')
                    """, (
                        user_id,
                        prompt_id,
                        all_prompts_access,
                        public_prompts_access,
                        llm_id,
                        allow_file_upload,
                        allow_image_generation,
                        0.0,
                        current_user.id if current_user else None,
                        authentication_mode,
                        can_change_password,
                        api_key_mode,
                        category_access,
                        billing_account_id,
                        billing_limit,
                        billing_limit_action,
                        billing_auto_refill_amount,
                        billing_max_limit
                    ))

                    await apply_initial_balance(
                        conn,
                        user_id=user_id,
                        amount=balance,
                        funder_user_id=initial_balance_funder_id,
                        allow_platform_grant=allow_platform_balance_grant,
                        granted_by_user_id=current_user.id if current_user else None,
                    )

                    if current_user:
                        await upsert_creator_relationship(
                            c,
                            user_id,
                            current_user.id,
                            "assigned_by",
                            "manual",
                        )

                    await conn.commit()
                return user_id
    except InitialBalanceError:
        raise
    except sqlite3.Error as e:
        logger.error(f"Error adding user: {e}")
        return None


#async def get_current_active_user(current_user: User = Depends(get_current_user)):
#    if not current_user.is_enabled:
#        raise HTTPException(status_code=400, detail="Inactive user")
#    return current_user




@app.get("/health")
async def public_health_check():
    """Public health check for Cloudflare Load Balancing."""
    return JSONResponse(content={"status": "ok", "readonly": READONLY_MODE})


# Health check endpoint for monitoring (admin only)
@app.get("/healthz")
async def health_check(current_user: User = Depends(get_current_user)):
    """
    Simple health check that verifies Redis and SQLite connectivity
    Returns JSON with status of each service
    """
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            "tts_billing": (
                {
                    "status": "degraded",
                    "missing_providers": list(TTS_BILLING_MISSING_PROVIDERS),
                }
                if TTS_BILLING_MISSING_PROVIDERS
                else {"status": "healthy"}
            )
        }
    }

    # Check Redis
    try:
        await redis_client.ping()
        health_status["services"]["redis"] = "healthy"
    except Exception as e:
        health_status["services"]["redis"] = f"error: {str(e)}"
        health_status["status"] = "unhealthy"

    # Check SQLite
    try:
        async with get_db_connection() as db:
            await db.execute("SELECT 1")
        health_status["services"]["sqlite"] = "healthy"
    except Exception as e:
        health_status["services"]["sqlite"] = f"error: {str(e)}"
        health_status["status"] = "unhealthy"

    # Return appropriate HTTP status
    status_code = 200 if health_status["status"] == "healthy" else 503
    return JSONResponse(content=health_status, status_code=status_code)

# Basic metrics endpoint (admin only)
@app.get("/metrics")
async def get_app_metrics(current_user: User = Depends(get_current_user)):
    """
    Basic application metrics endpoint.
    Returns usage counters and active users.
    """
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        # Get all metrics from Redis
        metrics = await get_metrics()
        active_users = await get_active_users_count()

        return JSONResponse(content={
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": {
                **metrics,
                "active_users_current_hour": active_users
            }
        })

    except Exception as e:
        logger.error(f"Error getting metrics: {e}")
        return JSONResponse(
            content={"error": "Could not retrieve metrics", "timestamp": datetime.now(timezone.utc).isoformat()},
            status_code=500
        )

@app.get("/change-password")
async def show_change_password_form(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    # Fetch auth_provider and password status for Google OAuth users
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT auth_provider, password IS NOT NULL FROM USERS WHERE id = ?",
            (current_user.id,)
        )
        row = await cursor.fetchone()

    context = await get_template_context(request, current_user)
    context["auth_provider"] = row[0] if row else None
    context["has_password"] = bool(row[1]) if row else True
    return templates.TemplateResponse("admin_profile.html", context)

@app.post("/api/change-password")
async def change_password(
    old_password: str = Form(...),
    new_password: str = Form(...),
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        return unauthenticated_response()

    user_id = current_user.id

    # Check if user can change password
    if not current_user.should_show_change_password():
        raise HTTPException(status_code=403, detail="You don't have permission to change your password")

    # Validate new password
    if len(new_password) < 8:
        return JSONResponse(status_code=400, content={"detail": "New password must be at least 8 characters"})
    if new_password == old_password:
        return JSONResponse(status_code=400, content={"detail": "New password must be different from the current password"})

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT password FROM USERS WHERE id = ?", (user_id,))
        row = await cursor.fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="User not found")

        stored_password = row[0]

        if not stored_password or not verify_password(stored_password, old_password):
            return JSONResponse(status_code=400, content={"detail": "Current password is incorrect"})

        hashed_new_password = hash_password(new_password)
        await cursor.execute("UPDATE USERS SET password = ? WHERE id = ?", (hashed_new_password, user_id))
        await bump_session_version(conn, user_id)
        await conn.commit()

    response = JSONResponse(
        status_code=200,
        content={
            "detail": "Password changed successfully. Please log in again.",
            "reauthenticate": True,
        },
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

@app.post("/api/set-password")
async def set_initial_password(
    request: Request,
    new_password: str = Form(...),
    current_user: User = Depends(get_current_user)
):
    """Set initial password for users who registered via Google OAuth."""
    if current_user is None:
        return unauthenticated_response()

    if not current_user.can_change_password:
        raise HTTPException(status_code=403, detail="You don't have permission to set a password")
    if not has_recent_authentication(current_user):
        raise HTTPException(
            status_code=403,
            detail="Please sign in again before setting a password",
        )

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT password, auth_provider FROM USERS WHERE id = ?",
            (current_user.id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        stored_password, auth_provider = row[0], row[1]

        # Only allow if user has no password set (Google OAuth users)
        if stored_password is not None:
            raise HTTPException(status_code=400, detail="Password already set. Use change-password instead.")

        if auth_provider not in ("google", "google_linked"):
            raise HTTPException(status_code=400, detail="This endpoint is only for Google OAuth users")

        if len(new_password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

        hashed = hash_password(new_password)
        await conn.execute(
            "UPDATE USERS SET password = ? WHERE id = ?",
            (hashed, current_user.id)
        )
        await bump_session_version(conn, current_user.id)
        await conn.commit()

        logger.info(f"User {current_user.username} (ID={current_user.id}) set initial password via Google OAuth flow")

    response = JSONResponse(
        content={
            "detail": "Password set successfully. Please log in again.",
            "reauthenticate": True,
        }
    )
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

@app.get("/edit-profile")
async def show_edit_profile_form(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT username, phone_number, phone_verified, email, user_info, profile_picture FROM USERS WHERE id = ?", (current_user.id,))
        user_data = await cursor.fetchone()
        await cursor.execute("SELECT balance, voice_id, current_alter_ego_id, home_preferences FROM USER_DETAILS WHERE user_id = ?", (current_user.id,))
        user_details = await cursor.fetchone()
        formatted_balance = f"{user_details[0]:.3f}" if user_details else "0.000"

        voice_id = user_details[1]
        current_alter_ego_id = user_details[2]  # Get the current_alter_ego_id
        raw_home_prefs = user_details[3] if user_details else None
        voice_code = None
        if voice_id:
            await cursor.execute("SELECT voice_code FROM VOICES WHERE id = ?", (voice_id,))
            voice_row = await cursor.fetchone()
            voice_code = voice_row[0] if voice_row else None

        # Get all alter egos for the user
        await cursor.execute("SELECT id, name, description, profile_picture FROM USER_ALTER_EGOS WHERE user_id = ?", (current_user.id,))
        alter_egos = await cursor.fetchall()

    user_data_dict = {
        "username": user_data[0],
        "phone_number": user_data[1] if user_data[1] not in (None, "None", "null") else "",
        "phone_verified": user_data[2] == 1,
        "email": user_data[3] if user_data[3] not in (None, "None", "null") else "",
        "user_info": user_data[4] if user_data[4] else "",
        "profile_picture": user_data[5] if user_data[5] else "",
        "current_alter_ego_id": current_alter_ego_id  # Add current_alter_ego_id here
    }

    # Generate token URL for profile picture, add _128 suffix, and replace 'sk' with 'get_image'
    if user_data_dict["profile_picture"]:
        current_time = datetime.now(timezone.utc)
        new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
        profile_picture_url = f"{user_data_dict['profile_picture']}_128.webp"
        token = generate_img_token(profile_picture_url, new_expiration, current_user)
        user_data_dict["profile_picture"] = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"

    # Prepare alter ego data
    alter_ego_list = []
    for alter_ego in alter_egos:
        alter_ego_dict = {
            "id": alter_ego[0],
            "name": alter_ego[1],
            "description": alter_ego[2],
            "profile_picture": alter_ego[3] if alter_ego[3] else ""
        }

        # Generate token URL for alter ego profile picture
        if alter_ego_dict["profile_picture"]:
            current_time = datetime.now(timezone.utc)
            new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
            profile_picture_url = f"{alter_ego_dict['profile_picture']}_128.webp"
            token = generate_img_token(profile_picture_url, new_expiration, current_user)
            alter_ego_dict["profile_picture"] = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"

        alter_ego_list.append(alter_ego_dict)

    # Parse home_preferences JSON
    home_preferences = {}
    if raw_home_prefs:
        try:
            home_preferences = json.loads(raw_home_prefs)
        except (json.JSONDecodeError, TypeError):
            pass

    context = await get_template_context(request, current_user)
    context.update({
        "user_data": user_data_dict,
        "user_details": {"balance": formatted_balance},
        "current_user_voice_id": voice_code,
        "current_user_id": current_user.id,
        "alter_egos": alter_ego_list,
        "current_alter_ego_id": current_alter_ego_id,
        "home_preferences": home_preferences
    })
    return templates.TemplateResponse("profile/edit_profile.html", context)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, current_user: User = Depends(get_current_user)):
    """Unified settings page with Profile, Usage & Billing, and API Keys tabs."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    from common import get_user_api_key_mode, user_requires_own_keys
    from request_security import ensure_csrf_token

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        # Profile data
        await cursor.execute("SELECT username, phone_number, phone_verified, email, user_info, profile_picture FROM USERS WHERE id = ?", (current_user.id,))
        user_data = await cursor.fetchone()
        await cursor.execute("SELECT balance, voice_id, current_alter_ego_id, home_preferences FROM USER_DETAILS WHERE user_id = ?", (current_user.id,))
        user_details = await cursor.fetchone()
        formatted_balance = f"{user_details[0]:.3f}" if user_details else "0.000"

        voice_id = user_details[1]
        current_alter_ego_id = user_details[2]
        raw_home_prefs = user_details[3] if user_details else None
        voice_code = None
        if voice_id:
            await cursor.execute("SELECT voice_code FROM VOICES WHERE id = ?", (voice_id,))
            voice_row = await cursor.fetchone()
            voice_code = voice_row[0] if voice_row else None

        await cursor.execute("SELECT id, name, description, profile_picture FROM USER_ALTER_EGOS WHERE user_id = ?", (current_user.id,))
        alter_egos = await cursor.fetchall()

    user_data_dict = {
        "username": user_data[0],
        "phone_number": user_data[1] if user_data[1] not in (None, "None", "null") else "",
        "phone_verified": user_data[2] == 1,
        "email": user_data[3] if user_data[3] not in (None, "None", "null") else "",
        "user_info": user_data[4] if user_data[4] else "",
        "profile_picture": user_data[5] if user_data[5] else "",
        "current_alter_ego_id": current_alter_ego_id
    }

    if user_data_dict["profile_picture"]:
        current_time = datetime.now(timezone.utc)
        new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
        profile_picture_url = f"{user_data_dict['profile_picture']}_128.webp"
        token = generate_img_token(profile_picture_url, new_expiration, current_user)
        user_data_dict["profile_picture"] = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"

    alter_ego_list = []
    for alter_ego in alter_egos:
        alter_ego_dict = {
            "id": alter_ego[0],
            "name": alter_ego[1],
            "description": alter_ego[2],
            "profile_picture": alter_ego[3] if alter_ego[3] else ""
        }
        if alter_ego_dict["profile_picture"]:
            current_time = datetime.now(timezone.utc)
            new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
            profile_picture_url = f"{alter_ego_dict['profile_picture']}_128.webp"
            token = generate_img_token(profile_picture_url, new_expiration, current_user)
            alter_ego_dict["profile_picture"] = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"
        alter_ego_list.append(alter_ego_dict)

    home_preferences = {}
    if raw_home_prefs:
        try:
            home_preferences = json.loads(raw_home_prefs)
        except (json.JSONDecodeError, TypeError):
            pass

    # API credentials data
    api_key_mode = await get_user_api_key_mode(current_user.id)
    requires_own_keys = await user_requires_own_keys(current_user.id)

    # Balance
    user_balance = await get_balance(current_user.id)

    context = await get_template_context(request, current_user)
    context.update({
        "user_data": user_data_dict,
        "user_details": {"balance": formatted_balance},
        "current_user_voice_id": voice_code,
        "current_user_id": current_user.id,
        "alter_egos": alter_ego_list,
        "current_alter_ego_id": current_alter_ego_id,
        "home_preferences": home_preferences,
        "can_change_password": current_user.should_show_change_password(),
        "api_key_mode": api_key_mode,
        "requires_own_keys": requires_own_keys,
        "user_balance": user_balance,
        "telephony_csrf_token": ensure_csrf_token(request)
    })
    return templates.TemplateResponse("settings.html", context)


class MemoryPreferencesUpdateRequest(BaseModel):
    remember_across_chats: Optional[bool] = None
    remember_across_devices: Optional[bool] = None
    memory_privacy_mode: Optional[str] = None
    memory_scope: Optional[str] = None


VALID_MEMORY_PRIVACY_MODES = {"balanced", "trusted_private"}
VALID_MEMORY_SCOPES = {"global", "prompt"}


@app.get("/api/user/memory-preferences")
async def get_user_memory_preferences(current_user: User = Depends(get_current_user)):
    """Get the current user's active memory-provider preferences."""
    if current_user is None:
        return unauthenticated_response()

    from memory.config import get_active_memory_provider, get_user_memory_preferences as get_local_memory_preferences
    from memory.health import get_user_memory_health_snapshot

    provider = await get_active_memory_provider()
    local_preferences = await get_local_memory_preferences(current_user.id, provider)
    preferences = dict(local_preferences)
    if provider == "atagia" and local_preferences.get("remember_across_chats") is not False:
        from atagia_bridge import get_atagia_bridge

        atagia_preferences = await get_atagia_bridge().get_memory_preferences(current_user.id)
        memory_scope = preferences.get("memory_scope")
        if atagia_preferences.get("available", False):
            preferences = {**atagia_preferences, "provider": "atagia", "memory_scope": memory_scope}
        else:
            preferences = {
                **preferences,
                "provider": "atagia",
                "remember_across_devices": True,
                "memory_privacy_mode": "balanced",
                "provider_available": False,
                "provider_message": "Memory is not available right now.",
            }
    elif provider == "atagia":
        preferences = {
            **preferences,
            "provider": "atagia",
            "remember_across_devices": True,
            "memory_privacy_mode": "balanced",
        }
    memory_enabled = provider != "none" and local_preferences.get("remember_across_chats") is not False
    preferences["memory_health"] = get_user_memory_health_snapshot(provider, enabled=memory_enabled)
    return JSONResponse(content=preferences)


@app.put("/api/user/memory-preferences")
async def update_user_memory_preferences(
    payload: MemoryPreferencesUpdateRequest,
    current_user: User = Depends(get_current_user),
):
    """Update the current user's active memory-provider preferences."""
    if current_user is None:
        return unauthenticated_response()

    from memory.config import (
        get_active_memory_provider,
        get_user_memory_preferences as get_local_memory_preferences,
        save_user_memory_preferences,
    )
    from memory.health import get_user_memory_health_snapshot

    if (
        payload.memory_privacy_mode is not None
        and payload.memory_privacy_mode not in VALID_MEMORY_PRIVACY_MODES
    ):
        raise HTTPException(status_code=400, detail="Invalid memory privacy mode.")
    if payload.memory_scope is not None and payload.memory_scope not in VALID_MEMORY_SCOPES:
        raise HTTPException(status_code=400, detail="Invalid memory scope.")

    provider = await get_active_memory_provider()
    if provider == "none":
        preferences = await get_local_memory_preferences(current_user.id, provider)
        preferences["memory_health"] = get_user_memory_health_snapshot(provider, enabled=False)
        return JSONResponse(content=preferences, status_code=503)

    local_preferences = await get_local_memory_preferences(current_user.id, provider)
    requested_remember = (
        payload.remember_across_chats
        if payload.remember_across_chats is not None
        else local_preferences.get("remember_across_chats", True)
    )
    memory_enabled = requested_remember is not False
    if provider == "atagia" and memory_enabled:
        from atagia_bridge import get_atagia_bridge

        atagia_preferences = await get_atagia_bridge().set_memory_preferences(
            current_user.id,
            remember_across_chats=payload.remember_across_chats,
            remember_across_devices=payload.remember_across_devices,
            memory_privacy_mode=payload.memory_privacy_mode,
        )
        if not atagia_preferences.get("available", False):
            preferences = {
                **atagia_preferences,
                "provider": "atagia",
                "message": "Memory is not available right now.",
                "memory_scope": payload.memory_scope or local_preferences.get("memory_scope"),
                "memory_health": get_user_memory_health_snapshot(provider, enabled=True),
            }
            return JSONResponse(content=preferences, status_code=503)
        local_preferences = await save_user_memory_preferences(
            current_user.id,
            provider,
            remember_across_chats=payload.remember_across_chats,
            memory_scope=payload.memory_scope,
        )
        preferences = {
            **atagia_preferences,
            "provider": "atagia",
            "memory_scope": local_preferences.get("memory_scope"),
        }
    else:
        local_preferences = await save_user_memory_preferences(
            current_user.id,
            provider,
            remember_across_chats=payload.remember_across_chats,
            memory_scope=payload.memory_scope,
        )
        memory_enabled = local_preferences.get("remember_across_chats") is not False
        preferences = local_preferences

    if provider == "atagia" and not memory_enabled:
        preferences = {
            **local_preferences,
            "provider": "atagia",
            "remember_across_devices": True,
            "memory_privacy_mode": payload.memory_privacy_mode or "balanced",
        }
    preferences["memory_health"] = get_user_memory_health_snapshot(provider, enabled=memory_enabled)
    status_code = 200 if preferences.get("available", False) else 503
    return JSONResponse(content=preferences, status_code=status_code)


# ============================================================================
# API Credentials Routes
# ============================================================================

@app.get("/api-credentials")
async def api_credentials_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the API credentials management page."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    from common import get_user_api_key_mode, user_requires_own_keys

    api_key_mode = await get_user_api_key_mode(current_user.id)
    requires_own_keys = await user_requires_own_keys(current_user.id)

    # ChatGPT subscription (GPTSub) linking state for the settings card.
    # Both reads are guarded so the open-source build (module absent) or a
    # transient DB error simply hides the feature (fail-closed UI).
    subscription_available = False
    subscription_linked = False
    subscription_revocation_pending = False
    subscription_reset_required = False
    subscription_plan_type = None
    try:
        import subscription_auth  # noqa: F401 -- private module; absent in the public GitHub mirror
        from common import get_subscription_auth_enabled
        subscription_available = await get_subscription_auth_enabled()
    except Exception:
        subscription_available = False
    try:
        from subscription_auth.gate import linked_blob_for_user

        link_blob = await linked_blob_for_user(current_user.id)
        if link_blob:
            subscription_linked = True
            subscription_plan_type = link_blob.get("plan_type")
        else:
            from subscription_auth.token_store import load_link

            stored_blob = await load_link(current_user.id)
            subscription_revocation_pending = bool(
                isinstance(stored_blob, dict)
                and stored_blob.get("status") == "revocation_pending"
                and isinstance(stored_blob.get("auth_json"), dict)
            )
            from subscription_auth.linking import _stored_link_block

            subscription_reset_required = (
                await _stored_link_block(current_user.id) == "reset_required"
            )
    except Exception:
        subscription_linked = False
        subscription_revocation_pending = False
        subscription_reset_required = True
        subscription_plan_type = None

    context = await get_template_context(request, current_user)
    context.update({
        "current_user_id": current_user.id,
        "api_key_mode": api_key_mode,
        "requires_own_keys": requires_own_keys,
        "subscription_available": subscription_available,
        "subscription_linked": subscription_linked,
        "subscription_revocation_pending": subscription_revocation_pending,
        "subscription_reset_required": subscription_reset_required,
        "subscription_plan_type": subscription_plan_type,
    })
    return templates.TemplateResponse("profile/api_credentials.html", context)


@app.post("/api/test-api-key")
async def test_api_key(request: Request, current_user: User = Depends(get_current_user)):
    """Test if an API key is valid for a given provider."""
    if current_user is None:
        return unauthenticated_response()

    try:
        data = await request.json()
        provider = data.get("provider")
        key = data.get("key")

        if not provider or not key:
            return JSONResponse(content={"success": False, "message": "Provider and key are required"})

        # Test the key based on provider
        if provider == "openai":
            from openai import OpenAI
            test_client = OpenAI(api_key=key)
            # Make a simple API call to verify the key
            test_client.models.list()
            return JSONResponse(content={"success": True, "message": "OpenAI API key is valid"})

        elif provider == "anthropic":
            import anthropic as anthropic_test
            test_client = anthropic_test.Anthropic(api_key=key)
            # Make an authenticated, read-only request without consuming tokens.
            test_client.models.list(limit=1)
            return JSONResponse(content={"success": True, "message": "Anthropic API key is valid"})

        elif provider == "google":
            from google import genai as genai_test
            test_client = genai_test.Client(api_key=key)
            # List models to verify the key works
            list(test_client.models.list())
            return JSONResponse(content={"success": True, "message": "Google AI API key is valid"})

        elif provider == "xai":
            # xAI uses OpenAI-compatible API
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {key}"}
                async with session.get("https://api.x.ai/v1/models", headers=headers) as response:
                    if response.status == 200:
                        return JSONResponse(content={"success": True, "message": "xAI API key is valid"})
                    else:
                        error_text = await response.text()
                        return JSONResponse(content={"success": False, "message": f"Invalid xAI key: {error_text}"})

        elif provider == "minimax":
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {key}"}
                async with session.get("https://api.minimax.io/v1/models", headers=headers) as response:
                    if response.status == 200:
                        return JSONResponse(content={"success": True, "message": "MiniMax API key is valid"})
                    error_text = await response.text()
                    return JSONResponse(content={"success": False, "message": f"Invalid MiniMax key: {error_text}"})

        elif provider in ("kimi", "moonshot"):
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {key}"}
                async with session.get("https://api.moonshot.ai/v1/models", headers=headers) as response:
                    if response.status == 200:
                        return JSONResponse(content={"success": True, "message": "Kimi API key is valid"})
                    error_text = await response.text()
                    return JSONResponse(content={"success": False, "message": f"Invalid Kimi key: {error_text}"})

        elif provider == "elevenlabs":
            async with aiohttp.ClientSession() as session:
                headers = {"xi-api-key": key}
                async with session.get("https://api.elevenlabs.io/v1/user", headers=headers) as response:
                    if response.status == 200:
                        return JSONResponse(content={"success": True, "message": "ElevenLabs API key is valid"})
                    else:
                        return JSONResponse(content={"success": False, "message": "Invalid ElevenLabs key"})

        else:
            return JSONResponse(content={"success": False, "message": f"Unknown provider: {provider}"})

    except Exception as e:
        logger.error(f"Error testing API key: {e}")
        return JSONResponse(content={"success": False, "message": str(e)})


@app.get("/api/user-credentials")
async def get_all_user_credentials(request: Request, current_user: User = Depends(get_current_user)):
    """Get all saved API credentials for the current user (masked)."""
    if current_user is None:
        return unauthenticated_response()

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,)
            )
            result = await cursor.fetchone()

            if result and result[0]:
                # Decrypt and parse the stored keys
                encrypted_keys = result[0]
                try:
                    keys_json = decrypt_api_key(encrypted_keys)
                    if keys_json:
                        keys = orjson.loads(keys_json)
                        # Return masked versions of the keys
                        masked_keys = {provider: mask_api_key(key) for provider, key in keys.items()}
                        return JSONResponse(content={"success": True, "keys": masked_keys})
                except Exception as e:
                    logger.error(f"Error decrypting user API keys: {e}")

            return JSONResponse(content={"success": True, "keys": {}})

    except Exception as e:
        logger.error(f"Error getting user credentials: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.get("/api/user-credentials/{provider}")
async def get_user_credential(provider: str, request: Request, current_user: User = Depends(get_current_user)):
    """Get a specific API credential for the current user (masked)."""
    if current_user is None:
        return unauthenticated_response()

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,)
            )
            result = await cursor.fetchone()

            if result and result[0]:
                keys_json = decrypt_api_key(result[0])
                if keys_json:
                    keys = orjson.loads(keys_json)
                    if provider in keys:
                        return JSONResponse(content={
                            "exists": True,
                            "key": mask_api_key(keys[provider])
                        })

            return JSONResponse(content={"exists": False})

    except Exception as e:
        logger.error(f"Error getting user credential for {provider}: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.get("/api/user/api-key-status")
async def get_user_api_key_status(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get the current user's API key mode and configuration status.

    Security: Only returns information for the authenticated user.
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from common import (
        get_user_api_key_mode,
        user_can_configure_own_keys,
        user_requires_own_keys,
        user_has_valid_api_keys,
        API_KEY_MODE_LABELS
    )

    mode = await get_user_api_key_mode(current_user.id)
    has_keys = await user_has_valid_api_keys(current_user.id)
    can_configure = await user_can_configure_own_keys(current_user.id)
    requires_own = await user_requires_own_keys(current_user.id)

    # Determine if user can send messages
    can_send_messages = True
    if requires_own and not has_keys:
        can_send_messages = False

    return {
        "mode": mode,
        "mode_label": API_KEY_MODE_LABELS.get(mode, mode),
        "has_own_keys": has_keys,
        "can_configure_own": can_configure,
        "requires_own_keys": requires_own,
        "can_send_messages": can_send_messages
    }


@app.post("/api/user-credentials")
async def save_user_credential(request: Request, current_user: User = Depends(get_current_user)):
    """Save a single API credential for the current user."""
    if current_user is None:
        return unauthenticated_response()

    # Check if user can configure own keys
    from common import user_can_configure_own_keys
    if not await user_can_configure_own_keys(current_user.id):
        return JSONResponse(
            status_code=403,
            content={
                'success': False,
                'error': 'not_allowed',
                'message': 'Your account is configured to use system API keys only.'
            }
        )

    try:
        data = await request.json()
        provider = data.get("provider")
        key = data.get("key")

        if not provider:
            return JSONResponse(content={"success": False, "message": "Provider is required"})

        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            # Get existing keys
            await cursor.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,)
            )
            result = await cursor.fetchone()

            existing_keys = {}
            if result and result[0]:
                keys_json = decrypt_api_key(result[0])
                if keys_json:
                    existing_keys = orjson.loads(keys_json)

            # Update or add the key
            if key:
                existing_keys[provider] = key
            elif provider in existing_keys:
                del existing_keys[provider]

            # Encrypt and save
            encrypted_keys = encrypt_api_key(orjson.dumps(existing_keys).decode('utf-8')) if existing_keys else None

            await cursor.execute(
                "UPDATE USER_DETAILS SET user_api_keys = ? WHERE user_id = ?",
                (encrypted_keys, current_user.id)
            )
            await conn.commit()

            return JSONResponse(content={"success": True, "message": f"Credential for {provider} saved"})

    except Exception as e:
        logger.error(f"Error saving user credential: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.post("/api/user-credentials/batch")
async def save_user_credentials_batch(request: Request, current_user: User = Depends(get_current_user)):
    """Save multiple API credentials at once."""
    if current_user is None:
        return unauthenticated_response()

    # Check if user can configure own keys
    from common import user_can_configure_own_keys
    if not await user_can_configure_own_keys(current_user.id):
        return JSONResponse(
            status_code=403,
            content={
                'success': False,
                'error': 'not_allowed',
                'message': 'Your account is configured to use system API keys only.'
            }
        )

    try:
        data = await request.json()
        keys = data.get("keys", {})

        if not keys:
            return JSONResponse(content={"success": True, "message": "No keys to save"})

        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            # Get existing keys
            await cursor.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,)
            )
            result = await cursor.fetchone()

            existing_keys = {}
            if result and result[0]:
                keys_json = decrypt_api_key(result[0])
                if keys_json:
                    existing_keys = orjson.loads(keys_json)

            # Merge with new keys
            for provider, key in keys.items():
                if key:
                    existing_keys[provider] = key

            # Encrypt and save
            encrypted_keys = encrypt_api_key(orjson.dumps(existing_keys).decode('utf-8')) if existing_keys else None

            await cursor.execute(
                "UPDATE USER_DETAILS SET user_api_keys = ? WHERE user_id = ?",
                (encrypted_keys, current_user.id)
            )
            await conn.commit()

            return JSONResponse(content={"success": True, "message": f"Saved {len(keys)} credentials"})

    except Exception as e:
        logger.error(f"Error saving user credentials batch: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.delete("/api/user-credentials/{provider}")
async def delete_user_credential(provider: str, request: Request, current_user: User = Depends(get_current_user)):
    """Delete a specific API credential."""
    if current_user is None:
        return unauthenticated_response()

    try:
        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            # Get existing keys
            await cursor.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,)
            )
            result = await cursor.fetchone()

            if result and result[0]:
                keys_json = decrypt_api_key(result[0])
                if keys_json:
                    existing_keys = orjson.loads(keys_json)
                    if provider in existing_keys:
                        del existing_keys[provider]

                        # Encrypt and save
                        encrypted_keys = encrypt_api_key(orjson.dumps(existing_keys).decode('utf-8')) if existing_keys else None

                        await cursor.execute(
                            "UPDATE USER_DETAILS SET user_api_keys = ? WHERE user_id = ?",
                            (encrypted_keys, current_user.id)
                        )
                        await conn.commit()

            return JSONResponse(content={"success": True, "message": f"Credential for {provider} deleted"})

    except Exception as e:
        logger.error(f"Error deleting user credential: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.delete("/api/user-credentials")
async def delete_all_user_credentials(request: Request, current_user: User = Depends(get_current_user)):
    """Delete all API credentials for the current user."""
    if current_user is None:
        return unauthenticated_response()

    try:
        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "UPDATE USER_DETAILS SET user_api_keys = NULL WHERE user_id = ?",
                (current_user.id,)
            )
            await conn.commit()

        return JSONResponse(content={"success": True, "message": "All credentials deleted"})

    except Exception as e:
        logger.error(f"Error deleting all user credentials: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


# ============================================================================
# CURATION SETTINGS ENDPOINTS
# ============================================================================

@app.get("/api/user/curation-settings")
async def get_curation_settings(request: Request, current_user: User = Depends(get_current_user)):
    """Get curation markup settings for the current user."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    # Only users can have curation settings
    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Only users can access curation settings"})

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT referral_markup_per_mtokens, pending_earnings FROM USER_DETAILS WHERE user_id = ?",
                (current_user.id,)
            )
            result = await cursor.fetchone()

            if result:
                return JSONResponse(content={
                    "success": True,
                    "referral_markup_per_mtokens": float(result[0] or 0),
                    "pending_earnings": float(result[1] or 0)
                })
            else:
                return JSONResponse(content={
                    "success": True,
                    "referral_markup_per_mtokens": 0,
                    "pending_earnings": 0
                })

    except Exception as e:
        logger.error(f"Error getting curation settings: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.put("/api/user/curation-settings")
async def update_curation_settings(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Update curation markup settings for the current user."""
    require_creator_tools_enabled()

    if current_user is None:
        return unauthenticated_response()

    # Only users can have curation settings
    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Only users can update curation settings"})

    try:
        data = await request.json()
        markup = float(data.get("referral_markup_per_mtokens", 0))

        # Validate markup (must be non-negative)
        if markup < 0:
            return JSONResponse(status_code=400, content={"success": False, "message": "Markup cannot be negative"})

        # Maximum markup limit (e.g., $100 per Mtokens)
        if markup > 100:
            return JSONResponse(status_code=400, content={"success": False, "message": "Markup cannot exceed $100 per million tokens"})

        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "UPDATE USER_DETAILS SET referral_markup_per_mtokens = ? WHERE user_id = ?",
                (markup, current_user.id)
            )
            await conn.commit()

        return JSONResponse(content={"success": True, "message": "Curation settings updated", "referral_markup_per_mtokens": markup})

    except ValueError:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid markup value"})
    except Exception as e:
        logger.error(f"Error updating curation settings: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.get("/curation-settings", response_class=HTMLResponse)
async def curation_settings_page(request: Request, current_user: User = Depends(get_current_user)):
    """Page for users to configure their curation markup settings."""
    require_creator_tools_enabled()

    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    # Only users and admins can access
    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only users can access curation settings")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT referral_markup_per_mtokens, pending_earnings FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,)
        )
        result = await cursor.fetchone()

        curation_data = {
            "referral_markup_per_mtokens": float(result[0] or 0) if result else 0,
            "pending_earnings": float(result[1] or 0) if result else 0
        }

    context = await get_template_context(request, current_user)
    context["curation_data"] = curation_data
    return templates.TemplateResponse("curation_settings.html", context)


# ============================================================================
# User Team Billing Endpoints
# ============================================================================

@app.get("/user/team-billing")
async def user_team_billing_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the team billing dashboard page for users."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only users can access the team billing dashboard")

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("user_team_consumption.html", context)


@app.get("/api/user/team-billing")
async def get_user_team_billing(request: Request, current_user: User = Depends(get_current_user)):
    """Get team consumption data for the user dashboard."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Only users can access this endpoint"}, status_code=403)

    try:
        from datetime import datetime
        now = datetime.now()
        current_month_start = now.replace(day=1).strftime('%Y-%m-%d')
        if now.month == 12:
            next_month_start = f"{now.year + 1}-01-01"
        else:
            next_month_start = f"{now.year}-{now.month + 1:02d}-01"
        last_month_first = now.replace(day=1)
        if last_month_first.month == 1:
            last_month_start = f"{last_month_first.year - 1}-12-01"
        else:
            last_month_start = f"{last_month_first.year}-{last_month_first.month - 1:02d}-01"
        last_month_end = current_month_start

        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()

            # Get user's balance
            await cursor.execute('SELECT balance FROM USER_DETAILS WHERE user_id = ?', (current_user.id,))
            balance_row = await cursor.fetchone()
            my_balance = float(balance_row[0]) if balance_row else 0.0

            # Get customers under this user's billing
            await cursor.execute('''
                SELECT u.id, u.username, u.email,
                       ud.billing_limit, ud.billing_limit_action, ud.billing_current_month_spent
                FROM USERS u
                JOIN USER_DETAILS ud ON u.id = ud.user_id
                WHERE ud.billing_account_id = ?
                ORDER BY ud.billing_current_month_spent DESC
            ''', (current_user.id,))
            users_rows = await cursor.fetchall()

            team_spending_this_month = 0.0
            users = []
            for row in users_rows:
                user_id, username, email, billing_limit, billing_limit_action, current_spent = row
                current_spent = float(current_spent or 0)
                team_spending_this_month += current_spent

                # Determine status
                if billing_limit is not None:
                    billing_limit = float(billing_limit)
                    if current_spent >= billing_limit:
                        status = 'Blocked' if billing_limit_action == 'block' else 'Over Limit'
                    elif current_spent >= billing_limit * 0.9:
                        status = 'At Limit'
                    else:
                        status = 'Active'
                else:
                    status = 'Active'

                users.append({
                    'user_id': user_id,
                    'username': username,
                    'email': email,
                    'this_month_spent': current_spent,
                    'limit': billing_limit,
                    'limit_action': billing_limit_action or 'block',
                    'status': status
                })

            # Get last month spending (from TRANSACTIONS where description contains user IDs)
            # This is a simplified approach - just sum up current month totals from users
            await cursor.execute('''
                SELECT COALESCE(SUM(t.amount), 0)
                FROM TRANSACTIONS t
                JOIN USER_DETAILS ud ON t.user_id = ud.user_id
                WHERE ud.billing_account_id = ?
                AND t.type = 'payment'
                AND t.created_at >= ? AND t.created_at < ?
            ''', (current_user.id, last_month_start, last_month_end))
            last_month_row = await cursor.fetchone()
            team_spending_last_month = float(last_month_row[0]) if last_month_row else 0.0

            # Get spending breakdown by prompt (this month)
            await cursor.execute('''
                SELECT p.name as prompt_name,
                       COUNT(DISTINCT m.user_id) as user_count,
                       COUNT(m.id) as message_count,
                       COALESCE(SUM(m.input_tokens_used + m.output_tokens_used), 0) as tokens
                FROM MESSAGES m
                JOIN CONVERSATIONS c ON m.conversation_id = c.id
                JOIN PROMPTS p ON c.role_id = p.id
                JOIN USER_DETAILS ud ON m.user_id = ud.user_id
                WHERE ud.billing_account_id = ?
                AND m.date >= ? AND m.date < ?
                AND m.type = 'bot'
                GROUP BY p.id, p.name
                ORDER BY tokens DESC
                LIMIT 20
            ''', (current_user.id, current_month_start, next_month_start))
            prompts_rows = await cursor.fetchall()

            by_prompt = []
            for row in prompts_rows:
                tokens = row[3] or 0
                # Estimate cost based on average rate ($15/Mtokens as rough estimate)
                estimated_cost = tokens * 15 / 1_000_000
                by_prompt.append({
                    'prompt_name': row[0],
                    'user_count': row[1],
                    'message_count': row[2],
                    'tokens': tokens,
                    'cost': estimated_cost
                })

            # Get recent activity (last 20 messages)
            await cursor.execute('''
                SELECT u.username, p.name as prompt_name,
                       (m.input_tokens_used + m.output_tokens_used) as tokens,
                       m.date
                FROM MESSAGES m
                JOIN CONVERSATIONS c ON m.conversation_id = c.id
                JOIN PROMPTS p ON c.role_id = p.id
                JOIN USERS u ON m.user_id = u.id
                JOIN USER_DETAILS ud ON m.user_id = ud.user_id
                WHERE ud.billing_account_id = ?
                AND m.type = 'bot'
                ORDER BY m.date DESC
                LIMIT 20
            ''', (current_user.id,))
            activity_rows = await cursor.fetchall()

            recent_activity = []
            for row in activity_rows:
                tokens = row[2] or 0
                # Estimate cost based on average rate
                estimated_cost = tokens * 15 / 1_000_000
                recent_activity.append({
                    'username': row[0],
                    'prompt_name': row[1],
                    'cost': estimated_cost,
                    'timestamp': row[3]
                })

        return JSONResponse(content={
            'summary': {
                'my_balance': my_balance,
                'team_spending_this_month': team_spending_this_month,
                'team_spending_last_month': team_spending_last_month,
                'team_user_count': len(users)
            },
            'users': users,
            'by_prompt': by_prompt,
            'recent_activity': recent_activity
        })

    except Exception as e:
        logger.error(f"Error getting team consumption data: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


# ============================================================================
# Phase 5: White-Label Branding Endpoints
# ============================================================================

@app.get("/my-branding")
async def my_branding_page(request: Request, current_user: User = Depends(get_current_user)):
    """Render the user branding configuration page."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_user and not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only users can access branding settings")

    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("user_branding.html", context)


@app.get("/api/my-branding")
async def get_my_branding_api(request: Request, current_user: User = Depends(get_current_user)):
    """Get user's white-label branding configuration."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Only users can access branding settings"}, status_code=403)

    from common import get_user_branding
    branding = await get_user_branding(current_user.id)

    return JSONResponse(content={"branding": branding})


@app.put("/api/my-branding")
async def update_my_branding(request: Request, current_user: User = Depends(get_current_user)):
    """Update user's white-label branding configuration."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_user and not await current_user.is_admin:
        return JSONResponse(content={"error": "Only users can update branding settings"}, status_code=403)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(content={"error": "Invalid JSON"}, status_code=400)

    # Validate color format (must be hex color)
    def is_valid_hex_color(color):
        if not color:
            return True
        import re
        return bool(re.match(r'^#[0-9A-Fa-f]{6}$', color))

    brand_color_primary = data.get('brand_color_primary', '#6366f1')
    brand_color_secondary = data.get('brand_color_secondary', '#10B981')

    if not is_valid_hex_color(brand_color_primary):
        return JSONResponse(content={"error": "Invalid primary color format. Use hex format: #RRGGBB"}, status_code=400)
    if not is_valid_hex_color(brand_color_secondary):
        return JSONResponse(content={"error": "Invalid secondary color format. Use hex format: #RRGGBB"}, status_code=400)

    # Validate forced_theme if provided
    valid_themes = [
        'default', 'dark', 'light', 'writer', 'terminal', 'coder',
        'katarishoji', 'halloween', 'xmas', 'valentinesday', 'memphis',
        'nekoglass', 'frutigeraero', 'eink'
    ]
    forced_theme = data.get('forced_theme')
    if forced_theme and forced_theme not in valid_themes:
        return JSONResponse(content={"error": f"Invalid theme. Valid themes: {', '.join(valid_themes)}"}, status_code=400)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        # Check if branding record exists
        await cursor.execute('SELECT id FROM USER_BRANDING WHERE user_id = ?', (current_user.id,))
        existing = await cursor.fetchone()

        if existing:
            # Update existing record
            await cursor.execute('''
                UPDATE USER_BRANDING
                SET company_name = ?,
                    logo_url = ?,
                    brand_color_primary = ?,
                    brand_color_secondary = ?,
                    footer_text = ?,
                    email_signature = ?,
                    hide_platform_branding = ?,
                    forced_theme = ?,
                    disable_theme_selector = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
            ''', (
                data.get('company_name'),
                data.get('logo_url'),
                brand_color_primary,
                brand_color_secondary,
                data.get('footer_text'),
                data.get('email_signature'),
                1 if data.get('hide_platform_branding') else 0,
                forced_theme,
                1 if data.get('disable_theme_selector') else 0,
                current_user.id
            ))
        else:
            # Insert new record
            await cursor.execute('''
                INSERT INTO USER_BRANDING
                (user_id, company_name, logo_url, brand_color_primary, brand_color_secondary,
                 footer_text, email_signature, hide_platform_branding, forced_theme, disable_theme_selector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                current_user.id,
                data.get('company_name'),
                data.get('logo_url'),
                brand_color_primary,
                brand_color_secondary,
                data.get('footer_text'),
                data.get('email_signature'),
                1 if data.get('hide_platform_branding') else 0,
                forced_theme,
                1 if data.get('disable_theme_selector') else 0
            ))

        await conn.commit()

    return JSONResponse(content={"success": True, "message": "Branding settings saved successfully"})


@app.get("/api/user/init")
async def get_user_init(request: Request, current_user: User = Depends(get_current_user), context_type: str = None, context_id: str = None):
    """
    Combined endpoint returning session status and theme configuration.
    Reduces HTTP requests by combining /api/check-session and /api/user/theme-config.
    """
    # Session validation logic
    session_data = {"expired": False}

    if current_user is None:
        session_data = {"expired": True, "reason": "unauthenticated"}
        # Return early with default theme for unauthenticated users
        return JSONResponse(content={
            "session": session_data,
            "theme": {
                "forced_theme": None,
                "disable_theme_selector": False,
                "brand_color_primary": '#6366f1',
                "brand_color_secondary": '#10B981'
            }
        })

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        session_data = {"expired": True, "reason": "missing_token"}
        response = JSONResponse(content={
            "session": session_data,
            "theme": {
                "forced_theme": None,
                "disable_theme_selector": False,
                "brand_color_primary": '#6366f1',
                "brand_color_secondary": '#10B981'
            }
        })
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    try:
        payload = decode_jwt_cached(token, SECRET_KEY)
    except JWTError:
        session_data = {"expired": True, "reason": "invalid_token"}
        response = JSONResponse(content={
            "session": session_data,
            "theme": {
                "forced_theme": None,
                "disable_theme_selector": False,
                "brand_color_primary": '#6366f1',
                "brand_color_secondary": '#10B981'
            }
        })
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    exp = payload.get("exp")
    if exp is None:
        session_data = {"expired": True, "reason": "missing_expiration"}
        response = JSONResponse(content={
            "session": session_data,
            "theme": {
                "forced_theme": None,
                "disable_theme_selector": False,
                "brand_color_primary": '#6366f1',
                "brand_color_secondary": '#10B981'
            }
        })
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    expires_in = int(exp) - int(time.time())
    if expires_in <= 0:
        session_data = {"expired": True, "reason": "token_expired"}
        response = JSONResponse(content={
            "session": session_data,
            "theme": {
                "forced_theme": None,
                "disable_theme_selector": False,
                "brand_color_primary": '#6366f1',
                "brand_color_secondary": '#10B981'
            }
        })
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    user_info = payload.get("user_info")
    if not isinstance(user_info, dict):
        session_data = {"expired": True, "reason": "invalid_payload"}
        response = JSONResponse(content={
            "session": session_data,
            "theme": {
                "forced_theme": None,
                "disable_theme_selector": False,
                "brand_color_primary": '#6366f1',
                "brand_color_secondary": '#10B981'
            }
        })
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    used_magic_link = user_info.get("used_magic_link", False)
    magic_link_expires_in = None

    if used_magic_link:
        magic_link_expires_in = max(0, expires_in)

    # Session is valid - build session data
    session_data = {
        "expired": False,
        "expires_in": max(expires_in, 0),
        "magic_link_expires_in": magic_link_expires_in,
        "used_magic_link": used_magic_link
    }

    # Theme configuration logic
    is_user_role = await current_user.is_user
    is_admin = await current_user.is_admin

    if is_user_role or is_admin:
        # Users/admins are never subject to theme enforcement
        theme_data = {
            "forced_theme": None,
            "disable_theme_selector": False,
            "brand_color_primary": '#6366f1',
            "brand_color_secondary": '#10B981'
        }
    else:
        # Regular customers - check context-specific or user-level branding
        from common import get_branding_for_user, get_branding_for_context
        if context_type == "storefront" and context_id:
            branding = await get_branding_for_context({"storefront_slug": context_id})
        else:
            branding = await get_branding_for_user(current_user.id)
        theme_data = {
            "forced_theme": branding.get('forced_theme'),
            "disable_theme_selector": branding.get('disable_theme_selector', False),
            "brand_color_primary": branding.get('brand_color_primary', '#6366f1'),
            "brand_color_secondary": branding.get('brand_color_secondary', '#10B981')
        }

    return JSONResponse(content={
        "session": session_data,
        "theme": theme_data
    })


@app.get("/api/user/theme-config")
async def get_user_theme_config(request: Request, current_user: User = Depends(get_current_user), context_type: str = None, context_id: str = None):
    """Get theme configuration for a user, respecting the creator's forced theme if applicable."""
    if current_user is None:
        # Return defaults for unauthenticated users (login/register pages)
        # Personal theme still comes from localStorage, this just says "no forced theme"
        return JSONResponse(content={
            "forced_theme": None,
            "disable_theme_selector": False,
            "brand_color_primary": '#6366f1',
            "brand_color_secondary": '#10B981'
        })

    # Users and admins are never subject to theme enforcement - they control their own theme
    is_user_role = await current_user.is_user
    is_admin = await current_user.is_admin

    if is_user_role or is_admin:
        # Return no forced theme for users/admins
        return JSONResponse(content={
            "forced_theme": None,
            "disable_theme_selector": False,
            "brand_color_primary": '#6366f1',
            "brand_color_secondary": '#10B981'
        })

    # For regular customers, check context-specific or user-level branding
    from common import get_branding_for_user, get_branding_for_context
    if context_type == "storefront" and context_id:
        branding = await get_branding_for_context({"storefront_slug": context_id})
    else:
        branding = await get_branding_for_user(current_user.id)

    return JSONResponse(content={
        "forced_theme": branding.get('forced_theme'),
        "disable_theme_selector": branding.get('disable_theme_selector', False),
        "brand_color_primary": branding.get('brand_color_primary', '#6366f1'),
        "brand_color_secondary": branding.get('brand_color_secondary', '#10B981')
    })


# =============================================================================
# User Usage Dashboard
# =============================================================================

@app.get("/my-usage", response_class=HTMLResponse)
async def get_my_usage_page(request: Request, current_user: User = Depends(get_current_user)):
    """User's personal usage dashboard."""
    if current_user is None:
        return unauthenticated_response()
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("my_usage.html", context)


@app.get("/api/my-usage")
async def get_my_usage_data(
    request: Request,
    days: int = 30,
    current_user: User = Depends(get_current_user)
):
    """Get user's usage data from USAGE_DAILY table."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        # Get current balance
        cursor = await conn.execute(
            "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,)
        )
        result = await cursor.fetchone()
        balance = float(result[0] or 0) if result else 0

        # Build date filter
        date_filter = ""
        params = [current_user.id]
        if days > 0:
            date_filter = "AND date >= date('now', ?)"
            params.append(f'-{days} days')

        # Get totals for the period
        query = f"""
            SELECT
                COALESCE(SUM(operations), 0) as total_ops,
                COALESCE(SUM(tokens_in), 0) as tokens_in,
                COALESCE(SUM(tokens_out), 0) as tokens_out,
                COALESCE(SUM(total_cost), 0) as total_cost,
                COUNT(DISTINCT date) as active_days
            FROM USAGE_DAILY
            WHERE user_id = ? {date_filter}
        """
        cursor = await conn.execute(query, params)
        result = await cursor.fetchone()

        stats = {
            "total_operations": result[0] or 0,
            "tokens_in": result[1] or 0,
            "tokens_out": result[2] or 0,
            "total_tokens": (result[1] or 0) + (result[2] or 0),
            "total_cost": float(result[3] or 0),
            "avg_daily": float(result[3] or 0) / max(result[4] or 1, 1)
        }

        # Get usage by type
        query = f"""
            SELECT type, SUM(operations) as ops, SUM(total_cost) as cost
            FROM USAGE_DAILY
            WHERE user_id = ? {date_filter}
            GROUP BY type
            ORDER BY cost DESC
        """
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        by_type = [
            {"type": row[0], "operations": row[1], "total_cost": float(row[2] or 0)}
            for row in rows
        ]

        # Get daily breakdown
        query = f"""
            SELECT date, SUM(operations) as ops, SUM(tokens_in) as tin,
                   SUM(tokens_out) as tout, SUM(total_cost) as cost
            FROM USAGE_DAILY
            WHERE user_id = ? {date_filter}
            GROUP BY date
            ORDER BY date DESC
            LIMIT 90
        """
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        daily = [
            {
                "date": row[0],
                "operations": row[1],
                "tokens_in": row[2] or 0,
                "tokens_out": row[3] or 0,
                "total_cost": float(row[4] or 0)
            }
            for row in rows
        ]

        # Storage quota usage (bytes; quota_bytes 0 = unlimited).
        uploads_bytes = await storage_quota.get_uploads_usage_bytes(conn, current_user.id)
        generated_bytes = await storage_quota.get_generated_usage_bytes(conn, current_user.id)
        quota_bytes = await storage_quota.get_effective_quota_bytes(conn, current_user.id)
        storage = {
            "used_bytes": uploads_bytes + generated_bytes,
            "uploads_bytes": uploads_bytes,
            "generated_bytes": generated_bytes,
            "quota_bytes": quota_bytes,
        }

    wellbeing_days = days if days and days > 0 else 3650
    wellbeing = await get_user_wellbeing_summary(current_user.id, wellbeing_days)

    return JSONResponse(content={
        "balance": balance,
        "stats": stats,
        "by_type": by_type,
        "daily": daily,
        "wellbeing": wellbeing,
        "storage": storage
    })


# =============================================================================
# Session Health / Break Reminders
# =============================================================================

@app.get("/admin/session-health", response_class=HTMLResponse)
async def get_admin_session_health_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for continuous-session health and break reminder policy."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("admin_session_health.html", context)


@app.get("/api/admin/session-health/config")
async def get_admin_session_health_config(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)
    return JSONResponse(content={"config": await get_wellbeing_config()})


@app.put("/api/admin/session-health/config")
async def update_admin_session_health_config(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "Invalid payload"}, status_code=400)
    config = await update_wellbeing_config(payload)
    return JSONResponse(content={"success": True, "config": config})


@app.get("/api/admin/session-health/live")
async def get_admin_session_health_live(
    limit: int = 50,
    search: str = None,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)
    return JSONResponse(content={
        "overview": await get_wellbeing_admin_overview(),
        "sessions": await get_admin_live_sessions(limit=limit, search=search),
    })


@app.get("/api/admin/session-health/events")
async def get_admin_session_health_events(
    page: int = 1,
    per_page: int = 50,
    event_type: str = None,
    severity: str = None,
    search: str = None,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)
    return JSONResponse(content=await get_wellbeing_admin_events(
        page=page,
        per_page=per_page,
        event_type=event_type,
        severity=severity,
        search=search,
    ))


@app.get("/api/wellbeing/status")
async def api_wellbeing_status(
    conversation_id: int = None,
    client_active: bool = True,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    return JSONResponse(content=await get_wellbeing_status(
        current_user.id,
        conversation_id,
        allow_reminder=client_active,
    ))


@app.post("/api/wellbeing/activity")
async def api_wellbeing_activity(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "Invalid payload"}, status_code=400)
    conversation_id = payload.get("conversation_id")
    if conversation_id in ("", None):
        conversation_id = None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
    status_payload = await record_wellbeing_activity(
        user_id=current_user.id,
        conversation_id=conversation_id,
        activity_type=str(payload.get("activity_type") or "chat_presence"),
        metadata=metadata,
    )
    return JSONResponse(content=status_payload)


@app.post("/api/wellbeing/events")
async def api_wellbeing_events(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "Invalid payload"}, status_code=400)
    action = str(payload.get("action") or payload.get("event_type") or "")
    try:
        status_payload = await record_wellbeing_user_action(
            user_id=current_user.id,
            action=action,
            session_id=payload.get("session_id"),
            conversation_id=payload.get("conversation_id"),
            severity=payload.get("severity"),
            threshold_key=payload.get("threshold_key"),
            threshold_value=payload.get("threshold_value"),
            observed_value=payload.get("observed_value"),
            snooze_minutes=payload.get("snooze_minutes"),
            pause_minutes=payload.get("pause_minutes"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    return JSONResponse(content=status_payload)


@app.get("/api/wellbeing/preferences")
async def api_get_wellbeing_preferences(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    return JSONResponse(content={
        "preferences": await get_wellbeing_user_preferences(current_user.id),
        "status": await get_wellbeing_status(current_user.id),
    })


@app.put("/api/wellbeing/preferences")
async def api_update_wellbeing_preferences(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    payload = await request.json()
    if not isinstance(payload, dict):
        return JSONResponse(content={"error": "Invalid payload"}, status_code=400)
    preferences = await update_wellbeing_user_preferences(current_user.id, payload)
    return JSONResponse(content={
        "success": True,
        "preferences": preferences,
        "status": await get_wellbeing_status(current_user.id),
    })


@app.post("/api/wellbeing/reset-session")
async def api_reset_wellbeing_session(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()
    payload = await request.json()
    conversation_id = payload.get("conversation_id") if isinstance(payload, dict) else None
    try:
        return JSONResponse(content=await reset_wellbeing_user_session(current_user.id, conversation_id))
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=409)


# =============================================================================
# Admin Usage Dashboard
# =============================================================================

@app.get("/admin/usage", response_class=HTMLResponse)
async def get_admin_usage_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin platform usage dashboard."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("admin_usage.html", context)


@app.get("/api/admin/usage")
async def get_admin_usage_data(
    request: Request,
    days: int = 30,
    type: str = None,
    search: str = None,
    current_user: User = Depends(get_current_user)
):
    """Get platform-wide usage data for admin dashboard."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)

    async with get_db_connection(readonly=True) as conn:
        # Build filters
        filters = []
        params = []

        if days > 0:
            filters.append("ud.date >= date('now', ?)")
            params.append(f'-{days} days')

        if type:
            filters.append("ud.type = ?")
            params.append(type)

        where_clause = "WHERE " + " AND ".join(filters) if filters else ""

        # Get overall stats
        query = f"""
            SELECT
                COUNT(DISTINCT ud.user_id) as active_users,
                COALESCE(SUM(ud.operations), 0) as total_ops,
                COALESCE(SUM(ud.tokens_in + ud.tokens_out), 0) as total_tokens,
                COALESCE(SUM(ud.total_cost), 0) as total_cost
            FROM USAGE_DAILY ud
            {where_clause}
        """
        cursor = await conn.execute(query, params)
        result = await cursor.fetchone()

        stats = {
            "active_users": result[0] or 0,
            "total_operations": result[1] or 0,
            "total_tokens": result[2] or 0,
            "total_cost": float(result[3] or 0)
        }

        # Get usage by type
        query = f"""
            SELECT ud.type, SUM(ud.operations) as ops, SUM(ud.total_cost) as cost
            FROM USAGE_DAILY ud
            {where_clause}
            GROUP BY ud.type
            ORDER BY cost DESC
        """
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        by_type = [
            {"type": row[0], "operations": row[1], "total_cost": float(row[2] or 0)}
            for row in rows
        ]

        # Get daily breakdown
        query = f"""
            SELECT ud.date,
                   COUNT(DISTINCT ud.user_id) as unique_users,
                   SUM(ud.operations) as ops,
                   SUM(ud.tokens_in) as tin,
                   SUM(ud.tokens_out) as tout,
                   SUM(ud.total_cost) as cost
            FROM USAGE_DAILY ud
            {where_clause}
            GROUP BY ud.date
            ORDER BY ud.date DESC
            LIMIT 90
        """
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        daily = [
            {
                "date": row[0],
                "unique_users": row[1],
                "operations": row[2],
                "tokens_in": row[3] or 0,
                "tokens_out": row[4] or 0,
                "total_cost": float(row[5] or 0)
            }
            for row in rows
        ]

        # Get top users (with optional search filter)
        search_filter = ""
        search_params = params.copy()
        if search:
            search_filter = "AND (u.username LIKE ? OR u.email LIKE ?)"
            search_params.extend([f'%{search}%', f'%{search}%'])

        query = f"""
            SELECT
                u.id, u.username, u.email,
                SUM(ud.operations) as ops,
                SUM(ud.tokens_in + ud.tokens_out) as tokens,
                SUM(ud.total_cost) as cost,
                GROUP_CONCAT(DISTINCT ud.type) as types
            FROM USAGE_DAILY ud
            JOIN USERS u ON ud.user_id = u.id
            {where_clause} {search_filter}
            GROUP BY u.id
            ORDER BY cost DESC
            LIMIT 25
        """
        cursor = await conn.execute(query, search_params)
        rows = await cursor.fetchall()
        top_users = [
            {
                "user_id": row[0],
                "username": row[1],
                "email": row[2],
                "operations": row[3],
                "tokens": row[4] or 0,
                "total_cost": float(row[5] or 0),
                "types": row[6].split(',') if row[6] else []
            }
            for row in rows
        ]

    return JSONResponse(content={
        "stats": stats,
        "by_type": by_type,
        "daily": daily,
        "top_users": top_users
    })


@app.get("/api/admin/usage/export")
async def export_admin_usage_csv(
    request: Request,
    days: int = 30,
    type: str = None,
    current_user: User = Depends(get_current_user)
):
    """Export usage data as CSV."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)

    from io import StringIO
    import csv

    async with get_db_connection(readonly=True) as conn:
        filters = []
        params = []

        if days > 0:
            filters.append("ud.date >= date('now', ?)")
            params.append(f'-{days} days')

        if type:
            filters.append("ud.type = ?")
            params.append(type)

        where_clause = "WHERE " + " AND ".join(filters) if filters else ""

        query = f"""
            SELECT ud.date, u.username, ud.type, ud.operations,
                   ud.tokens_in, ud.tokens_out, ud.units, ud.total_cost
            FROM USAGE_DAILY ud
            JOIN USERS u ON ud.user_id = u.id
            {where_clause}
            ORDER BY ud.date DESC, u.username
        """
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Username', 'Type', 'Operations', 'Tokens In', 'Tokens Out', 'Units', 'Cost'])
    for row in rows:
        writer.writerow(row)

    csv_content = output.getvalue()

    from fastapi.responses import Response
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=usage_export_{days}days.csv"}
    )


@app.get("/admin/cache-stats")
async def get_cache_stats(current_user: User = Depends(get_current_user)):
    """Get landing page cache statistics for monitoring."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)

    landing_stats = get_landing_cache_stats()

    pack_total_requests = _pack_landing_cache_stats["hits"] + _pack_landing_cache_stats["misses"]
    pack_hit_rate = _pack_landing_cache_stats["hits"] / max(1, pack_total_requests)

    return JSONResponse(content={
        "landing_cache": {
            "size": landing_stats["size"],
            "max_size": landing_stats["max_size"],
            "hits": landing_stats["hits"],
            "misses": landing_stats["misses"],
            "hit_rate": round(landing_stats["hit_rate"] / 100, 4),
            "hit_rate_percent": f"{landing_stats['hit_rate']:.2f}%"
        },
        "pack_landing_cache": {
            "size": len(_pack_landing_cache),
            "max_size": PACK_LANDING_CACHE_SIZE,
            "hits": _pack_landing_cache_stats["hits"],
            "misses": _pack_landing_cache_stats["misses"],
            "hit_rate": round(pack_hit_rate, 4),
            "hit_rate_percent": f"{pack_hit_rate * 100:.2f}%"
        }
    })


async def get_user_api_keys(user_id: int) -> dict:
    """
    Helper function to get decrypted API keys for a user.
    Used by AI call functions to get user-specific keys.
    """
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT user_api_keys FROM USER_DETAILS WHERE user_id = ?",
                (user_id,)
            )
            result = await cursor.fetchone()

            if result and result[0]:
                keys_json = decrypt_api_key(result[0])
                if keys_json:
                    return orjson.loads(keys_json)

        return {}
    except Exception as e:
        logger.error(f"Error getting user API keys: {e}")
        return {}


def _save_profile_picture_variants(
    content: bytes,
    profile_directory: str,
    user_hash: str,
    alter_ego_suffix: str,
) -> None:
    """Decode, validate, resize, and persist profile pictures off the event loop."""
    with PilImage.open(io.BytesIO(content)) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Image dimensions too large. Maximum is "
                    f"{MAX_IMAGE_PIXELS:,} pixels"
                ),
            )

        image.load()
        os.makedirs(profile_directory, exist_ok=True)
        for size in (32, 64, 128, "fullsize"):
            if size == "fullsize":
                resized_image = image
                filename = f"{user_hash}{alter_ego_suffix}_fullsize.webp"
            else:
                resized_image = resize_image(image, size)
                filename = f"{user_hash}{alter_ego_suffix}_{size}.webp"

            file_path = os.path.join(profile_directory, filename)
            resized_image.save(file_path, "WEBP")


@app.post("/upload-profile-picture")
async def upload_profile_picture(
    file: UploadFile,
    request: Request,
    current_user: User = Depends(get_current_user),
    is_alter_ego: bool = False,
    alter_ego_id: Optional[int] = None
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)

    profile_pictures_directory = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "profile")

    content = await file.read()

    # Security: Check file size limit
    if len(content) > MAX_IMAGE_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"Image too large. Maximum size is {MAX_IMAGE_UPLOAD_SIZE // (1024*1024)}MB")

    # Generate suffix based on alter-ego ID if available
    if is_alter_ego:
        if alter_ego_id is not None:
            alter_ego_suffix = f"_{alter_ego_id:03d}"
        else:
            logger.error(f"alter_ego_id not found")
            raise HTTPException(status_code=500, detail="alter_ego_id not found")
    else:
        alter_ego_suffix = "_000"

    base_url = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/profile/{user_hash}{alter_ego_suffix}"

    try:
        await asyncio.to_thread(
            _save_profile_picture_variants,
            content,
            profile_pictures_directory,
            user_hash,
            alter_ego_suffix,
        )
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="Invalid image file")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving images: {str(e)}")
        raise HTTPException(status_code=500, detail="Error processing image")

    return base_url


# ── Neko Glass Custom Wallpaper Endpoints ────────────────────────────────────

def _save_nekoglass_wallpaper(content: bytes, wallpaper_path: str) -> None:
    """Decode, validate, resize, and persist a wallpaper off the event loop."""
    try:
        with PilImage.open(io.BytesIO(content)) as image:
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Image dimensions too large. Maximum is "
                        f"{MAX_IMAGE_PIXELS:,} pixels"
                    ),
                )

            allowed_formats = {"JPEG", "PNG", "WEBP", "GIF"}
            if image.format not in allowed_formats:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Unsupported image format: {image.format}. "
                        "Allowed: JPEG, PNG, WEBP, GIF"
                    ),
                )

            image.load()
            max_dimension = 3840
            if max(width, height) > max_dimension:
                image.thumbnail(
                    (max_dimension, max_dimension),
                    PilImage.Resampling.LANCZOS,
                )

            output = image if image.mode == "RGB" else image.convert("RGB")
            try:
                os.makedirs(os.path.dirname(wallpaper_path), exist_ok=True)
                output.save(wallpaper_path, "WEBP", quality=85)
            finally:
                if output is not image:
                    output.close()
    except UnidentifiedImageError as exc:
        raise HTTPException(status_code=400, detail="Invalid image file") from exc

@app.post("/api/nekoglass/wallpaper")
async def upload_nekoglass_wallpaper(
    file: UploadFile,
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    content = await file.read()

    # Size check: 5MB max for wallpapers
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Wallpaper image too large. Maximum size is 5MB")

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
    profile_dir = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "profile")
    wallpaper_path = os.path.join(profile_dir, "nekoglass-wallpaper.webp")
    await asyncio.to_thread(_save_nekoglass_wallpaper, content, wallpaper_path)

    return JSONResponse(content={"status": "ok"})


@app.get("/api/nekoglass/wallpaper")
async def get_nekoglass_wallpaper(
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
    wallpaper_path = os.path.join(
        users_directory, hash_prefix1, hash_prefix2, user_hash,
        "profile", "nekoglass-wallpaper.webp"
    )

    if not os.path.isfile(wallpaper_path):
        raise HTTPException(status_code=404, detail="No custom wallpaper")

    return FileResponse(
        wallpaper_path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=86400"}
    )


@app.delete("/api/nekoglass/wallpaper")
async def delete_nekoglass_wallpaper(
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
    wallpaper_path = os.path.join(
        users_directory, hash_prefix1, hash_prefix2, user_hash,
        "profile", "nekoglass-wallpaper.webp"
    )

    if os.path.isfile(wallpaper_path):
        os.remove(wallpaper_path)

    return JSONResponse(content={"status": "ok"})


@app.post("/api/edit-profile")
async def edit_profile(
    request: Request,
    username: str = Form(...),
    phone_number: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    new_password: Optional[str] = Form(None),
    phone_verification_id: Optional[str] = Form(None),
    sample_voice_id: Optional[str] = Form(None),
    user_info: Optional[str] = Form(None),
    profile_picture: Optional[UploadFile] = File(None),
    alter_ego_id: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    user_id = current_user.id
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    phone_number_changed = False
    phone_verification_completed = False

    try:
        async with get_db_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.cursor()

            await cursor.execute(
                """
                SELECT username, phone_number, phone_verified, email, user_info, profile_picture
                FROM USERS
                WHERE id = ?
                """,
                (user_id,),
            )
            current_user_data = await cursor.fetchone()
            if not current_user_data:
                raise HTTPException(status_code=404, detail="User not found.")

            (
                current_username,
                current_phone_number,
                current_phone_verified,
                current_email,
                current_user_info,
                _current_profile_picture,
            ) = current_user_data

            # Username is an immutable account identifier. It is also part of the
            # current storage layout, so accepting even a case-only rename would
            # orphan user assets.
            if username != current_username:
                raise HTTPException(
                    status_code=400,
                    detail="Username cannot be changed after account creation.",
                )

            submitted_phone = current_phone_number
            if phone_number is not None:
                raw_phone = phone_number.strip()
                if raw_phone:
                    submitted_phone = normalize_phone_number(raw_phone)
                else:
                    submitted_phone = None
                phone_number_changed = submitted_phone != current_phone_number

            if phone_number_changed:
                if not has_recent_authentication(current_user):
                    raise HTTPException(
                        status_code=403,
                        detail="Please sign in again before changing your phone number.",
                    )
                if not phone_verification_id:
                    raise HTTPException(
                        status_code=400,
                        detail="Use phone verification to change your phone number.",
                    )
                await cursor.execute(
                    "SELECT id FROM USERS WHERE phone_number = ? AND id != ?",
                    (submitted_phone, user_id),
                )
                if await cursor.fetchone():
                    raise HTTPException(
                        status_code=400,
                        detail="Phone number already in use.",
                    )
                await consume_phone_verification(
                    conn,
                    actor_user_id=user_id,
                    challenge_id=phone_verification_id,
                    phone_number=submitted_phone,
                    purpose=PURPOSE_PROFILE_PHONE_CHANGE,
                )
            elif (
                submitted_phone
                and current_phone_verified != 1
                and phone_verification_id
            ):
                if not has_recent_authentication(current_user):
                    raise HTTPException(
                        status_code=403,
                        detail="Please sign in again before verifying your phone number.",
                    )
                await consume_phone_verification(
                    conn,
                    actor_user_id=user_id,
                    challenge_id=phone_verification_id,
                    phone_number=submitted_phone,
                    purpose=PURPOSE_PROFILE_PHONE_CHANGE,
                )
                phone_verification_completed = True

            submitted_email = email.strip().lower() if email is not None else None
            submitted_email = submitted_email or None
            normalized_current_email = (
                current_email.strip().lower() if current_email else None
            )
            if email is not None and submitted_email != normalized_current_email:
                raise HTTPException(
                    status_code=400,
                    detail="Email changes require a dedicated verification flow.",
                )

            if new_password:
                raise HTTPException(
                    status_code=400,
                    detail="Use the password form to change your password.",
                )

            update_fields = []
            update_values = []

            if phone_number_changed:
                update_fields.extend(
                    [
                        "phone_number = ?",
                        "phone_verified = 1",
                        "session_version = COALESCE(session_version, 1) + 1",
                    ]
                )
                update_values.append(submitted_phone)
            elif phone_verification_completed:
                update_fields.append("phone_verified = 1")

            if user_info is not None and user_info != current_user_info:
                update_fields.append("user_info = ?")
                update_values.append(user_info)

            if profile_picture is not None and profile_picture.filename:
                try:
                    # Upload new image (will overwrite previous if exists)
                    new_profile_picture_url = await upload_profile_picture(
                        profile_picture,
                        request,
                        current_user
                    )

                    update_fields.append("profile_picture = ?")
                    update_values.append(new_profile_picture_url)
                except Exception as e:
                    logger.error(f"Error processing profile image: {str(e)}")
                    raise HTTPException(status_code=500, detail="Error processing profile image")

            if update_fields:
                update_query = f"UPDATE USERS SET {', '.join(update_fields)} WHERE id = ?"
                update_values.append(user_id)
                await cursor.execute(update_query, tuple(update_values))

            if sample_voice_id:
                await cursor.execute("SELECT id FROM VOICES WHERE voice_code = ?", (sample_voice_id,))
                row = await cursor.fetchone()

                if row is not None:
                    voice_id = row[0]
                    await cursor.execute("UPDATE USER_DETAILS SET voice_id = ? WHERE user_id = ?", (voice_id, user_id))
                else:
                    logger.info(f"No voice_id found for voice_code: {sample_voice_id}")

            if alter_ego_id == "" or alter_ego_id == "0":
                await cursor.execute("UPDATE USER_DETAILS SET current_alter_ego_id = NULL WHERE user_id = ?", (user_id,))
            elif alter_ego_id:
                await cursor.execute("UPDATE USER_DETAILS SET current_alter_ego_id = ? WHERE user_id = ?", (alter_ego_id, user_id))

            await conn.commit()

        if is_ajax:
            response = JSONResponse(
                content={
                    "success": True,
                    "message": "Profile updated successfully",
                    "reauthenticate": phone_number_changed,
                },
                status_code=200,
            )
            if phone_number_changed:
                response.delete_cookie(SESSION_COOKIE_NAME)
            return response
        else:
            redirect_url = "/login" if phone_number_changed else "/edit-profile"
            response = RedirectResponse(url=redirect_url, status_code=303)
            if phone_number_changed:
                response.delete_cookie(SESSION_COOKIE_NAME)
            return response

    except PhoneVerificationError as e:
        headers = None
        if isinstance(e, PhoneVerificationRateLimitError):
            headers = {"Retry-After": str(e.retry_after)}
        if is_ajax:
            return JSONResponse(
                content={"success": False, "message": e.detail},
                status_code=e.status_code,
                headers=headers,
            )
        return RedirectResponse(
            url=f"/edit-profile?error={quote(e.detail)}",
            status_code=303,
            headers=headers,
        )

    except HTTPException as e:
        if is_ajax:
            return JSONResponse(content={"success": False, "message": str(e.detail)}, status_code=e.status_code)
        else:
            return RedirectResponse(url=f"/edit-profile?error={str(e.detail)}", status_code=303)

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        if is_ajax:
            return JSONResponse(content={"success": False, "message": "An unexpected error occurred"}, status_code=500)
        else:
            return RedirectResponse(url="/edit-profile?error=An unexpected error occurred", status_code=303)

@app.post("/api/check-username")
async def check_username(request: Request):
    try:
        data = await request.json()
        username = data.get('username')
        current_user = await get_current_user(request)

        if not username:
            return JSONResponse(
                content={"exists": False, "message": "Invalid username"},
                status_code=400
            )

        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            # Verify if the username exists (case insensitive)
            await cursor.execute(
                "SELECT id FROM USERS WHERE LOWER(username) = LOWER(?) AND id != ?",
                (username, current_user.id)
            )
            existing_user = await cursor.fetchone()

            return JSONResponse(content={
                "exists": bool(existing_user),
                "message": "Username already exists" if existing_user else "Username available"
            })

    except Exception as e:
        logger.error(f"Error checking username: {str(e)}")
        return JSONResponse(
            content={"exists": False, "message": "Error checking username"},
            status_code=500
        )

@app.post("/api/delete-profile-picture")
async def delete_profile_picture(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    user_id = current_user.id

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT profile_picture FROM USERS WHERE id = ?", (user_id,))
        current_profile_picture = await cursor.fetchone()

        if current_profile_picture and current_profile_picture[0]:
            profile_picture_url = current_profile_picture[0]
            hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
            profile_pictures_directory = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "profile")

            base_filename = os.path.basename(profile_picture_url)
            file_name_without_extension = os.path.splitext(base_filename)[0]

            files_to_delete = [
                f"{file_name_without_extension}.webp",
                f"{file_name_without_extension}_fullsize.webp",
                f"{file_name_without_extension}_32.webp",
                f"{file_name_without_extension}_64.webp",
                f"{file_name_without_extension}_128.webp"
            ]

            deleted_files = []

            for filename in files_to_delete:
                file_path = os.path.join(profile_pictures_directory, filename)

                file_info = {
                    "path": file_path,
                    "found": os.path.exists(file_path),
                    "deleted": False
                }

                if file_info["found"]:
                    try:
                        os.remove(file_path)
                        file_info["deleted"] = True
                        logger.debug(f"File deleted: {file_path}")
                    except Exception as e:
                        logger.error(f"Error deleting file {file_path}: {str(e)}")
                        file_info["error"] = str(e)
                else:
                    logger.debug(f"File not found: {file_path}")

                deleted_files.append(file_info)

            # Update the database
            await cursor.execute("UPDATE USERS SET profile_picture = NULL WHERE id = ?", (user_id,))
            await conn.commit()

            return JSONResponse(content={
                "success": True,
                "message": "Profile image deleted successfully",
                "deleted_files": deleted_files
            }, status_code=200)
        else:
            return JSONResponse(content={"success": False, "message": "Profile image not found"}, status_code=404)

@app.get("/api/get-alter-egos")
async def get_alter_egos(current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT id, name FROM USER_ALTER_EGOS WHERE user_id = ?", (current_user.id,))
        alter_egos = await cursor.fetchall()

    return JSONResponse(content={"success": True, "alterEgos": [{"id": ae[0], "name": ae[1]} for ae in alter_egos]})

@app.get("/api/get-alter-ego-details/{alter_ego_id}")
async def get_alter_ego_details(alter_ego_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT name, description, profile_picture FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?", (alter_ego_id, current_user.id))
        alter_ego = await cursor.fetchone()

    if alter_ego:
        name, description, profile_picture = alter_ego

        # Generate token URL for alter-ego profile picture only if it exists
        profile_picture_url = None
        if profile_picture:
            current_time = datetime.now(timezone.utc)
            new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
            profile_picture_url = f"{profile_picture}_128.webp"
            token = generate_img_token(profile_picture_url, new_expiration, current_user)
            profile_picture_url = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"

        return JSONResponse(content={
            "success": True,
            "alterEgo": {
                "name": name,
                "description": description,
                "profilePicture": profile_picture_url
            }
        })
    else:
        raise HTTPException(status_code=404, detail="Alter-ego not found")

@app.post("/api/create-alter-ego")
async def create_alter_ego(
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    profile_picture: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    try:
        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            # First create the alter-ego without image and get its ID
            await cursor.execute("""
                INSERT INTO USER_ALTER_EGOS (user_id, name, description)
                VALUES (?, ?, ?)
                RETURNING id
                """,
                (current_user.id, name, description)
            )

            result = await cursor.fetchone()
            if not result:
                raise HTTPException(status_code=500, detail="Failed to create alter-ego")

            alter_ego_id = result[0]

            # If there's an image, process it and update alter-ego
            profile_picture_url = None
            if profile_picture and profile_picture.filename:
                try:
                    profile_picture_url = await upload_profile_picture(
                        profile_picture,
                        request,
                        current_user,
                        is_alter_ego=True,
                        alter_ego_id=alter_ego_id
                    )

                    # Update the alter-ego with the image URL
                    await cursor.execute("""
                        UPDATE USER_ALTER_EGOS
                        SET profile_picture = ?
                        WHERE id = ?
                        """,
                        (profile_picture_url, alter_ego_id)
                    )
                except UnidentifiedImageError:
                    raise HTTPException(status_code=400, detail="Invalid image file")
                except Exception as e:
                    logger.error(f"Error processing alter-ego image: {str(e)}")
                    raise HTTPException(status_code=500, detail="Error processing the image")

            await conn.commit()

            # Prepare the response with the new alter-ego data
            response_data = {
                "success": True,
                "message": "Alter-ego created successfully",
                "alter_ego": {
                    "id": alter_ego_id,
                    "name": name,
                    "description": description,
                    "profile_picture": None
                }
            }

            # If there's an image, generate URL with token for response
            if profile_picture_url:
                current_time = datetime.now(timezone.utc)
                new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
                profile_picture_token_url = f"{profile_picture_url}_128.webp"
                token = generate_img_token(profile_picture_token_url, new_expiration, current_user)
                response_data["alter_ego"]["profile_picture"] = f"{CLOUDFLARE_BASE_URL}{profile_picture_token_url}?token={token}"

            return JSONResponse(content=response_data)

    except HTTPException as e:
        # If something fails after creating the alter-ego but before finishing,
        # attempt rollback and delete the alter-ego
        try:
            if 'alter_ego_id' in locals():
                async with get_db_connection() as conn:
                    cursor = await conn.cursor()
                    await cursor.execute("DELETE FROM USER_ALTER_EGOS WHERE id = ?", (alter_ego_id,))
                    await conn.commit()
        except Exception as cleanup_error:
            logger.error(f"Error during cleanup after failed alter-ego creation: {cleanup_error}")
        raise e

    except Exception as e:
        logger.error(f"Unexpected error creating alter-ego: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred while creating the alter-ego")

@app.put("/api/update-alter-ego/{alter_ego_id}")
async def update_alter_ego(
    alter_ego_id: int,
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    profile_picture: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    try:
        async with get_db_connection() as conn:
            cursor = await conn.cursor()
            print(f"alter_ego_id: {alter_ego_id}")

            # Verify if the alter-ego belongs to the current user
            await cursor.execute(
                "SELECT profile_picture FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?",
                (alter_ego_id, current_user.id)
            )
            current_alter_ego = await cursor.fetchone()

            if not current_alter_ego:
                raise HTTPException(status_code=404, detail="Alter-ego not found")

            update_fields = ["name = ?", "description = ?"]
            update_values = [name, description]

            if profile_picture and profile_picture.filename:
                try:
                    # Upload new image (will overwrite previous if exists)
                    new_profile_picture_url = await upload_profile_picture(
                        profile_picture,
                        request,
                        current_user,
                        is_alter_ego=True,
                        alter_ego_id=alter_ego_id
                    )

                    update_fields.append("profile_picture = ?")
                    update_values.append(new_profile_picture_url)

                except UnidentifiedImageError:
                    raise HTTPException(status_code=400, detail="Invalid image file")
                except Exception as e:
                    logger.error(f"Error processing alter-ego image: {str(e)}")
                    raise HTTPException(status_code=500, detail="Error processing the image")

            # Build and execute update query
            update_query = f"UPDATE USER_ALTER_EGOS SET {', '.join(update_fields)} WHERE id = ? AND user_id = ?"
            update_values.extend([alter_ego_id, current_user.id])

            await cursor.execute(update_query, tuple(update_values))
            rows_affected = cursor.rowcount

            if rows_affected == 0:
                raise HTTPException(status_code=404, detail="Alter-ego not found or not owned by current user")

            await conn.commit()

            # Prepare response with updated information
            await cursor.execute("""
                SELECT name, description, profile_picture
                FROM USER_ALTER_EGOS
                WHERE id = ? AND user_id = ?
            """, (alter_ego_id, current_user.id))

            updated_alter_ego = await cursor.fetchone()

            if updated_alter_ego:
                response_data = {
                    "success": True,
                    "message": "Alter-ego updated successfully",
                    "alter_ego": {
                        "name": updated_alter_ego[0],
                        "description": updated_alter_ego[1],
                        "profile_picture": updated_alter_ego[2]
                    }
                }

                # If there's a profile picture, generate URL with token
                if updated_alter_ego[2]:
                    current_time = datetime.now(timezone.utc)
                    new_expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)
                    profile_picture_url = f"{updated_alter_ego[2]}_128.webp"
                    token = generate_img_token(profile_picture_url, new_expiration, current_user)
                    response_data["alter_ego"]["profile_picture"] = f"{CLOUDFLARE_BASE_URL}{profile_picture_url}?token={token}"

                return JSONResponse(content=response_data)
            else:
                raise HTTPException(status_code=404, detail="Could not retrieve updated alter-ego data")

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating alter-ego: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

@app.delete("/api/delete-alter-ego/{alter_ego_id}")
async def delete_alter_ego(alter_ego_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    try:
        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            # Get alter-ego information and verify it belongs to current user
            await cursor.execute(
                "SELECT profile_picture FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?",
                (alter_ego_id, current_user.id)
            )
            alter_ego = await cursor.fetchone()

            if not alter_ego:
                raise HTTPException(status_code=404, detail="Alter-ego not found")

            # If alter-ego has a profile picture, delete it
            if alter_ego[0]:  # profile_picture
                try:
                    hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
                    profile_pictures_directory = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "profile")

                    # List of files to delete (different sizes)
                    file_patterns = [
                        f"{user_hash}_{alter_ego_id:03d}.webp",
                        f"{user_hash}_{alter_ego_id:03d}_fullsize.webp",
                        f"{user_hash}_{alter_ego_id:03d}_32.webp",
                        f"{user_hash}_{alter_ego_id:03d}_64.webp",
                        f"{user_hash}_{alter_ego_id:03d}_128.webp"
                    ]

                    for filename in file_patterns:
                        file_path = os.path.join(profile_pictures_directory, filename)
                        if os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                logger.debug(f"File deleted: {file_path}")
                            except Exception as e:
                                logger.error(f"Error deleting file {file_path}: {str(e)}")

                except Exception as e:
                    logger.error(f"Error deleting alter-ego images: {str(e)}")

            # Delete the alter-ego from the database
            await cursor.execute("DELETE FROM USER_ALTER_EGOS WHERE id = ?", (alter_ego_id,))

            # If this alter-ego was the current one, clear current_alter_ego_id (NULL = no alter ego)
            await cursor.execute(
                "UPDATE USER_DETAILS SET current_alter_ego_id = NULL WHERE user_id = ? AND current_alter_ego_id = ?",
                (current_user.id, alter_ego_id)
            )

            await conn.commit()

        return JSONResponse(content={
            "success": True,
            "message": "Alter-ego and associated files deleted successfully"
        })

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting alter-ego: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred while deleting the alter-ego")

@app.delete("/api/delete-alter-ego-picture/{alter_ego_id}")
async def delete_alter_ego_picture(alter_ego_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        # Get the image path from the database
        await cursor.execute("SELECT profile_picture FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?", (alter_ego_id, current_user.id))
        result = await cursor.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="Alter-ego not found")

        profile_picture_url = result[0]

        deleted_files = []

        if profile_picture_url:
            # Extract the base filename from the URL
            parsed_url = urlparse(profile_picture_url)
            base_filename = os.path.basename(parsed_url.path)
            file_name_without_extension = os.path.splitext(base_filename)[0]

            hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user.username)
            profile_pictures_directory = os.path.join(users_directory, hash_prefix1, hash_prefix2, user_hash, "profile")

            # List of files to delete
            files_to_delete = [
                f"{file_name_without_extension}.webp",
                f"{file_name_without_extension}_fullsize.webp",
                f"{file_name_without_extension}_32.webp",
                f"{file_name_without_extension}_64.webp",
                f"{file_name_without_extension}_128.webp"
            ]

            for filename in files_to_delete:
                file_path = os.path.join(profile_pictures_directory, filename)

                file_info = {
                    "path": file_path,
                    "found": os.path.exists(file_path),
                    "deleted": False
                }

                if file_info["found"]:
                    try:
                        os.remove(file_path)
                        file_info["deleted"] = True
                        logger.debug(f"File deleted: {file_path}")
                    except Exception as e:
                        logger.error(f"Error deleting file {file_path}: {str(e)}")
                        file_info["error"] = str(e)
                else:
                    logger.debug(f"File not found: {file_path}")

                deleted_files.append(file_info)

        await cursor.execute("UPDATE USER_ALTER_EGOS SET profile_picture = NULL WHERE id = ?", (alter_ego_id,))
        await conn.commit()

        logger.debug(f"Database updated for alter-ego {alter_ego_id}")

    return JSONResponse(content={
        "success": True,
        "message": "Alter-ego profile image deletion process completed",
        "deleted_files": deleted_files
    })

def generate_random_username(length=8):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for i in range(length))

@app.post("/api/check-phone-number")
async def check_phone_number(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    data = await request.json()
    try:
        phone_number = normalize_phone_number(data.get("phone"))
    except PhoneVerificationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT id FROM USERS WHERE phone_number = ? AND id != ?",
            (phone_number, current_user.id),
        )
        existing_user = await cursor.fetchone()

    return JSONResponse(content={"exists": bool(existing_user)}, status_code=200)

async def check_prompts_access(user_id, conn):
    cursor = await conn.cursor()
    await cursor.execute("SELECT all_prompts_access FROM USER_DETAILS WHERE user_id = ?", (user_id,))
    result = await cursor.fetchone()
    if result is not None:
        access = result['all_prompts_access']
        return access == 1
    else:
        return False

async def check_allow_image_generation(user_id: int) -> bool:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT allow_image_generation FROM USER_DETAILS WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return bool(row and row[0])

@app.get("/api/check-session")
async def check_session(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        response = JSONResponse(content={"expired": True, "reason": "unauthenticated"})
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        response = JSONResponse(content={"expired": True, "reason": "missing_token"})
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    try:
        payload = decode_jwt_cached(token, SECRET_KEY)
    except JWTError:
        response = JSONResponse(content={"expired": True, "reason": "invalid_token"})
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    exp = payload.get("exp")
    if exp is None:
        response = JSONResponse(content={"expired": True, "reason": "missing_expiration"})
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    expires_in = int(exp) - int(time.time())
    if expires_in <= 0:
        response = JSONResponse(content={"expired": True, "reason": "token_expired"})
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    user_info = payload.get("user_info")
    if not isinstance(user_info, dict):
        response = JSONResponse(content={"expired": True, "reason": "invalid_payload"})
        response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
        return response

    used_magic_link = user_info.get("used_magic_link", False)
    magic_link_expires_in = None

    if used_magic_link:
        magic_link_expires_in = max(0, expires_in)

    return JSONResponse(content={
        "expired": False,
        "expires_in": max(expires_in, 0),
        "magic_link_expires_in": magic_link_expires_in,
        "used_magic_link": used_magic_link
    })


# =============================================================================
# Sitemap XML — public, no auth required
# =============================================================================

_sitemap_cache: dict = {"xml": None, "generated_at": 0.0, "flags": None}
_SITEMAP_CACHE_TTL = 3600  # 1 hour in seconds


@app.get("/sitemap.xml")
async def sitemap_xml():
    """
    Dynamic sitemap for search-engine crawlers.

    Includes:
      - Static pages (homepage, explore)
      - Public prompt landing pages (not unlisted, with landing page)
      - Public published packs with landing pages
      - Public creator storefronts

    Cached in-memory for 1 hour to avoid DB load on every crawl.
    """
    now = time.time()
    flags = get_marketplace_flags()
    flag_key = (
        flags.public_landings_enabled,
        flags.discovery_enabled,
        flags.storefronts_enabled,
        flags.creator_tools_enabled,
    )

    # Return cached version if still fresh
    if (
        _sitemap_cache["xml"]
        and _sitemap_cache["flags"] == flag_key
        and (now - _sitemap_cache["generated_at"]) < _SITEMAP_CACHE_TTL
    ):
        return Response(
            content=_sitemap_cache["xml"],
            media_type="application/xml",
            headers={"Cache-Control": f"public, max-age={_SITEMAP_CACHE_TTL}"},
        )

    from common import PUBLIC_PROFILE_DOMAIN
    from marketplace.landing.paths import build_prompt_filesystem_path

    domain = PUBLIC_PROFILE_DOMAIN
    protocol = "http" if "localhost" in domain else "https"
    base_url = f"{protocol}://{domain}"

    urls: list[dict] = []

    # -- Static marketing pages --
    urls.append({"loc": f"{base_url}/", "priority": "1.0", "changefreq": "daily"})
    urls.append({"loc": f"{base_url}/for-teams", "priority": "0.8", "changefreq": "monthly"})
    if flags.creator_tools_enabled:
        urls.append({"loc": f"{base_url}/for-creators", "priority": "0.8", "changefreq": "monthly"})
        urls.append({"loc": f"{base_url}/for-agencies", "priority": "0.8", "changefreq": "monthly"})
    if flags.discovery_enabled:
        urls.append({"loc": f"{base_url}/explore-landing.html", "priority": "0.8", "changefreq": "weekly"})

    try:
        async with get_db_connection(readonly=True) as conn:

            if flags.public_landings_enabled:
                # -- Public prompt landing pages --
                # Only trusted, publicly-servable landings belong in the sitemap:
                # public=1, is_unlisted=0, has_landing_page=1, landing_trusted=1,
                # public_id IS NOT NULL -- and only when home.html exists on disk.
                cursor = await conn.execute("""
                    SELECT p.public_id, p.name, p.created_at, p.id, u.username
                    FROM PROMPTS p
                    JOIN USERS u ON p.created_by_user_id = u.id
                    WHERE p.public = 1
                      AND p.is_unlisted = 0
                      AND p.has_landing_page = 1
                      AND p.landing_trusted = 1
                      AND p.public_id IS NOT NULL
                """)
                prompt_rows = await cursor.fetchall()

                for row in prompt_rows:
                    public_id = row[0]
                    name = row[1]
                    created_at = row[2]
                    prompt_id = row[3]
                    username = row[4]

                    # Never advertise a landing whose home.html no longer exists
                    # on disk (the DB flag can outlive the file).
                    landing_dir = build_prompt_filesystem_path(username, prompt_id, name)
                    if not (landing_dir / "home.html").is_file():
                        continue

                    slug = slugify(name) if name else ""

                    # Always use the main domain for the sitemap.
                    # Custom domains should have their own sitemaps or be
                    # configured separately in Google Search Console.
                    loc = f"{base_url}/p/{public_id}/{slug}/"

                    entry = {"loc": loc, "priority": "0.7", "changefreq": "weekly"}
                    if created_at:
                        entry["lastmod"] = str(created_at)[:10]  # YYYY-MM-DD
                    urls.append(entry)

                # -- Public published packs with landing pages --
                cursor = await conn.execute("""
                    SELECT public_id, slug, updated_at
                    FROM PACKS
                    WHERE status = 'published'
                      AND is_public = 1
                      AND public_id IS NOT NULL
                      AND has_custom_landing = 1
                """)
                pack_rows = await cursor.fetchall()

                for row in pack_rows:
                    pack_public_id = row[0]
                    pack_slug = row[1]
                    pack_updated_at = row[2]

                    loc = f"{base_url}/pack/{pack_public_id}/{pack_slug}/"
                    entry = {"loc": loc, "priority": "0.6", "changefreq": "weekly"}
                    if pack_updated_at:
                        entry["lastmod"] = str(pack_updated_at)[:10]
                    urls.append(entry)

            if flags.storefronts_enabled:
                # -- Public creator storefronts --
                cursor = await conn.execute("""
                    SELECT slug, updated_at
                    FROM CREATOR_PROFILES
                    WHERE is_public = 1
                """)
                storefront_rows = await cursor.fetchall()

                for row in storefront_rows:
                    store_slug = row[0]
                    store_updated_at = row[1]

                    loc = f"{base_url}/store/{store_slug}"
                    entry = {"loc": loc, "priority": "0.6", "changefreq": "weekly"}
                    if store_updated_at:
                        entry["lastmod"] = str(store_updated_at)[:10]
                    urls.append(entry)

    except Exception as e:
        logger.error(f"Sitemap generation DB error: {e}")
        # Fail fast: return minimal valid sitemap with just the homepage
        urls = [{"loc": f"{base_url}/", "priority": "1.0", "changefreq": "daily"}]

    # Build XML
    from xml.sax.saxutils import escape as xml_escape

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url_entry in urls:
        xml_parts.append("  <url>")
        xml_parts.append(f"    <loc>{xml_escape(url_entry['loc'])}</loc>")
        if "lastmod" in url_entry:
            xml_parts.append(f"    <lastmod>{xml_escape(url_entry['lastmod'])}</lastmod>")
        if "changefreq" in url_entry:
            xml_parts.append(f"    <changefreq>{url_entry['changefreq']}</changefreq>")
        if "priority" in url_entry:
            xml_parts.append(f"    <priority>{url_entry['priority']}</priority>")
        xml_parts.append("  </url>")
    xml_parts.append("</urlset>")

    xml_content = "\n".join(xml_parts)

    # Cache it
    _sitemap_cache["xml"] = xml_content
    _sitemap_cache["generated_at"] = now
    _sitemap_cache["flags"] = flag_key

    return Response(
        content=xml_content,
        media_type="application/xml",
        headers={"Cache-Control": f"public, max-age={_SITEMAP_CACHE_TTL}"},
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def home(request: Request, current_user: User = Depends(get_current_user)):
    # Handle custom domain landing pages
    if getattr(request.state, 'custom_domain', False):
        return await serve_custom_domain_home(request)

    if current_user is None:
        if not get_marketplace_flags().enabled:
            return RedirectResponse(url="/login", status_code=302)

        # Fallback: serve static landing if request reaches FastAPI without auth
        landing_path = Path("data/index.html")
        if landing_path.is_file():
            return HTMLResponse(content=landing_path.read_text(encoding='utf-8'))
        return RedirectResponse(url="/login", status_code=302)

    return RedirectResponse(url="/home", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return RedirectResponse(url="/login", status_code=302)

    await load_marketplace_config_from_db()
    base_ctx = await get_template_context(request, current_user)
    from request_security import ensure_csrf_token

    base_ctx["admin_csrf_token"] = ensure_csrf_token(request)

    if not base_ctx["is_admin"] and not base_ctx["is_user"]:
        raise HTTPException(status_code=403, detail="Access denied")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        prompts = await get_user_accessible_prompts(current_user, cursor)
        user_balance = await get_balance(current_user.id)

        manageable_prompts = await get_manageable_prompts(current_user.id, base_ctx["is_admin"]) if base_ctx["is_user"] or base_ctx["is_admin"] else []

        base_ctx.update({
            "uses_magic_link": current_user.uses_magic_link,
            "can_change_password": current_user.should_show_change_password(),
            "authentication_mode": current_user.authentication_mode,
            "prompts": prompts,
            "manageable_prompts": manageable_prompts,
            "user_balance": user_balance,
            "captcha_enabled": get_captcha_runtime_status(),
            "subscription_auth_available": _SUBSCRIPTION_AUTH_MODULE_AVAILABLE,
        })
        return templates.TemplateResponse("index.html", base_ctx)


@app.get("/api/get-ip-info")
async def get_ip_info(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    async with httpx.AsyncClient() as client:
        response = await client.get("https://ipinfo.io/json")
        return JSONResponse(content=response.json())

# /login route — consolidated in custom_domain_login() near custom domain routes

@app.route("/magic-link-recovery", methods=["GET", "POST"])
async def magic_link_recovery(request: Request):
    next_url = request.query_params.get("next")

    def _build_recovery_context(message=None, message_type=None, current_next_url=None):
        login_url = "/login"
        if current_next_url:
            login_url += "?" + urlencode({"next": current_next_url})
        return {
            "request": request,
            "message": message,
            "message_type": message_type,
            "next_url": current_next_url or "",
            "login_url": login_url
        }

    if request.method == "POST":
        form = await request.form()
        next_url = form.get("next") or next_url
        email = form.get("email", "").strip().lower()

        # Rate limiting by IP
        rate_error = check_rate_limits(
            request,
            ip_limit=RLC.RECOVERY_BY_IP,
            identifier=email if email else None,
            identifier_limit=RLC.RECOVERY_BY_EMAIL if email else None,
            action_name="recovery"
        )
        if rate_error:
            return templates.TemplateResponse(
                "magic_link_recovery.html",
                _build_recovery_context(rate_error["message"], "danger", next_url)
            )

        if not email:
            return templates.TemplateResponse(
                "magic_link_recovery.html",
                _build_recovery_context("Please enter your email address.", "danger", next_url)
            )

        # Basic email validation
        import re
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            return templates.TemplateResponse(
                "magic_link_recovery.html",
                _build_recovery_context("Please enter a valid email address.", "danger", next_url)
            )

        # Find user by email
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute('SELECT id, username FROM USERS WHERE email = ?', (email,))
            user_result = await cursor.fetchone()

        if not user_result:
            # Don't reveal if email exists or not for security
            return templates.TemplateResponse(
                "magic_link_recovery.html",
                _build_recovery_context(
                    "If your email is registered, you will receive a new magic link shortly.",
                    "success",
                    next_url
                )
            )

        user_id = user_result[0]
        username = user_result[1]

        # A valid recovery token is enough authorization regardless of auth mode.
        user_obj = await get_user_by_id(user_id)
        if not user_obj or not user_obj.is_enabled:
            return templates.TemplateResponse(
                "magic_link_recovery.html",
                _build_recovery_context(
                    "Magic link recovery is not available for this account.",
                    "danger",
                    next_url
                )
            )

        # Generate new magic link
        try:
            magic_link = await generate_magic_link(user_id, 'login', request, next_url=next_url)

            # Get branding for this user (from their creator)
            from common import get_branding_for_user
            branding = await get_branding_for_user(user_id)

            # Send the recovery link without blocking the event loop.
            email_sent = await asyncio.to_thread(
                email_service.send_magic_link_email,
                email,
                magic_link,
                username,
                branding=branding,
            )

            if email_sent:
                message = "If your email is registered, you will receive a new magic link shortly."
                return templates.TemplateResponse(
                    "magic_link_recovery.html",
                    _build_recovery_context(message, "success", next_url)
                )
            else:
                return templates.TemplateResponse(
                    "magic_link_recovery.html",
                    _build_recovery_context(
                        "There was an error sending your magic link. Please try again later.",
                        "danger",
                        next_url
                    )
                )

        except Exception as e:
            logger.error(f"Error generating magic link recovery: {e}")
            return templates.TemplateResponse(
                "magic_link_recovery.html",
                _build_recovery_context("An error occurred. Please try again later.", "danger", next_url)
            )

    # GET request - show the recovery form
    return templates.TemplateResponse("magic_link_recovery.html", _build_recovery_context(current_next_url=next_url))

@app.get("/logout", response_class=HTMLResponse)
async def logout(request: Request, current_user: User = Depends(get_current_user)):
    # A device-code login holds a live child process server-side. Tear down only
    # entries owned by this authenticated user before clearing the signed session;
    # never let a reused browser session act on another user's pending state.
    if current_user is not None:
        try:
            from subscription_auth.linking import discard_pending_for_user

            await discard_pending_for_user(current_user.id)
        except (ImportError, AttributeError):
            pass
        except Exception:
            logger.exception("Could not tear down pending subscription link on logout")
    request.session.clear()
    response = templates.TemplateResponse("login.html", {
        "request": request,
        "message": "You have successfully logged out.",
        "captcha": get_captcha_config(),
        "google_oauth_available": bool(GOOGLE_CLIENT_ID)
    })
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax", secure=SECURE_COOKIES)
    response.delete_cookie(key="_oauth_state", path="/", samesite="lax", secure=SECURE_COOKIES)

    return response

@app.get("/create-user", response_class=HTMLResponse)
async def create_user(request: Request, current_user: User = Depends(get_current_user), selected_prompt_id: int = None, selected_machine: str = None):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin and not await current_user.is_user:
        return handle_error(request, 403, "You do not have permission to access this page.")

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        # Use the function get_user_accessible_prompts
        prompts = await get_user_accessible_prompts(current_user, cursor)

        preserve_ids = [selected_machine] if selected_machine else []
        # New account: it can never have a linked ChatGPT subscription yet, so
        # GPTSub rows are hidden (fail-safe HIDE).
        llm_models_rows = await get_selector_llms(conn, preserve_ids=preserve_ids)
        llm_models = [
            (row["id"], row["machine"], row["model"], row["vision"])
            for row in llm_models_rows
        ]

        # Get categories for category access selection
        await cursor.execute('''
            SELECT id, name, icon, is_age_restricted
            FROM CATEGORIES
            ORDER BY display_order, name
        ''')
        categories = [
            {'id': r[0], 'name': r[1], 'icon': r[2], 'is_age_restricted': bool(r[3])}
            for r in await cursor.fetchall()
        ]

        await conn.close()

    context = await get_template_context(request, current_user)
    context.update({
        "prompts": prompts,
        "llm_models": llm_models,
        "selected_prompt_id": selected_prompt_id,
        "selected_machine": selected_machine,
        "categories": categories
    })
    return templates.TemplateResponse("create_user.html", context)

@app.post("/create-user", response_class=HTMLResponse)
async def create_user_post(
    request: Request,
    current_user: User = Depends(get_current_user),
    prompt_id: int = Form(...),
    all_prompts_access: bool = Form(default=False),
    public_prompts_access: bool = Form(default=False),
    machine: str = Form(...),
    allow_file_upload: bool = Form(default=False),
    allow_image_generation: bool = Form(default=False),
    balance: float = Form(...),
    phone: str = Form(default=None),
    skip_verification: bool = Form(default=False),
    phone_verification_id: str = Form(default=None),
    user_type: str = Form(...),
    username: str = Form(default=None),
    use_random_username: bool = Form(default=False),
    authentication_mode: str = Form(default="magic_link_only"),
    initial_password: str = Form(default=None),
    can_change_password: bool = Form(default=False),
    email: str = Form(default=None),
    api_key_mode: str = Form(default="both_prefer_own"),
    category_ids: List[int] = Form(default=None),
    billing_mode: str = Form(default="customer_pays"),
    billing_limit: Optional[str] = Form(default=None),
    billing_limit_action: str = Form(default="block"),
    billing_auto_refill_amount: Optional[str] = Form(default=None),
    billing_max_limit: Optional[str] = Form(default=None)
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    async with get_db_connection(readonly=True) as conn:
        actor_role = await get_live_user_role(conn, current_user.id)

    if actor_role not in {"admin", "user"}:
        raise HTTPException(status_code=403, detail="You do not have permission to access this page.")

    if skip_verification and actor_role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Only administrators can bypass phone verification.",
        )

    # Validate that users can only create regular customers
    if actor_role == "user" and user_type != "customer":
        raise HTTPException(status_code=403, detail="Users can only create regular customer accounts.")

    # Users cannot give access to all prompts
    if actor_role == "user" and all_prompts_access:
        raise HTTPException(status_code=403, detail="Users cannot give access to all prompts.")

    # Validate that the prompt is accessible to the user
    if actor_role == "user":
        accessible_prompts = await get_user_role_accessible_prompts(current_user.id)
        if prompt_id not in accessible_prompts:
            raise HTTPException(status_code=403, detail="You can only create users with prompts that you have access to.")

    try:
        balance = validate_managed_balance(balance)
    except InvalidInitialBalanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Validate authentication mode
    valid_auth_modes = ["magic_link_only", "magic_link_password", "password_only"]
    if authentication_mode not in valid_auth_modes:
        raise HTTPException(status_code=400, detail="Invalid authentication mode.")

    # Validate password requirements based on authentication mode
    if authentication_mode == "password_only" and (not initial_password or len(initial_password) < 8):
        raise HTTPException(status_code=400, detail="Password is required and must be at least 8 characters for password-only mode.")

    if authentication_mode == "magic_link_password" and initial_password and len(initial_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters when provided.")

    # Only allow can_change_password for password modes
    if can_change_password and authentication_mode == "magic_link_only":
        raise HTTPException(status_code=400, detail="Password change permission only applies to password authentication modes.")

    # Validate API key mode
    from common import VALID_API_KEY_MODES
    if api_key_mode not in VALID_API_KEY_MODES:
        raise HTTPException(status_code=400, detail="Invalid API key mode.")

    async with get_db_connection(readonly=True) as conn:
        # `machine` is the selected LLM id (legacy form field name). Validate it is
        # enabled and, for GPTSub, refuse it: a brand-new account can never have a
        # linked ChatGPT subscription, so a GPTSub default would be inert.
        async with conn.execute(
            """
            SELECT id, machine
            FROM LLM
            WHERE id = ?
              AND COALESCE(enabled, 1) = 1
            """,
            (machine,),
        ) as cursor:
            selected_default_llm = await cursor.fetchone()
            if not selected_default_llm:
                raise HTTPException(status_code=400, detail="Selected LLM is not available.")
            if selected_default_llm["machine"] == "GPTSub":
                raise HTTPException(
                    status_code=400,
                    detail="ChatGPT subscription models cannot be set as the default for a new account.",
                )

    if phone:
        try:
            phone = normalize_phone_number(phone)
        except PhoneVerificationError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
            ) from exc

        async with get_db_connection(readonly=True) as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id FROM USERS WHERE phone_number = ?", (phone,))
                existing_user = await cursor.fetchone()
                if existing_user:
                    raise HTTPException(status_code=400, detail="Phone number already in use. Please use a different number.")

        if not skip_verification and not phone_verification_id:
            raise HTTPException(
                status_code=400,
                detail="Phone verification is required.",
            )

    if use_random_username or not username:
        username = generate_random_username()
        while await username_exists(username):
            username = generate_random_username()
    else:
        # Validate username length
        if len(username) < 3 or len(username) > 20:
            raise HTTPException(status_code=400, detail="The username must be between 3 and 20 characters.")

        # Validate allowed characters
        if not re.match(r'^[a-zA-Z0-9_-]+$', username):
            raise HTTPException(status_code=400, detail="The username can only contain letters, numbers, hyphens, and underscores.")

        # Validate username is not forbidden (security)
        if is_forbidden_username(username):
            raise HTTPException(status_code=400, detail="This username is not available. Please choose a different username.")

        if await username_exists(username):
            raise HTTPException(status_code=400, detail="This username is already in use.")

    # Process category_access for curation mode
    # If public_prompts_access is enabled and specific categories were selected, store them
    # category_ids is None when "All Categories" is checked
    category_access = None
    if public_prompts_access and category_ids is not None and len(category_ids) > 0:
        category_access = orjson.dumps(category_ids).decode('utf-8')

    # Parse optional billing floats (HTML forms send "" for empty inputs)
    billing_limit = parse_optional_float(billing_limit)
    billing_auto_refill_amount = parse_optional_float(billing_auto_refill_amount, default=10.0)
    billing_max_limit = parse_optional_float(billing_max_limit)

    # Process enterprise billing mode
    # billing_mode: "customer_pays" (default) or "user_pays"
    billing_account_id = None
    processed_billing_limit = None
    processed_auto_refill_amount = 10.0
    processed_max_limit = None
    if billing_mode == "user_pays":
        # Only users can set themselves as billing account
        if actor_role in {"user", "admin"}:
            billing_account_id = current_user.id
            processed_billing_limit = billing_limit if billing_limit and billing_limit > 0 else None
            processed_auto_refill_amount = billing_auto_refill_amount if billing_auto_refill_amount and billing_auto_refill_amount > 0 else 10.0
            processed_max_limit = billing_max_limit if billing_max_limit and billing_max_limit > 0 else None
        # Validate billing_limit_action
        if billing_limit_action not in ['block', 'notify', 'auto_refill']:
            billing_limit_action = 'block'
        if (
            billing_limit_action == 'auto_refill'
            and processed_billing_limit is not None
            and processed_max_limit is not None
            and processed_max_limit < processed_billing_limit
        ):
            raise HTTPException(
                status_code=400,
                detail="Maximum billing limit cannot be lower than the billing limit.",
            )

    try:
        user_id = await add_user(
            username,
            prompt_id,
            all_prompts_access,
            public_prompts_access,
            machine,
            allow_file_upload,
            allow_image_generation,
            balance,
            phone,
            role_name=user_type,
            authentication_mode=authentication_mode,
            initial_password=initial_password,
            can_change_password=can_change_password,
            email=email,
            current_user=current_user,
            api_key_mode=api_key_mode,
            category_access=category_access,
            billing_account_id=billing_account_id,
            billing_limit=processed_billing_limit,
            billing_limit_action=billing_limit_action,
            billing_auto_refill_amount=processed_auto_refill_amount,
            billing_max_limit=processed_max_limit,
            initial_balance_funder_id=(
                current_user.id if actor_role == "user" and balance > 0 else None
            ),
            allow_platform_balance_grant=(actor_role == "admin" and balance > 0),
            phone_verification_id=(
                phone_verification_id if phone and not skip_verification else None
            ),
            phone_verification_actor_id=current_user.id,
            allow_unverified_phone=(actor_role == "admin" and skip_verification),
        )
    except InsufficientInitialBalanceError as exc:
        raise HTTPException(
            status_code=402,
            detail="Insufficient balance to fund the customer's initial balance.",
        ) from exc
    except InitialBalanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PhoneVerificationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

    if not user_id:
        raise HTTPException(status_code=500, detail="Failed to create user.")

    # Generate magic link only for modes that support it
    magic_link = None
    if authentication_mode in ["magic_link_only", "magic_link_password"]:
        magic_link = await generate_magic_link(user_id, 'login', request)

    response_data = {
        "status": "success",
        "selected_prompt_id": prompt_id,
        "selected_machine": machine,
        "authentication_mode": authentication_mode
    }

    if magic_link:
        response_data["magic_link"] = magic_link

    return JSONResponse(response_data)

@app.get("/find-user", response_class=HTMLResponse)
async def find_user_redirect(request: Request):
    """Redirect /find-user to /users-list (search is now integrated there)"""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/users-list", status_code=302)

@app.get("/edit-user/{username}", response_class=HTMLResponse)
async def edit_user_form(
    request: Request,
    username: str,
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        username = username.strip()
        await cursor.execute(
            "SELECT id FROM USERS WHERE LOWER(username) = LOWER(?)",
            (username,),
        )
        target_row = await cursor.fetchone()
        if not target_row:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")

        management_access = await get_user_management_access(
            conn,
            current_user.id,
            target_row[0],
        )
        if not management_access.can_manage:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to manage this user.",
            )

        # Get all prompts
        prompts = await get_user_accessible_prompts(current_user, cursor)

        # Get all categories for curation mode
        await cursor.execute("SELECT id, name, icon, is_age_restricted FROM CATEGORIES ORDER BY display_order")
        categories = [{'id': row[0], 'name': row[1], 'icon': row[2], 'is_age_restricted': row[3]} for row in await cursor.fetchall()]

        # Get all user roles (for admin role selector)
        await cursor.execute("SELECT id, role_name FROM USER_ROLES ORDER BY id")
        user_roles = [{'id': row[0], 'name': row[1]} for row in await cursor.fetchall()]

        # Get user data
        await cursor.execute("""
                SELECT u.id, u.username, u.role_id, ud.current_prompt_id, ud.llm_id,
                       ud.allow_file_upload, ud.allow_image_generation, ud.balance,
                       ud.all_prompts_access, ud.public_prompts_access, u.phone_number,
                       ud.can_change_password, u.email, ud.api_key_mode, ud.user_api_keys,
                       ud.category_access, ud.billing_account_id, ud.billing_limit,
                       ud.billing_limit_action, ur.role_name, ud.billing_auto_refill_amount,
                       ud.billing_max_limit, ud.authentication_mode, u.is_enabled, u.auth_provider,
                       ud.storage_quota_bytes
                FROM USERS u
                JOIN USER_DETAILS ud ON u.id = ud.user_id
                JOIN USER_ROLES ur ON u.role_id = ur.id
                WHERE u.id = ?
            """, (target_row[0],))

        user_row = await cursor.fetchone()

        if user_row:
            # GPTSub rows are shared across linked accounts, so filter them by this
            # target user's saved live catalog (not merely a linked boolean).
            from common import get_subscription_auth_enabled
            gptsub_models = []
            try:
                gptsub_feature_enabled = await get_subscription_auth_enabled()
            except Exception:
                gptsub_feature_enabled = False
            if gptsub_feature_enabled:
                try:
                    from subscription_auth.gate import linked_blob_for_user

                    blob = await linked_blob_for_user(user_row[0])
                    saved_models = (
                        blob.get("available_models", [])
                        if blob and blob.get("models_sync_status") == "synced"
                        else []
                    )
                    if isinstance(saved_models, list):
                        gptsub_models = [
                            model for model in saved_models
                            if isinstance(model, str) and model and len(model) <= 128
                        ]
                except Exception:
                    gptsub_models = []
            llm_models_rows = await get_selector_llms(
                conn,
                preserve_ids=[user_row[4]],
                gptsub_models=gptsub_models,
            )
            llm_models = [
                (row["id"], row["machine"], row["model"], row["vision"])
                for row in llm_models_rows
            ]
            user_data = {
                'id': user_row[0],
                'username': user_row[1],
                'role_id': user_row[2],
                'current_prompt_id': user_row[3],
                'llm_id': user_row[4],
                'allow_file_upload': user_row[5],
                'allow_image_generation': user_row[6],
                'balance': user_row[7],
                'all_prompts_access': user_row[8],
                'public_prompts_access': user_row[9],
                'phone_number': user_row[10],
                'can_change_password': user_row[11],
                'email': user_row[12],
                'api_key_mode': user_row[13] or 'both_prefer_own',
                'has_own_api_keys': bool(user_row[14]),
                'category_access': user_row[15],  # JSON string or None
                'billing_account_id': user_row[16],
                'billing_limit': user_row[17],
                'billing_limit_action': user_row[18] or 'block',
                'role_name': user_row[19],
                'billing_auto_refill_amount': user_row[20] or 10.0,
                'billing_max_limit': user_row[21],
                'authentication_mode': user_row[22] or 'magic_link_only',
                'is_enabled': bool(user_row[23]),
                'auth_provider': user_row[24],
                # Per-user storage quota override in bytes (NULL = use system default).
                'storage_quota_bytes': user_row[25]
            }
        else:
            user_data = None

        # System default quota (bytes; 0 = unlimited) for the field's help text.
        storage_quota_default_bytes = await storage_quota.get_default_quota_bytes(conn)

        await conn.close()

    if not user_data:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    context = await get_template_context(request, current_user)
    context.update({
        "prompts": prompts,
        "llm_models": llm_models,
        "user_data": user_data,
        "categories": categories,
        "user_roles": user_roles,
        "storage_quota_default_bytes": storage_quota_default_bytes,
        "error": None
    })
    return templates.TemplateResponse("edit_user.html", context)

@app.post("/edit-user", response_class=HTMLResponse)
async def update_user(
    request: Request,
    current_user: User = Depends(get_current_user),
    username: str = Form(...),
    new_username: str = Form(...),
    phone_number: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    new_password: Optional[str] = Form(None),
    prompt_id: int = Form(...),
    machine: str = Form(...),
    allow_file_upload: bool = Form(False),
    allow_image_generation: bool = Form(False),
    balance: float = Form(...),
    all_prompts_access: bool = Form(False),
    public_prompts_access: bool = Form(False),
    can_change_password: bool = Form(False),
    api_key_mode: Optional[str] = Form(None),
    category_ids: Optional[List[str]] = Form(None),
    allow_all_categories: bool = Form(False),
    billing_mode: str = Form(default="customer_pays"),
    billing_limit: Optional[str] = Form(default=None),
    billing_limit_action: str = Form(default="block"),
    billing_auto_refill_amount: Optional[str] = Form(default=None),
    billing_max_limit: Optional[str] = Form(default=None),
    user_role_id: Optional[str] = Form(default=None),
    authentication_mode: str = Form(default="magic_link_only"),
    storage_quota_gb: Annotated[Optional[str], Form()] = None
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    pending_admin_role_audit = None
    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        # Verify if the user exists
        await cursor.execute(
            """
            SELECT id, role_id, username, phone_number, email, password
            FROM USERS
            WHERE LOWER(username) = LOWER(?)
            """,
            (username,),
        )
        user = await cursor.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        (
            user_id,
            role_id,
            current_username,
            current_phone_number,
            current_email,
            current_password_hash,
        ) = user

        management_access = await get_user_management_access(
            conn,
            current_user.id,
            user_id,
        )
        if not management_access.can_manage:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to manage this user.",
            )

        # Get current balance/default LLM for audit trail and disabled-model preservation
        await cursor.execute(
            """
            SELECT
                balance,
                llm_id,
                all_prompts_access,
                can_change_password,
                authentication_mode,
                billing_account_id,
                billing_limit,
                billing_limit_action,
                billing_auto_refill_amount,
                billing_max_limit,
                storage_quota_bytes
            FROM USER_DETAILS
            WHERE user_id = ?
            """,
            (user_id,),
        )
        balance_row = await cursor.fetchone()
        previous_balance = balance_row[0] if balance_row else 0.0
        current_user_llm_id = balance_row[1] if balance_row else None
        current_all_prompts_access = bool(balance_row[2]) if balance_row else False
        current_can_change_password = bool(balance_row[3]) if balance_row else False
        current_authentication_mode = (
            balance_row[4] if balance_row and balance_row[4] else "magic_link_only"
        )
        current_billing_account_id = balance_row[5] if balance_row else None
        if (
            current_billing_account_id is not None
            and int(current_billing_account_id) == int(user_id)
        ):
            current_billing_account_id = None
        current_billing_configuration = (
            current_billing_account_id,
            float(balance_row[6]) if balance_row and balance_row[6] is not None else None,
            str(balance_row[7] or 'block') if balance_row else 'block',
            float(balance_row[8]) if balance_row and balance_row[8] is not None else 10.0,
            float(balance_row[9]) if balance_row and balance_row[9] is not None else None,
        )

        # Parse optional numeric fields (HTML forms send empty string instead of null)
        billing_limit = parse_optional_float(billing_limit)
        billing_auto_refill_amount = parse_optional_float(billing_auto_refill_amount, default=10.0)
        billing_max_limit = parse_optional_float(billing_max_limit)
        user_role_id = int(user_role_id) if user_role_id and user_role_id.strip() else None

        # Storage quota override (admin-only). Canonical unit is bytes; the form
        # sends binary GB. Absent field (None) = no change; empty string = clear
        # the override (NULL -> system default); 0 = unlimited.
        current_storage_quota_bytes = (
            int(balance_row[10]) if balance_row and balance_row[10] is not None else None
        )
        if storage_quota_gb is None:
            requested_storage_quota_bytes = current_storage_quota_bytes
        elif storage_quota_gb.strip() == "":
            requested_storage_quota_bytes = None
        else:
            try:
                storage_quota_gb_value = float(storage_quota_gb)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Storage quota must be a number of GB.")
            if (
                not math.isfinite(storage_quota_gb_value)
                or storage_quota_gb_value < 0
                or storage_quota_gb_value * (1024 ** 3) > storage_quota.MAX_QUOTA_BYTES
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Storage quota must be a finite non-negative value within the supported range.",
                )
            requested_storage_quota_bytes = int(storage_quota_gb_value * (1024 ** 3))

        try:
            balance = validate_managed_balance(balance)
        except InvalidInitialBalanceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if management_access.is_creator:
            if abs(balance - previous_balance) > 0.001:
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can change account balances.",
                )
            if user_role_id is not None:
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can change account roles.",
                )
            if requested_storage_quota_bytes != current_storage_quota_bytes:
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can change storage quotas.",
                )
            requested_storage_quota_bytes = current_storage_quota_bytes
            if bool(all_prompts_access) != current_all_prompts_access:
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can change full prompt access.",
                )
            balance = float(previous_balance)
            all_prompts_access = current_all_prompts_access

            normalized_submitted_phone = (
                phone_number.strip() if phone_number is not None else None
            )
            normalized_submitted_phone = normalized_submitted_phone or None
            normalized_submitted_email = (
                email.strip().lower() if email is not None else None
            )
            normalized_submitted_email = normalized_submitted_email or None
            normalized_current_email = (
                current_email.strip().lower() if current_email else None
            )
            if (
                phone_number is not None
                and normalized_submitted_phone != current_phone_number
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can change account phone numbers.",
                )
            if (
                email is not None
                and normalized_submitted_email != normalized_current_email
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can change account email addresses.",
                )
            if new_password:
                raise HTTPException(
                    status_code=403,
                    detail="Only administrators can reset account passwords.",
                )

            phone_number = current_phone_number
            email = current_email
            can_change_password = current_can_change_password
            authentication_mode = current_authentication_mode

            accessible_prompts = await get_user_role_accessible_prompts(
                current_user.id
            )
            if prompt_id not in accessible_prompts:
                raise HTTPException(
                    status_code=403,
                    detail="You can only assign prompts that you can access.",
                )

        # Validate authentication mode
        valid_auth_modes = ["magic_link_only", "magic_link_password", "password_only"]
        if authentication_mode not in valid_auth_modes:
            raise HTTPException(status_code=400, detail="Invalid authentication mode.")

        await cursor.execute(
            """
            SELECT id, COALESCE(enabled, 1), machine
            FROM LLM
            WHERE id = ?
            """,
            (machine,),
        )
        selected_llm = await cursor.fetchone()
        if not selected_llm:
            raise HTTPException(status_code=400, detail="Selected LLM does not exist.")
        if not bool(selected_llm[1]) and (
            selected_llm[2] == "GPTSub"
            or int(machine) != int(current_user_llm_id or 0)
        ):
            raise HTTPException(status_code=400, detail="Selected LLM is disabled.")
        if selected_llm[2] == "GPTSub":
            # A personal model is valid only for the target user's exact catalog.
            # Do not preserve an unchanged GPTSub default after unlink.
            await cursor.execute("SELECT model FROM LLM WHERE id = ?", (machine,))
            selected_model_row = await cursor.fetchone()
            selected_model = selected_model_row[0] if selected_model_row else None
            try:
                from common import get_subscription_auth_enabled
                from subscription_auth.gate import linked_blob_for_user

                target_blob = (
                    await linked_blob_for_user(user_id)
                    if await get_subscription_auth_enabled()
                    else None
                )
                available_models = (
                    target_blob.get("available_models", [])
                    if target_blob
                    and target_blob.get("models_sync_status") == "synced"
                    else []
                )
                target_model_allowed = (
                    isinstance(available_models, list)
                    and selected_model in available_models
                )
            except Exception:
                target_model_allowed = False
            if not target_model_allowed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This ChatGPT subscription model is not available to this "
                        "user; reconnect their subscription or choose another model."
                    ),
                )

        if new_username != current_username:
            raise HTTPException(
                status_code=400,
                detail="Username cannot be changed after account creation.",
            )

        if management_access.is_admin:
            if phone_number is None:
                phone_number = current_phone_number
            else:
                phone_number = phone_number.strip() or None
                if phone_number and not phone_number.startswith("+"):
                    phone_number = f"+{phone_number}"

            if email is None:
                email = current_email
            else:
                email = email.strip().lower() or None

        phone_number_changed = phone_number != current_phone_number
        email_changed = email != current_email

        if phone_number_changed and phone_number:
            await cursor.execute(
                "SELECT id FROM USERS WHERE phone_number = ? AND id != ?",
                (phone_number, user_id),
            )
            if await cursor.fetchone():
                raise HTTPException(
                    status_code=400,
                    detail="Phone number already in use.",
                )

        if email:
            email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
            if not re.match(email_pattern, email):
                raise HTTPException(
                    status_code=400,
                    detail="Please enter a valid email address.",
                )

            if email_changed:
                await cursor.execute(
                    "SELECT id FROM USERS WHERE email = ? AND id != ?",
                    (email, user_id),
                )
                if await cursor.fetchone():
                    raise HTTPException(
                        status_code=400,
                        detail="Email address already in use.",
                    )

        if new_password and len(new_password) < 8:
            raise HTTPException(
                status_code=400,
                detail="Password must be at least 8 characters.",
            )
        if authentication_mode == "password_only" and not (
            current_password_hash or new_password
        ):
            raise HTTPException(
                status_code=400,
                detail="Password-only mode requires a password.",
            )
        if (
            authentication_mode in {"magic_link_only", "magic_link_password"}
            and not email
            and (
                email_changed
                or authentication_mode != current_authentication_mode
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="Magic-link authentication requires an email address.",
            )
        if can_change_password and authentication_mode == "magic_link_only":
            raise HTTPException(
                status_code=400,
                detail="Password changes require a password authentication mode.",
            )

        user_update_fields = [
            "phone_number = ?",
            "email = ?",
            "session_version = COALESCE(session_version, 1) + 1",
        ]
        update_params = [phone_number, email]
        if phone_number_changed:
            user_update_fields.append("phone_verified = 0")
        if new_password:
            user_update_fields.append("password = ?")
            update_params.append(hash_password(new_password))

        # Role change - only admins can change roles
        if user_role_id and management_access.is_admin:
            # Validate role_id exists
            await cursor.execute("SELECT id FROM USER_ROLES WHERE id = ?", (user_role_id,))
            if await cursor.fetchone():
                # Prevent admin from demoting themselves
                if user_id != current_user.id or user_role_id == 1:
                    # Changing an admin's role requires Ultra Admin+ elevation
                    is_target_admin = (role_id == 1)
                    is_role_change = (user_role_id != role_id)
                    if is_target_admin and is_role_change:
                        req_ip = get_client_ip(request)
                        if not await is_elevated(current_user.id, request_ip=req_ip):
                            return JSONResponse(content={"success": False, "error": "Ultra Admin+ elevation required to change an admin's role."})
                        await cursor.execute("SELECT role_name FROM USER_ROLES WHERE id = ?", (user_role_id,))
                        new_role_row = await cursor.fetchone()
                        new_role_name = new_role_row[0] if new_role_row else f"role_id={user_role_id}"
                        pending_admin_role_audit = {
                            "admin_id": current_user.id,
                            "action_type": "ultra_admin_changed_admin_role",
                            "request": request,
                            "target_user_id": user_id,
                            "details": (
                                f"Admin '{username}' role changed to "
                                f"'{new_role_name}' by "
                                f"'{current_user.username}' via Ultra Admin+"
                            ),
                        }
                    user_update_fields.append("role_id = ?")
                    update_params.append(user_role_id)

        update_params.append(user_id)

        # Validate API key mode if provided
        if api_key_mode:
            from common import VALID_API_KEY_MODES
            if api_key_mode not in VALID_API_KEY_MODES:
                return JSONResponse(
                    content={'success': False, 'error': 'Invalid API key mode'},
                    status_code=400
                )

        # Update user details
        update_details_query = """
        UPDATE USER_DETAILS SET
            current_prompt_id = ?,
            llm_id = ?,
            allow_file_upload = ?,
            allow_image_generation = ?,
            balance = ?,
            all_prompts_access = ?,
            public_prompts_access = ?,
            can_change_password = ?,
            authentication_mode = ?,
            storage_quota_bytes = ?
        """
        update_details_params = [
            prompt_id, machine, allow_file_upload, allow_image_generation,
            balance, all_prompts_access, public_prompts_access, can_change_password,
            authentication_mode, requested_storage_quota_bytes
        ]

        # Add api_key_mode to update if provided
        if api_key_mode:
            update_details_query += ", api_key_mode = ?"
            update_details_params.append(api_key_mode)

        # Process category_access for curation mode
        if public_prompts_access:
            if allow_all_categories:
                # NULL means all categories
                category_access_value = None
            else:
                # Filter out empty strings and convert to int
                valid_category_ids = [int(cid) for cid in (category_ids or []) if cid and cid.strip()]
                category_access_value = orjson.dumps(valid_category_ids).decode('utf-8') if valid_category_ids else None
        else:
            # No public prompts access means no category filtering needed
            category_access_value = None

        update_details_query += ", category_access = ?"
        update_details_params.append(category_access_value)

        # Process enterprise billing mode
        if billing_mode == "user_pays" and (
            management_access.is_creator or management_access.is_admin
        ) and current_user.id != user_id:
            billing_account_id_value = current_user.id
            billing_limit_value = billing_limit if billing_limit and billing_limit > 0 else None
            billing_action_value = billing_limit_action if billing_limit_action in ['block', 'notify', 'auto_refill'] else 'block'
            billing_auto_refill_value = billing_auto_refill_amount if billing_auto_refill_amount and billing_auto_refill_amount > 0 else 10.0
            billing_max_limit_value = billing_max_limit if billing_max_limit and billing_max_limit > 0 else None
        else:
            billing_account_id_value = None
            billing_limit_value = None
            billing_action_value = 'block'
            billing_auto_refill_value = 10.0
            billing_max_limit_value = None

        if (
            billing_action_value == 'auto_refill'
            and billing_limit_value is not None
            and billing_max_limit_value is not None
            and billing_max_limit_value < billing_limit_value
        ):
            return JSONResponse(
                content={
                    'success': False,
                    'error': 'Maximum billing limit cannot be lower than the billing limit.',
                },
                status_code=400,
            )

        requested_billing_configuration = (
            billing_account_id_value,
            billing_limit_value,
            billing_action_value,
            billing_auto_refill_value,
            billing_max_limit_value,
        )
        if requested_billing_configuration != current_billing_configuration:
            # Reap expired holds before deciding that billing is still busy.
            # The write transaction below then prevents a fresh reservation
            # from appearing between the final check and this configuration
            # update.
            await conn.rollback()
            await reconcile_stale_usage_reservations()
            await conn.execute("BEGIN IMMEDIATE")
            await cursor.execute(
                """
                SELECT 1
                FROM BILLING_USAGE_RESERVATIONS
                WHERE user_id = ? AND status = 'active'
                LIMIT 1
                """,
                (user_id,),
            )
            if await cursor.fetchone():
                await conn.rollback()
                return JSONResponse(
                    content={
                        'success': False,
                        'error': (
                            'Billing settings cannot be changed while this '
                            'account has usage in progress.'
                        ),
                    },
                    status_code=409,
                )
            await cursor.execute(
                "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
                (user_id,),
            )
            refreshed_balance_row = await cursor.fetchone()
            if refreshed_balance_row is not None:
                refreshed_balance = float(refreshed_balance_row[0] or 0.0)
                if abs(balance - previous_balance) <= 0.001:
                    balance = refreshed_balance
                previous_balance = refreshed_balance
                update_details_params[4] = balance

        await cursor.execute(
            f"UPDATE USERS SET {', '.join(user_update_fields)} WHERE id = ?",
            update_params,
        )

        update_details_query += ", billing_account_id = ?, billing_limit = ?, billing_limit_action = ?, billing_auto_refill_amount = ?, billing_max_limit = ?"
        update_details_params.extend([billing_account_id_value, billing_limit_value, billing_action_value, billing_auto_refill_value, billing_max_limit_value])

        update_details_query += " WHERE user_id = ?"
        update_details_params.append(user_id)

        await cursor.execute(update_details_query, update_details_params)

        if authentication_mode == "password_only":
            await cursor.execute("DELETE FROM magic_links WHERE user_id = ?", (user_id,))

        # Record balance change in TRANSACTIONS if balance was modified
        if management_access.is_admin and abs(balance - previous_balance) > 0.001:
            balance_diff = balance - previous_balance
            if balance_diff > 0:
                tx_description = f"Admin balance adjustment: +${balance_diff:.2f} by {current_user.username}"
            else:
                tx_description = f"Admin balance adjustment: -${abs(balance_diff):.2f} by {current_user.username}"

            await cursor.execute('''
                INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after, description, reference_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id,
                'admin_adjustment',
                abs(balance_diff),
                previous_balance,
                balance,
                tx_description,
                f'admin_{current_user.id}_{user_id}_{secrets.token_hex(8)}'
            ))

        await conn.commit()

    if pending_admin_role_audit:
        await log_admin_action(**pending_admin_role_audit)

    return JSONResponse(content={"success": True, "message": "User updated successfully"})

@app.get("/users-list", response_class=HTMLResponse)
async def users_list(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not (await current_user.is_admin or await current_user.is_user):
        raise HTTPException(status_code=403, detail="You do not have permission to access this page.")

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        if await current_user.is_admin:
            await cursor.execute('''
            SELECT
                u.id,
                u.username,
                u.is_enabled,
                ud.tokens_spent,
                ud.total_cost,
                m.token AS magic_link,
                m.expires_at,
                COUNT(c.id) AS conversation_count,
                p.name AS prompt_name,
                ll.model AS llm_model,
                ud.balance,
                u.phone_number,
                ur.role_name,
                u.auth_provider,
                ud.authentication_mode
            FROM USERS u
            JOIN USER_DETAILS ud ON u.id = ud.user_id
            LEFT JOIN (
                SELECT user_id, token, expires_at
                FROM MAGIC_LINKS
                WHERE id IN (SELECT MAX(id) FROM MAGIC_LINKS GROUP BY user_id)
            ) m ON u.id = m.user_id
            LEFT JOIN CONVERSATIONS c
              ON u.id = c.user_id
             AND COALESCE(c.hidden_from_history, 0) = 0
            LEFT JOIN PROMPTS p ON ud.current_prompt_id = p.id
            LEFT JOIN LLM ll ON ud.llm_id = ll.id
            JOIN USER_ROLES ur ON u.role_id = ur.id
            GROUP BY u.id, u.username, u.is_enabled, ud.tokens_spent, ud.total_cost, m.token, m.expires_at, p.name, ll.model, ur.role_name, u.auth_provider, ud.authentication_mode
            ''')
        else:
            await cursor.execute('''
            SELECT
                u.id,
                u.username,
                u.is_enabled,
                ud.tokens_spent,
                ud.total_cost,
                m.token AS magic_link,
                m.expires_at,
                COUNT(c.id) AS conversation_count,
                p.name AS prompt_name,
                ll.model AS llm_model,
                ud.balance,
                u.phone_number,
                ur.role_name,
                u.auth_provider,
                ud.authentication_mode
            FROM USERS u
            JOIN USER_DETAILS ud ON u.id = ud.user_id
            LEFT JOIN (
                SELECT user_id, token, expires_at
                FROM MAGIC_LINKS
                WHERE id IN (SELECT MAX(id) FROM MAGIC_LINKS GROUP BY user_id)
            ) m ON u.id = m.user_id
            LEFT JOIN CONVERSATIONS c
              ON u.id = c.user_id
             AND COALESCE(c.hidden_from_history, 0) = 0
            LEFT JOIN PROMPTS p ON ud.current_prompt_id = p.id
            LEFT JOIN LLM ll ON ud.llm_id = ll.id
            JOIN USER_ROLES ur ON u.role_id = ur.id
            JOIN USER_CREATOR_RELATIONSHIPS ucr ON u.id = ucr.user_id
            WHERE ucr.creator_id = ?
              AND ucr.relationship_type = 'assigned_by'
            GROUP BY u.id, u.username, u.is_enabled, ud.tokens_spent, ud.total_cost, m.token, m.expires_at, p.name, ll.model, ur.role_name, u.auth_provider, ud.authentication_mode
            ''', (current_user.id,))

        url_path = 'login?token='
        base_url = get_auth_base_url(request).rstrip("/")
        users = []
        for row in await cursor.fetchall():
            user_id = row[0]
            username = row[1]
            is_enabled = bool(row[2])
            tokens = row[3]
            total_cost = row[4]
            if row[5] and row[6]:
                magic_link = f'{base_url}/{url_path}{row[5]}'
                try:
                    expires_at = datetime.strptime(row[6], '%Y-%m-%d %H:%M:%S.%f')
                except ValueError:
                    expires_at = datetime.strptime(row[6], '%Y-%m-%d %H:%M:%S')
                is_expired = 'Expired' if expires_at < datetime.now() else 'Active'
            else:
                magic_link = None
                expires_at = None
                auth_mode = row[14]
                is_expired = 'N/A' if auth_mode == 'password_only' else 'No Link'
            conversation_count = row[7]
            balance = row[10]
            phone = row[11]
            role_name = row[12]
            auth_provider = row[13]
            users.append({
                'user_id': user_id,
                'username': username,
                'is_enabled': is_enabled,
                'tokens': tokens,
                'total_cost': total_cost,
                'magic_link': magic_link,
                'is_expired': is_expired,
                'expires_at': expires_at,
                'conversation_count': conversation_count,
                'prompt_name': row[8],
                'llm_model': row[9],
                'balance': balance,
                'phone': phone,
                'role': role_name,
                'auth_provider': auth_provider,
                'authentication_mode': row[14] or 'magic_link_only',
                "is_admin": await current_user.is_admin
            })
        await conn.close()
        sorted_users = sorted(users, key=lambda x: (x['is_expired'] == 'Expired', -x['user_id']))

        # Ultra Admin+ state
        ultra_admin_elevated = False
        ultra_admin_ttl = -1
        if await current_user.is_admin:
            req_ip = get_client_ip(request)
            ultra_admin_elevated = await is_elevated(current_user.id, request_ip=req_ip)
            if ultra_admin_elevated:
                ultra_admin_ttl = await get_elevation_ttl(current_user.id)

        context = await get_template_context(request, current_user)
        context["users"] = sorted_users
        context["ultra_admin_elevated"] = ultra_admin_elevated
        context["ultra_admin_ttl"] = ultra_admin_ttl
        return templates.TemplateResponse("users_list.html", context)

@app.post("/admin/renew-token/{username}")
async def renew_token(request: Request, username: str, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not (await current_user.is_admin or await current_user.is_user):
        return JSONResponse(content={"error": "You do not have permission to access this action."}, status_code=403)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        await cursor.execute("SELECT id, is_enabled FROM USERS WHERE LOWER(username) = LOWER(?)", (username,))
        user = await cursor.fetchone()
        if user:
            user_id = user[0]
            if not bool(user[1]):
                return JSONResponse(content={"error": "This user is disabled. Activate the account before renewing the token."}, status_code=400)

            if not await current_user.is_admin:
                ucr_check = await cursor.execute(
                    "SELECT 1 FROM USER_CREATOR_RELATIONSHIPS WHERE user_id = ? AND creator_id = ? AND relationship_type = 'assigned_by'",
                    (user_id, current_user.id)
                )
                if not await ucr_check.fetchone():
                    return JSONResponse(content={"error": "You do not have permission to renew the token for this user."}, status_code=403)

            new_token = secrets.token_urlsafe(20)
            new_expires_at = datetime.now() + timedelta(days=1)
            try:
                await cursor.execute(
                    """
                    INSERT INTO magic_links (user_id, token, expires_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        token = excluded.token,
                        expires_at = excluded.expires_at
                    """,
                    (user_id, new_token, new_expires_at)
                )
            except sqlite3.OperationalError as e:
                if "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint" not in str(e):
                    raise
                query = """
                UPDATE magic_links
                SET token = ?, expires_at = ?
                WHERE user_id = ?
                """
                await cursor.execute(query, (new_token, new_expires_at, user_id))
                if cursor.rowcount == 0:
                    await cursor.execute(
                        "INSERT INTO magic_links (user_id, token, expires_at) VALUES (?, ?, ?)",
                        (user_id, new_token, new_expires_at)
                    )
            await bump_session_version(conn, user_id)
            await conn.commit()
            url_path = 'login?token='
            full_magic_link = f"{get_auth_base_url(request).rstrip('/')}/{url_path}{new_token}"
            return JSONResponse(content={"magic_link": full_magic_link, "expires_at": new_expires_at.isoformat()}, status_code=200)
        else:
            return JSONResponse(content={"error": "No user found with that username."}, status_code=404)

@app.post("/admin/users/{username}/activation")
async def set_user_activation(request: Request, username: str, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required."}, status_code=403)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    enabled_raw = payload.get("enabled")
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    elif isinstance(enabled_raw, str) and enabled_raw.lower() in {"true", "false", "1", "0"}:
        enabled = enabled_raw.lower() in {"true", "1"}
    else:
        return JSONResponse(content={"error": "Invalid enabled value."}, status_code=400)

    username_ci = username.strip().lower()
    if not username_ci:
        return JSONResponse(content={"error": "Username is required."}, status_code=400)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT u.id, u.username, u.role_id, u.is_enabled, ur.role_name
            FROM USERS u
            JOIN USER_ROLES ur ON u.role_id = ur.id
            WHERE LOWER(u.username) = ?
            """,
            (username_ci,),
        )
        target = await cursor.fetchone()
        if not target:
            return JSONResponse(content={"error": "User not found."}, status_code=404)

        target_user_id = target[0]
        target_username = target[1]
        current_enabled = bool(target[3])
        target_role_name = (target[4] or "").lower()

        if target_user_id == current_user.id and not enabled:
            return JSONResponse(content={"error": "You cannot deactivate your own account."}, status_code=400)

        if target_role_name == "admin" and target_user_id != current_user.id:
            request_ip = get_client_ip(request)
            if not await is_elevated(current_user.id, request_ip=request_ip):
                return JSONResponse(
                    content={"error": "Ultra Admin+ elevation required to change another admin's activation status."},
                    status_code=403,
                )

        if current_enabled != enabled:
            await cursor.execute(
                """
                UPDATE USERS
                SET is_enabled = ?,
                    session_version = COALESCE(session_version, 1) + 1
                WHERE id = ?
                """,
                (1 if enabled else 0, target_user_id),
            )
            await conn.commit()

        if enabled:
            await remove_revoked_user(target_user_id)
            action_type = "admin_activated_user"
            message = f"User {target_username} activated."
        else:
            await add_revoked_user(target_user_id)
            action_type = "admin_deactivated_user"
            message = f"User {target_username} deactivated."

    await log_admin_action(
        admin_id=current_user.id,
        action_type=action_type,
        request=request,
        target_user_id=target_user_id,
        details=f"User '{target_username}' activation set to {enabled} by '{current_user.username}'"
    )

    return JSONResponse(content={
        "success": True,
        "message": message,
        "username": target_username,
        "user_id": target_user_id,
        "is_enabled": enabled,
    })


def _get_rate_limit_reset_warning() -> Optional[str]:
    worker_count = int(os.getenv("UVICORN_WORKERS", "1"))
    if worker_count > 1:
        return (
            f"Rate limits are stored in-memory per worker. UVICORN_WORKERS={worker_count}, "
            "so this reset may be incomplete across workers."
        )
    return None


@app.post("/admin/rate-limits/clear/{username}")
async def clear_user_rate_limits(request: Request, username: str, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    cleared = rate_limiter.clear_for_identifier(username)

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT email FROM USERS WHERE LOWER(username) = LOWER(?)", (username,))
        row = await cursor.fetchone()
        if row and row[0]:
            cleared += rate_limiter.clear_for_identifier(row[0])

    warning = _get_rate_limit_reset_warning()
    logger.info(
        "Admin %s cleared rate limits for %s (%s keys)",
        current_user.username,
        username,
        cleared
    )

    return JSONResponse({
        "cleared": cleared,
        "username": username,
        "warning": warning,
        "worker_count": int(os.getenv("UVICORN_WORKERS", "1"))
    })


@app.post("/admin/rate-limits/clear-ip/{ip:path}")
async def clear_ip_rate_limits(request: Request, ip: str, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    cleared = rate_limiter.clear_for_ip(ip)
    warning = _get_rate_limit_reset_warning()
    logger.info(
        "Admin %s cleared rate limits for IP %s (%s keys)",
        current_user.username,
        ip,
        cleared
    )

    return JSONResponse({
        "cleared": cleared,
        "ip": ip,
        "warning": warning,
        "worker_count": int(os.getenv("UVICORN_WORKERS", "1"))
    })


@app.get("/admin/rate-limits/status/{username}")
async def get_user_rate_limit_status(request: Request, username: str, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")

    limits_config = {
        "id:login": RLC.LOGIN_ACCOUNT_OBSERVATION,
        "id_fail:login": RLC.LOGIN_ACCOUNT_OBSERVATION,
        "pair_fail:login": RLC.LOGIN_BY_ACCOUNT_IP_FAILURES,
        "id:recovery": RLC.RECOVERY_BY_EMAIL,
        "id_fail:recovery": RLC.RECOVERY_BY_EMAIL,
    }
    status_data = rate_limiter.get_status_for_identifier(username, limits_config)

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT email FROM USERS WHERE LOWER(username) = LOWER(?)", (username,))
        row = await cursor.fetchone()
        if row and row[0]:
            status_data.update(rate_limiter.get_status_for_identifier(row[0], limits_config))

    blocked_ips = rate_limiter.get_blocked_ips_for_action(
        "login",
        {
            "ip_all": RLC.LOGIN_BY_IP_ALL,
            "ip_fail": RLC.LOGIN_BY_IP_FAILURES,
        }
    )

    return JSONResponse({
        "username": username,
        "limits": status_data,
        "blocked_login_ips": blocked_ips,
        "warning": _get_rate_limit_reset_warning(),
        "worker_count": int(os.getenv("UVICORN_WORKERS", "1"))
    })

@app.post("/api/refresh-session")
async def refresh_session(request: Request, current_user: User = Depends(get_current_user)):
    """Refresh JWT session token for the current user"""
    if current_user is None:
        raise HTTPException(status_code=401, detail="User not authenticated")

    try:
        # Create new user info with current data
        user_info = await create_user_info(current_user, current_user.used_magic_link)

        expires_delta = None
        if current_user.used_magic_link:
            current_token = request.cookies.get(SESSION_COOKIE_NAME)
            if not current_token:
                raise HTTPException(status_code=401, detail="Session token missing")

            current_payload = decode_jwt_cached(current_token, SECRET_KEY)
            original_exp = current_payload.get("exp")
            if original_exp is None:
                raise HTTPException(status_code=401, detail="Session expired")

            remaining = int(original_exp) - int(time.time())
            if remaining <= 0:
                raise HTTPException(status_code=401, detail="Session expired")

            expires_delta = timedelta(seconds=remaining)

        token = create_access_token(
            data={
                "sub": user_info["username"],
                "user_info": user_info,
                "auth_time": current_user.auth_time,
            },
            expires_delta=expires_delta
        )

        # Set the new token in cookie
        response = JSONResponse(content={
            "success": True,
            "message": "Session refreshed successfully"
        })

        # Configure cookie with the correct expiration time
        if expires_delta is not None:
            max_age = int(expires_delta.total_seconds())
        else:
            max_age = ACCESS_TOKEN_EXPIRE_MINUTES * 60  # convert to seconds
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=max_age,
            httponly=True,
            samesite='lax',
            secure=SECURE_COOKIES
        )

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error refreshing session: {str(e)}")
        raise HTTPException(status_code=500, detail="Error refreshing session")


async def delete_user(username, current_user, request_ip=None):
    """Delete one account, delegating the full cascade to the user_deletion service.

    This endpoint wrapper keeps only the caller-authorization check. The service
    owns the active-usage guard (re-checked inside its write transaction, where
    BEGIN IMMEDIATE serializes with reservation creation), the
    admin/financial/Atagia guards, the external purge, the local cascade, and the
    filesystem cleanup, so the HTTP path and the CLI behave identically.
    """
    username = username.strip()
    username_ci = username.lower()
    logger.debug("Attempting to delete username %s by user %s", username, current_user.username)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT u.id
            FROM users u
            JOIN user_details ud ON u.id = ud.user_id
            WHERE LOWER(u.username) = ?
            """,
            (username_ci,),
        )
        user = await cursor.fetchone()
        if not user:
            logger.warning("Delete attempt failed: User %s not found", username)
            raise HTTPException(status_code=404, detail="User not found")
        user_id = user[0]

        # Non-admins may only delete their own account. Deleting admin accounts is
        # blocked entirely by the service (admin_account guard), so no elevation
        # path is needed here.
        is_current_user_admin = await current_user.is_admin
        is_self_deletion = current_user.username.strip().lower() == username_ci
        if not is_current_user_admin and not is_self_deletion:
            logger.warning(
                "Unauthorized deletion attempt: %s trying to delete %s",
                current_user.username,
                username,
            )
            raise HTTPException(
                status_code=403,
                detail="Unauthorized: You do not have permission to delete this account",
            )

    result = await user_deletion.delete_user_account(user_id=user_id)

    if result.status == "not_found":
        raise HTTPException(status_code=404, detail="User not found")
    if result.status == "blocked":
        raise HTTPException(status_code=409, detail=result.summary)
    if result.status == "atagia_unreachable":
        logger.error("User deletion blocked for %s: Atagia unreachable", username)
        raise HTTPException(
            status_code=503,
            detail=(
                "Account deletion requires the external memory service, which is "
                "currently unreachable. Please retry later."
            ),
        )
    if result.status not in ("completed", "completed_with_cleanup_errors"):
        logger.error("Unexpected deletion status for %s: %s", username, result.status)
        raise HTTPException(status_code=500, detail="Error during user deletion process.")

    if result.status == "completed_with_cleanup_errors":
        logger.error(
            "User %s deleted but some cleanup steps failed: %s",
            username,
            result.cleanup_errors,
        )

    logger.info("Successfully deleted user %s and all associated data", username)
    return {"message": f"User {username} successfully deleted"}

# ── Ultra Admin+ endpoints ──────────────────────────────────────────

@app.post("/api/ultra-admin/request-code")
async def ultra_admin_request_code(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required."}, status_code=403)

    # Check concurrent lock
    lock_owner = await get_active_lock_owner()
    if lock_owner is not None and lock_owner != current_user.id:
        return JSONResponse(content={"error": "Another admin is currently elevated."}, status_code=409)

    # Get admin email
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("SELECT email FROM USERS WHERE id = ?", (current_user.id,))
        row = await cursor.fetchone()

    email = row[0] if row else None
    if not email:
        return JSONResponse(content={"error": "No email configured for this account."}, status_code=400)

    code = await generate_elevation_code(current_user.id)
    if code is None:
        return JSONResponse(content={"error": "Please wait before requesting another code."}, status_code=429)

    # Send code via email
    sent = await asyncio.to_thread(
        email_service.send_ultra_admin_code,
        email,
        code,
        current_user.username,
    )
    if not sent:
        await redis_client.delete(f"ultra_admin:code:{current_user.id}")
        await redis_client.delete(f"ultra_admin:cooldown:{current_user.id}")
        return JSONResponse(content={"error": "Failed to send verification code. Try again."}, status_code=500)

    await log_admin_action(
        admin_id=current_user.id,
        action_type="ultra_admin_code_requested",
        request=request,
        details=f"Elevation code requested, sent to {email[:3]}***"
    )

    # Return email hint (first 3 chars + mask)
    at_idx = email.find('@')
    if at_idx > 3:
        email_hint = email[:3] + '*' * (at_idx - 3) + email[at_idx:]
    elif at_idx > 0:
        email_hint = email[:1] + '*' * (at_idx - 1) + email[at_idx:]
    else:
        email_hint = '***'

    return JSONResponse(content={"status": "sent", "email_hint": email_hint})

@app.post("/api/ultra-admin/verify")
async def ultra_admin_verify(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required."}, status_code=403)

    body = await request.json()
    code = body.get("code", "").strip()

    if not code or len(code) != 6 or not code.isdigit():
        return JSONResponse(content={"error": "Invalid code format."}, status_code=400)

    # Get request IP
    ip_address = get_client_ip(request)

    success, message = await verify_elevation_code(current_user.id, code, ip_address)

    if success:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="ultra_admin_elevated",
            request=request,
            details=f"Ultra Admin+ elevation granted from IP {ip_address}"
        )
        return JSONResponse(content={"status": "elevated", "ttl": ELEVATION_TTL})
    else:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="ultra_admin_verification_failed",
            request=request,
            details=f"Verification failed: {message}"
        )
        error_messages = {
            "no_code": "No verification code found. Please request a new one.",
            "max_attempts": "Too many failed attempts. Please request a new code.",
            "already_elevated": "Another admin is currently elevated. Only one Ultra Admin+ session allowed at a time.",
        }
        # Handle wrong_code:N format
        if message.startswith("wrong_code:"):
            remaining = message.split(":")[1]
            error_msg = f"Incorrect code. {remaining} attempt(s) remaining."
        else:
            error_msg = error_messages.get(message, "Verification failed.")

        status_code = 409 if message == "already_elevated" else 403
        return JSONResponse(content={"error": error_msg, "reason": message}, status_code=status_code)

@app.post("/api/ultra-admin/revoke")
async def ultra_admin_revoke(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required."}, status_code=403)

    await revoke_elevation(current_user.id)

    await log_admin_action(
        admin_id=current_user.id,
        action_type="ultra_admin_revoked",
        request=request,
        details="Ultra Admin+ elevation revoked manually"
    )

    return JSONResponse(content={"status": "revoked"})

@app.get("/api/ultra-admin/status")
async def ultra_admin_status(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"elevated": False, "remaining_seconds": -1})

    ip_address = get_client_ip(request)

    elevated = await is_elevated(current_user.id, request_ip=ip_address)
    ttl = await get_elevation_ttl(current_user.id) if elevated else -1

    return JSONResponse(content={"elevated": elevated, "remaining_seconds": ttl})

# ── End Ultra Admin+ endpoints ──────────────────────────────────────

@app.post("/admin/delete-users")
async def delete_users(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    # Admin-only: deleting accounts is an administrative action. Regular users
    # delete their own account through /api/delete-account.
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="You do not have permission to access this page.")

    form_data = await request.form()
    selected_users = form_data.getlist("selected_users")

    # Extract request IP for downstream auditing.
    request_ip = get_client_ip(request)

    if not selected_users:
        return JSONResponse(content={"error": "No users selected."}, status_code=400)

    outcomes = []
    errors = []
    error_statuses = []
    deleted = []
    for username in selected_users:
        try:
            await delete_user(username, current_user, request_ip=request_ip)
            deleted.append(username)
            outcomes.append({"username": username, "status": "deleted"})
        except HTTPException as e:
            errors.append(f"{username}: {e.detail}")
            error_statuses.append(e.status_code)
            outcomes.append(
                {
                    "username": username,
                    "status": "blocked" if e.status_code == 409 else "failed",
                    "http_status": e.status_code,
                    "detail": e.detail,
                }
            )

    if errors and not deleted:
        unique_statuses = set(error_statuses)
        status_code = error_statuses[0] if len(unique_statuses) == 1 else 400
        return JSONResponse(
            content={"detail": "; ".join(errors), "outcomes": outcomes},
            status_code=status_code,
        )
    elif errors:
        return JSONResponse(
            content={
                "message": f"Deleted {len(deleted)} of {len(selected_users)} user(s).",
                "deleted": deleted,
                "errors": errors,
                "outcomes": outcomes,
            }
        )
    else:
        return JSONResponse(
            content={
                "message": f"Successfully deleted {len(deleted)} user(s).",
                "deleted": deleted,
                "outcomes": outcomes,
            }
        )

@app.post("/api/delete-account")
async def delete_account(request: Request, current_user: User = Depends(get_current_user)):
    if not current_user:
        return unauthenticated_response()

    try:
        await delete_user(current_user.username, current_user, request_ip=None)
        return {
            "success": True,
            "message": "Account deleted successfully",
            "logout": True,
        }
    except HTTPException:
        raise
    except Exception:
        # Do not leak internal error detail to the client; full context is logged.
        logger.exception("Unexpected error deleting account for user %s", current_user.username)
        raise HTTPException(status_code=500, detail="Error during account deletion.")

LLM_FALLBACK_MODEL = "gpt-5-mini"


def _normalize_provider_name(provider: str) -> str:
    provider_key = (provider or "").strip().lower()
    aliases = {
        "gpt": "openai",
        "openai": "openai",
        "claude": "anthropic",
        "anthropic": "anthropic",
        "gemini": "google",
        "google": "google",
        "xai": "x-ai",
        "x-ai": "x-ai",
        "openrouter": "openrouter",
        "minimax": "minimax",
        "kimi": "kimi",
        "moonshot": "kimi",
    }
    return aliases.get(provider_key, provider_key)


def _llm_provider_key(machine: str, model: str) -> str:
    machine_name = (machine or "").strip()
    model_name = (model or "").strip()
    if machine_name.lower() == "openrouter":
        provider = model_name.split("/", 1)[0] if "/" in model_name else "openrouter"
        return _normalize_provider_name(provider)
    return _normalize_provider_name(machine_name)


def _to_cost(value) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _has_same_price(source_llm: Dict, candidate_llm: Dict) -> bool:
    return (
        math.isclose(_to_cost(source_llm["input_token_cost"]), _to_cost(candidate_llm["input_token_cost"]), rel_tol=0.0, abs_tol=1e-12)
        and math.isclose(_to_cost(source_llm["output_token_cost"]), _to_cost(candidate_llm["output_token_cost"]), rel_tol=0.0, abs_tol=1e-12)
    )


def _price_distance(source_llm: Dict, candidate_llm: Dict) -> float:
    return (
        abs(_to_cost(source_llm["input_token_cost"]) - _to_cost(candidate_llm["input_token_cost"]))
        + abs(_to_cost(source_llm["output_token_cost"]) - _to_cost(candidate_llm["output_token_cost"]))
    )


def _select_replacement_llm(source_llm: Dict, llm_catalog: List[Dict], blocked_llm_ids: set[int]) -> Optional[Dict]:
    candidates = [llm for llm in llm_catalog if int(llm["id"]) not in blocked_llm_ids]
    if not candidates:
        return None

    source_provider = _llm_provider_key(source_llm["machine"], source_llm["model"])
    same_provider = [
        llm for llm in candidates
        if _llm_provider_key(llm["machine"], llm["model"]) == source_provider
    ]

    same_provider_same_price = [llm for llm in same_provider if _has_same_price(source_llm, llm)]
    if same_provider_same_price:
        return max(same_provider_same_price, key=lambda llm: int(llm["id"]))

    if same_provider:
        return min(
            same_provider,
            key=lambda llm: (_price_distance(source_llm, llm), -int(llm["id"]))
        )

    return min(
        candidates,
        key=lambda llm: (_price_distance(source_llm, llm), -int(llm["id"]))
    )


def _replace_allowed_llm_ids(allowed_llms_raw: str, old_llm_id: int, new_llm_id: int) -> Optional[str]:
    if not allowed_llms_raw:
        return None

    try:
        allowed_ids = orjson.loads(allowed_llms_raw)
    except Exception:
        return None

    if not isinstance(allowed_ids, list):
        return None

    changed = False
    normalized_ids = []
    for value in allowed_ids:
        try:
            parsed_id = int(value)
        except (TypeError, ValueError):
            continue

        if parsed_id == old_llm_id:
            parsed_id = new_llm_id
            changed = True
        normalized_ids.append(parsed_id)

    if not changed:
        return None

    deduped_ids = []
    seen_ids = set()
    for llm_id in normalized_ids:
        if llm_id in seen_ids:
            continue
        seen_ids.add(llm_id)
        deduped_ids.append(llm_id)

    return orjson.dumps(deduped_ids).decode("utf-8")


async def _apply_llm_reassignment(conn: aiosqlite.Connection, old_llm_id: int, new_llm_id: int) -> Dict[str, int]:
    metrics = {
        "conversations": 0,
        "user_details": 0,
        "forced_prompts": 0,
        "phone_prompts": 0,
        "allowed_prompts": 0,
    }

    if old_llm_id == new_llm_id:
        return metrics

    cursor = await conn.execute(
        "UPDATE CONVERSATIONS SET llm_id = ? WHERE llm_id = ?",
        (new_llm_id, old_llm_id),
    )
    metrics["conversations"] = cursor.rowcount or 0

    cursor = await conn.execute(
        "UPDATE USER_DETAILS SET llm_id = ? WHERE llm_id = ?",
        (new_llm_id, old_llm_id),
    )
    metrics["user_details"] = cursor.rowcount or 0

    cursor = await conn.execute(
        """
        UPDATE PROMPTS
        SET forced_llm_id = ?, forced_reasoning_json = NULL
        WHERE forced_llm_id = ?
        """,
        (new_llm_id, old_llm_id),
    )
    metrics["forced_prompts"] = cursor.rowcount or 0

    cursor = await conn.execute(
        """
        UPDATE PROMPTS
        SET phone_llm_id = ?, phone_reasoning_json = NULL
        WHERE phone_llm_id = ?
        """,
        (new_llm_id, old_llm_id),
    )
    metrics["phone_prompts"] = cursor.rowcount or 0

    async with conn.execute(
        "SELECT id, allowed_llms FROM PROMPTS WHERE allowed_llms IS NOT NULL AND TRIM(allowed_llms) != ''"
    ) as cursor:
        prompt_rows = await cursor.fetchall()

    for row in prompt_rows:
        updated_allowed_llms = _replace_allowed_llm_ids(row["allowed_llms"], old_llm_id, new_llm_id)
        if updated_allowed_llms is None:
            continue
        await conn.execute(
            "UPDATE PROMPTS SET allowed_llms = ? WHERE id = ?",
            (updated_allowed_llms, row["id"]),
        )
        metrics["allowed_prompts"] += 1

    return metrics


async def _reassign_and_delete_llm(
    conn: aiosqlite.Connection,
    llm_id: int,
    blocked_llm_ids: set[int],
) -> Dict:
    async with conn.execute(
        "SELECT id, machine, model, input_token_cost, output_token_cost, vision FROM LLM WHERE id = ?",
        (llm_id,),
    ) as cursor:
        source_row = await cursor.fetchone()

    if not source_row:
        raise HTTPException(status_code=404, detail=f"LLM {llm_id} not found")

    source_llm = dict(source_row)

    async with conn.execute(
        """
        SELECT id, machine, model, input_token_cost, output_token_cost, vision
        FROM LLM
        WHERE machine NOT IN ('GranSabio', 'GPTSub')
          AND COALESCE(enabled, 1) = 1
          AND lower(model) NOT LIKE 'gpt-realtime-2.1%'
        """
    ) as cursor:
        llm_catalog = [dict(row) for row in await cursor.fetchall()]

    replacement = _select_replacement_llm(source_llm, llm_catalog, blocked_llm_ids)
    if replacement is None:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete LLM '{source_llm['model']}' because no replacement model is available."
        )

    reassignment_metrics = await _apply_llm_reassignment(conn, llm_id, int(replacement["id"]))
    await conn.execute("DELETE FROM LLM WHERE id = ?", (llm_id,))

    return {
        "deleted_llm_id": llm_id,
        "deleted_model": source_llm["model"],
        "replacement_llm_id": int(replacement["id"]),
        "replacement_model": replacement["model"],
        "reassigned": reassignment_metrics,
    }


async def _repair_orphan_llm_references(conn: aiosqlite.Connection, fallback_model: str = LLM_FALLBACK_MODEL) -> Dict[str, int]:
    async with conn.execute(
        "SELECT id FROM LLM WHERE model = ? AND machine NOT IN ('GPTSub', 'GranSabio') ORDER BY id DESC LIMIT 1",
        (fallback_model,),
    ) as cursor:
        fallback_row = await cursor.fetchone()

    if not fallback_row:
        async with conn.execute(
            "SELECT id FROM LLM WHERE machine NOT IN ('GPTSub', 'GranSabio') ORDER BY id DESC LIMIT 1"
        ) as cursor:
            fallback_row = await cursor.fetchone()
        if not fallback_row:
            raise HTTPException(status_code=500, detail="No LLMs available for orphan reassignment")

    fallback_llm_id = int(fallback_row["id"])

    metrics = {
        "fallback_llm_id": fallback_llm_id,
        "conversations": 0,
        "user_details": 0,
        "forced_prompts": 0,
        "phone_prompts": 0,
    }

    cursor = await conn.execute(
        """
        UPDATE CONVERSATIONS
        SET llm_id = ?
        WHERE llm_id IS NULL OR llm_id NOT IN (SELECT id FROM LLM)
        """,
        (fallback_llm_id,),
    )
    metrics["conversations"] = cursor.rowcount or 0

    cursor = await conn.execute(
        """
        UPDATE USER_DETAILS
        SET llm_id = ?
        WHERE llm_id IS NULL OR llm_id NOT IN (SELECT id FROM LLM)
        """,
        (fallback_llm_id,),
    )
    metrics["user_details"] = cursor.rowcount or 0

    cursor = await conn.execute(
        """
        UPDATE PROMPTS
        SET forced_llm_id = ?, forced_reasoning_json = NULL
        WHERE forced_llm_id IS NOT NULL
          AND forced_llm_id NOT IN (SELECT id FROM LLM)
        """,
        (fallback_llm_id,),
    )
    metrics["forced_prompts"] = cursor.rowcount or 0

    cursor = await conn.execute(
        """
        UPDATE PROMPTS
        SET phone_llm_id = NULL, phone_reasoning_json = NULL
        WHERE phone_llm_id IS NOT NULL
          AND phone_llm_id NOT IN (SELECT id FROM LLM)
        """
    )
    metrics["phone_prompts"] = cursor.rowcount or 0

    return metrics


@app.get("/admin/llms", response_class=HTMLResponse)
async def llm_list(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    async with get_db_connection(readonly=True) as conn:
        llms = await get_llm_catalog(conn)
        providers = sorted(set(llm["machine"] for llm in llms))
        context = await get_template_context(request, current_user)
        context.update({"llms": llms, "providers": providers})
        return templates.TemplateResponse("llms/llm_list.html", context)


@app.get("/api/llms")
async def api_llms_list(
    preserve_ids: str | None = Query(None),
    include_current_llm_id: int | None = Query(None),
    include_realtime: bool = Query(False),
    current_user: User = Depends(get_current_user),
):
    """Return list of LLMs as JSON for frontend selects"""
    if current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    preserved: set[int] = set()
    if include_current_llm_id:
        preserved.add(int(include_current_llm_id))
    if preserve_ids:
        for value in preserve_ids.split(","):
            try:
                if value.strip():
                    preserved.add(int(value.strip()))
            except ValueError:
                continue

    async with get_db_connection(readonly=True) as conn:
        # Shared GPTSub rows are visible only when their model id occurs in this
        # user's saved live catalog. Fail-safe HIDE on every read/decrypt error.
        from common import get_subscription_auth_enabled
        gptsub_models = []
        gptsub_reasoning = {}
        try:
            gptsub_feature_enabled = await get_subscription_auth_enabled()
        except Exception:
            gptsub_feature_enabled = False
        if gptsub_feature_enabled:
            try:
                from subscription_auth.gate import linked_blob_for_user

                blob = await linked_blob_for_user(current_user.id)
                saved_models = (
                    blob.get("available_models", [])
                    if blob and blob.get("models_sync_status") == "synced"
                    else []
                )
                if isinstance(saved_models, list):
                    gptsub_models = [
                        model for model in saved_models
                        if isinstance(model, str) and model and len(model) <= 128
                    ]
                saved_reasoning = blob.get("model_reasoning", {}) if blob else {}
                if isinstance(saved_reasoning, dict):
                    gptsub_reasoning = {
                        model: value
                        for model, value in saved_reasoning.items()
                        if model in gptsub_models and isinstance(value, dict)
                    }
            except Exception:
                gptsub_models = []
                gptsub_reasoning = {}
        rows = await get_selector_llms(
            conn,
            preserve_ids=preserved,
            include_realtime=include_realtime,
            gptsub_models=gptsub_models,
            gptsub_reasoning=gptsub_reasoning,
        )

    return [
        {
            "id": row["id"],
            "machine": row["machine"],
            "model": row["model"],
            "display_name": row["display_name"] or row["model"],
            "vision": bool(row["vision"]),
            "enabled": bool(row["enabled"]),
            "capabilities": row["capabilities"],
        }
        for row in rows
    ]


@app.get("/admin/llm/new", response_class=HTMLResponse)
async def create_llm(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("llms/create_llm.html", context)

@app.post("/admin/llm/new")
async def create_llm_post(
    request: Request,
    current_user: User = Depends(get_current_user),
    machine: str = Form(...),
    model: str = Form(...),
    input_token_cost: float = Form(...),
    output_token_cost: float = Form(...),
    vision: bool = Form(False)
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    if machine == 'GranSabio' and model == 'gransabio-pipeline':
        raise HTTPException(status_code=403, detail="This machine/model combination is reserved for the system.")
    if machine.strip().casefold() == "gptsub":
        raise HTTPException(
            status_code=403,
            detail="GPTSub rows are synchronized from linked user accounts and cannot be created manually.",
        )

    metadata = build_manual_insert_metadata(machine, model, vision)
    async with get_db_connection() as conn:
        await conn.execute(
            """
            INSERT INTO LLM (
                machine, model, input_token_cost, output_token_cost, vision,
                provider_key, provider_model_id, display_name, enabled,
                sync_source, sync_status, raw_metadata_json,
                capabilities_json, manual_overrides_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                machine,
                model,
                input_token_cost,
                output_token_cost,
                vision,
                metadata["provider_key"],
                metadata["provider_model_id"],
                metadata["display_name"],
                metadata["enabled"],
                metadata["sync_source"],
                metadata["sync_status"],
                metadata["raw_metadata_json"],
                metadata["capabilities_json"],
                metadata["manual_overrides_json"],
            ),
        )
        await conn.commit()

    return RedirectResponse(url="/admin/llms", status_code=303)

@app.get("/admin/llm/edit/{llm_id}", response_class=HTMLResponse)
async def edit_llm(request: Request, llm_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT machine, model, input_token_cost, output_token_cost, vision,
                   provider_key, provider_model_id, display_name, sync_source,
                   manual_overrides_json
            FROM LLM
            WHERE id = ?
            """,
            (llm_id,),
        )
        llm = await cursor.fetchone()
        await conn.close()

        if not llm:
            raise HTTPException(status_code=404, detail="LLM not found")

        # Block editing the synthetic GranSabio LLM
        if llm[0] == 'GranSabio' and llm[1] == 'gransabio-pipeline':
            raise HTTPException(status_code=403, detail="System LLM cannot be edited.")
        if llm[0] == "GPTSub":
            raise HTTPException(
                status_code=403,
                detail="Synchronized GPTSub rows cannot be edited manually.",
            )

        context = await get_template_context(request, current_user)
        context.update({
            "llm_id": llm_id,
            "llm_machine": llm[0],
            "llm_model": llm[1],
            "llm_input_cost": llm[2],
            "llm_output_cost": llm[3],
            "llm_vision": llm[4],
            "llm_provider_key": llm[5],
            "llm_provider_model_id": llm[6],
            "llm_display_name": llm[7] or llm[1],
            "llm_sync_source": llm[8] or "manual",
            "llm_sync_managed": is_sync_managed({
                "machine": llm[0],
                "provider_key": llm[5],
                "sync_source": llm[8],
            }),
        })
        return templates.TemplateResponse("llms/edit_llm.html", context)

@app.post("/admin/llm/update/{llm_id}")
async def update_llm(
    request: Request,
    llm_id: int,
    current_user: User = Depends(get_current_user),
    machine: str = Form(...),
    model: str = Form(...),
    input_token_cost: float = Form(...),
    output_token_cost: float = Form(...),
    vision: bool = Form(False),
    display_name: str | None = Form(None),
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    # Prevent modification of synthetic GranSabio LLM row
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            """
            SELECT machine, model, provider_key, provider_model_id, sync_source, manual_overrides_json
            FROM LLM
            WHERE id = ?
            """,
            (llm_id,),
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] == 'GranSabio' and row[1] == 'gransabio-pipeline':
            raise HTTPException(status_code=403, detail="System LLM cannot be modified.")
        if row and row[0] == "GPTSub":
            raise HTTPException(
                status_code=403,
                detail="Synchronized GPTSub rows cannot be modified manually.",
            )
    # Prevent renaming another LLM into the reserved pair
    if machine == 'GranSabio' and model == 'gransabio-pipeline':
        raise HTTPException(status_code=403, detail="This machine/model combination is reserved for the system.")
    if machine.strip().casefold() == "gptsub":
        raise HTTPException(
            status_code=403,
            detail="The GPTSub machine is reserved for account catalog synchronization.",
        )

    if not row:
        raise HTTPException(status_code=404, detail="LLM not found")

    sync_managed = is_sync_managed({
        "machine": row[0],
        "provider_key": row[2],
        "sync_source": row[4],
    })
    display_name_value = (display_name or model).strip()

    async with get_db_connection() as conn:
        if sync_managed:
            overrides_json = merge_manual_overrides(
                row[5],
                ["display_name", "input_token_cost", "output_token_cost", "vision"],
            )
            await conn.execute(
                """
                UPDATE LLM
                SET display_name = ?,
                    input_token_cost = ?,
                    output_token_cost = ?,
                    vision = ?,
                    manual_overrides_json = ?
                WHERE id = ?
                """,
                (display_name_value or row[1], input_token_cost, output_token_cost, vision, overrides_json, llm_id),
            )
        else:
            provider_key = normalize_provider_key(machine)
            await conn.execute(
                """
                UPDATE LLM
                SET machine = ?,
                    model = ?,
                    input_token_cost = ?,
                    output_token_cost = ?,
                    vision = ?,
                    provider_key = ?,
                    provider_model_id = ?,
                    display_name = ?
                WHERE id = ?
                """,
                (
                    machine,
                    model,
                    input_token_cost,
                    output_token_cost,
                    vision,
                    provider_key,
                    model,
                    display_name_value or model,
                    llm_id,
                ),
            )
        await conn.commit()
        return RedirectResponse(url="/admin/llms", status_code=303)

@app.delete("/admin/llm/delete/{llm_id}")
async def delete_llm(llm_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    # Prevent deletion of synthetic GranSabio LLM row
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute("SELECT machine, model FROM LLM WHERE id = ?", (llm_id,)) as cur:
            row = await cur.fetchone()
        if row and row[0] == 'GranSabio' and row[1] == 'gransabio-pipeline':
            raise HTTPException(status_code=403, detail="System LLM cannot be deleted.")
        if row and row[0] == "GPTSub":
            raise HTTPException(
                status_code=403,
                detail="Synchronized GPTSub rows cannot be deleted manually.",
            )

    async with get_db_connection() as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            result = await _reassign_and_delete_llm(conn, llm_id, blocked_llm_ids={llm_id})
            orphan_fix_metrics = await _repair_orphan_llm_references(conn, fallback_model=LLM_FALLBACK_MODEL)
            await conn.commit()
            return JSONResponse(content={"success": True, **result, "orphan_fix": orphan_fix_metrics}, status_code=200)
        except HTTPException:
            await conn.rollback()
            raise
        except Exception as e:
            await conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/llm/bulk-delete")
async def bulk_delete_llms(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    body = await request.json()
    llm_ids = body.get("llm_ids", [])

    if not llm_ids or not isinstance(llm_ids, list):
        raise HTTPException(status_code=400, detail="No LLM IDs provided")

    sanitized_ids = []
    for llm_id in llm_ids:
        try:
            sanitized_ids.append(int(llm_id))
        except (TypeError, ValueError):
            continue

    sanitized_ids = list(dict.fromkeys(sanitized_ids))
    if not sanitized_ids:
        raise HTTPException(status_code=400, detail="No valid LLM IDs provided")

    placeholders = ",".join("?" for _ in sanitized_ids)
    async with get_db_connection() as conn:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            async with conn.execute(
                f"SELECT id FROM LLM WHERE id IN ({placeholders}) AND machine NOT IN ('OpenRouter', 'GranSabio', 'GPTSub')",
                sanitized_ids
            ) as cursor:
                target_rows = await cursor.fetchall()

            target_ids = [int(row["id"]) for row in target_rows]
            blocked_ids = set(target_ids)
            results = []

            for target_id in target_ids:
                result = await _reassign_and_delete_llm(conn, target_id, blocked_llm_ids=blocked_ids)
                results.append(result)

            deleted = len(results)
            reassigned_conversations = sum(item["reassigned"]["conversations"] for item in results)
            reassigned_user_details = sum(item["reassigned"]["user_details"] for item in results)

            if target_ids and deleted != len(target_ids):
                raise HTTPException(status_code=500, detail="Some LLMs could not be deleted")

            if target_ids:
                logger.info(
                    "Bulk LLM delete: deleted=%s, reassigned_conversations=%s, reassigned_user_details=%s",
                    deleted,
                    reassigned_conversations,
                    reassigned_user_details,
                )

            orphan_fix_metrics = await _repair_orphan_llm_references(conn, fallback_model=LLM_FALLBACK_MODEL)
            await conn.commit()
            return JSONResponse(
                content={
                    "success": True,
                    "deleted": deleted,
                    "details": results,
                    "orphan_fix": orphan_fix_metrics,
                },
                status_code=200,
            )
        except HTTPException:
            await conn.rollback()
            raise
        except Exception as e:
            await conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Atagia Admin
# ---------------------------------------------------------------------------

_ATAGIA_WORKER_CONTROL_ACTIONS = {
    "pause_new_jobs",
    "drain_and_pause",
    "hard_pause",
    "resume_processing",
    "active",
}


def _normalize_atagia_admin_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    data = jsonable_encoder(value)
    if isinstance(data, dict):
        return data
    return {"value": data}


def _format_atagia_bridge_error(error: Any) -> dict[str, Any] | None:
    if error is None:
        return None
    data = _normalize_atagia_admin_payload(error)
    if data:
        return data
    return {"message": str(error)}


async def _read_atagia_json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError("Invalid JSON payload.") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid JSON payload.")
    return data


@app.get("/admin/atagia")
async def admin_atagia_get(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return RedirectResponse(url="/")
    from atagia_config import get_atagia_config, template_config

    context = await get_template_context(request, current_user)
    context["config"] = template_config(await get_atagia_config())
    return templates.TemplateResponse("admin_atagia.html", context)


@app.post("/admin/atagia")
async def admin_atagia_post(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_bridge import reset_atagia_bridge
    from atagia_config import get_atagia_config, save_atagia_admin_config, template_config

    try:
        data = await _read_atagia_json_body(request)
        await save_atagia_admin_config(data)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("Failed to save Atagia configuration: %s", exc, exc_info=True)
        return JSONResponse(
            content={"success": False, "message": "Failed to save Atagia configuration."},
            status_code=500,
        )

    await reset_atagia_bridge()
    return JSONResponse(
        content={
            "success": True,
            "message": "Atagia configuration saved.",
            "config": template_config(await get_atagia_config()),
        }
    )


@app.post("/admin/atagia/defaults")
async def admin_atagia_defaults(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_bridge import reset_atagia_bridge
    from atagia_config import reset_atagia_admin_config, template_config

    try:
        config = await reset_atagia_admin_config()
    except Exception as exc:
        logger.error("Failed to reset Atagia configuration: %s", exc, exc_info=True)
        return JSONResponse(
            content={"success": False, "message": "Failed to restore Atagia defaults."},
            status_code=500,
        )

    await reset_atagia_bridge()
    return JSONResponse(
        content={
            "success": True,
            "message": "Atagia configuration restored to defaults.",
            "config": template_config(config),
        }
    )


@app.post("/admin/atagia/test-connection")
async def admin_atagia_test(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_bridge import AtagiaBridge
    from atagia_config import preview_bridge_config_from_admin_payload

    try:
        data = await _read_atagia_json_body(request)
        config = await preview_bridge_config_from_admin_payload(data)
    except ValueError as exc:
        return JSONResponse(content={"success": False, "message": str(exc)}, status_code=400)

    bridge = AtagiaBridge(config)
    try:
        ok, message = await bridge.test_connection()
    finally:
        await bridge.close()

    status_code = 200 if ok else 502
    return JSONResponse(
        content={"success": ok, "message": message},
        status_code=status_code,
    )


@app.post("/admin/atagia/sync")
async def admin_atagia_sync(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_sync import DEFAULT_BATCH_SIZE, start_atagia_history_sync

    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        batch_size = int(data.get("batch_size") or DEFAULT_BATCH_SIZE)
    except (TypeError, ValueError):
        batch_size = DEFAULT_BATCH_SIZE

    result = await start_atagia_history_sync(batch_size=max(1, min(batch_size, 1000)))
    status_code = 202 if result.get("started") else 409
    return JSONResponse(content={"success": bool(result.get("started")), **result}, status_code=status_code)


@app.get("/admin/atagia/sync-status")
async def admin_atagia_sync_status(current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_sync import get_atagia_sync_status

    return JSONResponse(content={"success": True, "status": await get_atagia_sync_status()})


@app.get("/admin/atagia/diagnostics")
async def admin_atagia_diagnostics(current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_admin_status import get_atagia_admin_status

    return JSONResponse(content={"success": True, "status": await get_atagia_admin_status()})


@app.get("/admin/atagia/worker-control")
async def admin_atagia_worker_control_get(current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_bridge import get_atagia_bridge

    bridge = get_atagia_bridge()
    state = await bridge.get_worker_control()
    error = _format_atagia_bridge_error(bridge.last_error)
    return JSONResponse(
        content={
            "success": True,
            "available": state is not None,
            "state": _normalize_atagia_admin_payload(state),
            "error": error,
        }
    )


@app.post("/admin/atagia/worker-control")
async def admin_atagia_worker_control_post(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from atagia_bridge import get_atagia_bridge

    try:
        data = await request.json()
    except Exception:
        data = {}

    action = str(data.get("action") or data.get("mode") or "").strip().lower()
    if action not in _ATAGIA_WORKER_CONTROL_ACTIONS:
        return JSONResponse(
            content={"success": False, "message": "Invalid Atagia processing action."},
            status_code=400,
        )

    reason = str(data.get("reason") or "").strip() or None
    timeout_seconds = data.get("timeout_seconds")
    try:
        timeout_seconds = float(timeout_seconds) if timeout_seconds not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse(
            content={"success": False, "message": "Invalid timeout_seconds value."},
            status_code=400,
        )
    if timeout_seconds is not None and (timeout_seconds <= 0 or timeout_seconds > 300):
        return JSONResponse(
            content={
                "success": False,
                "message": "timeout_seconds must be greater than 0 and at most 300.",
            },
            status_code=400,
        )

    bridge = get_atagia_bridge()
    if action == "pause_new_jobs":
        state = await bridge.pause_new_jobs(reason=reason)
    elif action == "drain_and_pause":
        state = await bridge.drain_and_pause(
            reason=reason,
            timeout_seconds=timeout_seconds,
        )
    elif action == "hard_pause":
        state = await bridge.hard_pause(reason=reason)
    else:
        state = await bridge.resume_processing(reason=reason)

    if state is None:
        error = _format_atagia_bridge_error(bridge.last_error)
        message = "Atagia processing control is unavailable."
        if error and error.get("message"):
            message = f"{message} {error['message']}"
        return JSONResponse(
            content={"success": False, "message": message, "error": error},
            status_code=502,
        )

    return JSONResponse(
        content={
            "success": True,
            "message": "Atagia processing control updated.",
            "state": _normalize_atagia_admin_payload(state),
        }
    )


# GranSabio Admin
# ---------------------------------------------------------------------------

@app.get("/admin/gransabio")
async def admin_gransabio_get(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return RedirectResponse(url="/")
    from gransabio_config import get_gransabio_config
    raw = await get_gransabio_config()
    # Transform SYSTEM_CONFIG keys to template-friendly short names
    tpl_config = {
        "url": raw.get("gransabio_url", ""),
        "enabled": raw.get("gransabio_enabled", "false") == "true",
        "generator_model": raw.get("gransabio_default_generator", ""),
        "qa_models": orjson.loads(raw.get("gransabio_default_qa_models", "[]")) if raw.get("gransabio_default_qa_models") else [],
        "gran_sabio_model": raw.get("gransabio_default_gran_sabio_model", ""),
        "arbiter_model": raw.get("gransabio_default_arbiter_model", ""),
        "min_global_score": raw.get("gransabio_default_min_score", "8.0"),
        "max_iterations": raw.get("gransabio_default_max_iterations", "3"),
        "smart_editing_mode": raw.get("gransabio_default_smart_edit", "auto"),
        "gran_sabio_fallback": raw.get("gransabio_default_gran_sabio_fallback", "true") == "true",
        "verbose": raw.get("gransabio_default_verbose", "false") == "true",
        "context_max_tokens": raw.get("gransabio_default_context_max_tokens", "4000"),
        "cost_safety_multiplier": raw.get("gransabio_cost_safety_multiplier", "3"),
        "extra_allowed_ips": raw.get("gransabio_extra_allowed_ips", ""),
    }
    context = await get_template_context(request, current_user)
    context["config"] = tpl_config
    return templates.TemplateResponse("admin_gransabio.html", context)


@app.post("/admin/gransabio")
async def admin_gransabio_post(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from gransabio_config import validate_gransabio_url, invalidate_gransabio_config_cache, validate_extra_allowed_ips, get_gransabio_config

    data = await request.json()
    # Map short JS keys -> full SYSTEM_CONFIG keys
    _key_map = {
        "enabled": "gransabio_enabled",
        "url": "gransabio_url",
        "generator_model": "gransabio_default_generator",
        "qa_models": "gransabio_default_qa_models",
        "min_global_score": "gransabio_default_min_score",
        "max_iterations": "gransabio_default_max_iterations",
        "gran_sabio_model": "gransabio_default_gran_sabio_model",
        "arbiter_model": "gransabio_default_arbiter_model",
        "smart_editing_mode": "gransabio_default_smart_edit",
        "gran_sabio_fallback": "gransabio_default_gran_sabio_fallback",
        "verbose": "gransabio_default_verbose",
        "context_max_tokens": "gransabio_default_context_max_tokens",
        "cost_safety_multiplier": "gransabio_cost_safety_multiplier",
        "extra_allowed_ips": "gransabio_extra_allowed_ips",
    }
    updates = {}
    for short_key, db_key in _key_map.items():
        val = data.get(short_key)
        if val is None:
            val = data.get(db_key)  # Also accept full key names
        if val is not None:
            if isinstance(val, bool):
                val = "true" if val else "false"
            elif isinstance(val, (list, dict)):
                val = orjson.dumps(val).decode()
            else:
                val = str(val)
            updates[db_key] = val

    # Validate URL (SSRF protection)
    url = updates.get("gransabio_url", "")
    extra_ips = updates.get("gransabio_extra_allowed_ips", "")
    if url:
        ok, err = validate_gransabio_url(url, extra_ips)
        if not ok:
            return JSONResponse(content={"success": False, "message": f"URL validation failed: {err}"}, status_code=400)

    # Validate extra IPs if provided
    if extra_ips:
        try:
            validate_extra_allowed_ips(extra_ips)
        except ValueError as e:
            return JSONResponse(content={"success": False, "message": str(e)}, status_code=400)

    # Validate BEFORE persisting if GranSabio is being enabled
    if updates.get("gransabio_enabled") == "true":
        try:
            from gransabio_service import merge_gransabio_config, validate_merged_config
            # Build a preview config by overlaying updates onto current defaults
            preview = dict(GRANSABIO_DEFAULTS) if 'GRANSABIO_DEFAULTS' in dir() else {}
            try:
                current = await get_gransabio_config()
                preview.update(current)
            except Exception:
                pass
            preview.update(updates)
            merged = merge_gransabio_config({}, preview)
            valid, err = validate_merged_config(merged)
            if not valid:
                return JSONResponse(content={
                    "success": False,
                    "message": f"Configuration invalid: {err}. Fix before enabling GranSabio.",
                }, status_code=400)
        except ImportError:
            logger.warning("GranSabio modules not available, skipping pre-validation")
        except Exception as e:
            # Fail-closed: if validation can't complete, don't allow enabling
            return JSONResponse(content={
                "success": False,
                "message": f"Cannot validate configuration: {e}. Fix before enabling GranSabio.",
            }, status_code=400)

    async with get_db_connection() as conn:
        for key, value in updates.items():
            # UPDATE existing keys (preserves description column), INSERT only if new
            await conn.execute(
                "UPDATE SYSTEM_CONFIG SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                (value, key),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO SYSTEM_CONFIG (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (key, value),
            )
        await conn.commit()

    invalidate_gransabio_config_cache()
    from gransabio_config import invalidate_pricing_cache
    invalidate_pricing_cache()

    # Post-save validation warning (non-blocking, config already saved)
    if updates.get("gransabio_enabled") == "true":
        try:
            from gransabio_service import merge_gransabio_config, validate_merged_config
            fresh_config = await get_gransabio_config()
            merged = merge_gransabio_config({}, fresh_config)
            valid, err = validate_merged_config(merged)
            if not valid:
                return JSONResponse(content={
                    "success": True,
                    "message": f"Configuration saved, but validation warning: {err}. Prompts using admin defaults may fail at runtime.",
                })
        except Exception:
            pass

    return JSONResponse(content={"success": True, "message": "GranSabio configuration saved."})


@app.post("/admin/gransabio/test-connection")
async def admin_gransabio_test(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    from gransabio_config import validate_gransabio_url, get_gransabio_config
    from gransabio_service import test_gransabio_connection

    data = await request.json()
    url = data.get("url", "")
    extra_ips = data.get("extra_allowed_ips", "")
    if not extra_ips:
        config = await get_gransabio_config()
        extra_ips = config.get("gransabio_extra_allowed_ips", "")
    ok, err = validate_gransabio_url(url, extra_ips)
    if not ok:
        return JSONResponse(content={"success": False, "error": err})

    result = await test_gransabio_connection(url)
    return JSONResponse(content=result)


# Subscription Auth Admin (subscription-auth kill switch)
# ---------------------------------------------------------------------------

@app.get("/admin/subscription-auth")
async def admin_subscription_auth_get(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return RedirectResponse(url="/")
    if not _SUBSCRIPTION_AUTH_MODULE_AVAILABLE:
        raise HTTPException(status_code=404, detail="Not found")
    from common import get_subscription_auth_enabled
    from subscription_auth.linking import pending_link_count
    from request_security import ensure_csrf_token
    from subscription_auth.token_store import load_link
    from subscription_auth.doctor import runtime_ready

    context = await get_template_context(request, current_user)
    context["enabled"] = await get_subscription_auth_enabled()
    context["subscription_csrf_token"] = ensure_csrf_token(request)
    context["pending_link_count"] = await pending_link_count()
    context["runtime_ready"] = await runtime_ready()

    linked_count = 0
    state_counts: dict[str, int] = {}
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT user_id FROM USER_DETAILS WHERE subscription_auth IS NOT NULL"
        )
        candidate_rows = await cursor.fetchall()
        llm_cursor = await conn.execute(
            "SELECT id, model, display_name, enabled FROM LLM "
            "WHERE machine = 'GPTSub' ORDER BY id"
        )
        context["gptsub_models"] = [dict(row) for row in await llm_cursor.fetchall()]
    for candidate in candidate_rows:
        blob = await load_link(int(candidate["user_id"]))
        status = blob.get("status", "broken") if isinstance(blob, dict) else "broken"
        state_counts[status] = state_counts.get(status, 0) + 1
        if status == "linked":
            linked_count += 1
    context["linked_count"] = linked_count
    context["link_state_counts"] = state_counts
    return templates.TemplateResponse("admin_subscription_auth.html", context)


@app.post("/admin/subscription-auth")
async def admin_subscription_auth_post(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None or not await current_user.is_admin:
        return JSONResponse(content={"success": False, "message": "Admin only"}, status_code=403)
    if not _SUBSCRIPTION_AUTH_MODULE_AVAILABLE:
        raise HTTPException(status_code=404, detail="Not found")
    from request_security import validate_mutation_request

    csrf_rejection = validate_mutation_request(request)
    if csrf_rejection:
        return csrf_rejection
    from common import set_subscription_auth_enabled
    data = await request.json()
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(
            content={"success": False, "message": "'enabled' must be a boolean."},
            status_code=400,
        )
    if enabled:
        from subscription_auth.gate import subscription_auth_emergency_off_latched

        if subscription_auth_emergency_off_latched():
            return JSONResponse(
                content={
                    "success": False,
                    "message": (
                        "GPTSub was disabled by a runtime safety check. Restart "
                        "Aurvek after fixing the runtime and rerun the doctor."
                    ),
                },
                status_code=503,
            )
        from subscription_auth.doctor import runtime_ready

        if not await runtime_ready():
            return JSONResponse(
                content={
                    "success": False,
                    "message": (
                        "GPTSub runtime is not ready. Keep the switch off and run "
                        "the secret-free production doctor."
                    ),
                },
                status_code=503,
            )
    if not enabled:
        # Linearize an emergency OFF locally before the DB write or any await. If
        # persistence is unavailable, the process must still remain denied; a
        # deliberate re-enable therefore requires a clean restart and doctor run.
        from subscription_auth.gate import latch_subscription_auth_off

        latch_subscription_auth_off()
    await set_subscription_auth_enabled(enabled)
    if not enabled:
        # The switch is already durably OFF, so no poll can pass its final
        # pre-persistence revalidation. Tear down every held device flow promptly
        # instead of retaining child processes until their individual TTLs.
        from subscription_auth.linking import shutdown_pending_links

        await shutdown_pending_links()
    return JSONResponse(content={"success": True, "message": "Subscription auth setting saved."})


# ============================================================
# Security Guard LLM Configuration
# ============================================================

@app.get("/admin/security-guard", response_class=HTMLResponse)
async def admin_security_guard(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for configuring Security Guard LLM."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    # Get current security guard config
    current_llm_id = None
    async with get_db_connection(readonly=True) as conn:
        # Check if SYSTEM_CONFIG table exists
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='SYSTEM_CONFIG'"
        )
        table_exists = await cursor.fetchone()

        if table_exists:
            cursor = await conn.execute(
                "SELECT value FROM SYSTEM_CONFIG WHERE key = 'security_guard_llm_id'"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                current_llm_id = int(row[0])

        # The Security Guard is a system-wide LLM, not a per-user personal
        # subscription; GPTSub (ChatGPT subscription) must never be selectable here.
        llm_rows = await get_selector_llms(
            conn,
            preserve_ids=[current_llm_id] if current_llm_id else [],
        )
        llms = [(row["id"], row["machine"], row["model"]) for row in llm_rows]

    context = await get_template_context(request, current_user)
    context.update({
        "current_llm_id": current_llm_id,
        "llms": llms
    })
    return templates.TemplateResponse("admin/security_guard_config.html", context)


@app.post("/admin/security-guard")
async def save_security_guard_config(
    request: Request,
    current_user: User = Depends(get_current_user),
    llm_id: str = Form("")
):
    """Save Security Guard LLM configuration."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    async with get_db_connection() as conn:
        # Ensure SYSTEM_CONFIG table exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS SYSTEM_CONFIG (
                key TEXT PRIMARY KEY,
                value TEXT,
                description TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # A system-wide guard cannot borrow one user's private ChatGPT
        # subscription. Validate the forged/manual POST as well as filtering the
        # selector UI; only a live non-GPTSub row may be persisted.
        llm_id_value = None
        if llm_id:
            try:
                parsed_llm_id = int(llm_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid LLM selection")
            cursor = await conn.execute(
                "SELECT id FROM LLM "
                "WHERE id = ? AND enabled = 1 AND machine != 'GPTSub'",
                (parsed_llm_id,),
            )
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=400, detail="Invalid LLM selection")
            llm_id_value = str(parsed_llm_id)

        # Update or insert the config
        await conn.execute("""
            INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
            VALUES ('security_guard_llm_id', ?, 'LLM ID for security checks before AI Wizard', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP
        """, (llm_id_value, llm_id_value))

        await conn.commit()

    logger.info(f"Security Guard LLM configuration updated: llm_id={llm_id_value}")

    return RedirectResponse(url="/admin/security-guard?saved=1", status_code=303)


@app.get("/api/security-guard/status")
async def get_security_guard_status(current_user: User = Depends(get_current_user)):
    """Get current Security Guard configuration status."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    enabled = await is_security_guard_enabled()

    config = None
    if enabled:
        from security_guard_llm import get_security_guard_config
        config = await get_security_guard_config()

    return JSONResponse({
        "enabled": enabled,
        "config": config
    })


# ============================================================
# LLM Catalog Sync Management
# ============================================================

LLM_SYNC_PROVIDER_TABS = [
    {"key": "openrouter", "label": "OpenRouter", "icon": "fa-route"},
    {"key": "openai", "label": "OpenAI", "icon": "fa-robot"},
    {"key": "anthropic", "label": "Claude", "icon": "fa-brain"},
    {"key": "google", "label": "Gemini", "icon": "fa-gem"},
    {"key": "xai", "label": "xAI", "icon": "fa-xmark"},
    {"key": "minimax", "label": "MiniMax", "icon": "fa-bolt"},
    {"key": "kimi", "label": "Kimi", "icon": "fa-moon"},
]


@app.get("/admin/models", response_class=HTMLResponse)
async def admin_models(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for discovering and syncing LLM provider catalogs."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    requested_provider = normalize_provider_key(request.query_params.get("provider") or "openrouter")
    provider_keys = {provider["key"] for provider in LLM_SYNC_PROVIDER_TABS}
    if requested_provider not in provider_keys:
        requested_provider = "openrouter"

    async with get_db_connection(readonly=True) as conn:
        catalog = await get_llm_catalog(conn)

    provider_counts = {}
    for llm in catalog:
        provider_key = normalize_provider_key(llm.get("provider_key") or llm.get("machine"))
        counts = provider_counts.setdefault(provider_key, {"total": 0, "enabled": 0, "needs_review": 0})
        counts["total"] += 1
        if llm.get("enabled"):
            counts["enabled"] += 1
        if llm.get("needs_review"):
            counts["needs_review"] += 1

    context = await get_template_context(request, current_user)
    context.update({
        "provider_tabs": LLM_SYNC_PROVIDER_TABS,
        "active_provider": requested_provider,
        "catalog_count": len(catalog),
        "enabled_count": sum(1 for llm in catalog if llm.get("enabled")),
        "needs_review_count": sum(1 for llm in catalog if llm.get("needs_review")),
        "provider_counts": provider_counts,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })
    return templates.TemplateResponse("admin_models.html", context)


@app.get("/admin/openrouter", response_class=HTMLResponse)
async def admin_openrouter(request: Request, current_user: User = Depends(get_current_user)):
    """Compatibility alias for the previous OpenRouter-only admin page."""
    return RedirectResponse(url="/admin/models?provider=openrouter", status_code=303)


@app.get("/api/openrouter/models")
async def get_openrouter_models(current_user: User = Depends(get_current_user)):
    """Fetch available models from OpenRouter API."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    try:
        async with get_db_connection(readonly=True) as conn:
            provider_view = await get_provider_catalog_view(conn, "openrouter")
        models = []
        for model in provider_view.get("models", []):
            if not model.get("remote_available", True):
                continue
            model_id = model["provider_model_id"]
            provider = model_id.split("/")[0] if "/" in model_id else "unknown"
            models.append({
                "id": model_id,
                "name": model["display_name"],
                "provider": provider,
                "context_length": model.get("context_window_tokens") or 0,
                "max_output_tokens": model.get("max_output_tokens") or 0,
                "input_price": model.get("input_token_cost") or 0,
                "output_price": model.get("output_token_cost") or 0,
                "vision": bool(model.get("vision")),
                "local_id": model.get("local_id"),
                "enabled": bool(model.get("enabled")),
                "sync_status": model.get("sync_status"),
                "needs_review": bool(model.get("needs_review")),
                "metadata_source": model.get("metadata_source"),
            })
        models.sort(key=lambda x: (x["provider"].lower(), x["name"].lower()))
        return JSONResponse(content={"models": models})
    except LlmCatalogError as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("Error fetching OpenRouter models")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/openrouter/sync")
async def sync_openrouter_models(request: Request, current_user: User = Depends(get_current_user)):
    """Sync selected OpenRouter models to database."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    try:
        body = await request.json()
        models_to_save = body.get("models", [])
        selected_model_ids = [m.get("id") for m in models_to_save if m.get("id")]
        disabled_model_ids = body.get("disabled_model_ids", [])

        async with get_db_connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                result = await sync_provider(
                    conn,
                    "openrouter",
                    selected_model_ids=selected_model_ids,
                    remote_models=models_to_save,
                    disabled_model_ids=disabled_model_ids,
                )
                await conn.commit()
            except HTTPException:
                await conn.rollback()
                raise
            except Exception:
                await conn.rollback()
                raise

        return JSONResponse(content={
            "success": True,
            "added": result["added"],
            "updated": result["updated"],
            "disabled": result["disabled"],
            "removed": 0,
            "stale": result["stale"],
            "skipped": result["skipped"],
        })

    except HTTPException:
        raise
    except LlmCatalogError as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Error syncing OpenRouter models")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.get("/api/admin/llms/catalog")
async def api_admin_llm_catalog(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection(readonly=True) as conn:
        catalog = await get_llm_catalog(conn)
    return JSONResponse(content={"models": catalog})


@app.get("/api/admin/llms/providers/{provider_key}/remote")
async def api_admin_llm_provider_remote(provider_key: str, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    try:
        async with get_db_connection(readonly=True) as conn:
            provider_view = await get_provider_catalog_view(conn, provider_key)
        return JSONResponse(content=provider_view)
    except LlmCatalogError as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Error fetching remote LLM provider")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/admin/llms/providers/{provider_key}/sync")
async def api_admin_llm_provider_sync(request: Request, provider_key: str, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    try:
        body = await request.json()
        selected_model_ids = body.get("selected_model_ids")
        remote_models = body.get("models")
        disabled_model_ids = body.get("disabled_model_ids")
        if remote_models is None:
            remote_models = await fetch_remote_models(provider_key)
        async with get_db_connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                result = await sync_provider(
                    conn,
                    provider_key,
                    selected_model_ids=selected_model_ids,
                    remote_models=remote_models,
                    disabled_model_ids=disabled_model_ids,
                )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return JSONResponse(content={"success": True, **result})
    except LlmCatalogError as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Error syncing LLM provider")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.patch("/api/admin/llms/{llm_id}/enabled")
async def api_admin_llm_enabled(llm_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    try:
        body = await request.json()
        raw_enabled = body.get("enabled")
        if isinstance(raw_enabled, str):
            enabled = raw_enabled.lower() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(raw_enabled)
        async with get_db_connection() as conn:
            result = await set_model_enabled(conn, llm_id, enabled)
            await conn.commit()
        return JSONResponse(content={"success": True, **result})
    except LlmCatalogError as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)
    except Exception as e:
        logger.exception("Error updating LLM enabled state")
        return JSONResponse(content={"error": str(e)}, status_code=500)


@app.post("/api/admin/llms/sync-all")
async def api_admin_llm_sync_all(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection() as conn:
        results = await sync_all_providers(conn)
        await conn.commit()
    return JSONResponse(content={"success": True, "results": results})



# =============================================================================
# Admin System Prompt Blocks
# =============================================================================

VALID_POSITIONS = {"pre_prompt", "post_prompt"}
VALID_CONDITIONS = {"always", "watchdog_only"}


@app.get("/admin/system-prompts", response_class=HTMLResponse)
async def admin_system_prompts(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("admin_system_prompts.html", {**context, "request": request})


@app.get("/api/system-prompt-blocks")
async def api_list_system_prompt_blocks(request: Request, current_user: User = Depends(get_current_user)):
    """List all system prompt blocks for admin UI, including virtual entries for missing system blocks."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """SELECT id, system_key, name, content, description, position, condition,
                          is_enabled, is_system, display_order, updated_at
                   FROM SYSTEM_PROMPT_BLOCKS
                   ORDER BY CASE WHEN position = 'pre_prompt' THEN 0 ELSE 1 END,
                            display_order ASC, id ASC"""
            )
            rows = [dict(row) for row in await cursor.fetchall()]
    except Exception:
        logger.warning("Failed to read SYSTEM_PROMPT_BLOCKS for admin, using defaults")
        rows = []

    # Normalize system block metadata for display
    seen_keys = set()
    for row in rows:
        sk = row.get("system_key")
        if sk and sk in SYSTEM_BLOCK_METADATA:
            seen_keys.add(sk)
            meta = SYSTEM_BLOCK_METADATA[sk]
            row["position"] = meta["position"]
            row["condition"] = meta["condition"]
            row["_mandatory"] = sk in MANDATORY_SYSTEM_KEYS

    # Virtual entries for missing system blocks
    for key, default in DEFAULT_SYSTEM_BLOCKS.items():
        if key not in seen_keys:
            meta = SYSTEM_BLOCK_METADATA[key]
            rows.append({
                "id": None,
                "system_key": key,
                "name": key.replace("_", " ").title(),
                "content": default["content"],
                "description": "",
                "position": meta["position"],
                "condition": meta["condition"],
                "is_enabled": 1,
                "is_system": 1,
                "display_order": meta["display_order"],
                "updated_at": None,
                "_missing": True,
                "_mandatory": key in MANDATORY_SYSTEM_KEYS,
            })

    rows.sort(key=lambda r: (0 if r["position"] == "pre_prompt" else 1,
                              r["display_order"], r.get("id") or 999999))
    return JSONResponse(content=rows)


@app.post("/api/system-prompt-blocks")
async def api_create_system_prompt_block(request: Request, current_user: User = Depends(get_current_user)):
    """Create a custom system prompt block."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    data = await request.json()

    # Force custom identity
    data["system_key"] = None
    data["is_system"] = 0

    # Reject reserved system_key in body
    if data.get("system_key") in SYSTEM_BLOCK_METADATA:
        return JSONResponse(status_code=422, content={"error": "Cannot use a reserved system key"})

    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=422, content={"error": "Name is required"})

    content = data.get("content", "")
    if len(content) > MAX_BLOCK_CONTENT_SIZE:
        return JSONResponse(status_code=422, content={"error": f"Content exceeds {MAX_BLOCK_CONTENT_SIZE} byte limit"})

    position = data.get("position", "post_prompt")
    if position not in VALID_POSITIONS:
        return JSONResponse(status_code=422, content={"error": f"Invalid position. Must be one of: {', '.join(VALID_POSITIONS)}"})

    condition = data.get("condition", "always")
    if condition not in VALID_CONDITIONS:
        return JSONResponse(status_code=422, content={"error": f"Invalid condition. Must be one of: {', '.join(VALID_CONDITIONS)}"})

    description = data.get("description", "")
    display_order = int(data.get("display_order", 0))
    is_enabled = 1 if data.get("is_enabled", True) else 0

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            """INSERT INTO SYSTEM_PROMPT_BLOCKS
               (system_key, name, content, description, position, condition, is_enabled, is_system, display_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (None, name, content, description, position, condition, is_enabled, 0, display_order)
        )
        await conn.commit()
        new_id = cursor.lastrowid

        cursor = await conn.execute(
            "SELECT id, system_key, name, content, description, position, condition, is_enabled, is_system, display_order, updated_at FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (new_id,)
        )
        created = dict(await cursor.fetchone())

    try:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="create_system_prompt_block",
            request=request,
            target_resource_type="system_prompt_block",
            target_resource_id=new_id,
            details=f"Created custom block: {name}"
        )
    except Exception:
        pass

    return JSONResponse(content=created, status_code=201)


@app.put("/api/system-prompt-blocks/{block_id:int}")
async def api_update_system_prompt_block(block_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Update an existing system prompt block."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    data = await request.json()

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, system_key, name, content, description, position, condition, is_enabled, is_system, display_order FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (block_id,)
        )
        block = await cursor.fetchone()
        if not block:
            return JSONResponse(status_code=404, content={"error": "Block not found"})

        block = dict(block)
        sk = block["system_key"]

        new_content = data.get("content", block["content"])
        if len(new_content) > MAX_BLOCK_CONTENT_SIZE:
            return JSONResponse(status_code=422, content={"error": f"Content exceeds {MAX_BLOCK_CONTENT_SIZE} byte limit"})

        if sk and sk in SYSTEM_BLOCK_METADATA:
            # System block: restricted updates
            if not new_content.strip():
                return JSONResponse(status_code=422, content={"error": "System blocks cannot have empty content"})

            new_is_enabled = data.get("is_enabled", block["is_enabled"])
            if isinstance(new_is_enabled, bool):
                new_is_enabled = 1 if new_is_enabled else 0
            new_is_enabled = int(new_is_enabled)

            if sk in MANDATORY_SYSTEM_KEYS and not new_is_enabled:
                return JSONResponse(status_code=422, content={"error": "This block cannot be disabled"})

            canonical = SYSTEM_BLOCK_METADATA[sk]
            new_condition = data.get("condition", canonical["condition"])
            if new_condition != canonical["condition"]:
                return JSONResponse(status_code=422, content={"error": f"Condition is frozen to '{canonical['condition']}'"})

            new_position = data.get("position", canonical["position"])
            if new_position != canonical["position"]:
                return JSONResponse(status_code=422, content={"error": f"Position is frozen to '{canonical['position']}'"})

            new_name = data.get("name", block["name"])
            new_description = data.get("description", block["description"])
            new_display_order = block["display_order"]  # Frozen for system blocks

            await conn.execute(
                """UPDATE SYSTEM_PROMPT_BLOCKS
                   SET name = ?, content = ?, description = ?, is_enabled = ?, display_order = ?
                   WHERE id = ?""",
                (new_name, new_content, new_description, new_is_enabled, new_display_order, block_id)
            )
        else:
            # Custom block: all fields editable
            new_name = (data.get("name") or block["name"]).strip()
            if not new_name:
                return JSONResponse(status_code=422, content={"error": "Name is required"})

            new_position = data.get("position", block["position"])
            if new_position not in VALID_POSITIONS:
                return JSONResponse(status_code=422, content={"error": f"Invalid position. Must be one of: {', '.join(VALID_POSITIONS)}"})

            new_condition = data.get("condition", block["condition"])
            if new_condition not in VALID_CONDITIONS:
                return JSONResponse(status_code=422, content={"error": f"Invalid condition. Must be one of: {', '.join(VALID_CONDITIONS)}"})

            new_description = data.get("description", block["description"])
            new_display_order = int(data.get("display_order", block["display_order"]))
            new_is_enabled = data.get("is_enabled", block["is_enabled"])
            if isinstance(new_is_enabled, bool):
                new_is_enabled = 1 if new_is_enabled else 0
            new_is_enabled = int(new_is_enabled)

            await conn.execute(
                """UPDATE SYSTEM_PROMPT_BLOCKS
                   SET name = ?, content = ?, description = ?, position = ?, condition = ?,
                       is_enabled = ?, display_order = ?
                   WHERE id = ?""",
                (new_name, new_content, new_description, new_position, new_condition,
                 new_is_enabled, new_display_order, block_id)
            )

        await conn.commit()

        cursor = await conn.execute(
            "SELECT id, system_key, name, content, description, position, condition, is_enabled, is_system, display_order, updated_at FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (block_id,)
        )
        updated = dict(await cursor.fetchone())

    try:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="update_system_prompt_block",
            request=request,
            target_resource_type="system_prompt_block",
            target_resource_id=block_id,
            details=f"Updated block: {updated['name']}"
        )
    except Exception:
        pass

    return JSONResponse(content=updated)


@app.post("/api/system-prompt-blocks/{block_id}/reset")
async def api_reset_system_prompt_block(block_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Reset a system block to its default content."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, system_key, is_system FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (block_id,)
        )
        block = await cursor.fetchone()
        if not block:
            return JSONResponse(status_code=404, content={"error": "Block not found"})

        block = dict(block)
        if not block["is_system"]:
            return JSONResponse(status_code=403, content={"error": "Only system blocks can be reset"})

        sk = block["system_key"]
        if sk not in DEFAULT_SYSTEM_BLOCKS:
            return JSONResponse(status_code=404, content={"error": "No default found for this system block"})

        default_content = DEFAULT_SYSTEM_BLOCKS[sk]["content"]

        await conn.execute(
            "UPDATE SYSTEM_PROMPT_BLOCKS SET content = ?, is_enabled = 1 WHERE id = ?",
            (default_content, block_id)
        )
        await conn.commit()

        cursor = await conn.execute(
            "SELECT id, system_key, name, content, description, position, condition, is_enabled, is_system, display_order, updated_at FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (block_id,)
        )
        updated = dict(await cursor.fetchone())

    try:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="reset_system_prompt_block",
            request=request,
            target_resource_type="system_prompt_block",
            target_resource_id=block_id,
            details=f"Reset block to default: {sk}"
        )
    except Exception:
        pass

    return JSONResponse(content=updated)


@app.post("/api/system-prompt-blocks/restore/{system_key}")
async def api_restore_system_prompt_block(system_key: str, request: Request, current_user: User = Depends(get_current_user)):
    """Restore a missing system block from code defaults."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    if system_key not in SYSTEM_BLOCK_METADATA:
        return JSONResponse(status_code=404, content={"error": "Unknown system key"})

    default = DEFAULT_SYSTEM_BLOCKS[system_key]
    meta = SYSTEM_BLOCK_METADATA[system_key]

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM SYSTEM_PROMPT_BLOCKS WHERE system_key = ?",
            (system_key,)
        )
        existing = await cursor.fetchone()
        if existing:
            return JSONResponse(status_code=409, content={"error": "Block already exists"})

        cursor = await conn.execute(
            """INSERT INTO SYSTEM_PROMPT_BLOCKS
               (system_key, name, content, description, position, condition, is_enabled, is_system, display_order)
               VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?)""",
            (system_key, system_key.replace("_", " ").title(), default["content"], "",
             meta["position"], meta["condition"], meta["display_order"])
        )
        await conn.commit()
        new_id = cursor.lastrowid

        cursor = await conn.execute(
            "SELECT id, system_key, name, content, description, position, condition, is_enabled, is_system, display_order, updated_at FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (new_id,)
        )
        created = dict(await cursor.fetchone())

    try:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="restore_system_prompt_block",
            request=request,
            target_resource_type="system_prompt_block",
            target_resource_id=new_id,
            details=f"Restored missing system block: {system_key}"
        )
    except Exception:
        pass

    return JSONResponse(content=created, status_code=201)


@app.delete("/api/system-prompt-blocks/{block_id}")
async def api_delete_system_prompt_block(block_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Delete a custom system prompt block."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, is_system FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?",
            (block_id,)
        )
        block = await cursor.fetchone()
        if not block:
            return JSONResponse(status_code=404, content={"error": "Block not found"})

        block = dict(block)
        if block["is_system"]:
            return JSONResponse(status_code=403, content={"error": "System blocks cannot be deleted"})

        await conn.execute("DELETE FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?", (block_id,))
        await conn.commit()

    try:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="delete_system_prompt_block",
            request=request,
            target_resource_type="system_prompt_block",
            target_resource_id=block_id,
            details=f"Deleted custom block: {block['name']}"
        )
    except Exception:
        pass

    return JSONResponse(content={"success": True})


@app.put("/api/system-prompt-blocks/reorder")
async def api_reorder_system_prompt_blocks(request: Request, current_user: User = Depends(get_current_user)):
    """Reorder custom system prompt blocks (system blocks have frozen order)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"error": "Admin access required"})

    data = await request.json()
    if not isinstance(data, list):
        return JSONResponse(status_code=422, content={"error": "Expected a list of {id, display_order}"})

    async with get_db_connection() as conn:
        for item in data:
            item_id = item.get("id")
            new_order = item.get("display_order")
            if item_id is None or new_order is None:
                return JSONResponse(status_code=422, content={"error": "Each item must have 'id' and 'display_order'"})

            cursor = await conn.execute(
                "SELECT is_system FROM SYSTEM_PROMPT_BLOCKS WHERE id = ?", (item_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return JSONResponse(status_code=422, content={"error": f"Block {item_id} not found"})
            if row[0]:
                return JSONResponse(status_code=422, content={"error": f"Cannot reorder system block {item_id}"})

            await conn.execute(
                "UPDATE SYSTEM_PROMPT_BLOCKS SET display_order = ? WHERE id = ?",
                (int(new_order), item_id)
            )
        await conn.commit()

    try:
        await log_admin_action(
            admin_id=current_user.id,
            action_type="reorder_system_prompt_blocks",
            request=request,
            target_resource_type="system_prompt_block",
            details=f"Reordered {len(data)} custom blocks"
        )
    except Exception:
        pass

    return JSONResponse(content={"success": True})




@app.get("/admin/services", response_class=HTMLResponse)
async def service_list(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)

    async with get_db_connection(readonly=True) as conn:
        async with conn.execute("SELECT id, name, unit, cost_per_unit, type FROM SERVICES ORDER BY name DESC") as cursor:
            services = await cursor.fetchall()
            services = [(id, name, unit, cost_per_unit, type) for (id, name, unit, cost_per_unit, type) in services]

    context = await get_template_context(request, current_user)
    context["services"] = services
    return templates.TemplateResponse("services/services_list.html", context)


@app.get("/admin/services/new", response_class=HTMLResponse)
async def create_service(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    service_types = ["TTS", "STT", "Phone", "Images", "Video", "Music"]
    context = await get_template_context(request, current_user)
    context["service_types"] = service_types
    return templates.TemplateResponse("services/create_service.html", context)

@app.post("/admin/services/new")
async def create_service_post(request: Request, current_user: User = Depends(get_current_user), name: str = Form(...), unit: str = Form(...), cost_per_unit: float = Form(...), type: str = Form(...)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute("INSERT INTO SERVICES (name, unit, cost_per_unit, type) VALUES (?, ?, ?, ?)",
                             (name, unit, cost_per_unit, type))
        await conn.commit()
        await conn.close()
        return RedirectResponse(url="/admin/services", status_code=303)

@app.get("/admin/services/edit/{service_id}", response_class=HTMLResponse)
async def edit_service(request: Request, service_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT name, unit, cost_per_unit, type FROM SERVICES WHERE id = ?", (service_id,))
        service = await cursor.fetchone()
        await conn.close()
        if service:
            service_types = ["TTS", "STT", "Phone", "Images", "Video", "Music"]
            context = await get_template_context(request, current_user)
            context.update({
                "service_id": service_id,
                "service_name": service[0],
                "service_unit": service[1],
                "service_cost_per_unit": service[2],
                "service_type": service[3],
                "service_types": service_types
            })
            return templates.TemplateResponse("services/edit_service.html", context)
        else:
            raise HTTPException(status_code=404, detail="Service not found")

@app.post("/admin/services/update/{service_id}")
async def update_service(request: Request, service_id: int, current_user: User = Depends(get_current_user), name: str = Form(...), unit: str = Form(...), cost_per_unit: float = Form(...), type: str = Form(...)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute("UPDATE SERVICES SET name = ?, unit = ?, cost_per_unit = ?, type = ? WHERE id = ?",
                             (name, unit, cost_per_unit, type, service_id))
        await conn.commit()
        await conn.close()

    await Cost.initialize()

    return RedirectResponse(url="/admin/services", status_code=303)

@app.delete("/admin/services/delete/{service_id}")
async def delete_service(service_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute('DELETE FROM SERVICES WHERE id = ?', (service_id,))
            await conn.commit()
        except Exception as e:
            await conn.rollback()
            return JSONResponse(content={"error": str(e)}, status_code=500)
        finally:
            await conn.close()

        return JSONResponse(content={"success": True}, status_code=200)

@app.get("/api/voices")
async def get_voices(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            """
            SELECT v.id,v.voice_code,v.name
            FROM VOICES v
            JOIN SERVICES s ON s.id=v.tts_service
            WHERE COALESCE(v.deprecated,0)=0
              AND TRIM(COALESCE(v.voice_code,''))<>''
              AND TRIM(COALESCE(s.name,''))<>''
            ORDER BY v.id
            """
        )
        voices = await cursor.fetchall()
    return [
        {"id": voice[1], "catalog_id": voice[0], "name": voice[2]}
        for voice in voices
    ]

@app.get("/admin/voices", response_class=HTMLResponse)
async def list_voices(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("""
            SELECT V.id, V.name, V.voice_code, S.name as tts_service_name, V.is_default, V.deprecated
            FROM VOICES V
            JOIN SERVICES S ON V.tts_service = S.id
            ORDER BY S.name, V.name
        """)
        voices = await cursor.fetchall()
        await conn.close()
        context = await get_template_context(request, current_user)
        context["voices"] = voices
        return templates.TemplateResponse("voices/voices_list.html", context)

@app.get("/admin/voices/new", response_class=HTMLResponse)
async def create_voice(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    # OpenAI voices only - ElevenLabs managed via Sync page
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("voices/create_voice.html", context)

@app.post("/admin/voices/new")
async def create_voice_post(request: Request, current_user: User = Depends(get_current_user), name: str = Form(...), voice_code: str = Form(...), tts_service: str = Form(...)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute("INSERT INTO VOICES (name, voice_code, tts_service) VALUES (?, ?, ?)", (name, voice_code, tts_service))
        await conn.commit()
        await conn.close()
        return RedirectResponse(url="/admin/voices", status_code=303)

@app.get("/admin/voices/edit/{voice_id}", response_class=HTMLResponse)
async def edit_voice(request: Request, voice_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT name, voice_code FROM VOICES WHERE id = ?", (voice_id,))
        voice = await cursor.fetchone()
        await conn.close()
        if voice:
            # OpenAI voices only - ElevenLabs managed via Sync page
            context = await get_template_context(request, current_user)
            context.update({
                "voice_id": voice_id,
                "voice_name": voice[0],
                "voice_code": voice[1]
            })
            return templates.TemplateResponse("voices/edit_voice.html", context)
        else:
            raise HTTPException(status_code=404, detail="Voice not found")

@app.post("/admin/voices/update/{voice_id}")
async def update_voice(request: Request, voice_id: int, current_user: User = Depends(get_current_user), name: str = Form(...), voice_code: str = Form(...), tts_service: str = Form(...)):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute("UPDATE VOICES SET name = ?, voice_code = ?, tts_service = ? WHERE id = ?", (name, voice_code, tts_service, voice_id))
        await conn.commit()
        await conn.close()
        return RedirectResponse(url="/admin/voices", status_code=303)

@app.delete("/admin/voices/delete/{voice_id}")
async def delete_voice(voice_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        try:
            await cursor.execute('DELETE FROM VOICES WHERE id = ?', (voice_id,))
            await conn.commit()
        except Exception as e:
            await conn.rollback()
            return JSONResponse(content={"error": str(e)}, status_code=500)
        finally:
            await conn.close()

        return JSONResponse(content={"success": True}, status_code=200)


@app.post("/admin/voices/set-default/{voice_id}")
async def set_default_voice(voice_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Access denied"}, status_code=403)
    async with get_db_connection() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            """
            SELECT v.id FROM VOICES v
            JOIN SERVICES s ON s.id=v.tts_service
            WHERE v.id=? AND COALESCE(v.deprecated, 0)=0
              AND length(trim(v.voice_code)) > 0
            """,
            (voice_id,),
        )
        if await cursor.fetchone() is None:
            await conn.rollback()
            return JSONResponse(
                content={"error": "Voice not found or unavailable"}, status_code=404
            )
        await conn.execute("UPDATE VOICES SET is_default = 0 WHERE is_default = 1")
        await conn.execute("UPDATE VOICES SET is_default = 1 WHERE id = ?", (voice_id,))
        await conn.commit()
    return JSONResponse(content={"success": True})


@app.get("/get-image/{path:path}")
async def get_image(path: str, request: Request, token: Optional[str] = None):
    try:
        # Use a local variable to determine the use of Cloudflare
        use_cloudflare = CLOUDFLARE_FOR_IMAGES
        if "profile" in path:
            use_cloudflare = False

        if use_cloudflare:
            logger.info("Entering use_cloudflare")
            # Verify token if needed (depending on your security logic)
            if not token:
                raise HTTPException(status_code=401, detail="Token is required")

            payload = decode_jwt_cached(token, SECRET_KEY)

            exp = payload.get("exp")
            if exp is None or datetime.now(timezone.utc) > datetime.fromtimestamp(exp, timezone.utc):
                raise HTTPException(status_code=401, detail="Token has expired")

            current_user = payload.get("username")
            if not current_user:
                raise HTTPException(status_code=401, detail="Invalid token")

            # Generate user hash
            hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user)

            # Validate path is within user directory
            user_base = Path(f"data/users/{hash_prefix1}/{hash_prefix2}/{user_hash}")
            validated_path = validate_path_within_directory(path, user_base)

            if not validated_path.is_file():
                raise HTTPException(status_code=404, detail="Image not found")

            # Build image path for Cloudflare URL
            image_path = f"/users/{hash_prefix1}/{hash_prefix2}/{user_hash}/{path}"

            # Generate Cloudflare signed URL
            signed_url = generate_signed_url_cloudflare(image_path, expiration_seconds=3600)

            return JSONResponse(content={"url": signed_url})
        else:
            logger.info("Entering WITHOUT cloudflare")

            # Current method without Cloudflare
            if not token:
                raise HTTPException(status_code=401, detail="Token is required")

            # Decode token
            payload = decode_jwt_cached(token, SECRET_KEY)

            # Verify token expiration
            exp = payload.get("exp")
            if exp is None or datetime.now(timezone.utc) > datetime.fromtimestamp(exp, timezone.utc):
                raise HTTPException(status_code=401, detail="Token has expired")

            # Get the current user
            current_user = payload.get("username")
            if not current_user:
                raise HTTPException(status_code=401, detail="Invalid token")

            # Generate user hash
            hash_prefix1, hash_prefix2, user_hash = generate_user_hash(current_user)

            # Validate path is within user directory
            user_base = Path(f"data/users/{hash_prefix1}/{hash_prefix2}/{user_hash}")
            validated_path = validate_path_within_directory(path, user_base)

            if not validated_path.is_file():
                raise HTTPException(status_code=404, detail="Image not found")

            # Build image path for URL
            image_path = f"/users/{hash_prefix1}/{hash_prefix2}/{user_hash}/{path}"

            # Calculate time until expiration
            time_until_expiration = int(exp - datetime.now(timezone.utc).timestamp())

            # Build the URL using the scheme, host and port from the request
            scheme = request.url.scheme
            host = request.url.hostname
            port = request.url.port

            if port and port not in [80, 443]:
                image_url = f"{scheme}://{host}:{port}{quote(image_path)}"
            else:
                image_url = f"{scheme}://{host}{quote(image_path)}"

            # Configure headers for redirect
            headers = {
                "Cache-Control": f"public, max-age={time_until_expiration}",
                "Expires": (datetime.fromtimestamp(exp, timezone.utc)).strftime("%a, %d %b %Y %H:%M:%S GMT")
            }

            # Return redirect to image
            return RedirectResponse(url=image_url, headers=headers)

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/auth-image")
async def auth_image(request: Request, token: str = Query(None), request_uri: str = Query(None)):
    if not token:
        logger.error("[auth_image] No token provided")
        raise HTTPException(status_code=401, detail="No token provided")

    try:
        payload = decode_jwt_cached(token, SECRET_KEY)

        if not verify_token_expiration(payload):
            logger.warning("[auth_image] Token expired")
            raise HTTPException(status_code=401, detail="Token expired")

        username = payload.get("username")
        if username is None:
            logger.error("[auth_image] No Username in jwt")
            raise HTTPException(status_code=401, detail="Invalid token")

        # Verify if the image path corresponds to the user
        if not request_uri:
            raise HTTPException(status_code=400, detail="No request_uri provided")

        try:
            clean_uri = normalize_image_path(request_uri)
            token_path = payload.get("media_path")
            if token_path:
                token_path = normalize_image_path(token_path)
        except (TypeError, ValueError):
            raise HTTPException(status_code=403, detail="Access denied")

        if token_path:
            # New tokens authorize one exact resource. This also supports public
            # prompt avatars owned by another user without granting access to
            # any sibling file in that owner's directory.
            if not secrets.compare_digest(token_path, clean_uri):
                logger.warning("[auth_image] Token path does not match requested image")
                raise HTTPException(status_code=403, detail="Access denied")

            uri_parts = clean_uri.split('/')
            if len(uri_parts) < 5 or uri_parts[0] != 'users':
                raise HTTPException(status_code=403, detail="Access denied")
            image_base = Path("data").joinpath(*uri_parts[:4])
            relative_path = '/'.join(uri_parts[4:])
            validate_path_within_directory(relative_path, image_base)
        else:
            # Tokens issued before path binding, plus the deliberately generic
            # media token, are restricted to the token holder's own directory.
            hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)
            user_base = Path(f"data/users/{hash_prefix1}/{hash_prefix2}/{user_hash}")
            expected_prefix = f"users/{hash_prefix1}/{hash_prefix2}/{user_hash}/"
            if clean_uri.startswith("users/"):
                if not clean_uri.startswith(expected_prefix):
                    logger.warning("[auth_image] Token user does not match image path")
                    raise HTTPException(status_code=403, detail="Access denied")
                relative_path = clean_uri[len(expected_prefix):]
            else:
                relative_path = clean_uri
            validate_path_within_directory(relative_path, user_base)

        logger.debug(f"[auth_image] Authentication successful for user: {username}")
        return Response(status_code=200)
    except JWTError as e:
        raise HTTPException(status_code=401, detail="Invalid token")
    except FastAPIHTTPException as e:
        # Re-raise HTTP exceptions (includes 403 from validate_path_within_directory)
        raise e


# Endpoint to serve the MP3 file once generated
# ====== CHAT FOLDERS API ENDPOINTS ======

@app.get("/api/voice-sample/{sample_voice_id}")
async def get_voice_sample(
    request: Request,
    sample_voice_id: str,
    category: int = Query(..., ge=0, le=11),
    provider: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    is_admin_user = bool(await current_user.is_admin)
    try:
        resolved_voice = await resolve_playback_voice(
            sample_voice_id,
            allow_uncatalogued_preview=is_admin_user,
            preview_provider=provider,
        )
    except CanonicalVoiceResolutionError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error_code": exc.code, "reason": str(exc)},
        ) from exc

    # Provider-qualified digest prevents traversal and cross-provider cache collisions.
    voice_digest = hashlib.sha256(
        f"{resolved_voice.provider}:{resolved_voice.voice_code}".encode("utf-8")
    ).hexdigest()
    folder_1 = voice_digest[:2]
    folder_2 = voice_digest[2:5]

    # Create the folder structure
    voice_sample_dir = os.path.join(VOICE_SAMPLES_DIR, folder_1, folder_2)
    os.makedirs(voice_sample_dir, exist_ok=True)

    sample_filename = f"{voice_digest}_sample-{category}.opus"
    sample_path = os.path.join(voice_sample_dir, sample_filename)

    if os.path.exists(sample_path):
        return FileResponse(sample_path, media_type="audio/ogg")

    rate_error = check_rate_limits(
        request,
        ip_limit=RLC.VOICE_SAMPLE_BY_IP,
        identifier=str(current_user.id),
        identifier_limit=RLC.VOICE_SAMPLE_BY_USER,
        action_name="voice_sample",
    )
    if rate_error:
        retry_after = int(rate_error.get("retry_after_seconds") or 0)
        raise HTTPException(
            status_code=429,
            detail=rate_error["message"],
            headers={"Retry-After": str(retry_after)},
        )

    try:
        sample_texts = [
            "Hello kids! Today we'll learn the colors of the rainbow. Are you ready for a colorful adventure?",
            "The stock index closed up 2%, driven by positive results in the technology sector.",
            "Breathe in deeply... and exhale slowly. Feel how the tension leaves your body with each breath. Relax, everything is fine.",
            "Hey! How was your day? Did you see last night's episode? I can't believe how it ended, I won't miss tomorrow's!",
            "Tears rolled down her cheeks as she held the letter, her hands trembling with every word she read. In the background, her cat looked at her strangely. Had Max left?",
            "The sun was setting on the horizon, painting the sky in golden and pink tones, while Maria walked along the beach, remembering the summers of her childhood.",
            "Incredible offer! For only 9 dollars, get two and pay for just one. Hurry, the offer ends today!",
            "Oxidative phosphorylation in the mitochondrial electron transport chain is the process by which most of the cellular ATP is synthesized through the generation of an electrochemical proton gradient.",
            "In multivariate data analysis, logistic regression is used to model the probability of an event based on other factors.",
            "To optimize team performance, it is crucial to establish SMART goals: Specific, Measurable, Achievable, Relevant, and Time-bound.",
            "A chill ran down his spine as he heard footsteps approaching in the darkness of the abandoned house. He looked back and there it was..",
            "Goal! What a spectacular play! The stadium erupts in an ovation as the team celebrates this crucial moment."
        ]

        sample_text = sample_texts[category]

        data = {
            "text": sample_text,
            "author": "bot",
            "conversationId": "sample"
        }
        audio_path, error = await handle_tts_request(
            None,
            data,
            current_user,
            is_whatsapp=True,
            sample_voice_id=sample_voice_id,
            tts_context="external",
            resolved_sample_voice=resolved_voice,
        )

        if error:
            raise HTTPException(status_code=500, detail=f"Error generating voice sample: {error}")

        # Move the generated .opus file to the voice samples folder
        shutil.move(audio_path, sample_path)

        # Delete the .mp3 file from the temporary cache if it exists
        mp3_path = audio_path.replace(".opus", ".mp3")
        if os.path.exists(mp3_path):
            os.remove(mp3_path)

        return FileResponse(sample_path, media_type="audio/ogg")

    except Exception as e:
        logger.error(f"Error generating voice sample: {str(e)}")
        raise HTTPException(status_code=500, detail="An error occurred while generating the voice sample")


async def cost_image(current_user, dalle_cost):
    user_id = current_user.id
    if await deduct_balance(user_id, dalle_cost):
        last_lock_error = None
        for attempt in range(DB_MAX_RETRIES):
            retry_needed = False
            wait_time = 0.0
            async with get_db_connection() as conn:
                transaction_started = False
                try:
                    await conn.execute('BEGIN IMMEDIATE')
                    transaction_started = True

                    await conn.execute('''
                        INSERT INTO SERVICE_USAGE (user_id, service_id, usage_quantity, cost)
                        VALUES (?, ?, 1, ?)
                    ''', (user_id, Cost.DALLE_SERVICE_ID, dalle_cost))

                    await conn.execute('''
                        UPDATE USER_DETAILS
                        SET total_cost = total_cost + ?, total_image_cost = total_image_cost + ?
                        WHERE user_id = ?
                    ''', (dalle_cost, dalle_cost, user_id))

                    # Record daily usage summary
                    await record_daily_usage(
                        user_id=user_id,
                        usage_type='image',
                        cost=dalle_cost,
                        units=1,
                        conn=conn
                    )

                    await conn.commit()
                    return
                except sqlite3.OperationalError as exc:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                        wait_time = DB_RETRY_DELAY_BASE * (attempt + 1)
                        logger.warning(
                            "Lock detected while recording image cost (user_id=%s, retry %s/%s, wait %.2fs)",
                            user_id,
                            attempt + 1,
                            DB_MAX_RETRIES,
                            wait_time,
                        )
                        last_lock_error = exc
                        retry_needed = True
                    else:
                        logger.error(f"Error executing image cost query: {exc}")
                        return
                except Exception as e:
                    if transaction_started:
                        try:
                            await conn.rollback()
                        except Exception:
                            pass
                    logger.error(f"Error executing image cost query: {e}")
                    return

            if retry_needed:
                await asyncio.sleep(wait_time)
                continue
            break

        if last_lock_error:
            logger.error(
                "Could not record image cost for user_id=%s after %s retries: %s",
                user_id,
                DB_MAX_RETRIES,
                last_lock_error,
            )


# Control panel for task management in Redis

import redis
from dramatiq.results import Results
from dramatiq.results.backends import RedisBackend


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
results_backend = RedisBackend(url=REDIS_URL)
broker.add_middleware(Results(backend=results_backend))


def serialize_redis_data(data):
    if isinstance(data, bytes):
        return data.decode('utf-8')
    elif isinstance(data, (set, list)):
        return [serialize_redis_data(item) for item in data]
    elif isinstance(data, dict):
        return {serialize_redis_data(k): serialize_redis_data(v) for k, v in data.items()}
    return data

async def get_messages_from_queue(queue_name):
    """
    Gets all messages from a specific queue.
    """
    messages = []
    try:
        # Get all message IDs from the queue
        message_ids = await redis_client.lrange(f"dramatiq:{queue_name}", 0, -1)
        message_ids = [msg_id.decode('utf-8') for msg_id in message_ids]

        # Get the message hash
        msgs_key = f"dramatiq:{queue_name}.msgs"
        if await redis_client.exists(msgs_key):
            msgs = await redis_client.hgetall(msgs_key)
            for msg_id in message_ids:
                msg_data = msgs.get(msg_id.encode('utf-8'))
                if msg_data:
                    messages.append(orjson.loads(msg_data.decode('utf-8')))
    except Exception as e:
        logger.error(f"Error getting messages from queue {queue_name}: {e}")
    return messages

@app.get("/admin/task-manager", response_class=HTMLResponse)
async def task_manager(request: Request, current_user: User = Depends(get_current_user)):
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this page")

    logger.debug("Accessing task manager")

    # Verify Redis connection
    redis_connected = False
    dramatiq_connected = False
    dramatiq_queues = []
    redis_keys = []

    try:
        redis_info = await redis_client.info()
        redis_connected = True
        logger.debug(f"Redis connection successful. Version: {redis_info['redis_version']}")
    except Exception as e:
        logger.error(f"Error connecting to Redis: {e}")

    try:
        dramatiq_queues = broker.get_declared_queues()
        dramatiq_connected = True
        logger.debug(f"Dramatiq connected. Declared queues: {dramatiq_queues}")
    except Exception as e:
        logger.error(f"Error verifying Dramatiq: {e}")

    # Get all tasks
    pending_tasks = []
    active_tasks = []
    failed_tasks = []
    delayed_tasks = []
    aged_out_tasks = []  # For tasks that exceeded their age limits

    if redis_connected:
        try:
            # Get all Redis keys related to Dramatiq
            redis_keys = serialize_redis_data(await redis_client.keys("dramatiq:*"))

            # Pending tasks
            pending_tasks = await get_messages_from_queue("default.DQ")

            # Active tasks
            active_tasks = await get_messages_from_queue("default.active")

            # Failed tasks
            failed_tasks = await get_messages_from_queue("default.failed")

            # Delayed tasks
            delayed_tasks = await get_messages_from_queue("default.DQ.delayed")

            # Tasks that exceeded their age limit (AgeLimit)
            async for task_key in redis_client.scan_iter("dramatiq:__state__.*"):
                task_state = serialize_redis_data(await redis_client.hgetall(task_key))
                if 'max_age' in task_state and int(task_state.get('age', 0)) > int(task_state.get('max_age', 0)):
                    aged_out_tasks.append(task_state)

            logger.debug(f"Aged out tasks (exceeded age limit): {aged_out_tasks}")

        except Exception as e:
            logger.error(f"Error getting tasks from Redis: {e}")

    context = await get_template_context(request, current_user)
    context.update({
        "pending_tasks": pending_tasks,
        "active_tasks": active_tasks,
        "failed_tasks": failed_tasks,
        "delayed_tasks": delayed_tasks,
        "aged_out_tasks": aged_out_tasks,
        "redis_connected": redis_connected,
        "dramatiq_connected": dramatiq_connected,
        "redis_keys": redis_keys,
        "dramatiq_queues": dramatiq_queues if dramatiq_connected else []
    })

    return templates.TemplateResponse("task_manager.html", context)

@app.get("/admin/inspect-redis-key")
async def inspect_redis_key(key: str, current_user: User = Depends(get_current_user)):
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")

    try:
        key_type = (await redis_client.type(key)).decode('utf-8')

        if key_type == 'string':
            value = await redis_client.get(key)
        elif key_type == 'list':
            value = await redis_client.lrange(key, 0, -1)
        elif key_type == 'set':
            value = await redis_client.smembers(key)
        elif key_type == 'zset':
            value = await redis_client.zrange(key, 0, -1, withscores=True)
        elif key_type == 'hash':
            value = await redis_client.hgetall(key)
        else:
            value = "Unsupported key type"

        value = serialize_redis_data(value)

        return JSONResponse({
            "key": key,
            "type": key_type,
            "value": value
        })
    except Exception as e:
        logger.error(f"Error inspecting Redis key {key}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/admin/delete-task")
async def delete_task(request: Request, current_user: User = Depends(get_current_user)):
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")

    data = await request.json()
    task_id = data.get("task_id")
    queue = data.get("queue")

    try:
        if queue == "pending":
            await redis_client.lrem("dramatiq:default.DQ", 0, task_id)
        elif queue == "failed":
            await redis_client.lrem("dramatiq:default.failed", 0, task_id)
        logger.info(f"Task {task_id} removed from queue {queue}")
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"Error deleting task {task_id}: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@app.post("/admin/retry-task")
async def retry_task(request: Request, current_user: User = Depends(get_current_user)):
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")

    data = await request.json()
    task_id = data.get("task_id")

    try:
        # Retry task by simply moving it from failed list to main queue
        # First, get the failed message data
        failed_msg_key = "dramatiq:default.failed.msgs"
        msg_data = await redis_client.hget(failed_msg_key, task_id)
        if msg_data:
            # Move the message from failed to main queue
            await redis_client.lpush("dramatiq:default.DQ", task_id)
            # Optionally, remove the message from the failed hash
            await redis_client.hdel(failed_msg_key, task_id)
            logger.info(f"Task {task_id} retried")
            return JSONResponse({"success": True})
        else:
            logger.warning(f"Task {task_id} not found in failed queue.")
            return JSONResponse({"success": False, "error": "Task not found in failed queue."})
    except Exception as e:
        logger.error(f"Error retrying task {task_id}: {e}")
        return JSONResponse({"success": False, "error": str(e)})

@app.post("/admin/clear-dramatiq")
async def clear_dramatiq(current_user: User = Depends(get_current_user)):
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")

    try:
        # Get all keys related to Dramatiq
        keys = await redis_client.keys("dramatiq:*")
        if keys:
            await redis_client.delete(*keys)
            logger.info("All Dramatiq keys have been deleted.")
        return JSONResponse({"success": True})
    except Exception as e:
        logger.error(f"Error cleaning Dramatiq: {e}")
        return JSONResponse({"success": False, "error": str(e)})


@app.get("/admin/security", response_class=HTMLResponse)
async def admin_security_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin security operations panel."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")

    context = await get_template_context(request, current_user)
    context.update({
        "events_default_limit": 100,
        "blocked_ips_default_limit": 200,
    })
    return templates.TemplateResponse("admin/security_operations.html", context)


@app.get("/admin/security/stats")
async def admin_security_stats(current_user: User = Depends(get_current_user)):
    """Security tracker telemetry (backend, counters, blocked IPs)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    stats = await get_security_stats_async()
    return JSONResponse(stats)


@app.get("/admin/security/blocked-ips")
async def admin_security_blocked_ips(
    limit: int = Query(default=200, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    """List currently blocked IPs with metadata and Cloudflare sync status."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    blocked_ips = await get_security_blocked_ips_async(limit=limit)
    return JSONResponse({"blocked_ips": blocked_ips, "count": len(blocked_ips)})


@app.get("/admin/security/events")
async def admin_security_events(
    limit: int = Query(default=50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    """Recent security block events."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    events = await get_security_events_async(limit=limit)
    return JSONResponse({"events": events, "count": len(events)})


@app.get("/admin/security/ip-status")
async def admin_security_ip_status(
    ip: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
):
    """Check if an IP is currently blocked by middleware tracker."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    try:
        blocked = await is_ip_blocked_async(ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ip": ip, "blocked": blocked})


@app.post("/admin/security/block-ip")
async def admin_security_block_ip(
    request: Request,
    payload: SecurityManualBlockRequest,
    current_user: User = Depends(get_current_user),
):
    """Manually block an IP in security tracker."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    try:
        result = await manually_block_ip_async(
            ip=payload.ip,
            hours=payload.hours,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await log_admin_action(
        admin_id=current_user.id,
        action_type="security_manual_block_ip",
        request=request,
        target_resource_type="ip_security",
        details=f"ip={result.get('ip')};hours={payload.hours};reason={payload.reason}",
    )
    return JSONResponse({"success": True, "result": result})


@app.post("/admin/security/unblock-ip")
async def admin_security_unblock_ip(
    request: Request,
    payload: SecurityManualUnblockRequest,
    current_user: User = Depends(get_current_user),
):
    """Manually unblock an IP in security tracker."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    try:
        result = await manually_unblock_ip_async(payload.ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await log_admin_action(
        admin_id=current_user.id,
        action_type="security_manual_unblock_ip",
        request=request,
        target_resource_type="ip_security",
        details=f"ip={payload.ip};unblocked={result.get('unblocked')};cf_deleted={result.get('cloudflare_deleted')}",
    )
    return JSONResponse({"success": True, **result})


@app.post("/admin/security/retry-sync")
async def admin_security_retry_sync(
    request: Request,
    payload: SecurityRetrySyncRequest,
    current_user: User = Depends(get_current_user),
):
    """Retry Cloudflare sync for a blocked IP."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")

    try:
        result = await retry_cloudflare_sync_async(ip=payload.ip, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await log_admin_action(
        admin_id=current_user.id,
        action_type="security_retry_cloudflare_sync",
        request=request,
        target_resource_type="ip_cloudflare_sync",
        details=f"ip={payload.ip};status={result.get('status')};rule_id={result.get('rule_id')}",
    )
    return JSONResponse({"success": True, "result": result})


# ---------------------------------------------------------------------------
# IP Reputation admin endpoints
# ---------------------------------------------------------------------------

@app.get("/admin/security/reputation/stats")
async def admin_security_reputation_stats(current_user: User = Depends(get_current_user)):
    """IP Reputation system summary stats."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    from middleware.ip_reputation import reputation_manager
    stats = await reputation_manager.get_stats()
    return JSONResponse(stats)


@app.get("/admin/security/reputation/top-ips")
async def admin_security_reputation_top_ips(
    limit: int = Query(default=500, ge=1, le=500),
    current_user: User = Depends(get_current_user),
):
    """Top scored IPs by reputation."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    from middleware.ip_reputation import reputation_manager
    top_ips = await reputation_manager.get_top_ips(limit=limit)
    return JSONResponse({"top_ips": top_ips, "count": len(top_ips)})


@app.get("/admin/security/reputation/ip-detail")
async def admin_security_reputation_ip_detail(
    ip: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
):
    """Full reputation record for a single IP."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    from middleware.ip_reputation import reputation_manager
    from middleware.security import _normalize_ip
    try:
        normalized_ip = _normalize_ip(ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    detail = await reputation_manager.get_ip_detail(normalized_ip)
    if detail is None:
        return JSONResponse({"found": False, "ip": normalized_ip})
    return JSONResponse({"found": True, **detail})


@app.post("/admin/security/reputation/reset-score")
async def admin_security_reputation_reset_score(
    request: Request,
    payload: SecurityResetScoreRequest,
    current_user: User = Depends(get_current_user),
):
    """Reset reputation score for an IP (keeps ban history)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Only administrators can access this function")
    from middleware.ip_reputation import reputation_manager
    success = await reputation_manager.reset_ip_score(payload.ip)

    await log_admin_action(
        admin_id=current_user.id,
        action_type="security_reputation_reset_score",
        request=request,
        target_resource_type="ip_reputation",
        details=f"ip={payload.ip};success={success}",
    )
    return JSONResponse({"success": success, "ip": payload.ip})


# ---------------------------------------------------------------------------
# Watchdog admin panel
# ---------------------------------------------------------------------------

@app.get("/admin/watchdog-events", response_class=HTMLResponse)
async def admin_watchdog_events(request: Request, current_user: User = Depends(get_current_user)):
    """Admin panel to inspect watchdog evaluation events."""
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")

    # Read optional filters from query params
    f_prompt_id = request.query_params.get("prompt_id", "").strip()
    f_severity = request.query_params.get("severity", "").strip()
    f_event_type = request.query_params.get("event_type", "").strip()
    f_conversation_id = request.query_params.get("conversation_id", "").strip()

    # Pagination params
    ALLOWED_PER_PAGE = [25, 50, 100]
    try:
        per_page = int(request.query_params.get("per_page", "25"))
    except (ValueError, TypeError):
        per_page = 25
    if per_page not in ALLOWED_PER_PAGE:
        per_page = 25

    try:
        page = int(request.query_params.get("page", "1"))
    except (ValueError, TypeError):
        page = 1
    if page < 1:
        page = 1

    # Build dynamic WHERE clause
    conditions = []
    params = []
    if f_prompt_id.isdigit():
        conditions.append("we.prompt_id = ?")
        params.append(int(f_prompt_id))
    if f_severity:
        conditions.append("we.severity = ?")
        params.append(f_severity)
    if f_event_type:
        conditions.append("we.event_type = ?")
        params.append(f_event_type)
    if f_conversation_id.isdigit():
        conditions.append("we.conversation_id = ?")
        params.append(int(f_conversation_id))

    where_clause = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params_tuple = tuple(params)

    async with get_db_connection(readonly=True) as conn:
        # Total count for pagination
        cursor = await conn.execute(
            f"SELECT COUNT(*) FROM WATCHDOG_EVENTS we{where_clause}",
            params_tuple,
        )
        total_events = (await cursor.fetchone())[0]
        total_pages = max(1, math.ceil(total_events / per_page))

        # Clamp page to valid range
        if page > total_pages:
            page = total_pages

        offset = (page - 1) * per_page

        # Stats across the full filtered dataset (not just current page)
        stat_base = f"SELECT COUNT(*) FROM WATCHDOG_EVENTS we{where_clause}"
        stat_condition = " AND " if conditions else " WHERE "

        cursor = await conn.execute(
            f"{stat_base}{stat_condition}we.action_taken = ?",
            params_tuple + ("hint_generated",),
        )
        hints = (await cursor.fetchone())[0]

        cursor = await conn.execute(
            f"{stat_base}{stat_condition}we.action_taken = ?",
            params_tuple + ("error",),
        )
        errors = (await cursor.fetchone())[0]

        # Paginated event rows
        cursor = await conn.execute(
            f"""SELECT we.*, c.chat_name, p.name AS prompt_name
                FROM WATCHDOG_EVENTS we
                LEFT JOIN CONVERSATIONS c ON we.conversation_id = c.id
                LEFT JOIN PROMPTS p ON we.prompt_id = p.id
                {where_clause}
                ORDER BY we.created_at DESC LIMIT ? OFFSET ?""",
            params_tuple + (per_page, offset),
        )
        events = [dict(row) for row in await cursor.fetchall()]

        # Prompts list for filter dropdown
        cursor = await conn.execute("SELECT id, name FROM PROMPTS ORDER BY name ASC")
        prompts = [dict(row) for row in await cursor.fetchall()]

    # "Showing X-Y of Z" range
    showing_start = offset + 1 if total_events > 0 else 0
    showing_end = offset + len(events)

    context = await get_template_context(request, current_user)
    context.update({
        "events": events,
        "prompts": prompts,
        "stats": {"hints": hints, "errors": errors},
        "filters": {
            "prompt_id": int(f_prompt_id) if f_prompt_id.isdigit() else None,
            "severity": f_severity or None,
            "event_type": f_event_type or None,
            "conversation_id": int(f_conversation_id) if f_conversation_id.isdigit() else None,
        },
        "event_types": ["drift", "rabbit_hole", "stuck", "inconsistency", "saturation", "none", "error", "security", "role_breach", "role_breach_hard", "role_breach_soft"],
        "severities": ["info", "nudge", "redirect", "alert"],
        "page": page,
        "per_page": per_page,
        "total_events": total_events,
        "total_pages": total_pages,
        "showing_start": showing_start,
        "showing_end": showing_end,
    })
    return templates.TemplateResponse("admin_watchdog.html", context)


@app.post("/api/send-verification-code")
async def send_verification_code(
    payload: PhoneVerificationRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if payload.purpose not in {
        PURPOSE_CREATE_USER,
        PURPOSE_PROFILE_PHONE_CHANGE,
    }:
        raise HTTPException(status_code=400, detail="Invalid phone verification purpose.")

    if (
        payload.purpose == PURPOSE_PROFILE_PHONE_CHANGE
        and not has_recent_authentication(current_user)
    ):
        raise HTTPException(
            status_code=403,
            detail="Please sign in again before changing your phone number.",
        )

    if payload.purpose == PURPOSE_CREATE_USER:
        async with get_db_connection(readonly=True) as conn:
            actor_role = await get_live_user_role(conn, current_user.id)
        if actor_role not in {"admin", "user"}:
            raise HTTPException(
                status_code=403,
                detail="You cannot verify a phone number for user creation.",
            )

    try:
        phone_number = normalize_phone_number(payload.phone)
        async with get_db_connection(readonly=True) as conn:
            if payload.purpose == PURPOSE_PROFILE_PHONE_CHANGE:
                cursor = await conn.execute(
                    "SELECT id FROM USERS WHERE phone_number = ? AND id != ?",
                    (phone_number, current_user.id),
                )
            else:
                cursor = await conn.execute(
                    "SELECT id FROM USERS WHERE phone_number = ?",
                    (phone_number,),
                )
            if await cursor.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="Phone number already in use.",
                )

        challenge = await request_phone_verification(
            actor_user_id=current_user.id,
            phone_number=phone_number,
            purpose=payload.purpose,
            request_ip=get_client_ip(request),
            twilio_client=async_twilio,
            service_sid=service_sid,
        )
        return {
            "status": challenge.status,
            "challenge_id": challenge.challenge_id,
            "expires_in": challenge.expires_in,
        }
    except HTTPException:
        raise
    except PhoneVerificationError as exc:
        headers = None
        if isinstance(exc, PhoneVerificationRateLimitError):
            headers = {"Retry-After": str(exc.retry_after)}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from exc

@app.post("/api/verify-code")
async def verify_code(
    verification_request: VerificationCodeRequest,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if verification_request.purpose not in {
        PURPOSE_CREATE_USER,
        PURPOSE_PROFILE_PHONE_CHANGE,
    }:
        raise HTTPException(status_code=400, detail="Invalid phone verification purpose.")

    if (
        verification_request.purpose == PURPOSE_PROFILE_PHONE_CHANGE
        and not has_recent_authentication(current_user)
    ):
        raise HTTPException(
            status_code=403,
            detail="Please sign in again before changing your phone number.",
        )

    if verification_request.purpose == PURPOSE_CREATE_USER:
        async with get_db_connection(readonly=True) as conn:
            actor_role = await get_live_user_role(conn, current_user.id)
        if actor_role not in {"admin", "user"}:
            raise HTTPException(
                status_code=403,
                detail="You cannot verify a phone number for user creation.",
            )

    try:
        challenge = await verify_phone_code(
            actor_user_id=current_user.id,
            challenge_id=verification_request.challenge_id,
            code=verification_request.code,
            phone_number=verification_request.phone,
            purpose=verification_request.purpose,
            twilio_client=async_twilio,
            service_sid=service_sid,
        )
        return JSONResponse(
            content={
                "status": challenge.status,
                "challenge_id": challenge.challenge_id,
            },
            status_code=200,
        )
    except PhoneVerificationError as exc:
        headers = None
        if isinstance(exc, PhoneVerificationRateLimitError):
            headers = {"Retry-After": str(exc.retry_after)}
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=headers,
        ) from exc

@app.get("/api/current-user-id")
async def get_current_user_id(current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    return {"user_id": current_user.id}

@app.post("/api/select-prompt")
async def select_prompt(
    request: Request,
    prompt_id: int = Form(...),
    current_user: User = Depends(get_current_user)
):
    if current_user is None:
        return templates.TemplateResponse("login.html", {"request": request, "captcha": get_captcha_config(), "google_oauth_available": bool(GOOGLE_CLIENT_ID)})


    async with get_db_connection() as conn:
        async with conn.cursor() as cursor:
            # Verify if prompt exists and get basic information
            await cursor.execute("""
                SELECT id, name, public
                FROM PROMPTS
                WHERE id = ?
            """, (prompt_id,))
            prompt = await cursor.fetchone()

            if not prompt:
                raise HTTPException(status_code=404, detail="Prompt not found")

            prompt_id, prompt_name, is_public = prompt

            # Centralized access check (respects category_access + public_prompts_access)
            has_access = await can_user_access_prompt(current_user, prompt_id, cursor)

            if not has_access:
                raise HTTPException(status_code=403, detail="Access denied")

            # Update user's current prompt selection in DB
            await cursor.execute(
                "UPDATE USER_DETAILS SET current_prompt_id = ? WHERE user_id = ?",
                (prompt_id, current_user.id)
            )
            await conn.commit()

            # Get permission details for response metadata
            is_admin = await current_user.is_admin
            await cursor.execute("""
                SELECT permission_level
                FROM PROMPT_PERMISSIONS
                WHERE prompt_id = ? AND user_id = ?
                ORDER BY CASE permission_level WHEN 'owner' THEN 1 WHEN 'edit' THEN 2 WHEN 'access' THEN 3 END
                LIMIT 1
            """, (prompt_id, current_user.id))
            permission = await cursor.fetchone()
            is_owner = permission and permission[0] == 'owner'
            is_editor = permission and permission[0] == 'edit'

    return JSONResponse({
        "success": True,
        "user_id": current_user.id,
        "prompt_id": prompt_id,
        "prompt_name": prompt_name,
        "is_public": is_public,
        "is_owner": is_owner,
        "is_editor": is_editor,
        "is_admin": is_admin
    })


@app.get("/register")
async def custom_domain_register(request: Request):
    """
    Registration page for custom domains.
    If request comes from a custom domain, render registration for that prompt.
    Otherwise, fall through to user registration.
    """
    # Check if this is a custom domain request
    if not getattr(request.state, 'custom_domain', False):
        # Not a custom domain - use user registration
        response = templates.TemplateResponse("register_public.html", {
            "request": request,
            "target_role": "user",
            "prompt": None,
            "login_url": "/login",
            "captcha": get_captcha_config(),
            "google_oauth_available": bool(GOOGLE_CLIENT_ID)
        })
        response.headers["X-Robots-Tag"] = "noindex"
        return response

    if not marketplace_public_landings_enabled():
        return landing_404_response()

    try:
        return await render_custom_domain_register(
            request,
            captcha=get_captcha_config(),
            google_oauth_available=bool(GOOGLE_CLIENT_ID),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving custom domain register: {e}")
        raise HTTPException(status_code=500, detail="Registration error")


@app.api_route("/login", methods=["GET", "POST"])
async def custom_domain_login(request: Request):
    """
    Login page — handles both main site and custom domain requests.
    Follows the same pattern as custom_domain_register().
    """
    # If already authenticated, redirect to home
    if request.method == "GET":
        current_user = await get_current_user(request)
        if current_user is not None:
            return RedirectResponse(url="/home", status_code=302)

    if not getattr(request.state, 'custom_domain', False):
        # Not a custom domain — main site login
        response = await handle_login_request(
            request,
            prompt_context=None,
            login_url="/login",
            register_url="/register"
        )
        response.headers["X-Robots-Tag"] = "noindex"
        return response

    # Custom domain — login for this specific prompt
    if not marketplace_public_landings_enabled():
        return landing_404_response()

    try:
        response = await render_custom_domain_login(
            request,
            login_handler=handle_login_request,
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving custom domain login: {e}")
        raise HTTPException(status_code=500, detail="Login error")


# =============================================================================
# Google OAuth (must be before catch-all route)
# =============================================================================

def _get_google_flow(redirect_uri: str) -> Flow:
    """Create Google OAuth flow with dynamic redirect URI."""
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["openid", "email", "profile"],
        redirect_uri=redirect_uri
    )


def _build_redirect_uri(request: Request) -> str:
    """Build OAuth redirect URI from current request."""
    return f"{get_request_base_url(request).rstrip('/')}/auth/google/callback"


@app.get("/auth/google")
async def auth_google(request: Request, prompt_id: int = None, pack_id: int = None, next: str = None):
    """
    Initiate Google OAuth flow.
    Saves prompt_id/pack_id in session to determine role after callback.
    If both pack_id and prompt_id are provided, pack_id takes precedence.
    """
    # Rate limiting
    rate_error = check_rate_limits(
        request,
        ip_limit=RLC.OAUTH_BY_IP,
        action_name="oauth_start"
    )
    if rate_error:
        return RedirectResponse(url="/login?error=rate_limited")

    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        logger.error("Google OAuth not configured")
        raise HTTPException(status_code=500, detail="Google OAuth not configured")

    if prompt_id is not None or pack_id is not None:
        if not marketplace_public_landings_enabled() or not marketplace_checkout_enabled():
            raise HTTPException(status_code=404, detail="Not found")

    # If both pack_id and prompt_id provided, pack_id takes precedence
    if pack_id is not None and prompt_id is not None:
        prompt_id = None

    # Validate pack_id if provided: must exist, be published, and public
    if pack_id is not None:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT id FROM PACKS WHERE id = ? AND status = 'published' AND is_public = 1",
                (pack_id,)
            )
            result = await cursor.fetchone()
            if not result:
                pack_id = None

    # Validate prompt_id if provided: must exist and be public
    if prompt_id is not None:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT id FROM PROMPTS WHERE id = ? AND public = 1",
                (prompt_id,)
            )
            result = await cursor.fetchone()
            if not result:
                prompt_id = None

    redirect_uri = _build_redirect_uri(request)
    flow = _get_google_flow(redirect_uri)

    # Save context in session
    request.session["oauth_prompt_id"] = prompt_id
    request.session["oauth_pack_id"] = pack_id
    request.session["oauth_redirect_uri"] = redirect_uri
    if next and next.startswith("/") and not next.startswith("//"):
        request.session["oauth_next"] = next
    else:
        request.session.pop("oauth_next", None)

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account"
    )

    request.session["oauth_state"] = state

    return RedirectResponse(authorization_url)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str = None, state: str = None, error: str = None):
    """
    Handle Google OAuth callback.
    Creates new user or logs in existing user.

    Error codes:
    - oauth_denied: User cancelled or denied OAuth
    - invalid_state: CSRF protection triggered
    - no_email: Google didn't provide email
    - account_disabled: User account is disabled
    - email_linked_other_google: Email already linked to different Google account
    - create_failed: Failed to create new user
    - oauth_failed: Generic OAuth error
    - rate_limited: Too many attempts
    """
    try:
        return await _auth_google_callback_inner(request, code, state, error)
    except Exception as e:
        logger.error(f"[OAUTH CALLBACK] Unhandled exception: {type(e).__name__}: {e}", exc_info=True)
        return RedirectResponse(url="/login?error=oauth_failed")


async def _auth_google_callback_inner(request: Request, code: str, state: str, error: str):
    # Rate limiting for callback
    rate_error = check_rate_limits(
        request,
        ip_limit=RLC.OAUTH_BY_IP,
        action_name="oauth_callback"
    )
    if rate_error:
        return RedirectResponse(url="/login?error=rate_limited")

    # Check failure limit for callback
    fail_error = check_failure_limit(request, "oauth_callback", RLC.OAUTH_CALLBACK_FAILURES)
    if fail_error:
        return RedirectResponse(url="/login?error=rate_limited")

    # Handle OAuth errors (user cancelled, denied, etc.)
    if error:
        logger.warning(f"Google OAuth error: {error}")
        record_failure(request, "oauth_callback")
        return RedirectResponse(url="/login?error=oauth_denied")

    # Verify state (CSRF protection)
    stored_state = request.session.get("oauth_state")
    if not stored_state or state != stored_state:
        logger.warning("Invalid OAuth state - possible CSRF attempt")
        record_failure(request, "oauth_callback")
        return RedirectResponse(url="/login?error=invalid_state")

    # Get stored context
    prompt_id = request.session.pop("oauth_prompt_id", None)
    pack_id = request.session.pop("oauth_pack_id", None)
    redirect_uri = request.session.pop("oauth_redirect_uri", None)
    oauth_next = request.session.pop("oauth_next", None)
    request.session.pop("oauth_state", None)

    if (prompt_id or pack_id) and (
        not marketplace_public_landings_enabled() or not marketplace_checkout_enabled()
    ):
        logger.warning("OAuth acquisition context ignored because marketplace acquisition is disabled")
        prompt_id = None
        pack_id = None
        oauth_next = None

    if not redirect_uri:
        redirect_uri = _build_redirect_uri(request)

    try:
        # Exchange code for tokens
        flow = _get_google_flow(redirect_uri)
        flow.fetch_token(code=code)

        credentials = flow.credentials

        # Verify and decode ID token
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID
        )

        google_id = id_info["sub"]
        email = id_info.get("email", "").lower()
        name = id_info.get("name", "")

        if not email:
            logger.error("Google did not provide email")
            record_failure(request, "oauth_callback")
            return RedirectResponse(url="/login?error=no_email")

        # Determine target role based on context
        target_role = "customer" if (prompt_id or pack_id) else "user"

        # === CASE 1: User with this google_id already exists ===
        user = await get_user_by_google_id(google_id)

        if user:
            # Check if account is enabled
            if not user.is_enabled:
                logger.warning(f"Disabled user {user.id} attempted Google OAuth login")
                record_failure(request, "oauth_callback")
                return RedirectResponse(url="/login?error=account_disabled")

            # Handle pack access for existing user
            redirect_url = None
            if pack_id:
                redirect_url = await handle_pack_for_existing_user(pack_id, user.id)
            if redirect_url is None:
                redirect_url = oauth_next

            # Direct login
            logger.info(f"Google OAuth login for existing user {user.id}")
            user_info = await create_user_info(user, used_magic_link=False)
            default_redirect = await get_after_login_redirect(user.id)
            return create_login_response(user_info, redirect_url=redirect_url, default_redirect=default_redirect)

        # === CASE 2: No user with google_id, check by email ===
        user = await get_user_by_email(email)

        if user:
            # Check if account is enabled
            if not user.is_enabled:
                logger.warning(f"Disabled user {user.id} attempted Google OAuth via email linking")
                record_failure(request, "oauth_callback")
                return RedirectResponse(url="/login?error=account_disabled")

            # Check if email is already linked to a DIFFERENT Google account
            if user.google_id and user.google_id != google_id:
                logger.warning(f"Email {email} already linked to different Google account")
                record_failure(request, "oauth_callback")
                return RedirectResponse(url="/login?error=email_linked_other_google")

            # Link Google account to existing user
            await update_user_google_id(user.id, google_id, "google_linked")
            user = await get_user_by_id(user.id)
            if not user:
                raise RuntimeError("Linked user could not be reloaded")
            logger.info(f"Linked Google account to existing user {user.id}")

            # Handle pack access for existing user
            redirect_url = None
            if pack_id:
                redirect_url = await handle_pack_for_existing_user(pack_id, user.id)
            if redirect_url is None:
                redirect_url = oauth_next

            user_info = await create_user_info(user, used_magic_link=False)
            default_redirect = await get_after_login_redirect(user.id)
            return create_login_response(user_info, redirect_url=redirect_url, default_redirect=default_redirect)

        # === CASE 3: New user - create account ===
        # Generate unique username
        username = await generate_unique_username(email)

        # Get default LLM ID
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT id
                FROM LLM
                WHERE COALESCE(enabled, 1) = 1
                  AND machine != 'GPTSub'
                ORDER BY id
                LIMIT 1
                """
            )
            llm_row = await cursor.fetchone()
            default_llm_id = llm_row[0] if llm_row else 1

        # Resolve pack context for new user registration
        landing_config = None
        prompt_owner_id = None
        paid_pack_landing_url = None
        ucr_creator_id = None
        analytics_pack_id = pack_id  # Preserve for analytics even if pack_id is cleared

        if pack_id:
            resolved = await resolve_pack_oauth_context(pack_id)
            r_pack_id, r_prompt_id, r_is_paid, r_config, r_owner_id, r_paid_url = resolved

            if r_pack_id is None:
                # Invalid pack - treat as normal registration
                pack_id = None
                analytics_pack_id = None
            elif r_is_paid:
                # Paid pack - create user with defaults, redirect to purchase
                paid_pack_landing_url = r_paid_url
                pack_id = None  # Don't apply config
            else:
                # Free pack - apply landing config
                prompt_id = r_prompt_id
                landing_config = r_config
                prompt_owner_id = r_owner_id
                # Get pack creator for UCR (independent of billing mode)
                async with get_db_connection(readonly=True) as ucr_rd_conn:
                    ucr_cr = await ucr_rd_conn.execute("SELECT created_by_user_id FROM PACKS WHERE id = ?", (pack_id,))
                    ucr_row = await ucr_cr.fetchone()
                    ucr_creator_id = ucr_row[0] if ucr_row else None

        if not ucr_creator_id and prompt_id:
            try:
                ucr_creator_id = await get_prompt_owner_id(prompt_id)
            except Exception:
                pass

        # Standalone prompt landing (not pack-derived): branch free vs paid,
        # mirroring the email/password path in verify_email. A paid prompt must
        # never be granted at registration -> create the user with defaults and
        # route them to the purchase flow. A free prompt applies its landing
        # registration config so OAuth and email registrants behave identically.
        # landing_config is only non-None here for free packs (resolve_pack_oauth_context
        # returns a config dict), so `landing_config is None` isolates direct prompt landings.
        analytics_prompt_id = None  # Preserve for analytics even if prompt_id is cleared (paid prompt)
        paid_prompt_purchase_url = None
        if prompt_id and landing_config is None:
            try:
                async with get_db_connection(readonly=True) as price_conn:
                    price_cursor = await price_conn.execute(
                        "SELECT purchase_price FROM PROMPTS WHERE id = ?",
                        (prompt_id,),
                    )
                    price_row = await price_cursor.fetchone()
                prompt_purchase_price = float(price_row[0]) if price_row and price_row[0] else 0.0

                if prompt_purchase_price > 0:
                    # Paid prompt: keep defaults (no free config/access), preserve
                    # the id for analytics, and route to purchase. No creator
                    # relationship here (it is recorded when the purchase completes),
                    # so drop the eagerly-resolved owner to match verify_email.
                    analytics_prompt_id = prompt_id
                    paid_prompt_purchase_url = f"/purchase/prompt/{prompt_id}"
                    logger.info(f"Prompt {prompt_id} is paid - OAuth user created with defaults, purchase required")
                    prompt_id = None
                    ucr_creator_id = None
                else:
                    # Free prompt: apply the landing registration config via the
                    # same helper the email path uses, so both registration paths
                    # produce identical setup.
                    landing_config = await get_landing_registration_config(prompt_id)
                    if landing_config.get("billing_mode") == "user_pays":
                        prompt_owner_id = ucr_creator_id
            except Exception as config_err:
                logger.warning(f"Could not get landing config for prompt {prompt_id}: {config_err}")
                # Continue with defaults

        # Create user with pack config if available, otherwise with defaults
        if landing_config:
            # Prepare category_access
            category_access = landing_config.get("category_access")
            if isinstance(category_access, list):
                category_access = orjson.dumps(category_access).decode('utf-8')

            default_llm_id_cfg = landing_config.get("default_llm_id") or landing_config.get("_prompt_forced_llm_id") or default_llm_id

            user_id = await add_user(
                username=username,
                prompt_id=prompt_id,
                all_prompts_access=False,
                public_prompts_access=landing_config.get("public_prompts_access", True),
                llm_id=default_llm_id_cfg,
                allow_file_upload=landing_config.get("allow_file_upload", False),
                allow_image_generation=landing_config.get("allow_image_generation", False),
                balance=landing_config.get("initial_balance", 0.0),
                phone=None,
                role_name=target_role,
                authentication_mode="magic_link_password",
                initial_password=None,
                can_change_password=True,
                email=email,
                company_id=None,
                current_user=None,
                category_access=category_access,
                billing_account_id=prompt_owner_id if landing_config.get("billing_mode") == "user_pays" else None,
                billing_limit=landing_config.get("billing_limit") if landing_config.get("billing_mode") == "user_pays" else None,
                billing_limit_action=landing_config.get("billing_limit_action", "block") if landing_config.get("billing_mode") == "user_pays" else "block",
                billing_auto_refill_amount=landing_config.get("billing_auto_refill_amount", 10.0) if landing_config.get("billing_mode") == "user_pays" else 10.0,
                billing_max_limit=landing_config.get("billing_max_limit") if landing_config.get("billing_mode") == "user_pays" else None,
                initial_balance_funder_id=(
                    ucr_creator_id
                    if float(landing_config.get("initial_balance", 0.0)) > 0
                    else None
                ),
            )
        else:
            # Original add_user call (no pack config)
            user_id = await add_user(
                username=username,
                prompt_id=prompt_id,
                all_prompts_access=False,
                public_prompts_access=True,
                llm_id=default_llm_id,
                allow_file_upload=(target_role == "user"),
                allow_image_generation=(target_role == "user"),
                balance=0.0,
                phone=None,
                role_name=target_role,
                authentication_mode="magic_link_password",
                initial_password=None,
                can_change_password=True,
                email=email,
                company_id=None,
                current_user=None
            )

        if not user_id:
            logger.error(f"Failed to create user from Google OAuth for email {email}")
            record_failure(request, "oauth_callback")
            return RedirectResponse(url="/login?error=create_failed")

        # Set Google ID and auth_provider
        await update_user_google_id(user_id, google_id, "google")

        # Record creator relationship
        if ucr_creator_id:
            try:
                async with get_db_connection() as ucr_conn:
                    ucr_cursor = await ucr_conn.cursor()
                    from common import upsert_creator_relationship
                    source = ('pack', pack_id) if pack_id else ('prompt', prompt_id)
                    await upsert_creator_relationship(ucr_cursor, user_id, ucr_creator_id, 'registered_via', source[0], source[1])
                    await ucr_conn.commit()
            except Exception as ucr_err:
                logger.warning(f"Could not record creator relationship for user {user_id}: {ucr_err}")

        # Record captive domain relationship if user is captive
        oauth_public_access = landing_config.get("public_prompts_access", True) if landing_config else True
        if not oauth_public_access and prompt_id:
            try:
                async with get_db_connection() as cd_conn:
                    cursor = await cd_conn.execute(
                        "SELECT id FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ? AND is_active = 1",
                        (prompt_id,)
                    )
                    domain_row = await cursor.fetchone()
                    if domain_row:
                        await cd_conn.execute(
                            "INSERT OR IGNORE INTO USER_CAPTIVE_DOMAINS (user_id, domain_id, prompt_id) VALUES (?, ?, ?)",
                            (user_id, domain_row[0], prompt_id)
                        )
                        await cd_conn.commit()
                        logger.info(f"Recorded captive domain {domain_row[0]} for OAuth user {user_id} (prompt {prompt_id})")
            except Exception as cd_err:
                # Compensate: if we can't track captivity, don't make user captive
                logger.error(f"Failed to record captive domain for OAuth user {user_id}, reverting to public access: {cd_err}")
                try:
                    async with get_db_connection() as revert_conn:
                        await revert_conn.execute(
                            "UPDATE USER_DETAILS SET public_prompts_access = 1 WHERE user_id = ?",
                            (user_id,)
                        )
                        await revert_conn.commit()
                except Exception as revert_err:
                    logger.error(f"Failed to revert captive status for OAuth user {user_id}: {revert_err}")

        # Grant pack access for free pack registration
        if pack_id:
            try:
                async with get_db_connection() as conn:
                    await grant_pack_entitlement(
                        conn,
                        user_id=user_id,
                        pack_id=pack_id,
                        source="oauth_acquisition",
                        source_ref_type="oauth_pack",
                        source_ref_id=f"{user_id}:{pack_id}",
                        metadata={"provider": "google", "context": "registration"},
                    )
                    await conn.commit()
                logger.info(f"Granted pack access via Google OAuth: user_id={user_id}, pack_id={pack_id}")
            except Exception as pack_err:
                logger.error(f"Failed to grant pack access via Google OAuth: {pack_err}")

        # Analytics conversion tracking. Pack registrations attribute the
        # conversion to the pack; standalone prompt registrations to the prompt
        # (mirrors verify_email, including paid prompts via analytics_prompt_id).
        visitor_id = request.cookies.get('_aurvek_visitor')
        if visitor_id and (analytics_pack_id or prompt_id or analytics_prompt_id):
            try:
                async with get_db_connection() as conv_conn:
                    if analytics_pack_id:
                        await conv_conn.execute('''
                            UPDATE LANDING_PAGE_ANALYTICS
                            SET converted = 1, converted_user_id = ?
                            WHERE rowid = (
                                SELECT rowid FROM LANDING_PAGE_ANALYTICS
                                WHERE pack_id = ? AND visitor_id = ? AND converted = 0
                                ORDER BY visit_timestamp DESC LIMIT 1
                            )
                        ''', (user_id, analytics_pack_id, visitor_id))
                    else:
                        await conv_conn.execute('''
                            UPDATE LANDING_PAGE_ANALYTICS
                            SET converted = 1, converted_user_id = ?
                            WHERE rowid = (
                                SELECT rowid FROM LANDING_PAGE_ANALYTICS
                                WHERE prompt_id = ? AND visitor_id = ? AND converted = 0
                                ORDER BY visit_timestamp DESC LIMIT 1
                            )
                        ''', (user_id, prompt_id or analytics_prompt_id, visitor_id))
                    await conv_conn.commit()
            except Exception as conv_err:
                logger.warning(f"Could not mark analytics conversion for Google OAuth: {conv_err}")

        # Get the created user
        user = await get_user_by_id(user_id)
        logger.info(f"Created new {target_role} from Google OAuth: user_id={user_id}, username={username}")

        # Redirect precedence mirrors verify_email: paid pack landing, then paid
        # prompt purchase flow, then the normal post-login destination.
        redirect_url = paid_pack_landing_url or paid_prompt_purchase_url or oauth_next
        user_info = await create_user_info(user, used_magic_link=False)
        default_redirect = await get_after_login_redirect(user.id)
        return create_login_response(user_info, redirect_url=redirect_url, default_redirect=default_redirect)

    except Exception as e:
        logger.error(f"Google OAuth callback error: {e}", exc_info=True)
        record_failure(request, "oauth_callback")
        return RedirectResponse(url="/login?error=oauth_failed")


# =============================================================================
# User Registration - MOVED to custom_domain_register() which handles both
# =============================================================================


# =============================================================================
# Email Verification (must be before catch-all route)
# =============================================================================
@app.get("/verify-email/{token}", response_class=HTMLResponse)
async def verify_email(request: Request, token: str):
    """
    Verify email and create the user account.
    """
    # Rate limiting
    rate_error = check_rate_limits(
        request,
        ip_limit=RLC.VERIFY_BY_IP,
        action_name="verify_email"
    )
    if rate_error:
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": False,
            "error": rate_error["message"]
        })

    # Check failure limit
    fail_error = check_failure_limit(request, "verify_email", RLC.VERIFY_FAILURES)
    if fail_error:
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": False,
            "error": fail_error["message"]
        })

    # Get pending registration
    pending = await get_pending_registration(token)

    if not pending:
        record_failure(request, "verify_email")
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": False,
            "error": "Invalid or expired verification link."
        })

    # Check if expired
    if pending["expires_at"] < datetime.now():
        await delete_pending_registration(token)
        record_failure(request, "verify_email")
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": False,
            "error": "This verification link has expired. Please register again."
        })

    # Check again if email was registered in the meantime
    existing_user = await get_user_by_email_record(pending["email"])
    if existing_user:
        await delete_pending_registration(token)
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": False,
            "error": "This email is already registered. Please log in."
        })

    # Create the user
    try:
        # Determine settings based on role
        is_user = pending["target_role"] == "user"
        prompt_id = pending["prompt_id"]
        pack_id = pending.get("pack_id")

        if not is_user and (prompt_id or pack_id) and (
            not marketplace_public_landings_enabled() or not marketplace_checkout_enabled()
        ):
            await delete_pending_registration(token)
            return templates.TemplateResponse("verify_email.html", {
                "request": request,
                "success": False,
                "error": "This registration link is no longer available."
            })

        # Get landing registration config if this is a landing page registration
        # Default values (used if no config or not a landing registration)
        landing_config = DEFAULT_LANDING_REGISTRATION_CONFIG.copy()
        prompt_owner_id = None
        ucr_creator_id = None
        analytics_pack_id = pack_id  # Preserve for analytics even if pack_id is cleared
        paid_pack_landing_url = None
        analytics_prompt_id = None  # Preserve for analytics even if prompt_id is cleared (paid prompt)
        paid_prompt_purchase_url = None

        if pack_id and not is_user:
            # Pack registration: revalidate pack state and use pack's landing_reg_config
            try:
                async with get_db_connection(readonly=True) as conn:
                    cursor = await conn.execute(
                        "SELECT landing_reg_config, created_by_user_id, status, is_public, is_paid, public_id, slug FROM PACKS WHERE id = ?",
                        (pack_id,)
                    )
                    pack_config_row = await cursor.fetchone()

                    # Revalidate: pack must still exist, be published, and public
                    if not pack_config_row or pack_config_row[2] != "published" or not pack_config_row[3]:
                        logger.warning(f"Pack {pack_id} no longer valid at verify-email (status={pack_config_row[2] if pack_config_row else 'missing'}, is_public={pack_config_row[3] if pack_config_row else 'N/A'})")
                        pack_id = None
                        prompt_id = None
                        analytics_pack_id = None
                    elif pack_config_row[4]:
                        # Paid pack: create user with defaults, no config/access until purchase
                        # Save pack info for analytics and redirect before clearing pack_id
                        analytics_pack_id = pack_id
                        paid_pack_landing_url = f"/pack/{pack_config_row[5]}/{pack_config_row[6]}/"
                        logger.info(f"Pack {pack_id} is paid - user created with defaults, purchase required")
                        pack_id = None
                        prompt_id = None
                    else:
                        # Pack is still valid - check it has active prompts
                        prompt_cursor = await conn.execute(
                            """SELECT prompt_id FROM PACK_ITEMS
                               WHERE pack_id = ? AND is_active = 1
                               AND (disable_at IS NULL OR disable_at > datetime('now'))
                               ORDER BY display_order ASC LIMIT 1""",
                            (pack_id,)
                        )
                        active_prompt = await prompt_cursor.fetchone()
                        if not active_prompt:
                            logger.warning(f"Pack {pack_id} has no active prompts at verify-email")
                            pack_id = None
                            prompt_id = None
                            analytics_pack_id = None
                        else:
                            # Update prompt_id to current first active prompt
                            prompt_id = active_prompt[0]
                            if pack_config_row[0]:
                                stored_config = orjson.loads(pack_config_row[0])
                                landing_config.update(stored_config)
                            landing_config = await strip_personal_subscription_default(
                                landing_config
                            )
                            ucr_creator_id = pack_config_row[1]
                            if landing_config.get("billing_mode") == "user_pays":
                                prompt_owner_id = pack_config_row[1]
            except Exception as config_err:
                logger.warning(f"Could not get landing config for pack {pack_id}: {config_err}")
                pack_id = None
                prompt_id = None
                analytics_pack_id = None
        elif prompt_id and not is_user:
            try:
                # Branch free vs paid prompt landings. A paid prompt must never be
                # granted at registration: create the user with defaults and route
                # them to the purchase flow (mirrors the paid-pack path above).
                async with get_db_connection(readonly=True) as conn:
                    price_cursor = await conn.execute(
                        "SELECT purchase_price FROM PROMPTS WHERE id = ?",
                        (prompt_id,),
                    )
                    price_row = await price_cursor.fetchone()
                prompt_purchase_price = float(price_row[0]) if price_row and price_row[0] else 0.0

                if prompt_purchase_price > 0:
                    # Paid prompt: keep landing_config at defaults (no free balance/access);
                    # config is applied when the purchase webhook fires.
                    analytics_prompt_id = prompt_id
                    paid_prompt_purchase_url = f"/purchase/prompt/{prompt_id}"
                    logger.info(f"Prompt {prompt_id} is paid - user created with defaults, purchase required")
                    prompt_id = None
                else:
                    landing_config = await get_landing_registration_config(prompt_id)
                    ucr_creator_id = await get_prompt_owner_id(prompt_id)
                    # If user_pays mode, reuse the same owner ID for billing
                    if landing_config.get("billing_mode") == "user_pays":
                        prompt_owner_id = ucr_creator_id
            except Exception as config_err:
                logger.warning(f"Could not get landing config for prompt {prompt_id}: {config_err}")
                # Continue with defaults

        # Determine the LLM to use:
        # 1. Config's default_llm_id if set
        # 2. Prompt's forced_llm_id if set (from _prompt_forced_llm_id)
        # 3. System default (1)
        default_llm_id = landing_config.get("default_llm_id")
        if not default_llm_id:
            default_llm_id = landing_config.get("_prompt_forced_llm_id")
        if not default_llm_id:
            async with get_db_connection(readonly=True) as conn:
                cursor = await conn.execute(
                    """
                    SELECT id
                    FROM LLM
                    WHERE COALESCE(enabled, 1) = 1
                      AND machine != 'GPTSub'
                    ORDER BY id
                    LIMIT 1
                    """
                )
                llm_row = await cursor.fetchone()
                default_llm_id = llm_row[0] if llm_row else 1

        # Prepare category_access as JSON string if it's a list
        category_access = landing_config.get("category_access")
        if isinstance(category_access, list):
            category_access = orjson.dumps(category_access).decode('utf-8')

        user_id = await add_user(
            username=pending["username"],
            email=pending["email"],
            role_name=pending["target_role"],
            authentication_mode="password_only",
            initial_password=None,  # We'll set the hash directly
            prompt_id=prompt_id if not is_user else None,
            all_prompts_access=False,
            public_prompts_access=landing_config.get("public_prompts_access", True) if not is_user else True,
            llm_id=default_llm_id,
            allow_file_upload=is_user or landing_config.get("allow_file_upload", False),
            allow_image_generation=is_user or landing_config.get("allow_image_generation", False),
            balance=landing_config.get("initial_balance", 0.0) if not is_user else 0.0,
            phone=None,
            current_user=None,
            category_access=category_access if not is_user else None,
            billing_account_id=prompt_owner_id if landing_config.get("billing_mode") == "user_pays" and not is_user else None,
            billing_limit=landing_config.get("billing_limit") if landing_config.get("billing_mode") == "user_pays" and not is_user else None,
            billing_limit_action=landing_config.get("billing_limit_action", "block") if landing_config.get("billing_mode") == "user_pays" and not is_user else "block",
            billing_auto_refill_amount=landing_config.get("billing_auto_refill_amount", 10.0) if landing_config.get("billing_mode") == "user_pays" and not is_user else 10.0,
            billing_max_limit=landing_config.get("billing_max_limit") if landing_config.get("billing_mode") == "user_pays" and not is_user else None,
            initial_balance_funder_id=(
                ucr_creator_id
                if not is_user
                and float(landing_config.get("initial_balance", 0.0)) > 0
                else None
            ),
        )

        if not user_id:
            raise Exception("add_user returned None")

        # Record creator relationship
        if ucr_creator_id:
            try:
                async with get_db_connection() as ucr_conn:
                    ucr_cursor = await ucr_conn.cursor()
                    from common import upsert_creator_relationship
                    source = ('pack', pack_id) if pack_id else ('prompt', prompt_id)
                    await upsert_creator_relationship(ucr_cursor, user_id, ucr_creator_id, 'registered_via', source[0], source[1])
                    await ucr_conn.commit()
            except Exception as ucr_err:
                logger.warning(f"Could not record creator relationship for user {user_id}: {ucr_err}")

        # Record captive domain relationship if user is captive
        effective_public = landing_config.get("public_prompts_access", True) if not is_user else True
        if not effective_public and prompt_id:
            try:
                async with get_db_connection() as cd_conn:
                    cursor = await cd_conn.execute(
                        "SELECT id FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ? AND is_active = 1",
                        (prompt_id,)
                    )
                    domain_row = await cursor.fetchone()
                    if domain_row:
                        await cd_conn.execute(
                            "INSERT OR IGNORE INTO USER_CAPTIVE_DOMAINS (user_id, domain_id, prompt_id) VALUES (?, ?, ?)",
                            (user_id, domain_row[0], prompt_id)
                        )
                        await cd_conn.commit()
                        logger.info(f"Recorded captive domain {domain_row[0]} for user {user_id} (prompt {prompt_id})")
            except Exception as cd_err:
                # Compensate: if we can't track captivity, don't make user captive
                logger.error(f"Failed to record captive domain for user {user_id}, reverting to public access: {cd_err}")
                try:
                    async with get_db_connection() as revert_conn:
                        await revert_conn.execute(
                            "UPDATE USER_DETAILS SET public_prompts_access = 1 WHERE user_id = ?",
                            (user_id,)
                        )
                        await revert_conn.commit()
                except Exception as revert_err:
                    logger.error(f"Failed to revert captive status for user {user_id}: {revert_err}")

        # Update password hash directly (since add_user might not handle pre-hashed passwords)
        async with get_db_connection() as conn:
            await conn.execute(
                """
                UPDATE USERS
                SET password = ?,
                    session_version = COALESCE(session_version, 1) + 1
                WHERE id = ?
                """,
                (pending["password_hash"], user_id)
            )
            await conn.commit()

        # Grant pack access if this was a pack registration
        if pack_id:
            try:
                async with get_db_connection() as conn:
                    await grant_pack_entitlement(
                        conn,
                        user_id=user_id,
                        pack_id=pack_id,
                        source="landing_registration",
                        source_ref_type="registration_pack",
                        source_ref_id=f"{user_id}:{pack_id}",
                        metadata={"context": "email_registration"},
                    )
                    await conn.commit()
                logger.info(f"Granted pack access: user_id={user_id}, pack_id={pack_id}")
            except Exception as pack_err:
                logger.error(f"Failed to grant pack access: {pack_err}")

        # Delete pending registration
        await delete_pending_registration(token)

        # Create JWT token for auto-login
        user = await get_user_by_id(user_id)
        if user:
            user_info = await create_user_info(user, used_magic_link=False)
            access_token = create_access_token(data={"user_info": user_info})

            # Phase 5: Mark analytics conversion if this was a landing page registration
            # Pack registrations attribute the conversion to the pack, not the prompt
            # Use analytics_pack_id to track paid pack registrations even after pack_id is cleared
            visitor_id = request.cookies.get('_aurvek_visitor')
            if visitor_id and (analytics_pack_id or prompt_id or analytics_prompt_id):
                try:
                    async with get_db_connection() as conv_conn:
                        if analytics_pack_id:
                            await conv_conn.execute('''
                                UPDATE LANDING_PAGE_ANALYTICS
                                SET converted = 1, converted_user_id = ?
                                WHERE rowid = (
                                    SELECT rowid FROM LANDING_PAGE_ANALYTICS
                                    WHERE pack_id = ? AND visitor_id = ? AND converted = 0
                                    ORDER BY visit_timestamp DESC LIMIT 1
                                )
                            ''', (user_id, analytics_pack_id, visitor_id))
                        else:
                            await conv_conn.execute('''
                                UPDATE LANDING_PAGE_ANALYTICS
                                SET converted = 1, converted_user_id = ?
                                WHERE rowid = (
                                    SELECT rowid FROM LANDING_PAGE_ANALYTICS
                                    WHERE prompt_id = ? AND visitor_id = ? AND converted = 0
                                    ORDER BY visit_timestamp DESC LIMIT 1
                                )
                            ''', (user_id, prompt_id or analytics_prompt_id, visitor_id))
                        await conv_conn.commit()
                except Exception as conv_err:
                    logger.warning(f"Could not mark analytics conversion: {conv_err}")

            # Redirect: paid pack users go to the pack landing page, paid prompt
            # users go to the prompt purchase flow, everyone else to chat.
            if paid_pack_landing_url:
                redirect_url = paid_pack_landing_url
            elif paid_prompt_purchase_url:
                redirect_url = paid_prompt_purchase_url
            else:
                redirect_url = "/chat"
            response = RedirectResponse(url=redirect_url, status_code=303)
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=access_token,
                httponly=True,
                max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                samesite="lax",
                secure=SECURE_COOKIES
            )

            logger.info(f"User {pending['email']} registered successfully as {pending['target_role']}")
            return response

        # Fallback: show success page
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": True,
            "error": None,
            "message": "Your account has been created successfully! You can now log in."
        })

    except Exception as e:
        logger.error(f"Error creating user during verification: {e}")
        return templates.TemplateResponse("verify_email.html", {
            "request": request,
            "success": False,
            "error": "An error occurred while creating your account. Please try again."
        })


@app.get("/get-user-directory")
async def get_user_directory_endpoint(
    request: Request,
    username: str = Query(..., min_length=1),
    prompt_id: Optional[str] = Query(None),
    prompt_name: Optional[str] = Query(None),
    landing_section: Optional[str] = Query(None),
    debug: bool = False
):
    # Only allow internal requests (from nginx via localhost or internal IP)
    client_host = request.client.host if request.client else None
    is_internal = client_host in ("127.0.0.1", "::1", "localhost") if client_host else False

    # Also check for X-Internal-Request header that nginx can set
    is_nginx_internal = request.headers.get("X-Internal-Request") == "true"

    if not is_internal and not is_nginx_internal:
        return Response(content="", status_code=403)

    try:
        if not prompt_id or not prompt_name or not landing_section:
            return Response(
                content="",
                media_type="application/json",
                status_code=400
            )

        try:
            prompt_id_int = int(prompt_id)
            if prompt_id_int < 0:
                raise ValueError("Prompt ID must be positive")
        except ValueError as e:
            return Response(
                content="",
                media_type="application/json",
                status_code=400
            )

        # Get the hash components
        hash_prefix1, hash_prefix2, user_hash = generate_user_hash(username)

        # Build the relative base path using pathlib
        padded_id = f"{prompt_id_int:07d}"
        relative_base_path = Path("users") / hash_prefix1 / hash_prefix2 / user_hash / "prompts" / padded_id[:3] / f"{padded_id[3:]}_{prompt_name}"

        # Clean landing_section
        clean_section = landing_section.strip('/')
        if not clean_section:
            clean_section = 'home'

        # Build and verify paths
        if '/' not in clean_section:
            # Try as HTML file
            html_path = relative_base_path / f"{clean_section}.html"
            full_html_path = DATA_DIR / html_path

            if full_html_path.is_file():
                return Response(
                    content="",
                    media_type="application/json",
                    headers={
                        "X-File-Path": html_path.as_posix(),
                        "X-Resource-Type": "html"
                    }
                )

        # Try as a static resource
        resource_path = relative_base_path / clean_section
        full_resource_path = DATA_DIR / resource_path

        if full_resource_path.exists():
            return Response(
                content="",
                media_type="application/json",
                headers={
                    "X-File-Path": resource_path.as_posix(),
                    "X-Resource-Type": "static"
                }
            )

        return Response(
            content="",
            media_type="application/json",
            status_code=404
        )

    except Exception as e:
        logger.error(f"Error in get_user_directory: {e}")
        return Response(
            content="",
            media_type="application/json",
            status_code=500
        )


@app.post("/api/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    username: str = Form(None),
    prompt_id: int = Form(None),
    prompt_public_id: str = Form(None),
    captcha_token: str = Form("")
):
    """
    Process registration form submission.
    Creates pending registration and sends verification email.
    """
    # Clean up expired registrations occasionally
    await cleanup_expired_registrations()

    # Rate limiting - check by IP and email
    email_clean = email.strip().lower() if email else None
    rate_error = check_rate_limits(
        request,
        ip_limit=RLC.REGISTER_BY_IP_ALL,
        identifier=email_clean,
        identifier_limit=RLC.REGISTER_BY_EMAIL,
        action_name="register"
    )
    if rate_error:
        return JSONResponse(rate_error, status_code=429)

    # Check failure limit
    fail_error = check_failure_limit(request, "register", RLC.REGISTER_BY_IP_FAILURES)
    if fail_error:
        return JSONResponse(fail_error, status_code=429)

    # CAPTCHA verification
    client_ip = get_client_ip(request)
    captcha_ok, captcha_error = await verify_captcha(captcha_token, client_ip)
    if not captcha_ok:
        record_failure(request, "register", email_clean)
        return JSONResponse({
            "status": "error",
            "message": captcha_error
        }, status_code=400)

    # Validate passwords match
    if password != password_confirm:
        record_failure(request, "register", email_clean)
        return JSONResponse({
            "status": "error",
            "message": "Passwords do not match"
        }, status_code=400)

    # Validate password strength
    if len(password) < 8:
        record_failure(request, "register", email_clean)
        return JSONResponse({
            "status": "error",
            "message": "Password must be at least 8 characters"
        }, status_code=400)

    # Validate email (format + disposable domain check + MX records)
    email = email.strip().lower()
    is_valid_email, email_error = validate_email_robust(email)
    if not is_valid_email:
        record_failure(request, "register", email_clean)
        return JSONResponse({
            "status": "error",
            "message": email_error
        }, status_code=400)

    # Check if email already exists
    existing_user = await get_user_by_email_record(email)
    if existing_user:
        # Don't reveal that email exists - same anti-enumeration message as success
        logger.info(f"Registration attempt with existing email: {email}")
        # If registering from a landing page, send claim entitlement email
        if prompt_id and marketplace_public_landings_enabled() and marketplace_checkout_enabled():
            await send_entitlement_claim_email(
                request, email, existing_user["id"],
                prompt_id=prompt_id, pack_id=None
            )
        return JSONResponse({
            "status": "success",
            "message": "If this email is not already registered, you will receive a verification email shortly."
        })

    # Determine role and get prompt info
    target_role = "customer" if prompt_id else "user"
    prompt_name = None
    prompt_owner_id = None

    if prompt_id:
        if not marketplace_public_landings_enabled() or not marketplace_checkout_enabled():
            record_failure(request, "register", email)
            return JSONResponse({
                "status": "error",
                "message": "Invalid prompt"
            }, status_code=400)

        # prompt_public_id is mandatory when prompt_id is present
        if not prompt_public_id:
            record_failure(request, "register", email)
            return JSONResponse({
                "status": "error",
                "message": "Invalid prompt"
            }, status_code=400)

        # Verify prompt exists, is public, and cross-validate with prompt_public_id
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT name, created_by_user_id, public_id FROM PROMPTS WHERE id = ? AND public = 1",
                (prompt_id,)
            )
            result = await cursor.fetchone()
            if not result:
                record_failure(request, "register", email)
                return JSONResponse({
                    "status": "error",
                    "message": "Invalid prompt"
                }, status_code=400)
            # Prevent prompt_id manipulation: must match the public_id from the landing
            if result[2] != prompt_public_id:
                record_failure(request, "register", email)
                return JSONResponse({
                    "status": "error",
                    "message": "Invalid prompt"
                }, status_code=400)
            prompt_name = result[0]
            prompt_owner_id = result[1]

    # Handle username: validate if provided, generate if not
    if username and username.strip():
        username = username.strip()

        # Validate username format
        if not re.match(r'^[a-zA-Z0-9_-]+$', username):
            record_failure(request, "register", email)
            return JSONResponse({
                "status": "error",
                "message": "Username can only contain letters, numbers, hyphens and underscores"
            }, status_code=400)

        # Validate username length
        if len(username) < 3:
            record_failure(request, "register", email)
            return JSONResponse({
                "status": "error",
                "message": "Username must be at least 3 characters"
            }, status_code=400)

        if len(username) > 20:
            record_failure(request, "register", email)
            return JSONResponse({
                "status": "error",
                "message": "Username cannot exceed 20 characters"
            }, status_code=400)

        # Check if username already exists
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT id FROM USERS WHERE LOWER(username) = LOWER(?)",
                (username,)
            )
            if await cursor.fetchone():
                record_failure(request, "register", email)
                return JSONResponse({
                    "status": "error",
                    "message": "This username is already taken"
                }, status_code=400)
    else:
        # Generate username from email
        username = generate_username_from_email(email)

    # Hash password
    password_hash = hash_password(password)

    # Generate verification token
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=24)

    # Create pending registration
    success = await create_pending_registration(
        email=email,
        username=username,
        password_hash=password_hash,
        token=token,
        target_role=target_role,
        prompt_id=prompt_id,
        expires_at=expires_at
    )

    if not success:
        record_failure(request, "register", email)
        return JSONResponse({
            "status": "error",
            "message": "Registration failed. Please try again."
        }, status_code=500)

    # Build verification URL
    verification_url = f"{get_auth_base_url(request).rstrip('/')}/verify-email/{token}"

    # Get branding from prompt owner if registering via landing page
    branding = None
    if prompt_owner_id:
        from common import get_user_branding
        branding = await get_user_branding(prompt_owner_id)

    # Send verification email
    email_sent = await asyncio.to_thread(
        email_service.send_verification_email,
        to_email=email,
        verification_url=verification_url,
        is_user=(target_role == "user"),
        prompt_name=prompt_name,
        branding=branding,
    )

    if not email_sent:
        logger.error("Failed to send verification email to %s", email)
        # Keep the public response generic to avoid email enumeration; the
        # delivery failure remains explicit in the service result and logs.

    logger.info(f"Registration pending for {email} as {target_role}")

    return JSONResponse({
        "status": "success",
        "message": "If this email is not already registered, you will receive a verification email shortly."
    })


# Helper function to process image paths
def process_image_path(url: str, user_dir: Path) -> Tuple[Path, Path]:
    """Process the image URL and return paths for both variants"""
    path_str = url.split('://')[-1].split('/', 1)[-1].split('?', 1)[0]

    if path_str[:3] == 'sk/':
        relative_path = path_str[3:]
    elif path_str[:6] == 'users/':
        parts = path_str.split('/', 4)
        relative_path = parts[4] if len(parts) >= 5 else None
        if not relative_path:
            raise ValueError(f"Invalid path structure: {path_str}")
    else:
        relative_path = path_str

    full_path = user_dir / relative_path
    base_name = full_path.stem.rsplit('_', 1)[0] if any(suffix in full_path.stem for suffix in ['_256', '_fullsize']) else full_path.stem
    file_dir = full_path.parent
    extension = full_path.suffix or ".webp"

    return (
        file_dir / f"{base_name}_256{extension}",
        file_dir / f"{base_name}_fullsize{extension}"
    )

# Helper function to extract image URLs
def extract_image_urls(message_data: dict) -> List[str]:
    """Extracts image URLs from the message"""
    if isinstance(message_data, list):
        return [item['image_url']['url'] for item in message_data
                if isinstance(item, dict) and item.get('type') == 'image_url']
    elif isinstance(message_data, dict) and message_data.get('type') == 'image_url':
        return [message_data['image_url']['url']]
    return []


def build_message_after_image_delete(message_data) -> str:
    """Remove image blocks while preserving any remaining structured content."""
    if isinstance(message_data, list):
        remaining_items = [
            item for item in message_data
            if not (isinstance(item, dict) and item.get('type') == 'image_url')
        ]
        if remaining_items:
            return orjson.dumps(remaining_items).decode()
        return "[image deleted]"

    if isinstance(message_data, dict):
        if message_data.get('type') == 'image_url':
            return "[image deleted]"
        return orjson.dumps(message_data).decode()

    return "[image deleted]"

# Helper function to delete files
async def delete_file_variants(
    variants: List[Path],
    user_dir: Path,
    conn: aiosqlite.Connection,
) -> Tuple[int, int, int]:
    """Delete safe image variants and clear their generated-media rows."""
    deleted = failed = 0
    ledger_paths: List[Path] = []
    for variant_path in variants:
        try:
            variant_abs_path = variant_path.resolve()
            user_dir_abs_path = user_dir.resolve()

            # Ensure path is within user directory
            if not variant_abs_path.is_relative_to(user_dir_abs_path):
                logging.warning(f"Attempted to access file outside user directory: {variant_path}")
                failed += 1
                continue

            if await aiofiles.os.path.exists(str(variant_path)):
                await aiofiles.os.remove(str(variant_path))
                deleted += 1
                ledger_paths.append(variant_abs_path)
                logging.info(f"Successfully deleted: {variant_path}")
            else:
                logging.warning(f"File not found: {variant_path}")
                failed += 1
                # Clear a stale ledger row even when the physical file has
                # already disappeared. The path passed the user-root check.
                ledger_paths.append(variant_abs_path)
        except Exception as e:
            logging.error(f"Error deleting variant {variant_path}: {e}")
            failed += 1
    ledger_rows_deleted = await storage_quota.delete_generated_file_rows(
        conn, (str(path) for path in ledger_paths)
    )
    return deleted, failed, ledger_rows_deleted

@app.delete("/api/delete-image/{message_id}")
async def delete_image(
    message_id: int,
    attachment_ref: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        await cursor.execute('''
            SELECT m.message, c.user_id
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON m.conversation_id = c.id
            WHERE m.id = ? AND c.user_id = ?
        ''', (message_id, current_user.id))

        result = await cursor.fetchone()
        if not result:
            raise HTTPException(status_code=403, detail="Permission denied")

        user_dir = Path(get_user_directory(current_user.username))
        message_data = orjson.loads(result['message'])
        deleted_count = failed_count = 0
        attachment_refs = [
            item.get('image_url', {}).get('attachment_ref')
            for item in (message_data if isinstance(message_data, list) else [message_data])
            if isinstance(item, dict) and item.get('type') == 'image_url'
            and item.get('image_url', {}).get('attachment_ref')
        ]

        if attachment_refs:
            if attachment_ref:
                refs_to_delete = [attachment_ref] if attachment_ref in attachment_refs else []
                if not refs_to_delete:
                    raise HTTPException(status_code=404, detail="Attachment not found in this message")
            elif len(attachment_refs) == 1:
                refs_to_delete = attachment_refs
            else:
                raise HTTPException(status_code=400, detail="attachment_ref is required for multi-image messages")

            for public_id in refs_to_delete:
                deleted = await delete_attachment_and_rewrite_message(
                    conn,
                    public_id=public_id,
                    user_id=current_user.id,
                    allow_admin=False,
                )
                if deleted:
                    deleted_count += 1
                else:
                    failed_count += 1
            await conn.commit()
            await prune_unreferenced_blobs()
            return {
                "success": True,
                "message": f"Successfully deleted: {deleted_count}, Failed: {failed_count}"
            }

        for url in extract_image_urls(message_data):
            try:
                variant_paths = process_image_path(url, user_dir)
                deleted, failed, _ = await delete_file_variants(
                    list(variant_paths), user_dir, conn
                )
                deleted_count += deleted
                failed_count += failed
            except Exception as e:
                logging.error(f"Error processing image URL {url}: {e}")
                failed_count += 1

        replacement_message = build_message_after_image_delete(message_data)
        await cursor.execute(
            "UPDATE MESSAGES SET message = ? WHERE id = ?",
            (replacement_message, message_id)
        )
        await conn.commit()

        return {
            "success": True,
            "message": f"Successfully deleted: {deleted_count}, Failed: {failed_count}"
        }

@app.post("/delete-images")
async def delete_images(image_ids: List[int], current_user: User = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not image_ids:
        return {"success": True, "message": "No images to delete"}

    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        placeholders = ','.join(['?' for _ in image_ids])

        # Verify permissions
        await cursor.execute(f'''
            SELECT COUNT(*) FROM MESSAGES m
            JOIN CONVERSATIONS c ON m.conversation_id = c.id
            WHERE m.id IN ({placeholders}) AND c.user_id != ?
        ''', (*image_ids, current_user.id))

        if (await cursor.fetchone())[0] > 0:
            raise HTTPException(status_code=403, detail="Permission denied for some images")

        # Get messages
        await cursor.execute(f'''
            SELECT m.id, m.message
            FROM MESSAGES m
            WHERE m.id IN ({placeholders})
        ''', image_ids)

        user_dir = Path(get_user_directory(current_user.username))
        replacements_to_apply = []
        deleted_count = failed_count = 0
        ledger_rows_deleted = 0

        for message in await cursor.fetchall():
            message_data = orjson.loads(message['message'])
            message_deleted = False
            attachment_refs = [
                item.get('image_url', {}).get('attachment_ref')
                for item in (message_data if isinstance(message_data, list) else [message_data])
                if isinstance(item, dict) and item.get('type') == 'image_url'
                and item.get('image_url', {}).get('attachment_ref')
            ]

            if attachment_refs:
                if len(attachment_refs) == 1:
                    deleted = await delete_attachment_and_rewrite_message(
                        conn,
                        public_id=attachment_refs[0],
                        user_id=current_user.id,
                        allow_admin=False,
                    )
                    if deleted:
                        deleted_count += 1
                        message_deleted = True
                    else:
                        failed_count += 1
                else:
                    failed_count += len(attachment_refs)
                continue

            for url in extract_image_urls(message_data):
                try:
                    variant_paths = process_image_path(url, user_dir)
                    deleted, failed, ledger_deleted = await delete_file_variants(
                        list(variant_paths), user_dir, conn
                    )
                    if deleted > 0 and not message_deleted:
                        replacements_to_apply.append(
                            (build_message_after_image_delete(message_data), message['id'])
                        )
                        message_deleted = True
                    deleted_count += deleted
                    failed_count += failed
                    ledger_rows_deleted += ledger_deleted
                except Exception as e:
                    logging.error(f"Error processing image URL {url}: {e}")
                    failed_count += 1

        if replacements_to_apply:
            await cursor.executemany(
                "UPDATE MESSAGES SET message = ? WHERE id = ?",
                replacements_to_apply
            )
        if replacements_to_apply or deleted_count > 0 or ledger_rows_deleted > 0:
            await conn.commit()
            await prune_unreferenced_blobs()

        return {
            "success": True,
            "message": f"Successfully deleted: {deleted_count}, Failed: {failed_count}",
            "deleted_ids": [message_id for _, message_id in replacements_to_apply]
        }

@app.post("/admin/disable-cloudflare-cache")
async def disable_cloudflare_cache(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if not await is_admin(current_user.id):
        return JSONResponse(content={"error": "Admin access required."}, status_code=403)

    from request_security import validate_mutation_request

    csrf_rejection = validate_mutation_request(request)
    if csrf_rejection is not None:
        return csrf_rejection

    try:
        await run_cloudflare_cache_disable()
        logger.info(
            "Cloudflare development mode enabled by admin %s",
            current_user.id,
        )
        try:
            await log_admin_action(
                admin_id=current_user.id,
                action_type="cloudflare_cache_disabled",
                request=request,
                details="Cloudflare development mode enabled",
            )
        except Exception as audit_error:
            logger.warning(
                "Could not audit Cloudflare maintenance: %s",
                audit_error,
            )
        return {
            "success": True,
            "message": "Cloudflare cache disabled successfully",
        }
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=500, detail="Error disabling Cloudflare cache")
    except MaintenanceTaskBusy:
        raise HTTPException(
            status_code=409,
            detail="Cloudflare cache maintenance is already running.",
        )
    except MaintenanceTaskTimedOut:
        raise HTTPException(
            status_code=504,
            detail="Cloudflare cache maintenance timed out.",
        )

@app.post("/admin/clear-audio-cache")
async def clear_audio_cache(
    request: Request,
    time_arg: dict,
    current_user: User = Depends(get_current_user),
):
    if current_user is None:
        return unauthenticated_response()

    if not await is_admin(current_user.id):
        return JSONResponse(content={"error": "Admin access required."}, status_code=403)

    try:
        requested_age = time_arg.get("time_arg")
        await run_audio_cache_cleanup(requested_age)
        logger.info("Audio cache cleared by admin %s", current_user.id)
        try:
            await log_admin_action(
                admin_id=current_user.id,
                action_type="audio_cache_cleared",
                request=request,
                details=f"Cache age threshold: {requested_age}",
            )
        except Exception as audit_error:
            logger.warning("Could not audit audio maintenance: %s", audit_error)
        return {"success": True, "message": "Audio cache cleared successfully"}
    except (AttributeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=500, detail="Error clearing audio cache")
    except MaintenanceTaskBusy:
        raise HTTPException(
            status_code=409,
            detail="Audio cache maintenance is already running.",
        )
    except MaintenanceTaskTimedOut:
        raise HTTPException(
            status_code=504,
            detail="Audio cache maintenance timed out.",
        )


@app.post("/admin/toggle-captcha")
async def toggle_captcha(data: dict, current_user: User = Depends(get_current_user)):
    """Toggle CAPTCHA on/off at runtime (admin only)."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(content={"error": "Admin access required"}, status_code=403)

    enabled = data.get("enabled", True)
    set_captcha_enabled(enabled)
    status = "enabled" if enabled else "disabled"
    logger.info(f"CAPTCHA {status} by admin {current_user.username}")

    return {"status": "success", "captcha_enabled": enabled}


# =============================================================================
# Categories Management
# =============================================================================

@app.get("/api/categories")
async def get_categories(
    include_restricted: bool = False,
    current_user: User = Depends(get_current_user)
):
    """Get all categories. Age-restricted categories only shown if include_restricted=True."""
    async with get_db_connection(readonly=True) as conn:
        if include_restricted:
            query = "SELECT id, name, description, icon, is_age_restricted, display_order FROM CATEGORIES ORDER BY display_order"
            async with conn.execute(query) as cursor:
                rows = await cursor.fetchall()
        else:
            query = "SELECT id, name, description, icon, is_age_restricted, display_order FROM CATEGORIES WHERE is_age_restricted = 0 ORDER BY display_order"
            async with conn.execute(query) as cursor:
                rows = await cursor.fetchall()

        categories = [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "icon": row[3],
                "is_age_restricted": bool(row[4]),
                "display_order": row[5]
            }
            for row in rows
        ]
        return categories


@app.post("/api/categories")
async def create_category(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Create a new category (admin only)."""
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    data = await request.json()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    icon = data.get("icon", "fa-tag").strip()
    is_age_restricted = bool(data.get("is_age_restricted", False))
    display_order = int(data.get("display_order", 0))

    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")

    async with get_db_connection() as conn:
        try:
            await conn.execute(
                """INSERT INTO CATEGORIES (name, description, icon, is_age_restricted, display_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, description, icon, 1 if is_age_restricted else 0, display_order)
            )
            await conn.commit()

            # Get the new category id
            async with conn.execute("SELECT last_insert_rowid()") as cursor:
                row = await cursor.fetchone()
                new_id = row[0]

            return {"success": True, "id": new_id, "message": "Category created successfully"}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Category with this name already exists")


@app.put("/api/categories/{category_id}")
async def update_category(
    category_id: int,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Update a category (admin only)."""
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    data = await request.json()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    icon = data.get("icon", "fa-tag").strip()
    is_age_restricted = bool(data.get("is_age_restricted", False))
    display_order = int(data.get("display_order", 0))

    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")

    async with get_db_connection() as conn:
        # Check if category exists
        async with conn.execute("SELECT id FROM CATEGORIES WHERE id = ?", (category_id,)) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Category not found")

        try:
            await conn.execute(
                """UPDATE CATEGORIES
                   SET name = ?, description = ?, icon = ?, is_age_restricted = ?, display_order = ?
                   WHERE id = ?""",
                (name, description, icon, 1 if is_age_restricted else 0, display_order, category_id)
            )
            await conn.commit()
            return {"success": True, "message": "Category updated successfully"}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Category with this name already exists")


@app.delete("/api/categories/{category_id}")
async def delete_category(
    category_id: int,
    current_user: User = Depends(get_current_user)
):
    """Delete a category (admin only)."""
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    async with get_db_connection() as conn:
        # Check if category exists
        async with conn.execute("SELECT id FROM CATEGORIES WHERE id = ?", (category_id,)) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="Category not found")

        # Delete associations first
        await conn.execute("DELETE FROM PROMPT_CATEGORIES WHERE category_id = ?", (category_id,))
        # Delete the category
        await conn.execute("DELETE FROM CATEGORIES WHERE id = ?", (category_id,))
        await conn.commit()

        return {"success": True, "message": "Category deleted successfully"}


@app.get("/api/prompts/{prompt_id}/categories")
async def get_prompt_categories(
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """Get categories assigned to a prompt."""
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            """SELECT c.id, c.name, c.description, c.icon, c.is_age_restricted
               FROM CATEGORIES c
               JOIN PROMPT_CATEGORIES pc ON c.id = pc.category_id
               WHERE pc.prompt_id = ?
               ORDER BY c.display_order""",
            (prompt_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        categories = [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "icon": row[3],
                "is_age_restricted": bool(row[4])
            }
            for row in rows
        ]
        return categories


@app.put("/api/prompts/{prompt_id}/categories")
async def update_prompt_categories(
    prompt_id: int,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Assign categories to a prompt."""
    data = await request.json()
    category_ids = data.get("category_ids", [])

    # Verify user has permission to edit this prompt
    async with get_db_connection() as conn:
        # Check prompt exists
        async with conn.execute("SELECT id, public FROM PROMPTS WHERE id = ?", (prompt_id,)) as cursor:
            prompt = await cursor.fetchone()
            if not prompt:
                raise HTTPException(status_code=404, detail="Prompt not found")

        # Check permissions
        is_admin = await current_user.is_admin
        async with conn.execute(
            "SELECT 1 FROM PROMPT_PERMISSIONS WHERE prompt_id = ? AND user_id = ? AND permission_level IN ('owner', 'edit')",
            (prompt_id, current_user.id)
        ) as cursor:
            perm = await cursor.fetchone()
            has_permission = perm is not None

        if not is_admin and not has_permission:
            raise HTTPException(status_code=403, detail="Access denied")

        # Validate category IDs exist
        if category_ids:
            placeholders = ','.join('?' * len(category_ids))
            async with conn.execute(
                f"SELECT id FROM CATEGORIES WHERE id IN ({placeholders})",
                category_ids
            ) as cursor:
                valid_ids = [row[0] for row in await cursor.fetchall()]

            invalid_ids = set(category_ids) - set(valid_ids)
            if invalid_ids:
                raise HTTPException(status_code=400, detail=f"Invalid category IDs: {list(invalid_ids)}")

        # Update categories
        await conn.execute("DELETE FROM PROMPT_CATEGORIES WHERE prompt_id = ?", (prompt_id,))

        for cat_id in category_ids:
            await conn.execute(
                "INSERT INTO PROMPT_CATEGORIES (prompt_id, category_id) VALUES (?, ?)",
                (prompt_id, cat_id)
            )

        await conn.commit()
        return {"success": True, "message": "Categories updated successfully"}


@app.get("/api/prompts/{prompt_id}/forced-llm")
async def get_prompt_forced_llm(
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """Get the forced LLM configuration for a prompt.

    Returns the forced_llm_id if the prompt has a forced model configured,
    otherwise returns null. Used by create_user.html to auto-select LLM.
    """
    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            "SELECT forced_llm_id FROM PROMPTS WHERE id = ?",
            (prompt_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Prompt not found")

            return {"forced_llm_id": row[0]}


@app.get("/admin/categories", response_class=HTMLResponse)
async def admin_categories(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for managing categories."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    async with get_db_connection(readonly=True) as conn:
        async with conn.execute(
            """SELECT id, name, description, icon, is_age_restricted, display_order, created_at
               FROM CATEGORIES ORDER BY display_order"""
        ) as cursor:
            rows = await cursor.fetchall()

        categories = [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "icon": row[3],
                "is_age_restricted": bool(row[4]),
                "display_order": row[5],
                "created_at": row[6]
            }
            for row in rows
        ]

        # Count prompts per category
        async with conn.execute(
            """SELECT category_id, COUNT(*) as count
               FROM PROMPT_CATEGORIES GROUP BY category_id"""
        ) as cursor:
            counts = {row[0]: row[1] for row in await cursor.fetchall()}

        for cat in categories:
            cat["prompt_count"] = counts.get(cat["id"], 0)

    context = await get_template_context(request, current_user)
    context["categories"] = categories
    return templates.TemplateResponse("admin_categories.html", context)


@app.post("/api/categories/reorder")
async def reorder_categories(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Reorder categories (admin only)."""
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    data = await request.json()
    order = data.get("order", [])  # List of category IDs in new order

    if not order:
        raise HTTPException(status_code=400, detail="Order list is required")

    async with get_db_connection() as conn:
        for idx, cat_id in enumerate(order, start=1):
            await conn.execute(
                "UPDATE CATEGORIES SET display_order = ? WHERE id = ?",
                (idx, cat_id)
            )
        await conn.commit()

    return {"success": True, "message": "Categories reordered successfully"}




@app.get("/admin/pricing", response_class=HTMLResponse)
async def admin_pricing_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for configuring pricing margins and commissions."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)

    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    # Get current pricing config
    pricing_config = {}
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute(
            "SELECT key, value, description FROM SYSTEM_CONFIG WHERE key LIKE 'pricing_%' OR key = 'min_payout_amount'"
        )
        rows = await cursor.fetchall()
        for row in rows:
            pricing_config[row[0]] = {"value": row[1], "description": row[2]}

    context = await get_template_context(request, current_user)
    context["pricing_config"] = pricing_config
    return templates.TemplateResponse("admin_pricing.html", context)


@app.get("/api/admin/pricing-config")
async def get_pricing_config(request: Request, current_user: User = Depends(get_current_user)):
    """Get all pricing configuration values."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                "SELECT key, value, description FROM SYSTEM_CONFIG WHERE key LIKE 'pricing_%' OR key = 'min_payout_amount'"
            )
            rows = await cursor.fetchall()

        config = {}
        for row in rows:
            config[row[0]] = {"value": row[1], "description": row[2]}

        return JSONResponse(content={"success": True, "config": config})

    except Exception as e:
        logger.error(f"Error getting pricing config: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


@app.put("/api/admin/pricing-config")
async def update_pricing_config(request: Request, current_user: User = Depends(get_current_user)):
    """Update pricing configuration values."""
    if current_user is None:
        return unauthenticated_response()

    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        data = await request.json()

        # Valid pricing config keys
        valid_keys = [
            'pricing_margin_free',
            'pricing_margin_paid',
            'pricing_commission',
            'pricing_margin_personal',
            'min_payout_amount'
        ]

        async with get_db_connection() as conn:
            cursor = await conn.cursor()

            for key, value in data.items():
                if key not in valid_keys:
                    continue

                # Validate values are numeric and within reasonable range
                try:
                    numeric_value = float(value)
                    if key == 'min_payout_amount':
                        if numeric_value < 0 or numeric_value > 1000:
                            return JSONResponse(
                                status_code=400,
                                content={"success": False, "message": f"Invalid value for {key}: must be 0-1000"}
                            )
                    else:
                        if numeric_value < 0 or numeric_value > 100:
                            return JSONResponse(
                                status_code=400,
                                content={"success": False, "message": f"Invalid value for {key}: must be 0-100%"}
                            )
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={"success": False, "message": f"Invalid value for {key}: must be numeric"}
                    )

                await cursor.execute(
                    "UPDATE SYSTEM_CONFIG SET value = ? WHERE key = ?",
                    (str(value), key)
                )

            await conn.commit()

        return JSONResponse(content={"success": True, "message": "Pricing configuration updated"})

    except Exception as e:
        logger.error(f"Error updating pricing config: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


# =============================================================================
# Storage Quotas (per-user storage limits)
# =============================================================================

# The admin JSON API speaks bytes with the key "default_quota_bytes"; the value
# is persisted under the SYSTEM_CONFIG key "storage_quota_default_bytes".
STORAGE_QUOTA_CONFIG_KEY = "storage_quota_default_bytes"
STORAGE_QUOTA_CONFIG_DESCRIPTION = "Default per-user storage quota in bytes (0 = unlimited)"


async def _storage_quota_uploads_by_user(conn):
    """Per-user upload usage (dedupe-aware) as {user_id: bytes} via one GROUP BY.

    Sums each blob once per distinct (user_id, blob_id) pair so the same file
    re-uploaded by one user counts once, while each distinct user holding it is
    charged fully. Returns an empty dict when the FILE_* tables do not exist yet
    (fresh DB where nobody has uploaded), matching storage_quota's handling.
    """
    try:
        cursor = await conn.execute(
            """
            SELECT du.user_id, COALESCE(SUM(fb.size_bytes), 0) AS uploads_bytes
            FROM (
                SELECT DISTINCT user_id, blob_id
                FROM FILE_ATTACHMENTS
                WHERE status IN ('pending', 'active')
            ) du
            JOIN FILE_BLOBS fb ON fb.id = du.blob_id
            GROUP BY du.user_id
            """
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    rows = await cursor.fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


async def _storage_quota_generated_by_user(conn):
    """Per-user generated-media usage as {user_id: bytes} via one GROUP BY."""
    cursor = await conn.execute(
        "SELECT user_id, COALESCE(SUM(size_bytes), 0) FROM GENERATED_MEDIA_FILES GROUP BY user_id"
    )
    rows = await cursor.fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


@app.get("/admin/storage-quotas", response_class=HTMLResponse)
async def admin_storage_quotas_page(request: Request, current_user: User = Depends(get_current_user)):
    """Admin page for per-user storage quotas and the global default."""
    if current_user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not await current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("admin_storage_quotas.html", context)


@app.get("/api/admin/storage-quotas/config")
async def get_storage_quota_config(current_user: User = Depends(get_current_user)):
    """Return the global default quota in bytes (0 = unlimited)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})
    async with get_db_connection(readonly=True) as conn:
        default_quota_bytes = await storage_quota.get_default_quota_bytes(conn)
    return JSONResponse(content={"success": True, "default_quota_bytes": default_quota_bytes})


@app.put("/api/admin/storage-quotas/config")
async def update_storage_quota_config(request: Request, current_user: User = Depends(get_current_user)):
    """Update the global default quota (bytes, integer >= 0; 0 = unlimited)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    data = await request.json()
    if not isinstance(data, dict) or "default_quota_bytes" not in data:
        return JSONResponse(status_code=400, content={"success": False, "message": "Missing default_quota_bytes"})
    raw = data["default_quota_bytes"]
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return JSONResponse(status_code=400, content={"success": False, "message": "default_quota_bytes must be an integer >= 0"})
    try:
        new_value = int(raw)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"success": False, "message": "default_quota_bytes must be an integer >= 0"})
    if new_value < 0 or new_value > storage_quota.MAX_QUOTA_BYTES:
        return JSONResponse(status_code=400, content={"success": False, "message": "default_quota_bytes must be an integer >= 0"})

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT value FROM SYSTEM_CONFIG WHERE key = ?", (STORAGE_QUOTA_CONFIG_KEY,)
        )
        old_row = await cursor.fetchone()
        old_value = old_row[0] if old_row else None
        await conn.execute(
            """
            INSERT INTO SYSTEM_CONFIG (key, value, description, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (STORAGE_QUOTA_CONFIG_KEY, str(new_value), STORAGE_QUOTA_CONFIG_DESCRIPTION),
        )
        await conn.commit()

    await log_admin_action(
        admin_id=current_user.id,
        action_type="storage_quota_default_updated",
        request=request,
        details=f"Default storage quota changed from {old_value} to {new_value} bytes by '{current_user.username}'",
    )
    return JSONResponse(content={"success": True, "default_quota_bytes": new_value})


@app.get("/api/admin/storage-quotas/users")
async def get_storage_quota_users(
    search: str = None,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    """Per-user storage usage rows, ordered by total usage desc.

    Usage is computed with two GROUP BY queries (uploads, generated) merged in
    Python against a single overrides query -- never N+1 per-user queries.
    percent is null for unlimited (quota 0), else total/quota*100 (1 decimal).
    """
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    async with get_db_connection(readonly=True) as conn:
        default_quota_bytes = await storage_quota.get_default_quota_bytes(conn)
        uploads_by_user = await _storage_quota_uploads_by_user(conn)
        generated_by_user = await _storage_quota_generated_by_user(conn)

        cursor = await conn.execute(
            "SELECT user_id, storage_quota_bytes FROM USER_DETAILS WHERE storage_quota_bytes IS NOT NULL"
        )
        overrides = {int(row[0]): int(row[1]) for row in await cursor.fetchall()}

        if search and search.strip():
            cursor = await conn.execute(
                "SELECT id, username FROM USERS WHERE LOWER(username) LIKE ?",
                (f"%{search.strip().lower()}%",),
            )
        else:
            cursor = await conn.execute("SELECT id, username FROM USERS")
        user_rows = await cursor.fetchall()

    results = []
    for row in user_rows:
        user_id = int(row[0])
        username = row[1]
        uploads_bytes = uploads_by_user.get(user_id, 0)
        generated_bytes = generated_by_user.get(user_id, 0)
        total_bytes = uploads_bytes + generated_bytes
        is_override = user_id in overrides
        quota_bytes = overrides[user_id] if is_override else default_quota_bytes
        percent = None if quota_bytes == 0 else round(total_bytes / quota_bytes * 100, 1)
        results.append({
            "user_id": user_id,
            "username": username,
            "uploads_bytes": uploads_bytes,
            "generated_bytes": generated_bytes,
            "total_bytes": total_bytes,
            "quota_bytes": quota_bytes,
            "is_override": is_override,
            "percent": percent,
        })

    results.sort(key=lambda r: r["total_bytes"], reverse=True)
    results = results[:limit]
    return JSONResponse(content={"success": True, "users": results})


@app.put("/api/admin/storage-quotas/users/{user_id}")
async def update_storage_quota_user(user_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Set or clear a per-user quota override (bytes >= 0, or null to clear)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    data = await request.json()
    if not isinstance(data, dict) or "quota_bytes" not in data:
        return JSONResponse(status_code=400, content={"success": False, "message": "Missing quota_bytes"})
    raw = data["quota_bytes"]
    if raw is None:
        new_value = None
    else:
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            return JSONResponse(status_code=400, content={"success": False, "message": "quota_bytes must be an integer >= 0 or null"})
        try:
            new_value = int(raw)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={"success": False, "message": "quota_bytes must be an integer >= 0 or null"})
        if new_value < 0 or new_value > storage_quota.MAX_QUOTA_BYTES:
            return JSONResponse(status_code=400, content={"success": False, "message": "quota_bytes must be an integer >= 0 or null"})

    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT storage_quota_bytes FROM USER_DETAILS WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"success": False, "message": "User not found"})
        old_value = int(row[0]) if row[0] is not None else None
        await conn.execute(
            "UPDATE USER_DETAILS SET storage_quota_bytes = ? WHERE user_id = ?",
            (new_value, user_id),
        )
        await conn.commit()

    await log_admin_action(
        admin_id=current_user.id,
        action_type="storage_quota_override_updated",
        request=request,
        target_user_id=user_id,
        details=f"Storage quota override changed from {old_value} to {new_value} bytes by '{current_user.username}'",
    )
    return JSONResponse(content={"success": True, "user_id": user_id, "quota_bytes": new_value})


@app.get("/api/admin/storage-quotas/stats")
async def get_storage_quota_stats(current_user: User = Depends(get_current_user)):
    """Global storage statistics (GROUP BY aggregation, no N+1)."""
    if current_user is None:
        return unauthenticated_response()
    if not await current_user.is_admin:
        return JSONResponse(status_code=403, content={"success": False, "message": "Admin access required"})

    async with get_db_connection(readonly=True) as conn:
        default_quota_bytes = await storage_quota.get_default_quota_bytes(conn)
        uploads_by_user = await _storage_quota_uploads_by_user(conn)
        generated_by_user = await _storage_quota_generated_by_user(conn)

        # Physical disk. Upload blobs and their thumbnails (variants) live in the
        # FILE_* tables, which are runtime-created and may be absent on a fresh DB.
        try:
            cursor = await conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM FILE_BLOBS")
            physical_blob_total = int((await cursor.fetchone())[0])
            cursor = await conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM FILE_BLOB_VARIANTS")
            physical_variants_total = int((await cursor.fetchone())[0])
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                physical_blob_total = 0
                physical_variants_total = 0
            else:
                raise

        cursor = await conn.execute("SELECT user_id, storage_quota_bytes FROM USER_DETAILS")
        detail_rows = await cursor.fetchall()

    # Logical charged bytes: uploads (dedupe-aware per user) + generated (1:1).
    uploads_logical_total = sum(uploads_by_user.values())
    generated_total = sum(generated_by_user.values())
    logical_total = uploads_logical_total + generated_total
    # Only uploads can dedupe; generated media is physical 1:1.
    dedupe_savings = uploads_logical_total - physical_blob_total

    users_over_80 = 0
    users_over_100 = 0
    for row in detail_rows:
        user_id = int(row[0])
        override = row[1]
        effective_quota = int(override) if override is not None else default_quota_bytes
        if effective_quota == 0:
            continue  # unlimited quota never counts toward over-threshold tallies
        total_bytes = uploads_by_user.get(user_id, 0) + generated_by_user.get(user_id, 0)
        if total_bytes >= effective_quota:
            users_over_100 += 1
        if total_bytes >= 0.8 * effective_quota:
            users_over_80 += 1

    return JSONResponse(content={
        "success": True,
        "logical_total": logical_total,
        "physical_blob_total": physical_blob_total,
        "physical_variants_total": physical_variants_total,
        "generated_total": generated_total,
        "dedupe_savings": dedupe_savings,
        "users_over_80": users_over_80,
        "users_over_100": users_over_100,
    })


# =============================================================================
# Home Page Backend - API endpoints and welcome file system
# =============================================================================

WELCOME_HTML_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "div", "span", "section", "article", "header", "footer", "nav", "main", "aside",
    "strong", "em", "b", "i", "u", "s", "mark", "small", "sub", "sup", "abbr", "cite",
    "code", "pre", "blockquote", "q",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    "img", "figure", "figcaption", "picture", "source",
    "video", "audio",
    "a", "br", "hr", "wbr",
    "details", "summary",
    "style",
}

WELCOME_HTML_ALLOWED_ATTRIBUTES = {
    "*": {"class", "style", "id", "title", "role", "aria-label", "aria-hidden", "data-*"},
    "a": {"href", "target"},
    "img": {"src", "alt", "width", "height", "loading"},
    "source": {"src", "srcset", "type", "media"},
    "video": {"src", "controls", "autoplay", "muted", "loop", "poster", "width", "height", "preload"},
    "audio": {"src", "controls", "autoplay", "muted", "loop", "preload"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "blockquote": {"cite"},
    "ol": {"start", "type"},
}


def sanitize_welcome_html(html: str) -> str:
    """Sanitize welcome HTML once at save time using nh3. <style> tag is allowed because content renders inside Shadow DOM, preventing CSS leaks."""
    return nh3.clean(
        html,
        tags=WELCOME_HTML_ALLOWED_TAGS,
        attributes=WELCOME_HTML_ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
        url_schemes={"http", "https", "mailto"},
    )


# Welcome message sanitizer (restricted -- renders inside Home page DOM, no Shadow DOM)
WELCOME_MSG_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "b", "i", "u", "s",
    "a", "ul", "ol", "li", "h3", "h4", "h5", "h6",
    "blockquote", "code", "pre", "small", "span",
}

WELCOME_MSG_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
}


def sanitize_welcome_message(html: str) -> str:
    """Sanitize welcome message HTML for safe injection into Home page DOM.
    More restrictive than sanitize_welcome_html() because messages are NOT
    isolated in Shadow DOM -- no style tags, no style attributes, no media,
    no id/class (prevents DOM clobbering and CSS collisions)."""
    return nh3.clean(
        html,
        tags=WELCOME_MSG_ALLOWED_TAGS,
        attributes=WELCOME_MSG_ALLOWED_ATTRIBUTES,
        link_rel="nofollow noopener noreferrer",
        url_schemes={"http", "https", "mailto"},
    )


async def _get_pack_info_for_path(pack_id: int) -> dict:
    """Get pack info needed for filesystem path resolution."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute('''
            SELECT p.name, p.created_by_user_id, u.username as created_by_username
            FROM PACKS p JOIN USERS u ON p.created_by_user_id = u.id
            WHERE p.id = ?
        ''', (pack_id,))
        result = await cursor.fetchone()
        if result:
            return dict(result)
        return None


async def _require_welcome_access(current_user: User, entity_type: str, entity_id: int) -> None:
    """Apply the same access policy to welcome HTML and its static assets."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        if entity_type == "pack":
            allowed = await can_user_access_pack(current_user, entity_id, cursor)
        else:
            allowed = await can_user_access_prompt(current_user, entity_id, cursor)

    if not allowed:
        # Missing and inaccessible worlds deliberately have the same response.
        raise HTTPException(status_code=404, detail="Welcome resource not found")


@app.get("/home/static/{world_tag}/{path:path}")
async def serve_welcome_static_scoped(
    world_tag: str,
    path: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """Serve static assets from a specific welcome world directory.

    world_tag format: 'p{prompt_id}' or 'k{pack_id}', e.g. 'p57', 'k1'.
    This avoids browser caching issues when switching between worlds.
    """
    if current_user is None:
        return unauthenticated_response()

    try:
        # Parse world_tag
        if len(world_tag) < 2 or world_tag[0] not in ('p', 'k'):
            raise HTTPException(status_code=400, detail="Invalid world tag")
        try:
            entity_id = int(world_tag[1:])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid world tag")

        entity_type = 'prompt' if world_tag[0] == 'p' else 'pack'
        await _require_welcome_access(current_user, entity_type, entity_id)

        # Resolve filesystem path
        if world_tag[0] == 'p':
            info = await get_prompt_info(entity_id)
            entity_path = get_prompt_path(entity_id, info)
        else:
            from welcome_service import _get_pack_info
            pack_info = await _get_pack_info(entity_id)
            if not pack_info:
                raise HTTPException(status_code=404, detail="Pack not found")
            from prompts import get_pack_path
            entity_path = get_pack_path(entity_id, pack_info)

        if not entity_path:
            raise HTTPException(status_code=404, detail="Entity not found")

        welcome_static_dir = Path(entity_path, "welcome", "static").resolve()
        real_file = (welcome_static_dir / path).resolve()

        # Path-aware containment also blocks similarly-prefixed sibling folders
        # and symlinks that resolve outside the world's static directory.
        if not real_file.is_relative_to(welcome_static_dir):
            raise HTTPException(status_code=403, detail="Access denied")

        if not real_file.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(real_file)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error serving welcome static: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/home", response_class=HTMLResponse)
async def home_page(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return RedirectResponse(url="/login")
    context = await get_template_context(request, current_user)
    return templates.TemplateResponse("home.html", context)


@app.get("/welcome/{entity_type}/{entity_id}", response_class=HTMLResponse)
async def welcome_page(request: Request, entity_type: str, entity_id: int,
                       current_user: User = Depends(get_current_user)):
    if current_user is None:
        return RedirectResponse(url="/login")
    if entity_type not in ("prompt", "pack"):
        raise HTTPException(status_code=404)
    await _require_welcome_access(current_user, entity_type, entity_id)
    # Build and serve
    world = await build_world(entity_type, entity_id)
    if not world:
        raise HTTPException(status_code=404, detail="No welcome page found")
    return await serve_welcome_world(request, current_user, world)


@app.get("/api/home")
async def get_home_data(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        user_id = current_user.id

        # Get user's accessible packs via entitlements
        await cursor.execute(f'''
            SELECT p.id, p.name, p.slug, p.description, p.cover_image,
                   p.created_by_user_id, creator.username AS created_by_username,
                   (SELECT COUNT(*) FROM PACK_ITEMS pi WHERE pi.pack_id = p.id AND pi.is_active = 1) as prompt_count
            FROM PACKS p
            JOIN ENTITLEMENTS e ON e.asset_type = 'pack' AND e.asset_id = p.id
            JOIN USERS creator ON creator.id = p.created_by_user_id
            WHERE e.user_id = ? AND {active_entitlement_condition("e")}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 50
        ''', (user_id,))
        packs = [dict(row) for row in await cursor.fetchall()]

        # Check welcome files for packs
        for pack in packs:
            try:
                pack_dir = get_pack_path(pack['id'], pack)
                pack['has_welcome'] = os.path.isfile(
                    os.path.join(pack_dir, "welcome", "index.html")
                )
            except Exception:
                pack['has_welcome'] = False
            pack.pop('created_by_username', None)

        # Get user's accessible prompts not in any pack
        await cursor.execute('''
            SELECT ud.all_prompts_access, ud.current_prompt_id, ud.public_prompts_access, ud.category_access
            FROM USER_DETAILS ud WHERE ud.user_id = ?
        ''', (user_id,))
        ud = await cursor.fetchone()
        all_prompts_access = bool(ud['all_prompts_access']) if ud else False
        public_prompts_access = bool(ud['public_prompts_access']) if ud else False
        category_access = ud['category_access'] if ud else None

        pack_exclusion = f'''
            AND p.id NOT IN (
                SELECT pi.prompt_id FROM PACK_ITEMS pi
                JOIN ENTITLEMENTS e_pack ON e_pack.asset_type = 'pack'
                    AND e_pack.asset_id = pi.pack_id
                WHERE e_pack.user_id = ? AND pi.is_active = 1
                  AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
                  AND {active_entitlement_condition("e_pack")}
            )
        '''

        if all_prompts_access:
            await cursor.execute('''
                SELECT p.id, p.name, p.description, p.image, p.extensions_enabled,
                       creator.username AS created_by_username,
                       (SELECT COUNT(*) FROM PROMPT_EXTENSIONS pe WHERE pe.prompt_id = p.id) as extension_count,
                       CASE WHEN p.created_by_user_id = ? THEN 1
                            WHEN EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS pp2 WHERE pp2.prompt_id = p.id AND pp2.user_id = ? AND pp2.permission_level = 'owner') THEN 1
                            ELSE 0 END as is_mine
                FROM PROMPTS p
                JOIN USERS creator ON creator.id = p.created_by_user_id
                WHERE 1=1
            ''' + pack_exclusion + '''
                ORDER BY p.name
                LIMIT 50
            ''', (user_id, user_id, user_id))
        else:
            query = f'''
                SELECT DISTINCT p.id, p.name, p.description, p.image, p.extensions_enabled,
                       creator.username AS created_by_username,
                       (SELECT COUNT(*) FROM PROMPT_EXTENSIONS pe WHERE pe.prompt_id = p.id) as extension_count,
                       CASE WHEN p.created_by_user_id = ? THEN 1
                            WHEN EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS pp2 WHERE pp2.prompt_id = p.id AND pp2.user_id = ? AND pp2.permission_level = 'owner') THEN 1
                            ELSE 0 END as is_mine
                FROM PROMPTS p
                JOIN USERS creator ON creator.id = p.created_by_user_id
                LEFT JOIN PROMPT_PERMISSIONS pp ON p.id = pp.prompt_id AND pp.user_id = ?
                WHERE (
                    EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS pp2 WHERE pp2.prompt_id = p.id AND pp2.user_id = ? AND pp2.permission_level IN ('owner', 'edit'))
                    OR EXISTS (
                        SELECT 1 FROM ENTITLEMENTS e_prompt
                        WHERE e_prompt.user_id = ?
                          AND e_prompt.asset_type = 'prompt'
                          AND e_prompt.asset_id = p.id
                          AND {active_entitlement_condition("e_prompt")}
                    )
            '''
            params = [user_id, user_id, user_id, user_id, user_id]

            if public_prompts_access and marketplace_discovery_enabled():
                if category_access is None:
                    query += " OR (p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0))"
                else:
                    query += """ OR (p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0) AND EXISTS (
                        SELECT 1 FROM PROMPT_CATEGORIES pc
                        WHERE pc.prompt_id = p.id
                        AND pc.category_id IN (SELECT value FROM json_each(?))
                    ))"""
                    params.append(category_access)

            query += ")" + pack_exclusion + " ORDER BY p.name LIMIT 50"
            params.append(user_id)
            await cursor.execute(query, params)

        loose_prompts = [dict(row) for row in await cursor.fetchall()]

        # Check welcome files for loose prompts
        for p in loose_prompts:
            try:
                prompt_dir = get_prompt_path(p['id'], p)
                p['has_welcome'] = os.path.isfile(os.path.join(prompt_dir, "welcome", "index.html"))
            except Exception:
                p['has_welcome'] = False
            p.pop('created_by_username', None)

        # Get user branding via UCR (USER_CREATOR_RELATIONSHIPS)
        branding = None
        await cursor.execute('''
            SELECT ub.company_name, ub.logo_url, ub.brand_color_primary, ub.brand_color_secondary,
                   ub.footer_text, ub.forced_theme, ub.disable_theme_selector, ub.hide_platform_branding
            FROM USER_CREATOR_RELATIONSHIPS ucr
            JOIN USER_BRANDING ub ON ub.user_id = ucr.creator_id
            WHERE ucr.user_id = ? AND ucr.is_primary = 1
        ''', (user_id,))
        branding_row = await cursor.fetchone()
        if branding_row:
            branding = dict(branding_row)

        # Favorites
        await cursor.execute("SELECT prompt_id FROM FAVORITE_PROMPTS WHERE user_id = ?", (user_id,))
        favorites = [row['prompt_id'] for row in await cursor.fetchall()]

        # Stats
        await cursor.execute(
            """
            SELECT COUNT(*) as cnt
            FROM CONVERSATIONS
            WHERE user_id = ?
              AND COALESCE(hidden_from_history, 0) = 0
            """,
            (user_id,),
        )
        conv_count = (await cursor.fetchone())['cnt']

        await cursor.execute(
            """
            SELECT COUNT(*) as cnt
            FROM MESSAGES m
            JOIN CONVERSATIONS c ON c.id = m.conversation_id
            WHERE m.user_id = ?
              AND m.is_bookmarked = 1
              AND COALESCE(c.hidden_from_history, 0) = 0
            """,
            (user_id,),
        )
        bookmarks_count = (await cursor.fetchone())['cnt']

        # Latest public marketplace prompts
        latest_prompts = []
        if marketplace_discovery_enabled() and (public_prompts_access or all_prompts_access):
            if all_prompts_access:
                await cursor.execute('''
                    SELECT p.id, p.name, p.created_at,
                           creator.username AS created_by_username
                    FROM PROMPTS p
                    JOIN USERS creator ON creator.id = p.created_by_user_id
                    WHERE p.public = 1
                    ORDER BY p.created_at DESC LIMIT 5
                ''')
            else:
                await cursor.execute('''
                    SELECT p.id, p.name, p.created_at,
                           creator.username AS created_by_username
                    FROM PROMPTS p
                    JOIN USERS creator ON creator.id = p.created_by_user_id
                    WHERE p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0)
                    ORDER BY p.created_at DESC LIMIT 5
                ''')
            latest_prompts = [dict(row) for row in await cursor.fetchall()]

            # Check welcome files for latest prompts
            for p in latest_prompts:
                try:
                    prompt_dir = get_prompt_path(p['id'], p)
                    p['has_welcome'] = os.path.isfile(os.path.join(prompt_dir, "welcome", "index.html"))
                except Exception:
                    p['has_welcome'] = False
                p.pop('created_by_username', None)

        # Generate prompt image URLs for loose prompts
        new_expiration = datetime.now(timezone.utc) + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)
        for p in loose_prompts:
            p['image_url'] = None
            if p.get('image'):
                try:
                    img_path = f"{p['image']}_128.webp"
                    token = generate_img_token(img_path, new_expiration, current_user)
                    p['image_url'] = f"{CLOUDFLARE_BASE_URL}{img_path}?token={token}"
                except Exception:
                    pass

        # Generate cover_image URLs for packs
        for pack in packs:
            pack['cover_image_url'] = None
            if pack.get('cover_image'):
                try:
                    token = generate_img_token(pack['cover_image'], new_expiration, current_user)
                    pack['cover_image_url'] = f"{CLOUDFLARE_BASE_URL}{pack['cover_image']}?token={token}"
                except Exception:
                    pass

        # Fetch home preferences for dock state
        await cursor.execute("SELECT home_preferences FROM USER_DETAILS WHERE user_id = ?", (user_id,))
        prefs_row = await cursor.fetchone()
        home_preferences = {}
        if prefs_row and prefs_row['home_preferences']:
            try:
                home_preferences = json.loads(prefs_row['home_preferences'])
            except (json.JSONDecodeError, TypeError):
                pass

    return JSONResponse(content={
        "user": {"id": current_user.id, "username": current_user.username},
        "packs": packs,
        "prompts": loose_prompts,
        "latest_prompts": latest_prompts,
        "branding": branding,
        "favorites": favorites,
        "home_preferences": home_preferences,
        "stats": {
            "conversation_count": conv_count,
            "bookmarks_count": bookmarks_count,
        },
    })


@app.put("/api/home/preferences")
async def update_home_preferences(request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    body = await request.json()
    user_id = current_user.id

    # Whitelist valid keys
    valid_keys = {"pinned_prompt_id", "pinned_pack_id", "show_stats", "after_login", "minimized_windows"}
    updates = {k: v for k, v in body.items() if k in valid_keys}

    # Validate show_stats is boolean
    if "show_stats" in updates and not isinstance(updates["show_stats"], bool):
        return JSONResponse(content={"error": "show_stats must be boolean"}, status_code=400)

    # Validate after_login is a known route
    if "after_login" in updates:
        allowed_routes = {"/home", "/chat", "/dashboard"}
        if marketplace_discovery_enabled():
            allowed_routes.add("/explore")
        if updates["after_login"] not in allowed_routes:
            return JSONResponse(content={"error": "Invalid after_login route"}, status_code=400)

    # Validate minimized_windows is a list of known window IDs
    if "minimized_windows" in updates:
        mw = updates["minimized_windows"]
        if not isinstance(mw, list):
            return JSONResponse(content={"error": "minimized_windows must be a list"}, status_code=400)
        allowed_windows = {"welcome", "latest", "library"}
        updates["minimized_windows"] = sorted(set(mw) & allowed_windows)

    # Validate pinned_prompt_id
    if "pinned_prompt_id" in updates and updates["pinned_prompt_id"] is not None:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            if not await can_user_access_prompt(current_user, int(updates["pinned_prompt_id"]), cursor):
                return JSONResponse(content={"error": "Prompt not accessible"}, status_code=403)

    # Validate pinned_pack_id
    if "pinned_pack_id" in updates and updates["pinned_pack_id"] is not None:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            if not await can_user_access_pack(current_user, int(updates["pinned_pack_id"]), cursor):
                return JSONResponse(content={"error": "Pack not accessible"}, status_code=403)

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        # Get current preferences
        await cursor.execute("SELECT home_preferences FROM USER_DETAILS WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        current_prefs = {}
        if row and row['home_preferences']:
            try:
                current_prefs = json.loads(row['home_preferences'])
            except (json.JSONDecodeError, TypeError):
                pass

        # Merge updates
        current_prefs.update(updates)

        # Clean legacy keys from old Home Screen system
        for legacy_key in ("home_start", "home_fixed_world", "active_world"):
            current_prefs.pop(legacy_key, None)

        await cursor.execute(
            "UPDATE USER_DETAILS SET home_preferences = ? WHERE user_id = ?",
            (json.dumps(current_prefs), user_id)
        )
        await conn.commit()

    return JSONResponse(content={"success": True, "preferences": current_prefs})


@app.post("/api/home/favorites/{prompt_id}")
async def toggle_favorite_prompt(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    user_id = current_user.id

    async with get_db_connection() as conn:
        cursor = await conn.cursor()

        # Check access
        if not await can_user_access_prompt(current_user, prompt_id, cursor):
            return JSONResponse(content={"error": "Prompt not accessible"}, status_code=403)

        # Check if already favorited
        await cursor.execute(
            "SELECT 1 FROM FAVORITE_PROMPTS WHERE user_id = ? AND prompt_id = ?",
            (user_id, prompt_id)
        )
        exists = await cursor.fetchone()

        if exists:
            await cursor.execute(
                "DELETE FROM FAVORITE_PROMPTS WHERE user_id = ? AND prompt_id = ?",
                (user_id, prompt_id)
            )
            is_favorite = False
        else:
            await cursor.execute(
                "INSERT INTO FAVORITE_PROMPTS (user_id, prompt_id) VALUES (?, ?)",
                (user_id, prompt_id)
            )
            is_favorite = True

        await conn.commit()

    return JSONResponse(content={"is_favorite": is_favorite})


@app.get("/api/home/pack/{pack_id}")
async def get_home_pack(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        if not await can_user_access_pack(current_user, pack_id, cursor):
            raise HTTPException(status_code=403, detail="Access denied")

        # Pack info
        await cursor.execute('''
            SELECT p.id, p.name, p.slug, p.description, p.cover_image, p.tags,
                   p.created_by_user_id, u.username as created_by_username,
                   u.profile_picture as creator_profile_picture
            FROM PACKS p
            JOIN USERS u ON p.created_by_user_id = u.id
            WHERE p.id = ?
        ''', (pack_id,))
        pack = await cursor.fetchone()
        if not pack:
            raise HTTPException(status_code=404, detail="Pack not found")
        pack_data = dict(pack)

        # Generate signed URLs for pack cover and creator avatar
        new_expiration = datetime.now(timezone.utc) + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)

        pack_data['cover_image_url'] = None
        if pack_data.get('cover_image'):
            try:
                token = generate_img_token(pack_data['cover_image'], new_expiration, current_user)
                pack_data['cover_image_url'] = f"{CLOUDFLARE_BASE_URL}{pack_data['cover_image']}?token={token}"
            except Exception:
                pass

        pack_data['creator_avatar_url'] = None
        creator_pic = pack_data.pop('creator_profile_picture', None)
        if creator_pic:
            try:
                avatar_path = f"{creator_pic}_64.webp"
                token = generate_img_token(avatar_path, new_expiration, current_user)
                pack_data['creator_avatar_url'] = f"{CLOUDFLARE_BASE_URL}{avatar_path}?token={token}"
            except Exception:
                pass

        # Parse tags from JSON string to list
        try:
            pack_data['tags'] = json.loads(pack_data['tags']) if pack_data.get('tags') else []
        except (json.JSONDecodeError, TypeError):
            pack_data['tags'] = []

        # Prompts in this pack
        await cursor.execute('''
            SELECT pr.id, pr.name, pr.description, pr.image, pr.extensions_enabled,
                   creator.username AS created_by_username,
                   (SELECT COUNT(*) FROM PROMPT_EXTENSIONS pe WHERE pe.prompt_id = pr.id) as extension_count
            FROM PROMPTS pr
            JOIN PACK_ITEMS pi ON pi.prompt_id = pr.id
            JOIN USERS creator ON creator.id = pr.created_by_user_id
            WHERE pi.pack_id = ? AND pi.is_active = 1
            ORDER BY pi.display_order
        ''', (pack_id,))
        prompts = [dict(row) for row in await cursor.fetchall()]

        # Generate signed image URLs for prompt avatars
        for p in prompts:
            p['image_url'] = None
            if p.get('image'):
                try:
                    img_path = f"{p['image']}_64.webp"
                    token = generate_img_token(img_path, new_expiration, current_user)
                    p['image_url'] = f"{CLOUDFLARE_BASE_URL}{img_path}?token={token}"
                except Exception:
                    pass

        # Resolve all enabled prompt extensions in one query, preserving per-prompt order.
        enabled_prompt_ids = [p['id'] for p in prompts if p['extensions_enabled']]
        extensions_by_prompt = {prompt_id: [] for prompt_id in enabled_prompt_ids}
        if enabled_prompt_ids:
            placeholders = ",".join("?" for _ in enabled_prompt_ids)
            await cursor.execute(
                f'''SELECT prompt_id, id, name, slug, description
                    FROM PROMPT_EXTENSIONS
                    WHERE prompt_id IN ({placeholders})
                    ORDER BY prompt_id, display_order''',
                enabled_prompt_ids,
            )
            for row in await cursor.fetchall():
                extension = dict(row)
                prompt_id_for_extension = extension.pop('prompt_id')
                extensions_by_prompt[prompt_id_for_extension].append(extension)

        for p in prompts:
            p['extensions'] = extensions_by_prompt.get(p['id'], [])

        # Check welcome files for pack prompts
        for p in prompts:
            try:
                prompt_dir = get_prompt_path(p['id'], p)
                p['has_welcome'] = os.path.isfile(os.path.join(prompt_dir, "welcome", "index.html"))
            except Exception:
                p['has_welcome'] = False
            p.pop('created_by_username', None)

        # Recent conversations in this pack
        await cursor.execute('''
            SELECT c.id, c.chat_name, c.start_date, c.role_id,
                   pr.name as prompt_name, pe.name as active_extension_name
            FROM CONVERSATIONS c
            JOIN PACK_ITEMS pi ON pi.prompt_id = c.role_id AND pi.pack_id = ?
            LEFT JOIN PROMPTS pr ON c.role_id = pr.id
            LEFT JOIN PROMPT_EXTENSIONS pe ON c.active_extension_id = pe.id
            WHERE c.user_id = ? AND pi.is_active = 1
              AND COALESCE(c.hidden_from_history, 0) = 0
            ORDER BY c.last_activity DESC
            LIMIT 10
        ''', (pack_id, current_user.id))
        recent_chats = [dict(row) for row in await cursor.fetchall()]

    # Check for welcome file
    try:
        pack_dir = get_pack_path(pack_id, pack_data)
        welcome_path = os.path.join(pack_dir, "welcome", "index.html")
        pack_data['has_welcome'] = os.path.isfile(welcome_path)
    except Exception:
        pack_data['has_welcome'] = False

    return JSONResponse(content={
        "pack": pack_data,
        "prompts": prompts,
        "recent_chats": recent_chats
    })


@app.get("/api/home/prompt/{prompt_id}")
async def get_home_prompt(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    await ensure_conversation_privacy_schema()
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        # Verify prompt access
        if not await can_user_access_prompt(current_user, prompt_id, cursor):
            raise HTTPException(status_code=403, detail="Access denied")

        await cursor.execute('''
            SELECT p.id, p.name, p.description, p.image, p.extensions_enabled,
                   p.extensions_free_selection, p.created_by_user_id,
                   u.username as created_by_username,
                   u.profile_picture as creator_profile_picture
            FROM PROMPTS p
            JOIN USERS u ON p.created_by_user_id = u.id
            WHERE p.id = ?
        ''', (prompt_id,))
        prompt = await cursor.fetchone()
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")
        prompt_data = dict(prompt)

        # Generate signed URLs for prompt image and creator avatar
        new_expiration = datetime.now(timezone.utc) + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)

        prompt_data['image_url'] = None
        if prompt_data.get('image'):
            try:
                img_path = f"{prompt_data['image']}_128.webp"
                token = generate_img_token(img_path, new_expiration, current_user)
                prompt_data['image_url'] = f"{CLOUDFLARE_BASE_URL}{img_path}?token={token}"
            except Exception:
                pass

        prompt_data['creator_avatar_url'] = None
        creator_pic = prompt_data.pop('creator_profile_picture', None)
        if creator_pic:
            try:
                avatar_path = f"{creator_pic}_64.webp"
                token = generate_img_token(avatar_path, new_expiration, current_user)
                prompt_data['creator_avatar_url'] = f"{CLOUDFLARE_BASE_URL}{avatar_path}?token={token}"
            except Exception:
                pass

        # Extensions
        extensions = []
        if prompt_data['extensions_enabled']:
            await cursor.execute('''
                SELECT id, name, slug, description FROM PROMPT_EXTENSIONS
                WHERE prompt_id = ? ORDER BY display_order
            ''', (prompt_id,))
            extensions = [dict(r) for r in await cursor.fetchall()]

        # Recent conversations
        await cursor.execute('''
            SELECT c.id, c.chat_name, c.start_date,
                   pe.name as active_extension_name
            FROM CONVERSATIONS c
            LEFT JOIN PROMPT_EXTENSIONS pe ON c.active_extension_id = pe.id
            WHERE c.user_id = ? AND c.role_id = ?
              AND COALESCE(c.hidden_from_history, 0) = 0
            ORDER BY c.last_activity DESC
            LIMIT 10
        ''', (current_user.id, prompt_id))
        recent_chats = [dict(row) for row in await cursor.fetchall()]

    # Check for welcome file
    has_welcome = False
    try:
        pi = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, pi)
        has_welcome = os.path.isfile(os.path.join(prompt_dir, "welcome", "index.html"))
    except Exception:
        pass
    prompt_data['has_welcome'] = has_welcome

    return JSONResponse(content={
        "prompt": prompt_data,
        "extensions": extensions,
        "recent_chats": recent_chats
    })


@app.get("/api/home/prompt/{prompt_id}/welcome")
async def get_prompt_welcome(prompt_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        if not await can_user_access_prompt(current_user, prompt_id, cursor):
            raise HTTPException(status_code=403, detail="Access denied")

    try:
        pi = await get_prompt_info(prompt_id)
        prompt_dir = get_prompt_path(prompt_id, pi)
        welcome_path = os.path.join(prompt_dir, "welcome", "index.html")
        if os.path.isfile(welcome_path):
            with open(welcome_path, 'r', encoding='utf-8') as f:
                return JSONResponse(content={"html": f.read()})
    except Exception:
        pass
    return JSONResponse(content={"html": ""}, status_code=404)


@app.get("/api/home/pack/{pack_id}/welcome")
async def get_pack_welcome(pack_id: int, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        if not await can_user_access_pack(current_user, pack_id, cursor):
            raise HTTPException(status_code=403, detail="Access denied")

    try:
        pack_info = await _get_pack_info_for_path(pack_id)
        if pack_info:
            pack_dir = get_pack_path(pack_id, pack_info)
            welcome_path = os.path.join(pack_dir, "welcome", "index.html")
            if os.path.isfile(welcome_path):
                with open(welcome_path, 'r', encoding='utf-8') as f:
                    return JSONResponse(content={"html": f.read()})
    except Exception:
        pass
    return JSONResponse(content={"html": ""}, status_code=404)


@app.put("/api/home/prompt/{prompt_id}/welcome")
async def save_prompt_welcome(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    is_admin_user = await is_admin(current_user.id)
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin_user):
        raise HTTPException(status_code=403, detail="Access denied")

    body = await request.json()
    html_content = body.get("html", "")

    MAX_WELCOME_HTML_SIZE = 512 * 1024  # 512 KB
    if len(html_content) > MAX_WELCOME_HTML_SIZE:
        raise HTTPException(status_code=413, detail="Welcome HTML content exceeds maximum size (512 KB)")

    pi = await get_prompt_info(prompt_id)
    prompt_dir = get_prompt_path(prompt_id, pi)
    welcome_path = os.path.join(prompt_dir, "welcome", "index.html")

    if html_content.strip():
        clean_html = sanitize_welcome_html(html_content)
        os.makedirs(os.path.join(prompt_dir, "welcome"), exist_ok=True)
        with open(welcome_path, 'w', encoding='utf-8') as f:
            f.write(clean_html)
    else:
        # Empty content = remove welcome
        if os.path.isfile(welcome_path):
            os.remove(welcome_path)

    return JSONResponse(content={"success": True})


@app.put("/api/home/pack/{pack_id}/welcome")
async def save_pack_welcome(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    if current_user is None:
        return unauthenticated_response()

    is_admin_user = await is_admin(current_user.id)

    # Check pack ownership
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT created_by_user_id FROM PACKS WHERE id = ?", (pack_id,))
        pack = await cursor.fetchone()
        if not pack:
            raise HTTPException(status_code=404, detail="Pack not found")
        if pack['created_by_user_id'] != current_user.id and not is_admin_user:
            raise HTTPException(status_code=403, detail="Access denied")

    body = await request.json()
    html_content = body.get("html", "")

    MAX_WELCOME_HTML_SIZE = 512 * 1024  # 512 KB
    if len(html_content) > MAX_WELCOME_HTML_SIZE:
        raise HTTPException(status_code=413, detail="Welcome HTML content exceeds maximum size (512 KB)")

    pack_info = await _get_pack_info_for_path(pack_id)
    if not pack_info:
        raise HTTPException(status_code=404, detail="Pack info not found")

    pack_dir = get_pack_path(pack_id, pack_info)
    welcome_path = os.path.join(pack_dir, "welcome", "index.html")

    if html_content.strip():
        clean_html = sanitize_welcome_html(html_content)
        os.makedirs(os.path.join(pack_dir, "welcome"), exist_ok=True)
        with open(welcome_path, 'w', encoding='utf-8') as f:
            f.write(clean_html)
    else:
        if os.path.isfile(welcome_path):
            os.remove(welcome_path)

    return JSONResponse(content={"success": True})


# =============================================================================
# Welcome Messages API (DB-backed welcome messages, separate from filesystem welcome pages)
# NOTE: Access logic for the listing endpoint (GET /api/home/welcome-messages) mirrors
#       /api/home and must be updated if /api/home access logic changes.
# =============================================================================

@app.put("/api/home/prompt/{prompt_id}/welcome-message")
async def save_prompt_welcome_message(prompt_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Save or update a DB-backed welcome message for a prompt."""
    if current_user is None:
        return unauthenticated_response()

    is_admin_user = await is_admin(current_user.id)
    if not await can_manage_prompt(current_user.id, prompt_id, is_admin_user):
        raise HTTPException(status_code=403, detail="Access denied")

    body = await request.json()
    content = body.get("message", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="message must be a string")

    MAX_WELCOME_MSG_SIZE = 10 * 1024  # 10,240 characters
    if len(content) > MAX_WELCOME_MSG_SIZE:
        raise HTTPException(status_code=413, detail="Welcome message exceeds maximum size (10 KB)")

    sanitized = sanitize_welcome_message(content)
    is_active = 1 if sanitized.strip() else 0
    final_content = sanitized if is_active else ""

    async with get_db_connection(readonly=False) as conn:
        cursor = await conn.cursor()

        # Fetch existing content for change detection
        await cursor.execute(
            "SELECT id, content, last_notified_at FROM WELCOME_MESSAGES WHERE entity_type = 'prompt' AND entity_id = ?",
            (prompt_id,)
        )
        existing = await cursor.fetchone()
        old_content = existing['content'] if existing else None

        # Upsert (never INSERT OR REPLACE -- preserves WELCOME_MESSAGE_READS FK)
        await cursor.execute("""
            INSERT INTO WELCOME_MESSAGES (entity_type, entity_id, content, is_active, updated_at)
            VALUES ('prompt', ?, ?, ?, datetime('now'))
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                content = excluded.content,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
        """, (prompt_id, final_content, is_active))

        # Cooldown-based read reset: if content actually changed and >= 7 days since last notify
        if old_content is not None and final_content != old_content:
            last_notified = existing['last_notified_at'] if existing else None
            reset_reads = False
            if last_notified is None:
                reset_reads = True
            else:
                try:
                    notified_dt = datetime.fromisoformat(last_notified)
                    if notified_dt.tzinfo is None:
                        notified_dt = notified_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - notified_dt).days >= 7:
                        reset_reads = True
                except (ValueError, TypeError):
                    reset_reads = True

            if reset_reads:
                # Get the welcome_message_id (may be new or existing)
                await cursor.execute(
                    "SELECT id FROM WELCOME_MESSAGES WHERE entity_type = 'prompt' AND entity_id = ?",
                    (prompt_id,)
                )
                wm_row = await cursor.fetchone()
                if wm_row:
                    await cursor.execute(
                        "DELETE FROM WELCOME_MESSAGE_READS WHERE welcome_message_id = ? AND muted = 0",
                        (wm_row['id'],)
                    )
                    await cursor.execute(
                        "UPDATE WELCOME_MESSAGES SET last_notified_at = datetime('now') WHERE id = ?",
                        (wm_row['id'],)
                    )

        await conn.commit()

    return JSONResponse(content={"success": True})


@app.put("/api/home/pack/{pack_id}/welcome-message")
async def save_pack_welcome_message(pack_id: int, request: Request, current_user: User = Depends(get_current_user)):
    """Save or update a DB-backed welcome message for a pack."""
    if current_user is None:
        return unauthenticated_response()

    is_admin_user = await is_admin(current_user.id)

    # Check pack ownership (same pattern as save_pack_welcome)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute("SELECT created_by_user_id FROM PACKS WHERE id = ?", (pack_id,))
        pack = await cursor.fetchone()
        if not pack:
            raise HTTPException(status_code=404, detail="Pack not found")
        if pack['created_by_user_id'] != current_user.id and not is_admin_user:
            raise HTTPException(status_code=403, detail="Access denied")

    body = await request.json()
    content = body.get("message", "")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="message must be a string")

    MAX_WELCOME_MSG_SIZE = 10 * 1024  # 10,240 characters
    if len(content) > MAX_WELCOME_MSG_SIZE:
        raise HTTPException(status_code=413, detail="Welcome message exceeds maximum size (10 KB)")

    sanitized = sanitize_welcome_message(content)
    is_active = 1 if sanitized.strip() else 0
    final_content = sanitized if is_active else ""

    async with get_db_connection(readonly=False) as conn:
        cursor = await conn.cursor()

        # Fetch existing content for change detection
        await cursor.execute(
            "SELECT id, content, last_notified_at FROM WELCOME_MESSAGES WHERE entity_type = 'pack' AND entity_id = ?",
            (pack_id,)
        )
        existing = await cursor.fetchone()
        old_content = existing['content'] if existing else None

        # Upsert (never INSERT OR REPLACE -- preserves WELCOME_MESSAGE_READS FK)
        await cursor.execute("""
            INSERT INTO WELCOME_MESSAGES (entity_type, entity_id, content, is_active, updated_at)
            VALUES ('pack', ?, ?, ?, datetime('now'))
            ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                content = excluded.content,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at
        """, (pack_id, final_content, is_active))

        # Cooldown-based read reset: if content actually changed and >= 7 days since last notify
        if old_content is not None and final_content != old_content:
            last_notified = existing['last_notified_at'] if existing else None
            reset_reads = False
            if last_notified is None:
                reset_reads = True
            else:
                try:
                    notified_dt = datetime.fromisoformat(last_notified)
                    if notified_dt.tzinfo is None:
                        notified_dt = notified_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - notified_dt).days >= 7:
                        reset_reads = True
                except (ValueError, TypeError):
                    reset_reads = True

            if reset_reads:
                await cursor.execute(
                    "SELECT id FROM WELCOME_MESSAGES WHERE entity_type = 'pack' AND entity_id = ?",
                    (pack_id,)
                )
                wm_row = await cursor.fetchone()
                if wm_row:
                    await cursor.execute(
                        "DELETE FROM WELCOME_MESSAGE_READS WHERE welcome_message_id = ? AND muted = 0",
                        (wm_row['id'],)
                    )
                    await cursor.execute(
                        "UPDATE WELCOME_MESSAGES SET last_notified_at = datetime('now') WHERE id = ?",
                        (wm_row['id'],)
                    )

        await conn.commit()

    return JSONResponse(content={"success": True})


@app.get("/api/home/prompt/{prompt_id}/welcome-message")
async def get_prompt_welcome_message(prompt_id: int, current_user: User = Depends(get_current_user)):
    """Get the active welcome message for a single prompt."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        # Access check (return 404 instead of 403 to prevent enumeration)
        if not await can_user_access_prompt(current_user, prompt_id, cursor):
            raise HTTPException(status_code=404, detail="Not found")

        await cursor.execute(
            "SELECT content, updated_at FROM WELCOME_MESSAGES WHERE entity_type = 'prompt' AND entity_id = ? AND is_active = 1",
            (prompt_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No welcome message found")

    return JSONResponse(content={"message": row['content'], "updated_at": row['updated_at']})


@app.get("/api/home/pack/{pack_id}/welcome-message")
async def get_pack_welcome_message(pack_id: int, current_user: User = Depends(get_current_user)):
    """Get the active welcome message for a single pack."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()

        # Access check (return 404 instead of 403 to prevent enumeration)
        if not await can_user_access_pack(current_user, pack_id, cursor):
            raise HTTPException(status_code=404, detail="Not found")

        await cursor.execute(
            "SELECT content, updated_at FROM WELCOME_MESSAGES WHERE entity_type = 'pack' AND entity_id = ? AND is_active = 1",
            (pack_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No welcome message found")

    return JSONResponse(content={"message": row['content'], "updated_at": row['updated_at']})


@app.get("/api/home/welcome-messages")
async def get_all_welcome_messages(current_user: User = Depends(get_current_user)):
    """Get all active welcome messages for prompts/packs the current user can access.
    Access logic mirrors /api/home -- update both if access rules change."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        user_id = current_user.id
        is_admin_user = await is_admin(user_id)

        # Get user details for access determination
        await cursor.execute(
            "SELECT all_prompts_access FROM USER_DETAILS WHERE user_id = ?",
            (user_id,)
        )
        ud = await cursor.fetchone()
        all_prompts_access = bool(ud['all_prompts_access']) if ud else False

        # --- Prompt welcome messages ---
        if is_admin_user or all_prompts_access:
            # Admin or all_prompts_access: all active prompt welcome messages
            prompt_query = """
                SELECT wm.id, wm.entity_type, wm.entity_id, wm.content, wm.updated_at,
                       p.name, p.image, p.has_welcome_page,
                       (SELECT u.username FROM USERS u
                        JOIN PROMPT_PERMISSIONS pp ON pp.user_id = u.id
                        WHERE pp.prompt_id = p.id AND pp.permission_level = 'owner' LIMIT 1) as creator_name,
                       wmr.read_at, COALESCE(wmr.muted, 0) as muted
                FROM WELCOME_MESSAGES wm
                JOIN PROMPTS p ON wm.entity_id = p.id AND wm.entity_type = 'prompt'
                LEFT JOIN WELCOME_MESSAGE_READS wmr ON wmr.welcome_message_id = wm.id AND wmr.user_id = ?
                WHERE wm.is_active = 1
            """
            prompt_params = [user_id]
        else:
            # Regular user: only prompts with owner/editor permissions or entitlements
            prompt_query = f"""
                SELECT wm.id, wm.entity_type, wm.entity_id, wm.content, wm.updated_at,
                       p.name, p.image, p.has_welcome_page,
                       (SELECT u.username FROM USERS u
                        JOIN PROMPT_PERMISSIONS pp2 ON pp2.user_id = u.id
                        WHERE pp2.prompt_id = p.id AND pp2.permission_level = 'owner' LIMIT 1) as creator_name,
                       wmr.read_at, COALESCE(wmr.muted, 0) as muted
                FROM WELCOME_MESSAGES wm
                JOIN PROMPTS p ON wm.entity_id = p.id AND wm.entity_type = 'prompt'
                LEFT JOIN WELCOME_MESSAGE_READS wmr ON wmr.welcome_message_id = wm.id AND wmr.user_id = ?
                WHERE wm.is_active = 1
                AND (
                    EXISTS (
                        SELECT 1 FROM PROMPT_PERMISSIONS pp
                        WHERE pp.prompt_id = p.id AND pp.user_id = ?
                          AND pp.permission_level IN ('owner', 'edit')
                    )
                    OR EXISTS (
                        SELECT 1 FROM ENTITLEMENTS e_prompt
                        WHERE e_prompt.user_id = ?
                          AND e_prompt.asset_type = 'prompt'
                          AND e_prompt.asset_id = p.id
                          AND {active_entitlement_condition("e_prompt")}
                    )
                    OR EXISTS (
                        SELECT 1 FROM ENTITLEMENTS e_pack
                        JOIN PACK_ITEMS pi ON e_pack.asset_id = pi.pack_id
                        WHERE e_pack.user_id = ?
                        AND e_pack.asset_type = 'pack'
                        AND pi.prompt_id = p.id AND pi.is_active = 1
                        AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
                        AND {active_entitlement_condition("e_pack")}
                    )
                )
            """
            prompt_params = [user_id, user_id, user_id, user_id]

        await cursor.execute(prompt_query, prompt_params)
        prompt_rows = [dict(row) for row in await cursor.fetchall()]

        # --- Pack welcome messages ---
        if is_admin_user:
            pack_query = f"""
                SELECT wm.id, wm.entity_type, wm.entity_id, wm.content, wm.updated_at,
                       pk.name, pk.cover_image as image, pk.has_welcome_page,
                       (SELECT u.username FROM USERS u WHERE u.id = pk.created_by_user_id) as creator_name,
                       wmr.read_at, COALESCE(wmr.muted, 0) as muted
                FROM WELCOME_MESSAGES wm
                JOIN PACKS pk ON wm.entity_id = pk.id AND wm.entity_type = 'pack'
                LEFT JOIN WELCOME_MESSAGE_READS wmr ON wmr.welcome_message_id = wm.id AND wmr.user_id = ?
                WHERE wm.is_active = 1
            """
            pack_params = [user_id]
        else:
            pack_query = f"""
                SELECT wm.id, wm.entity_type, wm.entity_id, wm.content, wm.updated_at,
                       pk.name, pk.cover_image as image, pk.has_welcome_page,
                       (SELECT u.username FROM USERS u WHERE u.id = pk.created_by_user_id) as creator_name,
                       wmr.read_at, COALESCE(wmr.muted, 0) as muted
                FROM WELCOME_MESSAGES wm
                JOIN PACKS pk ON wm.entity_id = pk.id AND wm.entity_type = 'pack'
                LEFT JOIN WELCOME_MESSAGE_READS wmr ON wmr.welcome_message_id = wm.id AND wmr.user_id = ?
                WHERE wm.is_active = 1
                AND (
                    pk.created_by_user_id = ?
                    OR EXISTS (
                        SELECT 1 FROM ENTITLEMENTS e_pack
                        WHERE e_pack.asset_type = 'pack'
                          AND e_pack.asset_id = pk.id
                          AND e_pack.user_id = ?
                          AND {active_entitlement_condition("e_pack")}
                    )
                )
            """
            pack_params = [user_id, user_id, user_id]

        await cursor.execute(pack_query, pack_params)
        pack_rows = [dict(row) for row in await cursor.fetchall()]

    # Build response with signed image URLs
    new_expiration = datetime.now(timezone.utc) + timedelta(hours=MEDIA_TOKEN_EXPIRE_HOURS)
    messages = []

    for row in prompt_rows:
        image_url = None
        if row.get('image'):
            try:
                img_path = f"{row['image']}_128.webp"
                token = generate_img_token(img_path, new_expiration, current_user)
                image_url = f"{CLOUDFLARE_BASE_URL}{img_path}?token={token}"
            except Exception:
                pass

        name = row.get('name') or ""
        messages.append({
            "id": row['id'],
            "entity_type": row['entity_type'],
            "entity_id": row['entity_id'],
            "name": name,
            "creator_name": row.get('creator_name') or "",
            "initial": name[0].upper() if name else "?",
            "image_url": image_url,
            "content": row['content'],
            "updated_at": row['updated_at'],
            "is_read": row['read_at'] is not None,
            "is_muted": bool(row['muted']),
            "has_welcome_page": bool(row.get('has_welcome_page')),
        })

    for row in pack_rows:
        image_url = None
        if row.get('image'):
            try:
                token = generate_img_token(row['image'], new_expiration, current_user)
                image_url = f"{CLOUDFLARE_BASE_URL}{row['image']}?token={token}"
            except Exception:
                pass

        name = row.get('name') or ""
        messages.append({
            "id": row['id'],
            "entity_type": row['entity_type'],
            "entity_id": row['entity_id'],
            "name": name,
            "creator_name": row.get('creator_name') or "",
            "initial": name[0].upper() if name else "?",
            "image_url": image_url,
            "content": row['content'],
            "updated_at": row['updated_at'],
            "is_read": row['read_at'] is not None,
            "is_muted": bool(row['muted']),
            "has_welcome_page": bool(row.get('has_welcome_page')),
        })

    # Sort: unread+unmuted first, then by updated_at descending within each group
    messages.sort(key=lambda m: (
        0 if (not m['is_read'] and not m['is_muted']) else 1,
        -(datetime.fromisoformat(m['updated_at']).timestamp() if m['updated_at'] else 0)
    ))

    unread_count = sum(1 for m in messages if not m['is_read'] and not m['is_muted'])

    return JSONResponse(content={
        "messages": messages,
        "unread_count": unread_count,
    })


@app.put("/api/home/welcome-messages/{message_id}/read")
async def mark_welcome_message_read(message_id: int, current_user: User = Depends(get_current_user)):
    """Mark a welcome message as read for the current user."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=False) as conn:
        cursor = await conn.cursor()

        # Get the welcome message to validate access
        await cursor.execute(
            "SELECT entity_type, entity_id FROM WELCOME_MESSAGES WHERE id = ?",
            (message_id,)
        )
        wm = await cursor.fetchone()
        if not wm:
            raise HTTPException(status_code=404, detail="Not found")

        # Validate user has access to the underlying entity
        if wm['entity_type'] == 'prompt':
            if not await can_user_access_prompt(current_user, wm['entity_id'], cursor):
                raise HTTPException(status_code=404, detail="Not found")
        elif wm['entity_type'] == 'pack':
            if not await can_user_access_pack(current_user, wm['entity_id'], cursor):
                raise HTTPException(status_code=404, detail="Not found")

        # Upsert read status
        await cursor.execute("""
            INSERT INTO WELCOME_MESSAGE_READS (welcome_message_id, user_id, read_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(welcome_message_id, user_id) DO UPDATE SET read_at = datetime('now')
        """, (message_id, current_user.id))

        await conn.commit()

    return JSONResponse(content={"success": True})


@app.put("/api/home/welcome-messages/{message_id}/mute")
async def mute_welcome_message(message_id: int, current_user: User = Depends(get_current_user)):
    """Mute a welcome message for the current user (stops showing as unread)."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=False) as conn:
        cursor = await conn.cursor()

        # Get the welcome message to validate access
        await cursor.execute(
            "SELECT entity_type, entity_id FROM WELCOME_MESSAGES WHERE id = ?",
            (message_id,)
        )
        wm = await cursor.fetchone()
        if not wm:
            raise HTTPException(status_code=404, detail="Not found")

        if wm['entity_type'] == 'prompt':
            if not await can_user_access_prompt(current_user, wm['entity_id'], cursor):
                raise HTTPException(status_code=404, detail="Not found")
        elif wm['entity_type'] == 'pack':
            if not await can_user_access_pack(current_user, wm['entity_id'], cursor):
                raise HTTPException(status_code=404, detail="Not found")

        await cursor.execute("""
            INSERT INTO WELCOME_MESSAGE_READS (welcome_message_id, user_id, muted)
            VALUES (?, ?, 1)
            ON CONFLICT(welcome_message_id, user_id) DO UPDATE SET muted = 1
        """, (message_id, current_user.id))

        await conn.commit()

    return JSONResponse(content={"success": True})


@app.put("/api/home/welcome-messages/{message_id}/unmute")
async def unmute_welcome_message(message_id: int, current_user: User = Depends(get_current_user)):
    """Unmute a welcome message for the current user."""
    if current_user is None:
        return unauthenticated_response()

    async with get_db_connection(readonly=False) as conn:
        cursor = await conn.cursor()

        # Get the welcome message to validate access
        await cursor.execute(
            "SELECT entity_type, entity_id FROM WELCOME_MESSAGES WHERE id = ?",
            (message_id,)
        )
        wm = await cursor.fetchone()
        if not wm:
            raise HTTPException(status_code=404, detail="Not found")

        if wm['entity_type'] == 'prompt':
            if not await can_user_access_prompt(current_user, wm['entity_id'], cursor):
                raise HTTPException(status_code=404, detail="Not found")
        elif wm['entity_type'] == 'pack':
            if not await can_user_access_pack(current_user, wm['entity_id'], cursor):
                raise HTTPException(status_code=404, detail="Not found")

        await cursor.execute("""
            INSERT INTO WELCOME_MESSAGE_READS (welcome_message_id, user_id, muted)
            VALUES (?, ?, 0)
            ON CONFLICT(welcome_message_id, user_id) DO UPDATE SET muted = 0
        """, (message_id, current_user.id))

        await conn.commit()

    return JSONResponse(content={"success": True})


# =============================================================================
# Catch-all route for custom domains (MUST BE LAST)
# =============================================================================
# Custom-domain landing catch-all must remain after all normal GET routes.
app.include_router(prompt_custom_domain_router)


_dual_mode_active = False  # Set before uvicorn.run, read by GranSabio startup check

if __name__ == '__main__':
    # Parse command line arguments for different modes
    dual_mode = False
    use_ssl = True
    host = '0.0.0.0'
    worker_count = int(os.getenv("UVICORN_WORKERS", "1"))

    if 'dev' in sys.argv:
        # Development mode: HTTP only
        use_ssl = False
        print("Starting in DEVELOPMENT mode (HTTP only)")
    elif 'tunnel' in sys.argv or 'tunel' in sys.argv:
        # HTTP mode for Cloudflare tunnel optimization
        use_ssl = False
        host = '127.0.0.1'  # Localhost for tunnel optimization
        print("Starting in TUNNEL mode (HTTP on localhost for Cloudflare optimization)")
    elif 'https' in sys.argv:
        # Force HTTPS mode only
        use_ssl = True
        print("Starting in HTTPS mode (SSL only)")
    else:
        # Default dual mode: try HTTPS first, fallback to HTTP
        dual_mode = True
        _dual_mode_active = True
        os.environ["_AURVEK_DUAL_MODE"] = "1"  # Propagate to workers via env
        print("Starting in DUAL mode (HTTPS + HTTP fallback)")

    # Check SSL certificates if needed
    ssl_available = False
    if use_ssl or dual_mode:
        _project_root = os.path.dirname(__file__)
        ssl_keyfile = os.path.join(_project_root, 'certs', 'privkey.pem')
        ssl_certfile = os.path.join(_project_root, 'certs', 'cert.pem')

        if os.path.exists(ssl_keyfile) and os.path.exists(ssl_certfile):
            ssl_available = True
        else:
            print("INFO: SSL certificates not found:")
            print(f"   Key file: {ssl_keyfile}")
            print(f"   Cert file: {ssl_certfile}")
            if dual_mode:
                print("   Dual mode: Will start HTTP server only")
                dual_mode = False
                _dual_mode_active = False
                os.environ.pop("_AURVEK_DUAL_MODE", None)
                use_ssl = False
            else:
                print("   HTTPS mode requested but falling back to HTTP")
                use_ssl = False

    # Start appropriate server(s)
    if dual_mode and ssl_available:
        # Start both HTTPS and HTTP servers using threading
        import threading
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        print("Starting HTTPS server on 0.0.0.0:7789")
        print("Starting HTTP server on 127.0.0.1:7790 (tunnel/backup)")

        def start_https():
            uvicorn.run(
                "app:app",
                host='0.0.0.0',
                port=7789,
                ssl_keyfile=ssl_keyfile,
                ssl_certfile=ssl_certfile,
                log_level="debug",
                log_config=None,
                http="httptools",
                workers=worker_count
            )

        def start_http():
            uvicorn.run(
                app,
                host='127.0.0.1',
                port=7790,  # Different port for HTTP
                log_level="debug",
                log_config=None,
                http="httptools",
                workers=worker_count
            )

        # Start both servers in parallel
        https_thread = threading.Thread(target=start_https, daemon=True)
        http_thread = threading.Thread(target=start_http, daemon=True)

        https_thread.start()
        http_thread.start()

        try:
            # Keep main thread alive
            while https_thread.is_alive() or http_thread.is_alive():
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nShutting down servers...")
            # Force exit for Windows
            os._exit(0)
    elif use_ssl and ssl_available:
        # HTTPS only configuration
        print(f"HTTPS Server starting on {host}:7789")
        uvicorn.run(
            "app:app",
            host=host,
            #loop=uvloop, ## commented out because it's not compatible on Windows, keep this line to uncomment when running on Linux
            port=7789,
            ssl_keyfile=ssl_keyfile,
            ssl_certfile=ssl_certfile,
            log_level="debug",
            log_config=None,
            http="httptools",
            workers=worker_count
        )
    else:
        # HTTP only configuration
        print(f"HTTP Server starting on {host}:7789")
        uvicorn.run(
            "app:app",  # Use import string for multi-worker support
            host=host,
            port=7789,
            log_level="debug",
            log_config=None,
            http="httptools",
            workers=worker_count
        )
