"""Shared exclusion and revocation for manual and wizard landing writes."""

import asyncio
from contextlib import asynccontextmanager

from filelock import FileLock, Timeout as FileLockTimeout

from database import get_db_connection
from marketplace.landing.cache import invalidate_landing_cache
from marketplace.landing.jobs import JOBS_DIR


_LANDING_SAVE_LOCK_DIR = JOBS_DIR / "save-locks"
_LANDING_SAVE_LOCK_TIMEOUT_SECONDS = 30


@asynccontextmanager
async def _landing_save_lock(
    prompt_id: int = 0, *, pack_id: int | None = None, timeout_seconds: float | None = None,
):
    """Hold the existing process-shared lock through every write/publication."""
    _LANDING_SAVE_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    entity = f"pack-{pack_id}" if pack_id is not None else f"prompt-{prompt_id}"
    lock = FileLock(str(_LANDING_SAVE_LOCK_DIR / f"{entity}.lock"))
    loop = asyncio.get_running_loop()
    timeout = _LANDING_SAVE_LOCK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = loop.time() + timeout
    while True:
        try:
            lock.acquire(timeout=0)
            break
        except FileLockTimeout:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(0.05, remaining))
    try:
        yield
    finally:
        lock.release()


async def _revoke_landing_publication(
    prompt_id: int = 0, *, pack_id: int | None = None, revoke_welcome: bool = False,
) -> None:
    """Revoke before writing; a runner error must leave the result unpublished."""
    async with get_db_connection() as conn:
        if pack_id is None:
            await conn.execute(
                ("UPDATE PROMPTS SET has_landing_page = 0, landing_trusted = 0, has_welcome_page = 0 WHERE id = ?"
                 if revoke_welcome else
                 "UPDATE PROMPTS SET has_landing_page = 0, landing_trusted = 0 WHERE id = ?"),
                (prompt_id,),
            )
            cursor = await conn.execute("SELECT public_id FROM PROMPTS WHERE id = ?", (prompt_id,))
        else:
            await conn.execute(
                ("UPDATE PACKS SET has_custom_landing = 0, has_welcome_page = 0 WHERE id = ?"
                 if revoke_welcome else "UPDATE PACKS SET has_custom_landing = 0 WHERE id = ?"),
                (pack_id,),
            )
            cursor = await conn.execute("SELECT public_id FROM PACKS WHERE id = ?", (pack_id,))
        row = await cursor.fetchone()
        await conn.commit()
    if row and row[0]:
        if pack_id is None:
            invalidate_landing_cache(row[0])
        else:
            from marketplace.routes.packs import invalidate_pack_landing_cache
            invalidate_pack_landing_cache(row[0])


async def _set_welcome_page_publication(
    prompt_id: int = 0, *, pack_id: int | None = None, published: bool,
) -> None:
    """Manual saves and workers enable welcome only after a successful write."""
    async with get_db_connection() as conn:
        if pack_id is None:
            await conn.execute("UPDATE PROMPTS SET has_welcome_page = ? WHERE id = ?", (int(published), prompt_id))
        else:
            await conn.execute("UPDATE PACKS SET has_welcome_page = ? WHERE id = ?", (int(published), pack_id))
        await conn.commit()
