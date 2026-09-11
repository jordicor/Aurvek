"""Lazy store entry point keeps identity helpers independent of app startup."""
from .models import EMBED_COOKIE_NAME, EmbedError, EmbedPrincipal

_store = None


def get_embed_store():
    global _store
    if _store is None:
        from .store import EmbedStore
        _store = EmbedStore()
    return _store
