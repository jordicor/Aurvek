"""
Custom Domain API Endpoints for Users and Admins.

Handles configuration, verification, and activation of custom domains
for prompt landing pages.
"""

import logging
import os
import re
import socket
from typing import Optional

logger = logging.getLogger(__name__)

import dns.resolver
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError, ValidationInfo, field_validator

from auth import get_current_user
from models import User
from i18n import get_translator, Translator
from database import get_db_connection
from common import deduct_balance, get_balance, record_daily_usage
from marketplace.middleware.custom_domains import invalidate_domain_cache
from marketplace.config import require_creator_tools_enabled, require_public_landings_enabled
from marketplace.landing.isolation import (
    get_creator_content_config,
    is_host_isolated_from_primary,
)

# Configuration from environment
CNAME_TARGET = os.getenv("CLOUDFLARE_CNAME_TARGET", "")
SLOT_PRICE = float(os.getenv("CUSTOM_DOMAIN_SLOT_PRICE", "25.00"))
# Keep DOMAIN_PRICE for backwards compatibility
DOMAIN_PRICE = SLOT_PRICE

# Verification status constants (INTEGER values in DB)
VSTATUS_PENDING = 0
VSTATUS_VERIFIED = 1
VSTATUS_FAILED = 2
VSTATUS_EXPIRED = 3

# Map for API responses (INT -> string for frontend)
VSTATUS_NAMES = {
    VSTATUS_PENDING: 'pending',
    VSTATUS_VERIFIED: 'verified',
    VSTATUS_FAILED: 'failed',
    VSTATUS_EXPIRED: 'expired',
}

# Router for user endpoints
router = APIRouter(prefix="/api/domains", tags=["Custom Domains"])

# Router for admin endpoints
admin_router = APIRouter(prefix="/admin/domains", tags=["Admin - Custom Domains"])


def _require_custom_domain_tools_enabled(translator=None) -> None:
    require_creator_tools_enabled(translator)
    require_public_landings_enabled(translator)


# =============================================================================
# Slot Helper Functions
# =============================================================================

