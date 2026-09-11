from datetime import datetime, timedelta, timezone
from typing import Optional

from common import AVATAR_TOKEN_EXPIRE_HOURS, CLOUDFLARE_BASE_URL
from models import User
from save_images import generate_img_token


BOT_AVATAR_VARIANTS = {
    "bot_profile_picture": "_32.webp",
    "bot_profile_picture_128": "_128.webp",
    "bot_profile_picture_fullsize": "_fullsize.webp",
}


def get_signed_bot_avatar_urls(
    image_base_path: Optional[str],
    current_user: User,
    *,
    current_time: Optional[datetime] = None,
) -> dict[str, Optional[str]]:
    """Build independently signed URLs for every bot avatar variant."""
    if not image_base_path or getattr(current_user, "embed_principal", None):
        return {field: None for field in BOT_AVATAR_VARIANTS}

    if current_time is None:
        current_time = datetime.now(timezone.utc)
    expiration = current_time + timedelta(hours=AVATAR_TOKEN_EXPIRE_HOURS)

    avatar_urls = {}
    for field, suffix in BOT_AVATAR_VARIANTS.items():
        image_path = f"{image_base_path}{suffix}"
        token = generate_img_token(image_path, expiration, current_user)
        avatar_urls[field] = f"{CLOUDFLARE_BASE_URL}{image_path}?token={token}"

    return avatar_urls
