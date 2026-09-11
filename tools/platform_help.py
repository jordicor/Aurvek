# tools/platform_help.py

import logging
import json
import re
import unicodedata

from i18n import LANGUAGES, normalize_language
from tools import register_tool

logger = logging.getLogger(__name__)

# ---- FTS5 query builder (adapted from message_search.py) ----

# Short tokens that are meaningful for KB search (not filtered by length check)
FTS_SHORT_ALLOWLIST = {'ai', 'ui', 'qr', 'tts', 'stt', 'pdf', 'mp3'}

STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'can', 'this', 'that', 'these',
    'those', 'it', 'its', 'my', 'your', 'his', 'her', 'our', 'their',
    'what', 'which', 'who', 'whom', 'how', 'when', 'where', 'why',
    'not', 'no', 'nor', 'so', 'if', 'then', 'than', 'too', 'very',
    'just', 'about', 'also', 'como', 'que', 'por', 'para', 'una', 'uno',
    'los', 'las', 'del', 'con', 'sin', 'mas', 'pero', 'hay', 'ser',
    'esta', 'este', 'ese', 'esa',
})

def _search_tokens(raw_query: str) -> list[str]:
    normalized = unicodedata.normalize('NFKC', raw_query[:512]).casefold()
    words = re.findall(r'[^\W_]+', normalized)
    return list(dict.fromkeys(word for word in words if word not in STOPWORDS and (
        len(word) > 2 or word in FTS_SHORT_ALLOWLIST or re.search(r'[\u3040-\u30ff\u3400-\u9fff]', word)
    )))[:16]


def build_help_fts_query(raw_query: str) -> tuple[str, str]:
    """Bound and quote every token so punctuation cannot become FTS operators."""
    parts = [f'"{word}"' for word in _search_tokens(raw_query)]
    return ' '.join(parts), ' OR '.join(parts)


def _help_sql(locale):
    """Return the content source with the SAME live base controls on every path."""
    content = 'a' if locale == 'en' else 'v'
    fts = 'HELP_ARTICLES_FTS' if locale == 'en' else 'HELP_ARTICLE_VARIANTS_FTS'
    source = 'HELP_ARTICLES a'
    controls = """
        a.is_active = 1 AND a.approval_status = 'approved' AND a.tool_visible = 1
        AND (a.required_role IS NULL OR :user_role = 'admin' OR a.required_role = :user_role)
    """
    if locale != 'en':
        source += ' JOIN HELP_ARTICLE_VARIANTS v ON v.article_pk = a.id'
        controls += """ AND v.locale = :locale AND v.is_active = 1
            AND v.approval_status = 'approved' AND v.base_source_hash = a.source_hash"""
    fields = f'a.article_id, a.category, {content}.title, {content}.short_answer, {content}.tool_text, {content}.keywords'
    return content, fts, source, controls, fields


async def _search_locale(conn, query, category, user_role, locale):
    tokens = _search_tokens(query)
    if not tokens:
        return []
    and_query, or_query = build_help_fts_query(query)
    content, fts, source, controls, fields = _help_sql(locale)
    for search_category in ([category, None] if category else [None]):
        params = {'user_role': user_role, 'locale': locale, 'category': search_category}
        filtered = controls + (' AND a.category = :category' if search_category else '')
        sql = (f'SELECT {fields} FROM {source} JOIN {fts} ON {fts}.rowid = {content}.id '
               f'WHERE {fts} MATCH :query AND {filtered} ORDER BY bm25({fts}), a.article_id LIMIT 3')
        for fts_query in dict.fromkeys((and_query, or_query)):
            cursor = await conn.execute(sql, {**params, 'query': fts_query})
            rows = await cursor.fetchall()
            if rows:
                return rows

        if locale == 'ja':
            # unicode61 cannot split an unspaced Japanese sentence. Match curated
            # keywords against the query over this small, permission-filtered KB.
            cursor = await conn.execute(f'SELECT {fields} FROM {source} WHERE {filtered}', params)
            candidates = await cursor.fetchall()
            normalized = unicodedata.normalize('NFKC', query[:512]).casefold()
            ranked = []
            for row in candidates:
                keywords = {unicodedata.normalize('NFKC', word).casefold()
                            for word in json.loads(row['keywords'])}
                matches = [word for word in keywords if len(word) >= 2 and word in normalized]
                if matches:
                    ranked.append((sum(len(word) ** 2 for word in matches), row))
            if ranked:
                ranked.sort(key=lambda item: (-item[0], item[1]['article_id']))
                return [row for _, row in ranked[:3]]

        # Substring fallback also handles short Japanese feature names.
        word = max(tokens, key=len)
        if len(word) < (2 if locale == 'ja' else 3):
            continue
        escaped = word.replace('~', '~~').replace('%', '~%').replace('_', '~_')
        like = f"{content}.title LIKE :word ESCAPE '~'"
        cursor = await conn.execute(
            f'SELECT {fields} FROM {source} WHERE {filtered} AND '
            f"({like} OR {content}.keywords LIKE :word ESCAPE '~' OR {content}.short_answer LIKE :word ESCAPE '~') "
            f'ORDER BY CASE WHEN {like} THEN 0 ELSE 1 END, a.article_id LIMIT 3',
            {**params, 'word': '%' + escaped + '%'},
        )
        rows = await cursor.fetchall()
        if rows:
            return rows
    return []


