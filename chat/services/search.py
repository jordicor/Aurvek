"""
FTS5 message search service.

Provides full-text search over the MESSAGES_FTS virtual table with query
sanitization, snippet XSS protection, and async execution.
"""

import html
import re


def build_fts_query(raw_query: str) -> str:
    """
    Build a safe FTS5 query string from raw user input.

    - Extracts double-quoted phrases verbatim (e.g. "visa interview")
    - Extracts loose tokens split by whitespace
    - Strips dangerous FTS5 operators: * ^ { } ( ) [ ] | :
    - Combines all parts with spaces (FTS5 implicit AND)
    - Returns empty string if nothing remains after cleaning
    """
    if not raw_query or not raw_query.strip():
        return ""

    parts = []
    phrase_pattern = re.compile(r'"([^"]*)"')
    phrases = phrase_pattern.findall(raw_query)
    for phrase in phrases:
        stripped = phrase.strip()
        if stripped:
            parts.append(f'"{stripped}"')

    remaining = phrase_pattern.sub("", raw_query).strip()
    dangerous_chars = re.compile(r'[*^{}\(\)\[\]|:]')
    if remaining:
        for token in remaining.split():
            cleaned = dangerous_chars.sub("", token).strip()
            if cleaned:
                parts.append(cleaned)

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
  {privacy_filter}
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

    privacy_filter = ""
    try:
        cursor = await conn.execute("PRAGMA table_info(CONVERSATIONS)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "hidden_from_history" in columns:
            privacy_filter = "AND COALESCE(c.hidden_from_history, 0) = 0"
    except Exception:
        privacy_filter = ""

    sql = SEARCH_SQL.replace("{privacy_filter}", privacy_filter).replace(
        "{conv_filter}", conv_filter
    )

    params = {
        "fts_query": fts_query,
        "user_id": user_id,
        "limit": limit,
        "offset": offset,
    }
    if conversation_id is not None:
        params["conversation_id"] = conversation_id

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
