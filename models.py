# models.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import logging

# Custom libraries
from database import get_db_connection
from i18n import normalize_language
from common import tts_engine, load_service_costs

# Logger configuration
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[websocket] = {"task": None}

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            task = self.active_connections[websocket]["task"]
            if task:
                task.cancel()
            del self.active_connections[websocket]

    async def send_bytes(self, websocket: WebSocket, data: bytes):
        try:
            await websocket.send_bytes(data)
        except WebSocketDisconnect:
            self.disconnect(websocket)

    async def send_json(self, websocket: WebSocket, data: dict):
        try:
            await websocket.send_json(data)
        except WebSocketDisconnect:
            self.disconnect(websocket)



class User:
    def __init__(
        self,
        id: int,
        username: str,
        password: Optional[str],
        role_id: Optional[int],
        is_enabled: bool,
        can_send_files: bool,
        can_generate_images: bool,
        current_prompt_id: Optional[int],
        uses_magic_link: bool = False,
        voice_id: Optional[int] = None,
        voice_code: Optional[str] = None,
        all_prompts_access: bool = False,
        public_prompts_access: bool = False,
        is_admin: Optional[bool] = None,
        is_user: Optional[bool] = None,
        authentication_mode: str = "magic_link_only",
        can_change_password: bool = False,
        google_id: Optional[str] = None,
        auth_provider: str = "local",
        session_version: int = 1,
        ui_language: str = "en",
    ):
        self.id = id
        self.username = username
        self.password = password
        self.role_id = role_id
        self.is_enabled = is_enabled
        self.can_send_files = can_send_files
        self.can_generate_images = can_generate_images
        self.current_prompt_id = current_prompt_id
        self.uses_magic_link = uses_magic_link
        self.voice_id = voice_id
        self.voice_code = voice_code
        self.all_prompts_access = all_prompts_access
        self.public_prompts_access = public_prompts_access
        self.authentication_mode = authentication_mode
        self.can_change_password = can_change_password
        self.google_id = google_id
        self.auth_provider = auth_provider
        self.session_version = max(1, int(session_version or 1))
        self.ui_language = normalize_language(ui_language) or "en"
        self.auth_time = 0
        self.session_expires_at = 0
        self.used_magic_link = False

        # We initialize the role attributes
        self._is_admin = is_admin
        self._is_user = is_user
        self.role_ids = None  # Cache for role IDs

    async def fetch_role_ids(self):
        logger.info("enters fetch_role_ids")
        if self.role_ids is None:
            async with get_db_connection(readonly=True) as conn:
                logger.info("enters fetch_role_ids and executes SQL query")
                query = "SELECT id, role_name FROM USER_ROLES"
                async with conn.execute(query) as cursor:
                    results = await cursor.fetchall()
                    self.role_ids = {row[1].lower(): row[0] for row in results}

    @property
    async def is_admin(self):
        if self._is_admin is not None:
            return self._is_admin
        await self.fetch_role_ids()
        self._is_admin = self.role_id == self.role_ids.get('admin')
        return self._is_admin

    @property
    async def is_user(self):
        if self._is_user is not None:
            return self._is_user
        await self.fetch_role_ids()
        self._is_user = self.role_id == self.role_ids.get('user')
        return self._is_user

    @property
    async def is_customer(self):
        await self.fetch_role_ids()
        return self.role_id == self.role_ids.get('customer')

    async def get_magic_link_expiration(self) -> Optional[datetime]:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute(
                'SELECT expires_at FROM magic_links WHERE user_id = ? ORDER BY expires_at DESC LIMIT 1',
                (self.id,)
            )
            result = await cursor.fetchone()
        if result and result[0]:
            try:
                return datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                return datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
        return None

    def can_use_magic_link(self) -> bool:
        """Check if user can use magic link authentication"""
        return self.authentication_mode in ["magic_link_only", "magic_link_password"]
    
    def can_use_password(self) -> bool:
        """Check if user can use password authentication"""
        return self.authentication_mode in ["password_only", "magic_link_password"]
    
    def should_show_change_password(self) -> bool:
        """Check if change password option should be shown to user"""
        return self.can_change_password and self.can_use_password()

    async def has_sufficient_balance(self, required_balance: float) -> bool:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.cursor()
            await cursor.execute('SELECT balance FROM USER_DETAILS WHERE user_id = ?', (self.id,))
            result = await cursor.fetchone()
            await conn.close()
            current_balance = result[0] if result else 0.00
            logger.debug(f"Current balance: {current_balance}")
            if await self.is_admin:
                return True

            return current_balance >= required_balance
    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role_id": self.role_id,
            "is_enabled": self.is_enabled,
            "can_send_files": self.can_send_files,
            "can_generate_images": self.can_generate_images,
            "current_prompt_id": self.current_prompt_id,
            "uses_magic_link": self.uses_magic_link,
            "authentication_mode": self.authentication_mode,
            "can_change_password": self.can_change_password,
            "session_version": self.session_version,
        }


@dataclass
class Pack:
    id: int
    name: str
    slug: str
    created_by_user_id: int
    description: Optional[str] = None
    cover_image: Optional[str] = None
    is_public: bool = False
    is_paid: bool = False
    price: float = 0.00
    status: str = "draft"
    public_id: Optional[str] = None
    landing_reg_config: Optional[str] = None
    tags: Optional[str] = None
    max_items: int = 50
    has_custom_landing: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Joined fields (not stored in PACKS table)
    created_by_username: Optional[str] = None
    item_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "cover_image": self.cover_image,
            "created_by_user_id": self.created_by_user_id,
            "created_by_username": self.created_by_username,
            "is_public": self.is_public,
            "is_paid": self.is_paid,
            "price": self.price,
            "status": self.status,
            "public_id": self.public_id,
            "tags": self.tags,
            "max_items": self.max_items,
            "has_custom_landing": self.has_custom_landing,
            "item_count": self.item_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PackItem:
    id: int
    pack_id: int
    prompt_id: int
    display_order: int = 0
    notice_period_snapshot: int = 0
    disable_at: Optional[str] = None
    is_active: bool = True
    added_at: Optional[str] = None
    # Joined fields from PROMPTS table
    prompt_name: Optional[str] = None
    prompt_description: Optional[str] = None
    prompt_image: Optional[str] = None
    prompt_owner_username: Optional[str] = None

    def to_dict(self):
        return {
            "id": self.id,
            "pack_id": self.pack_id,
            "prompt_id": self.prompt_id,
            "display_order": self.display_order,
            "notice_period_snapshot": self.notice_period_snapshot,
            "disable_at": self.disable_at,
            "is_active": self.is_active,
            "added_at": self.added_at,
            "prompt_name": self.prompt_name,
            "prompt_description": self.prompt_description,
            "prompt_image": self.prompt_image,
            "prompt_owner_username": self.prompt_owner_username,
        }


@dataclass
class PromptExtension:
    id: int
    prompt_id: int
    name: str
    slug: str
    prompt_text: str
    description: Optional[str] = None
    display_order: int = 0
    is_default: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt_id": self.prompt_id,
            "name": self.name,
            "slug": self.slug,
            "prompt_text": self.prompt_text,
            "description": self.description,
            "display_order": self.display_order,
            "is_default": self.is_default,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