async def lookup_platform_help(conn, query: str, category: str = None, user_role: str = 'customer',
                               locale: str = 'en', english_query: str = None) -> tuple:
    """Return up to three articles in conversation language, or explicit English fallback.

    Locale is a retrieval hint supplied by the model from the conversation, never
    an authority for access checks. The caller supplies the user's live DB role.
    Existing callers without locale keep English behavior and the same tuple.
    """
    locale = normalize_language(locale) or 'en'
    if not isinstance(query, str) or not query.strip():
        return ('No results found. The query was empty.', 0, None)
    if not isinstance(english_query, str) or not english_query.strip():
        english_query = query
    if category not in ('whatsapp', 'telegram', 'voice', 'search', 'chat', 'settings',
                        'media', 'auth', 'billing', 'limitations'):
        category = None
    try:
        rows = await _search_locale(conn, query, category, user_role, locale)
        content_locale = locale
        if not rows and locale != 'en':
            rows = await _search_locale(conn, english_query, category, user_role, 'en')
            content_locale = 'en'
        if not rows:
            return (
                'No platform help articles found for this query. You do not have confirmed '
                'information about this platform feature. Tell the user in the conversation '
                "language that you do not have specific guidance and suggest contacting support.",
                0, None,
            )
        results = []
        for index, row in enumerate(rows):
            body = row['tool_text'] if index < 2 else row['short_answer']
            results.append(f"### {row['title']} (category: {row['category']})\n{body}\n")
        fallback = 'yes' if content_locale != locale else 'no'
        formatted = (
            f'PLATFORM HELP RESULTS (requested_language={locale}; content_language={content_locale}; '
            f'english_fallback={fallback}). Use this information to answer in the conversation language. '
            'If English fallback was needed, explain the guidance in that language without inventing details.\n\n'
            + '\n---\n'.join(results)
        )
        return formatted, len(rows), rows[0]['article_id']
    except Exception:
        logger.exception('[lookup_platform_help] Help search failed')
        return ('Platform help search is temporarily unavailable. Tell the user in the conversation '
                'language to try again later.', 0, None)


async def log_help_query(query: str, user_message: str, category: str, results_count: int,
                         top_article_id: str, prompt_id: int):
    """Log the query for KB gap analysis. Uses get_db_connection for proper PRAGMAs.
    Stores HMAC-SHA256 hashes keyed with PEPPER -- no PII persisted in DB."""
    import hashlib, hmac
    from common import PEPPER
    normalized = query.strip().lower()
    query_hash = hmac.new(PEPPER.encode(), normalized.encode(), hashlib.sha256).hexdigest()
    user_query_hash = None
    if user_message:
        try:
            user_norm = str(user_message).strip().lower()
            user_query_hash = hmac.new(PEPPER.encode(), user_norm.encode(), hashlib.sha256).hexdigest()
        except Exception:
            pass  # non-critical telemetry
    try:
        from database import get_db_connection
        async with get_db_connection() as conn:
            await conn.execute(
                """INSERT INTO HELP_QUERY_LOG
                   (query_hash, user_query_hash, category, results_count, top_article_id, prompt_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (query_hash, user_query_hash, category, results_count, top_article_id, prompt_id)
            )
            await conn.commit()
    except Exception as e:
        logger.warning(f"[log_help_query] Failed to log: {e}")


# ---- Tool registration ----

register_tool({
    "type": "function",
    "function": {
        "name": "lookup_platform_help",
        "description": (
            "Look up how to use Aurvek platform features. Use this tool when the user asks "
            "how to do something on the platform, whether a feature exists, how something works, "
            "or needs help with platform functionality. Do NOT guess -- always use this tool for "
            "platform-related questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "2-5 keywords in the conversation language describing the feature. Extract key terms rather than a full sentence. Preserve literal commands and product names."
                },
                "locale": {
                    "type": "string",
                    "enum": list(LANGUAGES),
                    "description": "Language in which to answer the current conversation, respecting the assistant instructions and the user's current request. This is independent of UI language. Use en for unsupported languages."
                },
                "english_query": {
                    "type": "string",
                    "description": "When locale is not en, also provide 2-5 English keywords for the same feature. Used only if no current approved localized help matches."
                },
                "category": {
                    "type": "string",
                    "description": "Optional category hint. Use chat for conversations and message search; search is for web search. Omit when uncertain.",
                    "enum": [
                        "whatsapp", "telegram", "voice", "search",
                        "chat", "settings", "media", "auth", "billing",
                        "limitations"
                    ]
                }
            },
            "required": ["query", "locale"],
            "additionalProperties": False
        }
    },
    "strict": False
})
