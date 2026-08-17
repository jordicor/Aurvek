import asyncio
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request

from common import get_auth_base_url
from database import get_db_connection
from email_service import email_service
from log_config import logger


async def create_pending_entitlement(
    user_id: int,
    prompt_id: int | None = None,
    pack_id: int | None = None,
) -> Optional[str]:
    """Create a pending entitlement claim and return the token."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(hours=24)
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                """
                INSERT INTO PENDING_ENTITLEMENTS (user_id, token, prompt_id, pack_id, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, token, prompt_id, pack_id, expires_at.isoformat()),
            )
            await conn.commit()
        return token
    except Exception as e:
        logger.error(f"Error creating pending entitlement: {e}")
        return None


async def send_entitlement_claim_email(
    request: Request,
    email: str,
    user_id: int,
    prompt_id: int | None = None,
    pack_id: int | None = None,
) -> bool:
    """Create a pending entitlement and send the claim email to the existing user."""
    token = await create_pending_entitlement(user_id, prompt_id, pack_id)
    if not token:
        return False

    claim_url = f"{get_auth_base_url(request).rstrip('/')}/claim-entitlement/{token}"

    from common import get_user_branding

    product_name = None
    branding = None
    try:
        async with get_db_connection(readonly=True) as conn:
            if pack_id:
                cur = await conn.execute(
                    "SELECT name, created_by_user_id FROM PACKS WHERE id = ?",
                    (pack_id,),
                )
                row = await cur.fetchone()
                if row:
                    product_name = row[0]
                    branding = await get_user_branding(row[1])
            elif prompt_id:
                cur = await conn.execute(
                    "SELECT name, created_by_user_id FROM PROMPTS WHERE id = ?",
                    (prompt_id,),
                )
                row = await cur.fetchone()
                if row:
                    product_name = row[0]
                    if row[1]:
                        branding = await get_user_branding(row[1])
    except Exception as e:
        logger.warning(f"Could not get product name/branding for claim email: {e}")

    email_sent = await asyncio.to_thread(
        email_service.send_claim_entitlement_email,
        to_email=email,
        claim_url=claim_url,
        product_name=product_name,
        branding=branding,
    )
    if email_sent:
        logger.info("Claim entitlement email sent to %s for user %s", email, user_id)
    else:
        logger.error("Failed to send claim entitlement email to %s for user %s", email, user_id)
    return email_sent
