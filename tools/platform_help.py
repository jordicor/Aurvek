# tools/platform_help.py

import logging
import json
import re
import unicodedata

from i18n import LANGUAGES, normalize_language
from tools import register_tool

logger = logging.getLogger(__name__)

HELP_EVIDENCE_GUIDANCE = (
    'Answer only the details explicitly supported by these results. Related articles do not '
    'confirm details they do not document. For a question covered only in part, answer the '
    'supported part and acknowledge the gaps. For each unsupported requested detail, say in '
    'the user\'s language the equivalent of: "I could not find documented information about '
    'that, so I cannot confirm it." Missing documentation does not prove that a feature '
    'does not exist. Mention menus, links, or support routes only when explicitly documented; '
    'do not suggest generic places to look.'
)

# ---- FTS5 query builder (adapted from message_search.py) ----

# Short tokens that are meaningful for KB search (not filtered by length check)
FTS_SHORT_ALLOWLIST = {'ai', 'ui', 'qa', 'qr', 'tts', 'stt', 'pdf', 'mp3'}

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
        AND a.knowledge_scope IN (:knowledge_scope, :shared_scope)
        AND NOT EXISTS (SELECT 1 FROM json_each(a.required_capabilities) required
            WHERE required.value NOT IN (SELECT value FROM json_each(:capabilities)))
        AND NOT EXISTS (SELECT 1 FROM json_each(a.required_tools) required
            WHERE required.value NOT IN (SELECT value FROM json_each(:tools)))
    """
    if locale != 'en':
        source += ' JOIN HELP_ARTICLE_VARIANTS v ON v.article_pk = a.id'
        controls += """ AND v.locale = :locale AND v.is_active = 1
            AND v.approval_status = 'approved' AND v.base_source_hash = a.source_hash"""
    fields = f'a.article_id, a.knowledge_scope, a.category, {content}.title, {content}.short_answer, {content}.tool_text, {content}.keywords'
    return content, fts, source, controls, fields


async def _search_locale(conn, query, category, user_role, locale, scope_params):
    tokens = _search_tokens(query)
    if not tokens:
        return []
    and_query, or_query = build_help_fts_query(query)
    content, fts, source, controls, fields = _help_sql(locale)
    for search_category in ([category, None] if category else [None]):
        params = {**scope_params, 'user_role': user_role, 'locale': locale, 'category': search_category}
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
                               locale: str = 'en', english_query: str = None, *,
                               application_context=None, available_tools=()) -> tuple:
    """Return up to three articles in conversation language, or explicit English fallback.

    Locale is a retrieval hint supplied by the model from the conversation, never
    an authority for access checks. The caller supplies the user's live DB role.
    Application scope, capabilities and tool names are server-resolved authority,
    never model arguments. Every search path uses the same scope restrictions.
    """
    locale = normalize_language(locale) or 'en'
    if not isinstance(query, str) or not query.strip():
        return (
            'Help search was not performed because the query was empty. No documentation '
            'was checked. Explain in the user\'s language that you could not check the '
            'documentation, so you cannot confirm the requested detail. This is not a '
            'search with no results and does not prove that a feature does not exist. '
            'Do not invent details, menus, links, or support routes.',
            0, None,
        )
    if not isinstance(english_query, str) or not english_query.strip():
        english_query = query
    if category is not None and (not isinstance(category, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', category)):
        category = None
    scope_params = {
        'knowledge_scope': 'app:' + application_context.app_id if application_context else 'platform',
        'shared_scope': 'embed' if application_context else 'platform',
        'capabilities': json.dumps([key for key, value in application_context.capabilities.items() if value is True]
                                   if application_context else []),
        'tools': json.dumps(sorted(available_tools)),
    }
    try:
        rows = await _search_locale(conn, query, category, user_role, locale, scope_params)
        content_locale = locale
        if not rows and locale != 'en':
            rows = await _search_locale(conn, english_query, category, user_role, 'en', scope_params)
            content_locale = 'en'
        if not rows:
            return (
                'No help articles matched this query in the authorized knowledge scope. '
                + HELP_EVIDENCE_GUIDANCE,
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
            'If English fallback was needed, explain the guidance in that language without inventing details. '
            + HELP_EVIDENCE_GUIDANCE + '\n\n'
            + '\n---\n'.join(results)
        )
        top = rows[0]
        article_id = top['article_id'] if top['knowledge_scope'] == 'platform' else top['knowledge_scope'] + '/' + top['article_id']
        return formatted, len(rows), article_id
    except Exception:
        logger.exception('[lookup_platform_help] Help search failed')
        return (
            'Help search failed; the documentation could not be checked. Explain in the '
            'user\'s language that you could not check the documentation, so you cannot '
            'confirm the requested detail. This is not a search with no results and does '
            'not prove that a feature does not exist. Do not invent details, menus, links, '
            'or support routes.',
            0, None,
        )


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
            "Look up approved guidance for the current product and its available chat features. "
            "Use for product questions, reference material, and how to perform supported actions. "
            "The server selects the authorized knowledge scope. Do not guess undocumented product behavior."
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
                    "description": "Optional product category hint, such as chat, product, writing, or qa. Omit when uncertain.",
                    "pattern": "^[a-z][a-z0-9_]{0,63}$",
                    "maxLength": 64
                }
            },
            "required": ["query", "locale"],
            "additionalProperties": False
        }
    },
    "strict": False
})