async def get_user_slots_info(user_id: int) -> dict:
    """
    Get slot information for a user.
    Returns dict with purchased, used, and available slots.
    """
    async with get_db_connection(readonly=True) as conn:
        # Get purchased slots
        cursor = await conn.execute(
            "SELECT domain_slots_purchased FROM USER_DETAILS WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        purchased = row[0] if row and row[0] else 0

        # Count used slots (active domains owned by this user)
        cursor = await conn.execute("""
            SELECT COUNT(*) FROM PROMPT_CUSTOM_DOMAINS pcd
            JOIN PROMPTS p ON pcd.prompt_id = p.id
            WHERE p.created_by_user_id = ? AND pcd.is_active = 1
        """, (user_id,))
        row = await cursor.fetchone()
        used = row[0] if row else 0

    return {
        "purchased": purchased,
        "used": used,
        "available": purchased - used
    }


async def user_has_available_slot(user_id: int) -> bool:
    """Check if user has at least one available (unused) slot."""
    slots = await get_user_slots_info(user_id)
    return slots["available"] > 0


async def add_slots_to_user(user_id: int, quantity: int = 1) -> bool:
    """Add slots to a user's account."""
    async with get_db_connection() as conn:
        await conn.execute("""
            UPDATE USER_DETAILS
            SET domain_slots_purchased = COALESCE(domain_slots_purchased, 0) + ?
            WHERE user_id = ?
        """, (quantity, user_id))
        await conn.commit()
    return True


async def revert_captive_users_if_needed(prompt_id: int, domain_id: int) -> list[int]:
    """
    When a domain is deactivated/deleted, check if captive users should be freed.
    Returns list of user IDs that were freed. Safe to call before migration runs.
    """
    try:
        async with get_db_connection() as conn:
            # Find users captive under this domain
            cursor = await conn.execute(
                "SELECT user_id FROM USER_CAPTIVE_DOMAINS WHERE domain_id = ?",
                (domain_id,)
            )
            captive_users = [row[0] for row in await cursor.fetchall()]

            if not captive_users:
                return []

            # Remove records for this domain
            await conn.execute(
                "DELETE FROM USER_CAPTIVE_DOMAINS WHERE domain_id = ?",
                (domain_id,)
            )

            # Free users who have no remaining active domain justifying captivity
            # Single set-based query instead of per-user loop
            placeholders = ",".join("?" for _ in captive_users)
            cursor = await conn.execute(f"""
                UPDATE USER_DETAILS SET public_prompts_access = 1
                WHERE user_id IN ({placeholders})
                  AND public_prompts_access = 0
                  AND NOT EXISTS (
                    SELECT 1 FROM USER_CAPTIVE_DOMAINS ucd
                    JOIN PROMPT_CUSTOM_DOMAINS pcd ON ucd.domain_id = pcd.id
                    WHERE ucd.user_id = USER_DETAILS.user_id AND pcd.is_active = 1
                  )
            """, captive_users)
            freed_count = cursor.rowcount

            # Identify which users were actually freed (for logging)
            freed_users = []
            if freed_count > 0:
                cursor = await conn.execute(f"""
                    SELECT user_id FROM USER_DETAILS
                    WHERE user_id IN ({placeholders}) AND public_prompts_access = 1
                """, captive_users)
                freed_users = [row[0] for row in await cursor.fetchall()]

            await conn.commit()

            if freed_users:
                logger.info(f"Domain {domain_id} (prompt {prompt_id}) deactivated: freed {len(freed_users)} captive users: {freed_users}")

            return freed_users
    except Exception as e:
        if "no such table" in str(e).lower():
            logger.warning(f"USER_CAPTIVE_DOMAINS table not found (migration pending?): {e}")
            return []
        raise


class DomainConfigRequest(BaseModel):
    domain: str

    @field_validator('domain')
    @classmethod
    def validate_domain(cls, v, info: ValidationInfo):
        tr = (info.context or {}).get("translator") or Translator("en")
        # Basic domain validation
        pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z]{2,})+$'
        if not re.match(pattern, v):
            raise ValueError(tr.t('landing_builder.response.invalid_domain_format'))
        domain = v.lower().strip().rstrip(".")
        if not is_host_isolated_from_primary(domain):
            raise ValueError(tr.t('landing_builder.response.custom_domain_must_be_on_a_separate_site_from_aurvek'))
        creator_config = get_creator_content_config()
        if creator_config and domain == creator_config.host:
            raise ValueError(tr.t('landing_builder.response.domain_is_reserved_for_isolated_creator_content'))
        return domain


# =============================================================================
# User Endpoints
# =============================================================================

