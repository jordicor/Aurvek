"""Request-local delegated identity without changing native account preferences."""
from __future__ import annotations

from .models import EmbedPrincipal


def is_embed_request(request) -> bool:
    return bool(getattr(request, "scope", {}).get("state", {}).get("embed_host"))


def get_embed_principal(request) -> EmbedPrincipal | None:
    state = getattr(request, "scope", {}).get("state", {})
    principal = state.get("embed_principal")
    return principal if state.get("embed_host") and isinstance(principal, EmbedPrincipal) else None


async def get_embed_user(request):
    """Load a fresh native account and narrow only this request's user object.

    Middleware has already checked the durable frame, membership, delegated
    session and resource binding. Native account revocation checks still apply;
    an administrator has no contextual ownership or billing bypass here.
    """
    from auth import get_user_by_id
    from rediscfg import is_user_revoked

    principal = get_embed_principal(request)
    if principal is None or await is_user_revoked(principal.user_id):
        return None
    user = await get_user_by_id(principal.user_id)
    if (not user or not user.is_enabled
            or user.session_version != principal.session_version):
        return None
    user._is_admin = False
    user.all_prompts_access = False
    user.public_prompts_access = False
    user.current_prompt_id = principal.prompt_id
    user.can_send_files = bool(
        user.can_send_files and principal.capabilities.get("attachments", False)
    )
    user.can_generate_images = bool(
        user.can_generate_images and principal.capabilities.get("image_generation", False)
    )
    user.session_expires_at = principal.expires_at
    user.embed_principal = principal
    return user
