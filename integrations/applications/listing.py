"""Filter native history metadata through the same durable read authorization."""
from __future__ import annotations

import sqlite3

from integrations.applications.runtime import authorize_application_read
from integrations.embed.models import EmbedError


async def blocked_application_conversation_ids(
    connection, user_id: int, *, viewer_user_id: int | None = None, service=None,
) -> list[int]:
    """Return inaccessible bound IDs, including legacy rows awaiting migration.

    Ordinary native databases have no registry and require no identity lookups.
    A partially migrated binding fails closed instead of exposing its metadata.
    """
    viewer_user_id = user_id if viewer_user_id is None else viewer_user_id
    async with connection.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
        tables = {str(row[0]).upper() for row in await cursor.fetchall()}
    candidates = set()
    for table in ("APPLICATION_CONVERSATIONS", "EMBED_INTERVIEWS"):
        if table not in tables:
            continue
        async with connection.execute(
            f"""SELECT c.id FROM CONVERSATIONS c JOIN {table} b
                ON b.conversation_id=c.id WHERE c.user_id=?""", (user_id,),
        ) as cursor:
            candidates.update(int(row[0]) for row in await cursor.fetchall())
    blocked = []
    for conversation_id in sorted(candidates):
        try:
            resolved = await authorize_application_read(
                connection, conversation_id, viewer_user_id, service=service,
            )
            # A discovered binding cannot become an ordinary native conversation.
            if resolved is None:
                blocked.append(conversation_id)
        except (EmbedError, sqlite3.Error, KeyError):
            blocked.append(conversation_id)
    return blocked