@router.get("/{prompt_id}")
async def get_domain_config(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """
    Get custom domain configuration for a prompt.
    Returns current domain, verification status, and CNAME target.
    """
    tr = get_translator(request, current_user)
    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    await _verify_prompt_access(prompt_id, current_user, translator=tr)

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("""
            SELECT custom_domain, verification_status, is_active,
                   activated_by_admin, last_verification_attempt,
                   verification_error, activated_at
            FROM PROMPT_CUSTOM_DOMAINS
            WHERE prompt_id = ?
        """, (prompt_id,))
        result = await cursor.fetchone()

    if not result:
        return JSONResponse({
            "has_domain": False,
            "cname_target": CNAME_TARGET,
            "price": DOMAIN_PRICE
        })

    return JSONResponse({
        "has_domain": True,
        "domain": result[0],
        "verification_status": VSTATUS_NAMES.get(result[1], 'pending'),
        "is_active": bool(result[2]),
        "activated_by_admin": bool(result[3]),
        "last_check": result[4],
        "verification_error": tr.t("landing_builder.ui.domain_verification_failed_check_your_dns_configuration_and_verify_again") if result[5] else None,
        "activated_at": result[6],
        "cname_target": CNAME_TARGET,
        "price": DOMAIN_PRICE
    })


@router.get("/slots/info")
async def get_slots_info(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Get domain slots information for the current user.
    Returns purchased, used, and available slots.
    """
    tr = get_translator(request, current_user)
    _require_custom_domain_tools_enabled(tr)

    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    slots = await get_user_slots_info(current_user.id)

    return JSONResponse({
        "success": True,
        "slots": slots,
        "slot_price": SLOT_PRICE
    })


@router.post("/slots/purchase")
async def purchase_slot(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Purchase a domain slot.
    Deducts SLOT_PRICE from user balance and adds 1 slot.
    """
    tr = get_translator(request, current_user)
    _require_custom_domain_tools_enabled(tr)

    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    # Only users (creators) can purchase slots
    is_user = await current_user.is_user
    if not is_user:
        raise HTTPException(403, tr.t('landing_builder.response.only_users_can_purchase_domain_slots'))

    # Atomic: deduct balance + add slot in single transaction
    async with get_db_connection() as conn:
        await conn.execute('BEGIN IMMEDIATE')
        try:
            # Deduct balance (only if sufficient)
            result = await conn.execute('''
                UPDATE USER_DETAILS
                SET balance = balance - ?
                WHERE user_id = ? AND balance >= ?
                RETURNING balance
            ''', (SLOT_PRICE, current_user.id, SLOT_PRICE))
            new_balance = await result.fetchone()

            if new_balance is None:
                await conn.execute('ROLLBACK')
                balance = await get_balance(current_user.id)
                raise HTTPException(
                    402,
                    tr.t('landing_builder.response.insufficient_balance_required_value1_available_value2', value1=tr.format_currency(SLOT_PRICE, 'USD', fraction_digits=2), value2=tr.format_currency(balance, 'USD', fraction_digits=2))
                )

            # Add slot
            await conn.execute("""
                UPDATE USER_DETAILS
                SET domain_slots_purchased = COALESCE(domain_slots_purchased, 0) + 1
                WHERE user_id = ?
            """, (current_user.id,))

            # Record daily usage (reuse connection)
            await record_daily_usage(
                user_id=current_user.id,
                usage_type='domain',
                cost=SLOT_PRICE,
                units=1,
                conn=conn
            )

            await conn.commit()
        except HTTPException:
            raise
        except Exception as e:
            await conn.rollback()
            logger.error(f"Error purchasing domain slot: {e}")
            raise HTTPException(500, tr.t('landing_builder.response.payment_processing_failed'))

    # Get updated slots info (outside transaction)
    slots = await get_user_slots_info(current_user.id)

    return JSONResponse({
        "success": True,
        "message": tr.t('landing_builder.response.domain_slot_purchased_successfully'),
        "amount_charged": SLOT_PRICE,
        "slots": slots
    })


@router.post("/{prompt_id}/configure")
async def configure_domain(
    prompt_id: int,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    Configure a custom domain for a prompt.
    Does NOT activate - just saves the domain and sets status to 'pending'.
    """
    tr = get_translator(request, current_user)
    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    await _verify_prompt_access(prompt_id, current_user, translator=tr)

    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, tr.t('landing_builder.response.invalid_json'))
    try:
        data = DomainConfigRequest.model_validate(payload, context={"translator": tr})
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False, include_context=False):
            error["loc"] = ["body", *error["loc"]]
            error["msg"] = tr.t('landing_builder.response.enter_a_valid_domain_name_on_a_separate_site_from')
            errors.append(error)
        return JSONResponse({"detail": errors}, status_code=422)
    domain = data.domain

    # Check if domain is already used by another prompt
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT prompt_id FROM PROMPT_CUSTOM_DOMAINS WHERE custom_domain = ?",
            (domain,)
        )
        existing = await cursor.fetchone()
        if existing and existing[0] != prompt_id:
            raise HTTPException(400, tr.t('landing_builder.response.domain_already_configured_for_another_prompt'))

    # Insert or update
    async with get_db_connection() as conn:
        await conn.execute("""
            INSERT INTO PROMPT_CUSTOM_DOMAINS (prompt_id, custom_domain, verification_status)
            VALUES (?, ?, ?)
            ON CONFLICT(prompt_id) DO UPDATE SET
                custom_domain = excluded.custom_domain,
                verification_status = ?,
                is_active = FALSE,
                updated_at = CURRENT_TIMESTAMP
        """, (prompt_id, domain, VSTATUS_PENDING, VSTATUS_PENDING))
        await conn.commit()

    invalidate_domain_cache(domain)

    return JSONResponse({
        "success": True,
        "message": tr.t('landing_builder.response.domain_configured_point_cname_to_value1', value1=CNAME_TARGET),
        "cname_target": CNAME_TARGET,
        "next_step": "verify"
    })


@router.post("/{prompt_id}/verify")
async def verify_domain(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """
    Verify that the domain's CNAME points to our server.
    """
    tr = get_translator(request, current_user)
    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    await _verify_prompt_access(prompt_id, current_user, translator=tr)

    # Get the configured domain
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT custom_domain FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?",
            (prompt_id,)
        )
        result = await cursor.fetchone()

    if not result:
        raise HTTPException(404, tr.t('landing_builder.response.no_domain_configured'))

    domain = result[0]
    verification_result = _verify_cname(domain, CNAME_TARGET, translator=tr)

    # Update verification status
    async with get_db_connection() as conn:
        if verification_result["success"]:
            await conn.execute("""
                UPDATE PROMPT_CUSTOM_DOMAINS
                SET verification_status = ?,
                    last_verification_success = CURRENT_TIMESTAMP,
                    last_verification_attempt = CURRENT_TIMESTAMP,
                    verification_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE prompt_id = ?
            """, (VSTATUS_VERIFIED, prompt_id))
        else:
            await conn.execute("""
                UPDATE PROMPT_CUSTOM_DOMAINS
                SET verification_status = ?,
                    last_verification_attempt = CURRENT_TIMESTAMP,
                    verification_error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE prompt_id = ?
            """, (VSTATUS_FAILED, verification_result["error"], prompt_id))
        await conn.commit()

    return JSONResponse(verification_result)


@router.post("/{prompt_id}/activate")
async def activate_domain(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """
    Activate a verified domain using a domain slot.
    Requires an available slot (purchased previously or buy one now).
    Only users can use this endpoint. Admins should use /admin/domains/{id}/activate-free.
    """
    tr = get_translator(request, current_user)
    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    # Only users can activate. Admins should use activate-free endpoint.
    is_admin = await current_user.is_admin
    is_user = await current_user.is_user
    if not is_user and not is_admin:
        raise HTTPException(403, tr.t('landing_builder.response.only_users_can_activate_custom_domains'))

    # If admin, redirect them to use the free endpoint
    if is_admin:
        raise HTTPException(400, tr.t('landing_builder.response.use_the_free_admin_activation_action_for_this_domain'))

    await _verify_prompt_access(prompt_id, current_user, translator=tr)

    # Check verification status
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("""
            SELECT custom_domain, verification_status, is_active, activated_by_admin
            FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?
        """, (prompt_id,))
        result = await cursor.fetchone()

    if not result:
        raise HTTPException(404, tr.t('landing_builder.response.no_domain_configured'))

    domain, status, is_active, admin_activated = result

    if is_active:
        raise HTTPException(400, tr.t('landing_builder.response.domain_already_active'))

    if status != VSTATUS_VERIFIED:
        raise HTTPException(400, tr.t('landing_builder.response.domain_not_verified'))

    # Atomic: check slot availability + activate in single transaction
    async with get_db_connection() as conn:
        await conn.execute('BEGIN IMMEDIATE')

        # Check purchased slots
        cursor = await conn.execute(
            "SELECT domain_slots_purchased FROM USER_DETAILS WHERE user_id = ?",
            (current_user.id,)
        )
        row = await cursor.fetchone()
        purchased = row[0] if row and row[0] else 0

        # Count used slots (active domains owned by this user)
        cursor = await conn.execute("""
            SELECT COUNT(*) FROM PROMPT_CUSTOM_DOMAINS pcd
            JOIN PROMPTS p ON pcd.prompt_id = p.id
            WHERE p.created_by_user_id = ? AND pcd.is_active = 1
        """, (current_user.id,))
        row = await cursor.fetchone()
        used = row[0] if row else 0

        if purchased - used <= 0:
            await conn.execute('ROLLBACK')
            raise HTTPException(
                402,
                tr.t('landing_builder.response.no_domain_slots_available_you_have_value1_value2_slots_in', value1=tr.format_number(used, maximum_fraction_digits=0), value2=tr.format_number(purchased, maximum_fraction_digits=0), value3=tr.format_currency(SLOT_PRICE, 'USD', fraction_digits=2))
            )

        # Activate domain
        await conn.execute("""
            UPDATE PROMPT_CUSTOM_DOMAINS
            SET is_active = TRUE,
                activated_at = CURRENT_TIMESTAMP,
                activated_by_user_id = ?,
                activated_by_admin = FALSE,
                verification_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE prompt_id = ?
        """, (current_user.id, prompt_id))
        await conn.commit()

    invalidate_domain_cache(domain)

    # Get updated slots info
    updated_slots = await get_user_slots_info(current_user.id)

    return JSONResponse({
        "success": True,
        "message": tr.t('landing_builder.response.domain_value1_activated_successfully', value1=domain),
        "slots": updated_slots
    })


@router.post("/{prompt_id}/deactivate")
async def deactivate_domain(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """
    Deactivate a domain (toggle OFF).
    This frees up the slot for use on another prompt.
    The domain configuration is preserved and can be reactivated later.
    """
    tr = get_translator(request, current_user)
    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    await _verify_prompt_access(prompt_id, current_user, translator=tr)

    # Get current domain state
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("""
            SELECT id, custom_domain, is_active
            FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?
        """, (prompt_id,))
        result = await cursor.fetchone()

    if not result:
        raise HTTPException(404, tr.t('landing_builder.response.no_domain_configured'))

    domain_id, domain, is_active = result

    if not is_active:
        raise HTTPException(400, tr.t('landing_builder.response.domain_is_already_inactive'))

    # Deactivate domain (frees the slot)
    async with get_db_connection() as conn:
        await conn.execute("""
            UPDATE PROMPT_CUSTOM_DOMAINS
            SET is_active = FALSE,
                updated_at = CURRENT_TIMESTAMP
            WHERE prompt_id = ?
        """, (prompt_id,))
        await conn.commit()

    invalidate_domain_cache(domain)

    # Revert captive users if needed
    freed = await revert_captive_users_if_needed(prompt_id, domain_id)
    if freed:
        logger.info(f"Freed {len(freed)} captive users after domain deactivation for prompt {prompt_id}")

    # Get updated slots info
    slots = await get_user_slots_info(current_user.id)

    return JSONResponse({
        "success": True,
        "message": tr.t('landing_builder.response.domain_value1_deactivated_slot_freed_for_use_elsewhere', value1=domain),
        "slots": slots
    })


@router.delete("/{prompt_id}")
async def remove_domain(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """Remove custom domain configuration."""
    tr = get_translator(request, current_user)
    if not current_user:
        raise HTTPException(401, tr.t('landing_builder.response.authentication_required'))

    await _verify_prompt_access(prompt_id, current_user, translator=tr)

    # Get domain for cache invalidation and captive user revert
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            "SELECT id, custom_domain FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?",
            (prompt_id,)
        )
        result = await cursor.fetchone()

    if result:
        domain_id, domain = result

        # Revert captive users BEFORE deleting the domain row
        # (revert function joins PROMPT_CUSTOM_DOMAINS to check other active domains)
        freed = await revert_captive_users_if_needed(prompt_id, domain_id)
        if freed:
            logger.info(f"Freed {len(freed)} captive users after domain deletion for prompt {prompt_id}")

        async with get_db_connection() as conn:
            await conn.execute(
                "DELETE FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?",
                (prompt_id,)
            )
            await conn.commit()
        invalidate_domain_cache(domain)

    return JSONResponse({"success": True, "message": tr.t('landing_builder.response.domain_removed')})


# =============================================================================
# Admin Endpoints
# =============================================================================

@admin_router.get("/")
async def list_all_domains(
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """List all configured custom domains (admin only)."""
    tr = get_translator(request, current_user)
    _require_custom_domain_tools_enabled(tr)

    if not current_user or not await current_user.is_admin:
        raise HTTPException(403, tr.t('landing_builder.response.admin_only'))

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("""
            SELECT
                pcd.id, pcd.custom_domain, pcd.verification_status,
                pcd.is_active, pcd.activated_by_admin, pcd.activated_at,
                p.id as prompt_id, p.name as prompt_name,
                u.username as owner
            FROM PROMPT_CUSTOM_DOMAINS pcd
            JOIN PROMPTS p ON pcd.prompt_id = p.id
            JOIN USERS u ON p.created_by_user_id = u.id
            ORDER BY pcd.created_at DESC
        """)
        rows = await cursor.fetchall()

    domains = []
    for row in rows:
        domains.append({
            "id": row[0],
            "domain": row[1],
            "verification_status": VSTATUS_NAMES.get(row[2], 'pending'),
            "is_active": bool(row[3]),
            "activated_by_admin": bool(row[4]),
            "activated_at": row[5],
            "prompt_id": row[6],
            "prompt_name": row[7],
            "owner": row[8]
        })

    return JSONResponse({"domains": domains})


@admin_router.post("/{prompt_id}/activate-free")
async def admin_activate_free(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """Activate a domain for free (admin only, bypasses payment)."""
    tr = get_translator(request, current_user)
    _require_custom_domain_tools_enabled(tr)

    if not current_user or not await current_user.is_admin:
        raise HTTPException(403, tr.t('landing_builder.response.admin_only'))

    async with get_db_connection() as conn:
        # Check domain exists and is verified
        cursor = await conn.execute("""
            SELECT custom_domain, verification_status
            FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?
        """, (prompt_id,))
        result = await cursor.fetchone()

        if not result:
            raise HTTPException(404, tr.t('landing_builder.response.no_domain_configured_for_this_prompt'))

        domain, status = result

        if status != VSTATUS_VERIFIED:
            raise HTTPException(400, tr.t('landing_builder.response.domain_not_verified_yet'))

        # Activate for free
        await conn.execute("""
            UPDATE PROMPT_CUSTOM_DOMAINS
            SET is_active = TRUE,
                activated_by_admin = TRUE,
                activated_at = CURRENT_TIMESTAMP,
                activated_by_user_id = ?,
                verification_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE prompt_id = ?
        """, (current_user.id, prompt_id))
        await conn.commit()

    invalidate_domain_cache(domain)

    return JSONResponse({
        "success": True,
        "message": tr.t('landing_builder.response.domain_value1_activated_for_free_admin', value1=domain)
    })


@admin_router.post("/{prompt_id}/deactivate")
async def admin_deactivate(
    request: Request,
    prompt_id: int,
    current_user: User = Depends(get_current_user)
):
    """Deactivate a domain (admin only)."""
    tr = get_translator(request, current_user)
    _require_custom_domain_tools_enabled(tr)

    if not current_user or not await current_user.is_admin:
        raise HTTPException(403, tr.t('landing_builder.response.admin_only'))

    domain_id = None
    async with get_db_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, custom_domain FROM PROMPT_CUSTOM_DOMAINS WHERE prompt_id = ?",
            (prompt_id,)
        )
        result = await cursor.fetchone()

        if result:
            domain_id, domain = result
            await conn.execute("""
                UPDATE PROMPT_CUSTOM_DOMAINS
                SET is_active = FALSE,
                    updated_at = CURRENT_TIMESTAMP
                WHERE prompt_id = ?
            """, (prompt_id,))
            await conn.commit()
            invalidate_domain_cache(domain)

    # Revert captive users if needed (outside connection block)
    if domain_id:
        freed = await revert_captive_users_if_needed(prompt_id, domain_id)
        if freed:
            logger.info(f"Freed {len(freed)} captive users after admin domain deactivation for prompt {prompt_id}")

    return JSONResponse({"success": True, "message": tr.t('landing_builder.response.domain_deactivated')})


# =============================================================================
# Helper Functions
# =============================================================================

async def _verify_prompt_access(prompt_id: int, user: User, translator=None):
    """Verify user has edit access to prompt."""
    tr = translator or Translator("en")
    _require_custom_domain_tools_enabled(tr)

    is_admin = await user.is_admin
    if is_admin:
        return

    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute("""
            SELECT 1 FROM PROMPTS p
            LEFT JOIN PROMPT_PERMISSIONS pp ON p.id = pp.prompt_id
            WHERE p.id = ? AND (
                p.created_by_user_id = ? OR
                (pp.user_id = ? AND pp.permission_level IN ('owner', 'edit'))
            )
        """, (prompt_id, user.id, user.id))
        if not await cursor.fetchone():
            raise HTTPException(403, tr.t('landing_builder.response.access_denied_to_this_prompt'))


def _verify_cname(domain: str, expected_target: str, translator=None) -> dict:
    """
    Verify CNAME record for a domain.
    Returns dict with success status and details.
    """
    tr = translator or Translator("en")
    try:
        # Try CNAME lookup
        try:
            answers = dns.resolver.resolve(domain, 'CNAME')
            for rdata in answers:
                cname_target = str(rdata.target).rstrip('.')
                if cname_target.lower() == expected_target.lower():
                    return {
                        "success": True,
                        "message": tr.t('landing_builder.response.cname_verified_value1_value2', value1=domain, value2=cname_target)
                    }
                else:
                    return {
                        "success": False,
                        "error": tr.t('landing_builder.response.cname_points_to_value1_expected_value2', value1=cname_target, value2=expected_target)
                    }
        except dns.resolver.NoAnswer:
            # No CNAME, try A record as fallback
            pass

        # Check if A record points to our IP (fallback for apex domains)
        try:
            our_ip = socket.gethostbyname(expected_target)
            their_ips = dns.resolver.resolve(domain, 'A')
            for ip in their_ips:
                if str(ip) == our_ip:
                    return {
                        "success": True,
                        "message": tr.t('landing_builder.response.a_record_verified_value1_value2', value1=domain, value2=ip)
                    }
            return {
                "success": False,
                "error": tr.t('landing_builder.response.domain_does_not_point_to_our_server_configure_cname_to', value1=expected_target)
            }
        except Exception:
            pass

        return {
            "success": False,
            "error": tr.t('landing_builder.response.no_valid_dns_records_found_configure_cname_to_value1', value1=expected_target)
        }

    except dns.resolver.NXDOMAIN:
        return {"success": False, "error": tr.t('landing_builder.response.domain_does_not_exist')}
    except dns.resolver.Timeout:
        return {"success": False, "error": tr.t('landing_builder.response.dns_lookup_timeout')}
    except Exception as e:
        logger.exception('Custom domain DNS verification failed for %s', domain)
        return {"success": False, "error": tr.t('landing_builder.response.dns_verification_error_value1', value1=tr.t('landing_builder.response.internal_server_error'))}
