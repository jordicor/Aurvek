"""
Async Telegram Bot API client using httpx.
Mirrors twilio_async.py pattern for consistency.
"""

import httpx
import logging

logger = logging.getLogger(__name__)

BOT_API_BASE = "https://api.telegram.org"


class TelegramAPIError(Exception):
    """Raised when Telegram Bot API returns a non-ok response."""

    def __init__(self, status_code: int, error_code: int, description: str):
        self.status_code = status_code
        self.error_code = error_code
        self.description = description
        super().__init__(
            f"Telegram API error {error_code} (HTTP {status_code}): {description}"
        )


class AsyncTelegramClient:
    """Async client for the Telegram Bot API."""

    def __init__(self, bot_token: str):
        self._token = bot_token
        self._base_url = f"{BOT_API_BASE}/bot{bot_token}"
        self._file_base_url = f"{BOT_API_BASE}/file/bot{bot_token}"
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init the httpx client inside the running event loop."""
        if self._client is None:
            transport = httpx.AsyncHTTPTransport(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                transport=transport,
                trust_env=False,
            )
        return self._client

    def _raise_on_error(self, response: httpx.Response) -> dict:
        """Parse response and raise if not ok."""
        data = response.json()
        if not data.get("ok"):
            raise TelegramAPIError(
                status_code=response.status_code,
                error_code=data.get("error_code", 0),
                description=data.get("description", response.text),
            )
        return data.get("result", {})

    async def get_me(self) -> dict:
        """Get bot info. Used to verify token and display bot name in admin."""
        response = await self._get_client().get(f"{self._base_url}/getMe")
        return self._raise_on_error(response)

    async def set_webhook(
        self, url: str, secret_token: str, max_connections: int = 40
    ) -> bool:
        """Register webhook URL with Telegram."""
        response = await self._get_client().post(
            f"{self._base_url}/setWebhook",
            json={
                "url": url,
                "secret_token": secret_token,
                "max_connections": max_connections,
                "allowed_updates": ["message"],
            },
        )
        self._raise_on_error(response)
        return True

    async def delete_webhook(self) -> bool:
        """Remove webhook."""
        response = await self._get_client().post(
            f"{self._base_url}/deleteWebhook"
        )
        self._raise_on_error(response)
        return True

    async def get_webhook_info(self) -> dict:
        """Get current webhook status."""
        response = await self._get_client().get(
            f"{self._base_url}/getWebhookInfo"
        )
        return self._raise_on_error(response)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        """Send a text message."""
        payload: dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        response = await self._get_client().post(
            f"{self._base_url}/sendMessage", json=payload
        )
        return self._raise_on_error(response)

    async def send_voice(
        self, chat_id: int, voice: bytes, *, caption: str | None = None
    ) -> dict:
        """Send a voice message (OGG/Opus)."""
        files = {"voice": ("voice.ogg", voice, "audio/ogg")}
        data: dict = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        response = await self._get_client().post(
            f"{self._base_url}/sendVoice", files=files, data=data
        )
        return self._raise_on_error(response)

    async def send_photo(
        self, chat_id: int, photo: bytes, *, caption: str | None = None
    ) -> dict:
        """Send a photo."""
        files = {"photo": ("photo.jpg", photo, "image/jpeg")}
        data: dict = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        response = await self._get_client().post(
            f"{self._base_url}/sendPhoto", files=files, data=data
        )
        return self._raise_on_error(response)

    async def get_file(self, file_id: str) -> dict:
        """Get file info (returns file_path for download)."""
        response = await self._get_client().post(
            f"{self._base_url}/getFile", json={"file_id": file_id}
        )
        return self._raise_on_error(response)

    async def download_file(self, file_path: str) -> bytes:
        """Download file content by file_path from getFile().

        Validates file_path to prevent path traversal attacks (defense in depth).
        """
        if ".." in file_path or file_path.startswith("/"):
            raise ValueError(f"Invalid file_path from Telegram API: {file_path!r}")

        url = f"{self._file_base_url}/{file_path}"
        response = await self._get_client().get(url)
        if response.status_code != 200:
            raise TelegramAPIError(
                status_code=response.status_code,
                error_code=0,
                description=f"Failed to download file: {file_path}",
            )
        return response.content

    async def close(self) -> None:
        """Close the underlying httpx client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
