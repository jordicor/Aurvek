# auth.py
import os
import bcrypt
import secrets
import jwt
from jwt import PyJWTError as JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from models import User
from database import get_db_connection
from typing import Optional
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, JSONResponse

# Own libraries
from log_config import logger
from rediscfg import is_user_revoked
from common import SECRET_KEY, PEPPER, SECURE_COOKIES, decode_jwt_cached, verify_token_expiration

load_dotenv()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 43200  # 30 days

def hash_password(user_password):
    if isinstance(user_password, bytes):
        user_password = user_password.decode('utf-8')
    user_password_peppered = user_password + PEPPER
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(user_password_peppered.encode('utf-8'), salt)
    return hashed_password

def verify_password(stored_password, provided_password) -> bool:

    provided_password_peppered = provided_password + PEPPER
    provided_password_peppered_encoded = provided_password_peppered.encode('utf-8')
    
    return bcrypt.checkpw(provided_password_peppered_encoded, stored_password)

# Function to create an access token
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    
    # Use UTC explicitly
    current_time = datetime.now(timezone.utc)
    if expires_delta:
        expire = current_time + expires_delta
    else:
        expire = current_time + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    # Use timestamp as integer
    to_encode.update({
        "iat": int(current_time.timestamp()),  # issued at
        "exp": int(expire.timestamp())         # expiration time
    })
    
    logger.info(f"Creating token at: {current_time.isoformat()}")
    logger.info(f"Token will expire at: {expire.isoformat()}")
    
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# Dependency to get the current user
async def get_current_user(request: Request) -> Optional[User]:
    logger.info("enters get_current_user")
    token = request.cookies.get("session")
    if not token:
        logger.info("Session token not found")
        return None

    try:
        # Use cached version instead of direct jwt.decode
        payload = decode_jwt_cached(token, SECRET_KEY)

        # Verify expiration using simple version with timestamps
        if not verify_token_expiration(payload):
            logger.info("Token expired")
            return None

        user_info: dict = payload.get("user_info")
        if not user_info:
            logger.info("Invalid token: missing user information")
            return None

        # Check if user is revoked
        if await is_user_revoked(user_info["id"]):
            logger.info("User revoked")
            return None

    except JWTError as e:
        logger.error(f"Error decoding JWT token: {e}")
        return None

    logger.debug(f"username: {user_info['username']}")

    user = User(
        id=user_info["id"],
        username=user_info["username"],
        password=None,  # We don't need the password here
        role_id=user_info["role_id"],
        is_enabled=user_info["is_enabled"],
        can_send_files=user_info["can_send_files"],
        can_generate_images=user_info["can_generate_images"],
        current_prompt_id=user_info["current_prompt_id"],
        uses_magic_link=user_info["uses_magic_link"],
        voice_id=user_info["voice_id"],
        voice_code=user_info["voice_code"],
        all_prompts_access=user_info["all_prompts_access"],
        public_prompts_access=user_info["public_prompts_access"],
        authentication_mode=user_info.get("authentication_mode", "magic_link_only"),
        can_change_password=user_info.get("can_change_password", False),
        is_admin=user_info["is_admin"],
        is_manager=user_info["is_manager"],
    )
    
    # Add indicator of whether magic link was used to the user
    user.used_magic_link = user_info.get("used_magic_link", False)

    return user

# Function to get a user by username
async def get_user_by_username(username: str) -> Optional[User]:
    logger.info("entra en get_user_by_username")
    async with get_db_connection(readonly=True) as conn:
        query = '''
        SELECT
            u.id, u.username, u.password, u.role_id, u.is_enabled,
            u.google_id, u.auth_provider,
            ud.current_prompt_id, ud.allow_file_upload, ud.allow_image_generation,
            ud.all_prompts_access, ud.public_prompts_access,
            ud.authentication_mode, ud.can_change_password,
            v.id AS voice_id, v.voice_code,
            (SELECT COUNT(*) FROM magic_links
             WHERE user_id = u.id AND expires_at > CURRENT_TIMESTAMP) AS magic_link_count
        FROM USERS u
        LEFT JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN VOICES v ON ud.voice_id = v.id
        WHERE u.username = ?
        '''
        async with conn.execute(query, (username,)) as cursor:
            row = await cursor.fetchone()
    return create_user_from_row(row) if row else None

