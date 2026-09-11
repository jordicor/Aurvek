"""Shared native account factory; an existing transaction keeps app signup atomic."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import HTTPException
from database import get_db_connection
from i18n import Translator, normalize_language
from phone_verification import (normalize_phone_number, consume_phone_verification,
                                PURPOSE_CREATE_USER)
from user_accounts import InitialBalanceError, apply_initial_balance, get_live_user_role

logger = logging.getLogger(__name__)


def hash_password(value):
    from auth import hash_password as native_hash
    return native_hash(value)


async def upsert_creator_relationship(*args):
    from common import upsert_creator_relationship as native_upsert
    return await native_upsert(*args)


@asynccontextmanager
async def _connection(existing, factory):
    if existing is not None:
        yield existing
    else:
        async with factory() as conn:
            yield conn


async def create_user(
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
    connection=None,
    dependencies=None,
    ui_language=None,
):
    if ui_language is None:
        ui_language = "en"
    else:
        ui_language = normalize_language(ui_language)
        if ui_language is None:
            raise HTTPException(status_code=400, detail=Translator("en").t("account.language.invalid"))
    dependencies = dependencies or {}
    get_db_connection = dependencies.get("get_db_connection", globals()["get_db_connection"])
    get_live_user_role = dependencies.get("get_live_user_role", globals()["get_live_user_role"])
    hash_password = dependencies.get("hash_password", globals()["hash_password"])
    normalize_phone_number = dependencies.get("normalize_phone_number", globals()["normalize_phone_number"])
    consume_phone_verification = dependencies.get("consume_phone_verification", globals()["consume_phone_verification"])
    apply_initial_balance = dependencies.get("apply_initial_balance", globals()["apply_initial_balance"])
    upsert_creator_relationship = dependencies.get("upsert_creator_relationship", globals()["upsert_creator_relationship"])
    try:
        async with _connection(connection, get_db_connection) as conn:
            if connection is None:
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
                        phone_number, phone_verified, email, ui_language
                    )
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                    RETURNING id
                """, (
                    username,
                    hashed_password,
                    role_id,
                    phone,
                    phone_verified,
                    email,
                    ui_language,
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

                    if connection is None:
                        await conn.commit()
                return user_id
    except InitialBalanceError:
        raise
    except sqlite3.Error as e:
        if connection is not None:
            raise
        logger.error(f"Error adding user: {e}")
        return None
