"""Render only Aurvek-owned public templates through the normal Jinja runtime."""

from common import templates
from i18n import get_translator
from marketplace.landing.isolation import primary_app_url


def public_page(request, filename: str, current_user=None, **context):
    translator = get_translator(request, current_user)
    return templates.TemplateResponse(
        f"public/{filename}",
        {"request": request, "primary_root": primary_app_url("/") or "/", **context},
        headers={
            "Content-Language": translator.language,
            "Cache-Control": "private, no-cache",
            "Vary": "Cookie, Accept-Language, Authorization",
        },
    )