def create_user_from_row(row):
    return User(
        id=row['id'],
        username=row['username'],
        password=row['password'],
        role_id=row['role_id'],
        is_enabled=bool(row['is_enabled']),
        can_send_files=bool(row['allow_file_upload']),
        can_generate_images=bool(row['allow_image_generation']),
        current_prompt_id=row['current_prompt_id'],
        uses_magic_link=row['magic_link_count'] > 0,
        voice_id=row['voice_id'],
        voice_code=row['voice_code'],
        all_prompts_access=bool(row['all_prompts_access']),
        public_prompts_access=bool(row['public_prompts_access']),
        authentication_mode=row['authentication_mode'] or 'magic_link_only',
        can_change_password=bool(row['can_change_password']),
        google_id=row['google_id'],
        auth_provider=row['auth_provider'] or 'local',
        is_admin=None,
        is_manager=None,
    )

async def get_user_by_id(user_id: int) -> Optional[User]:
    logger.info("entra en get_user_by_id")
    async with get_db_connection(readonly=True) as conn:
        query = '''
        SELECT
            u.id, u.username, u.password, u.role_id, u.is_enabled,
            u.google_id, u.auth_provider,
            ud.current_prompt_id, ud.allow_file_upload, ud.allow_image_generation,
            ud.all_prompts_access, ud.public_prompts_access,
            ud.authentication_mode, ud.can_change_password,
            v.id AS voice_id, v.voice_code,
            (SELECT COUNT(*) FROM magic_links
             WHERE user_id = u.id AND expires_at > CURRENT_TIMESTAMP) AS magic_link_count
        FROM USERS u
        LEFT JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN VOICES v ON ud.voice_id = v.id
        WHERE u.id = ?
        '''
        async with conn.execute(query, (user_id,)) as cursor:
            row = await cursor.fetchone()
    return create_user_from_row(row) if row else None

async def get_user_from_phone_number(phone_number: str) -> Optional[User]:
    logger.info("entra en get_user_from_phone_number")
    formatted_phone_number = phone_number.replace("whatsapp:", "")
    async with get_db_connection(readonly=True) as conn:
        query = '''
        SELECT
            u.id, u.username, u.password, u.role_id, u.is_enabled, u.phone_number,
            u.google_id, u.auth_provider,
            ud.current_prompt_id, ud.allow_file_upload, ud.allow_image_generation,
            ud.all_prompts_access, ud.public_prompts_access,
            ud.authentication_mode, ud.can_change_password,
            v.id AS voice_id, v.voice_code,
            (SELECT COUNT(*) FROM magic_links
             WHERE user_id = u.id AND expires_at > CURRENT_TIMESTAMP) AS magic_link_count
        FROM USERS u
        LEFT JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN VOICES v ON ud.voice_id = v.id
        WHERE u.phone_number = ?
        '''
        async with conn.execute(query, (formatted_phone_number,)) as cursor:
            row = await cursor.fetchone()
    return create_user_from_row(row) if row else None

async def get_user_from_telegram_chat_id(chat_id: int) -> Optional[User]:
    """Look up user by telegram_chat_id. Same query pattern as get_user_from_phone_number."""
    async with get_db_connection(readonly=True) as conn:
        query = '''
        SELECT
            u.id, u.username, u.password, u.role_id, u.is_enabled, u.phone_number,
            u.google_id, u.auth_provider,
            ud.current_prompt_id, ud.allow_file_upload, ud.allow_image_generation,
            ud.all_prompts_access, ud.public_prompts_access,
            ud.authentication_mode, ud.can_change_password,
            v.id AS voice_id, v.voice_code,
            (SELECT COUNT(*) FROM magic_links
             WHERE user_id = u.id AND expires_at > CURRENT_TIMESTAMP) AS magic_link_count
        FROM USERS u
        LEFT JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN VOICES v ON ud.voice_id = v.id
        WHERE u.telegram_chat_id = ?
        '''
        async with conn.execute(query, (chat_id,)) as cursor:
            row = await cursor.fetchone()
    return create_user_from_row(row) if row else None

