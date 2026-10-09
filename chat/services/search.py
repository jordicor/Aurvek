"""
FTS5 message search service.

Provides full-text search over the MESSAGES_FTS virtual table with query
sanitization, snippet XSS protection, and async execution.
"""

import html
import re

from integrations.applications.listing import blocked_application_conversation_ids


def build_fts_query(raw_query: str) -> str:
    """
    Build a safe FTS5 query string from raw user input.

    - Preserves double-quoted phrases (e.g. "visa interview")
    - Treats loose whitespace-separated terms as literals, including operators
    - Escapes embedded quotes and removes NUL before sending text to SQLite
    - Combines all parts with spaces (FTS5 implicit AND)
    - Returns empty string if nothing remains after cleaning
    """
    raw_query = (raw_query or "").replace("\x00", "")
    if not raw_query.strip():
        return ""

    parts = []
    for match in re.finditer(r'"((?:[^"]|"")*)"|(\S+)', raw_query):
        phrase, token = match.groups()
        text = (phrase.replace('""', '"') if phrase is not None else token).strip()
        if text:
            parts.append('"' + text.replace('"', '""') + '"')

    return " ".join(parts)


def sanitize_snippet(raw_snippet: str) -> str:
    """
    Sanitize an FTS5 snippet for safe HTML rendering.

    Escapes all HTML entities first, then restores only the <mark> and </mark>
    tags injected by FTS5 snippet().
    """
    safe = html.escape(raw_snippet)
    safe = safe.replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")
    return safe


SEARCH_SQL = """
SELECT
  m.id AS message_id,
  m.conversation_id,
  c.chat_name,
  m.type,
  strftime('%Y-%m-%d %H:%M:%S', m.date) AS date,
  snippet(MESSAGES_FTS, 0, '<mark>', '</mark>', ' ... ', 18) AS snippet_html_raw,
  bm25(MESSAGES_FTS) AS rank
FROM MESSAGES_FTS f
JOIN MESSAGES m ON m.id = f.rowid
JOIN CONVERSATIONS c ON c.id = m.conversation_id
WHERE MESSAGES_FTS MATCH :fts_query
  AND c.user_id = :user_id
  AND COALESCE(c.hidden_from_history, 0) = 0
  {application_filter}
  {conv_filter}
ORDER BY rank ASC, m.id DESC
LIMIT :limit OFFSET :offset
"""


async def execute_search(
    conn,
    user_id: int,
    fts_query: str,
    limit: int,
    offset: int,
    conversation_id: int = None,
) -> list[dict]:
    """
    Execute an FTS5 search query against the messages table.

    Args:
        conn: aiosqlite connection (with row_factory already set).
        user_id: ID of the authenticated user.
        fts_query: Pre-sanitized FTS5 query string from build_fts_query().
        limit: Maximum number of results to return.
        offset: Number of results to skip (pagination).
        conversation_id: Optional conversation scope filter.
    """
    if conversation_id is not None:
        conv_filter = "AND m.conversation_id = :conversation_id"
    else:
        conv_filter = ""

    params = {
        "fts_query": fts_query,
        "user_id": user_id,
        "limit": limit,
        "offset": offset,
    }
    if conversation_id is not None:
        params["conversation_id"] = conversation_id

    blocked_ids = await blocked_application_conversation_ids(conn, user_id)
    application_filter = ""
    if blocked_ids:
        placeholders = []
        for index, blocked_id in enumerate(blocked_ids):
            name = f"blocked_{index}"
            placeholders.append(f":{name}")
            params[name] = blocked_id
        application_filter = f"AND c.id NOT IN ({','.join(placeholders)})"

    sql = SEARCH_SQL.format(application_filter=application_filter, conv_filter=conv_filter)
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()

    results = []
    for row in rows:
        results.append(
            {
                "message_id": row[0],
                "conversation_id": row[1],
                "chat_name": row[2],
                "type": row[3],
                "date": row[4],
                "snippet_html": sanitize_snippet(row[5]),
            }
        )

    return results