async def get_current_user_from_websocket(websocket: WebSocket) -> Optional[User]:
    token = websocket.cookies.get("session")
    if not token:
        return None

    try:
        payload = decode_jwt_cached(token, SECRET_KEY)

        # Verify token expiration
        if not verify_token_expiration(payload):
            logger.info("WebSocket: Token expired")
            return None

        user_info: dict = payload.get("user_info")
        if not user_info:
            return None

        # Check if user is revoked
        if await is_user_revoked(user_info["id"]):
            logger.info("WebSocket: User revoked")
            return None

    except JWTError as e:
        logger.error(f"WebSocket: Error decoding JWT token: {e}")
        return None

    user = User(
        id=user_info["id"],
        username=user_info["username"],
        password=None,  # We don't need the password here
        role_id=user_info["role_id"],
        is_enabled=user_info["is_enabled"],
        can_send_files=user_info["can_send_files"],
        can_generate_images=user_info["can_generate_images"],
        current_prompt_id=user_info["current_prompt_id"],
        uses_magic_link=user_info["uses_magic_link"],
        voice_id=user_info.get("voice_id"),
        voice_code=user_info.get("voice_code"),
        all_prompts_access=user_info.get("all_prompts_access"),
        public_prompts_access=user_info.get("public_prompts_access"),
        authentication_mode=user_info.get("authentication_mode", "magic_link_only"),
        can_change_password=user_info.get("can_change_password", False),
        is_admin=user_info.get("is_admin"),
        is_manager=user_info.get("is_manager"),
    )

    # Add indicator of whether magic link was used to the user
    user.used_magic_link = user_info.get("used_magic_link", False)

    # If you need fetch_role_ids, you can call it here
    # await user.fetch_role_ids()

    return user
    
async def get_user_id_from_conversation(conversation_id: int) -> int:
    logger.info("entra en get_user_id_from_conversation")
    async with get_db_connection(readonly=True) as conn:
        try:
            async with conn.execute('SELECT user_id FROM conversations WHERE id = ?', (conversation_id,)) as cursor:
                result = await cursor.fetchone()
            
            if result:
                return result[0]
            else:
                raise ValueError(f"User_id not found for conversation_id {conversation_id}")
        except Exception as e:
            logger.error(f"Error executing SELECT query: {e}")
            raise
    
async def get_user_by_token(token: str):
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.cursor()
        await cursor.execute('SELECT user_id FROM magic_links WHERE token = ?', (token,))
        magic_link = await cursor.fetchone()
        await conn.close()

        if not magic_link:
            return None
        user_id = magic_link["user_id"]
        user = await get_user_by_id(user_id)
        return user

async def create_user_info(user, used_magic_link):
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": await user.is_admin,
        "is_manager": await user.is_manager,
        "is_enabled": user.is_enabled,
        "can_send_files": user.can_send_files,
        "can_generate_images": user.can_generate_images,
        "current_prompt_id": user.current_prompt_id,
        "uses_magic_link": user.uses_magic_link,
        "voice_id": user.voice_id,
        "voice_code": user.voice_code,
        "all_prompts_access": user.all_prompts_access,
        "public_prompts_access": user.public_prompts_access,
        "authentication_mode": user.authentication_mode,
        "can_change_password": user.can_change_password,
        "role_id": user.role_id,
        "used_magic_link": used_magic_link  # New boolean field
    }

def create_login_response(user_info, redirect_url=None, default_redirect="/home"):
    token = create_access_token(
        data={
            "sub": user_info["username"],
            "user_info": user_info
        }
    )

    # Use provided redirect_url if it's a safe internal path
    if redirect_url and redirect_url.startswith("/") and not redirect_url.startswith("//"):
        url_redirect = redirect_url
    else:
        url_redirect = default_redirect

    response = RedirectResponse(url=url_redirect, status_code=status.HTTP_302_FOUND)

    # Configure cookie with correct expiration time
    max_age = ACCESS_TOKEN_EXPIRE_MINUTES * 60  # convert to seconds
    response.set_cookie(
        key="session",
        value=token,
        max_age=max_age,
        httponly=True,
        samesite='lax',
        secure=SECURE_COOKIES
    )
    
    return response


def unauthenticated_response():
    """Standard response for unauthenticated API requests."""
    response = JSONResponse(
        content={"error": "unauthenticated", "redirect": "/login"},
        status_code=401
    )
    response.delete_cookie(key="session", path="/", samesite="lax", secure=SECURE_COOKIES)
    return response


async def generate_magic_link(user_id: int, url_path: str, request: Request) -> str:
    token = secrets.token_urlsafe(20)
    expires_at = datetime.now() + timedelta(days=3)
    async with get_db_connection() as conn:
        cursor = await conn.cursor()
        await cursor.execute('INSERT INTO magic_links (user_id, token, expires_at) VALUES (?, ?, ?)', (user_id, token, expires_at))
        await conn.commit()
        await conn.close()

        scheme = request.url.scheme
        host = request.url.hostname
        port = request.url.port

        if port and port not in [80, 443]:
            magic_link = f'{scheme}://{host}:{port}/{url_path}?token={token}'
        else:
            magic_link = f'{scheme}://{host}/{url_path}?token={token}'

        return magic_link


# Google OAuth helper functions
async def get_user_by_google_id(google_id: str) -> Optional[User]:
    """Get user by Google OAuth ID."""
    logger.info(f"Looking up user by google_id")
    async with get_db_connection(readonly=True) as conn:
        query = '''
        SELECT
            u.id, u.username, u.password, u.role_id, u.is_enabled,
            u.google_id, u.auth_provider,
            ud.current_prompt_id, ud.allow_file_upload, ud.allow_image_generation,
            ud.all_prompts_access, ud.public_prompts_access,
            ud.authentication_mode, ud.can_change_password,
            v.id AS voice_id, v.voice_code,
            (SELECT COUNT(*) FROM magic_links
             WHERE user_id = u.id AND expires_at > CURRENT_TIMESTAMP) AS magic_link_count
        FROM USERS u
        LEFT JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN VOICES v ON ud.voice_id = v.id
        WHERE u.google_id = ?
        '''
        async with conn.execute(query, (google_id,)) as cursor:
            row = await cursor.fetchone()
    return create_user_from_row(row) if row else None


async def get_user_by_email(email: str) -> Optional[User]:
    """Get user by email address."""
    logger.info(f"Looking up user by email")
    async with get_db_connection(readonly=True) as conn:
        query = '''
        SELECT
            u.id, u.username, u.password, u.role_id, u.is_enabled,
            u.google_id, u.auth_provider,
            ud.current_prompt_id, ud.allow_file_upload, ud.allow_image_generation,
            ud.all_prompts_access, ud.public_prompts_access,
            ud.authentication_mode, ud.can_change_password,
            v.id AS voice_id, v.voice_code,
            (SELECT COUNT(*) FROM magic_links
             WHERE user_id = u.id AND expires_at > CURRENT_TIMESTAMP) AS magic_link_count
        FROM USERS u
        LEFT JOIN USER_DETAILS ud ON u.id = ud.user_id
        LEFT JOIN VOICES v ON ud.voice_id = v.id
        WHERE u.email = ?
        '''
        async with conn.execute(query, (email.lower(),)) as cursor:
            row = await cursor.fetchone()
    return create_user_from_row(row) if row else None


async def update_user_google_id(user_id: int, google_id: str, auth_provider: str = "google_linked") -> bool:
    """Link Google account to existing user."""
    logger.info(f"Linking Google account to user {user_id}")
    try:
        async with get_db_connection() as conn:
            await conn.execute(
                "UPDATE USERS SET google_id = ?, auth_provider = ? WHERE id = ?",
                (google_id, auth_provider, user_id)
            )
            await conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error linking Google account: {e}")
        return False
