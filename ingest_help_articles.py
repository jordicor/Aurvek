"""Ingest English help and localized variants, with scoped retirement.

Run without options for all seven languages; --locale es selects one language,
and --article settings_profile updates one article without retiring other files.
--rebuild refreshes only the selected language's FTS entries.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

import yaml

from help_schema import ensure_help_schema
from i18n import LANGUAGES

DOCS_DIR = os.path.join(os.path.dirname(__file__), 'docs', 'user_help')
DB_PATH = os.path.join(os.path.dirname(__file__), 'db', 'Aurvek.db')

SECTION_HEADINGS = {
    'en': ('Short answer', 'Steps', 'Notes', 'Prerequisites'),
    'es': ('Respuesta breve', 'Pasos', 'Notas', 'Requisitos previos'),
    'ja': ('要点', '手順', '注意事項', '前提条件'),
    'fr': ('Réponse courte', 'Étapes', 'Remarques', 'Prérequis'),
    'pt': ('Resposta breve', 'Passos', 'Notas', 'Pré-requisitos'),
    'it': ('Risposta breve', 'Passaggi', 'Note', 'Prerequisiti'),
    'de': ('Kurzantwort', 'Schritte', 'Hinweise', 'Voraussetzungen'),
}


def parse_article(filepath: str, locale: str = 'en') -> dict:
    """Parse a markdown article with YAML frontmatter."""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Normalize line endings (Windows CRLF -> LF)
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # Extract frontmatter
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
    if not match:
        raise ValueError(f"No valid frontmatter in {filepath}")

    meta = yaml.safe_load(match.group(1))
    if not isinstance(meta, dict):
        raise ValueError(f"Frontmatter must be a mapping in {filepath}")
    if locale not in LANGUAGES or meta.get('locale', 'en') != locale:
        raise ValueError(f"Locale does not match the selected directory in {filepath}")
    if locale != 'en' and not re.fullmatch(r'[0-9a-f]{64}', str(meta.get('base_source_hash', ''))):
        raise ValueError(f"Missing or invalid base_source_hash in {filepath}")

    # Validate required_role is explicitly set
    raw_role = meta.get('required_role')
    if raw_role is None and meta.get('approval_status') == 'approved':
        raise ValueError(f"Missing required 'required_role' (public/user/admin) in approved article {filepath}")
    if raw_role is not None and raw_role not in ('public', 'user', 'admin'):
        raise ValueError(f"Invalid required_role '{raw_role}' in {filepath} (must be public/user/admin)")

    # Validate frontmatter field types
    if not isinstance(meta.get('id'), str) or not meta['id'].strip():
        raise ValueError(f"'id' must be a non-empty string in {filepath}")
    if meta['id'] != Path(filepath).stem:
        raise ValueError(f"Article id must match the filename in {filepath}")
    if not isinstance(meta.get('title'), str) or not meta['title'].strip():
        raise ValueError(f"'title' must be a non-empty string in {filepath}")
    if not isinstance(meta.get('category'), str):
        raise ValueError(f"'category' must be a string in {filepath}")
    valid_categories = ('whatsapp', 'telegram', 'voice', 'search', 'chat', 'settings', 'media', 'auth', 'billing', 'limitations')
    if meta['category'] not in valid_categories:
        raise ValueError(f"Invalid category '{meta['category']}' in {filepath} (must be one of: {', '.join(valid_categories)})")
    if not isinstance(meta.get('keywords', []), list):
        raise ValueError(f"'keywords' must be a list in {filepath}")
    if meta.get('prerequisites') is not None and not isinstance(meta['prerequisites'], list):
        raise ValueError(f"'prerequisites' must be a list (or omitted) in {filepath}")
    # Existing articles use numeric search terms such as the 1568 pixel limit.
    keywords = meta.get('keywords', [])
    if any(type(item) not in (str, int) or not str(item).strip() for item in keywords):
        raise ValueError(f"'keywords' must contain text or integer search terms in {filepath}")
    if any(not isinstance(item, str) or not item.strip() for item in (meta.get('prerequisites') or [])):
        raise ValueError(f"'prerequisites' must contain non-empty strings in {filepath}")
    if not isinstance(meta.get('tool_visible', False), bool):
        raise ValueError(f"'tool_visible' must be true or false in {filepath}")
    valid_statuses = ('draft', 'review', 'approved')
    if meta.get('approval_status', 'draft') not in valid_statuses:
        raise ValueError(f"Invalid approval_status '{meta.get('approval_status')}' in {filepath} (must be one of: {', '.join(valid_statuses)})")
    if meta.get('last_reviewed') is not None:
        lr = str(meta['last_reviewed'])
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', lr):
            raise ValueError(f"'last_reviewed' must be YYYY-MM-DD format in {filepath}, got '{lr}'")

    body = match.group(2).strip()

    short_heading, steps_heading, notes_heading, prereq_heading = SECTION_HEADINGS[locale]

    def section(heading):
        return re.search(r'^##[ \t]+' + re.escape(heading) + r'[ \t]*\n+(.*?)(?=\n##[ \t]|\Z)',
                         body, re.DOTALL | re.MULTILINE)

    short_match = section(short_heading)
    if not short_match:
        raise ValueError(f"Missing required short answer section in {filepath}")
    short_answer = short_match.group(1).strip()
    if not short_answer:
        raise ValueError(f"Empty short answer section in {filepath}")

    # Extract optional sections for tool_text
    steps_match = section(steps_heading)
    notes_match = section(notes_heading)

    # Build tool_text: clean concatenation of structured sections for the AI
    tool_parts = [short_answer]
    if steps_match:
        tool_parts.append(steps_match.group(1).strip())
    if notes_match:
        tool_parts.append(notes_match.group(1).strip())

    # Incorporate prerequisites into tool_text (clean text, not JSON)
    prereqs = meta.get('prerequisites') or []
    if prereqs and isinstance(prereqs, list):
        prereq_text = prereq_heading + ": " + ", ".join(prereqs)
        tool_parts.append(prereq_text)

    tool_text = '\n\n'.join(tool_parts)

    # Size limit for tool_text
    TOOL_TEXT_MAX_CHARS = 2000
    if len(tool_text) > TOOL_TEXT_MAX_CHARS:
        if meta.get('approval_status') == 'approved' and meta.get('tool_visible', False):
            raise ValueError(
                f"tool_text exceeds {TOOL_TEXT_MAX_CHARS} chars ({len(tool_text)}) in approved+tool_visible article {filepath}. "
                f"Shorten ## Short answer, ## Steps, or ## Notes sections."
            )
        else:
            print(f"  WARNING: tool_text is {len(tool_text)} chars (max recommended: {TOOL_TEXT_MAX_CHARS})")

    # Compute file hash
    source_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()

    return {
        'article_id': meta['id'],
        'title': meta['title'],
        'category': meta['category'],
        'keywords': json.dumps([str(item) for item in keywords], ensure_ascii=False),
        'prerequisites': json.dumps(prereqs, ensure_ascii=False),
        'short_answer': short_answer,
        'tool_text': tool_text,
        'body': body,
        'tool_visible': 1 if meta.get('tool_visible', False) else 0,
        'approval_status': meta.get('approval_status', 'draft'),
        'required_role': None if meta.get('required_role') == 'public' else meta.get('required_role'),
        'last_reviewed_at': meta.get('last_reviewed'),
        'source_hash': source_hash,
        'base_source_hash': meta.get('base_source_hash'),
    }


def _save_article(conn, article, locale, rebuild):
    fields = ('title', 'keywords', 'prerequisites', 'short_answer', 'tool_text',
              'body', 'approval_status', 'last_reviewed_at', 'source_hash')
    if locale == 'en':
        table = 'HELP_ARTICLES'
        identity = {'article_id': article['article_id']}
        fields += ('category', 'tool_visible', 'required_role')
    else:
        base = conn.execute('SELECT * FROM HELP_ARTICLES WHERE article_id = ?',
                            (article['article_id'],)).fetchone()
        if base is None:
            raise ValueError('Missing English base article: ' + article['article_id'])
        for field in ('category', 'required_role', 'tool_visible'):
            if article[field] != base[field]:
                raise ValueError('Variant must share base ' + field + ': ' + article['article_id'])
        table = 'HELP_ARTICLE_VARIANTS'
        identity = {'article_pk': base['id'], 'locale': locale}
        fields += ('base_source_hash',)

    where = ' AND '.join(f'{key} = ?' for key in identity)
    existing = conn.execute(f'SELECT id, source_hash, is_active, tool_text FROM {table} WHERE {where}',
                            tuple(identity.values())).fetchone()
    # Recompute excerpts after parser fixes even if the source file is unchanged.
    if (existing and not rebuild and existing['is_active']
            and existing['source_hash'] == article['source_hash']
            and existing['tool_text'] == article['tool_text']):
        return 'skipped'
    values = tuple(article[field] for field in fields)
    if existing:
        assignments = ', '.join(f'{field} = ?' for field in fields)
        conn.execute(f'UPDATE {table} SET {assignments}, is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (*values, existing['id']))
        return 'updated'
    columns = ', '.join((*identity, *fields))
    marks = ', '.join('?' for _ in (*identity, *fields))
    conn.execute(f'INSERT INTO {table} ({columns}) VALUES ({marks})',
                 (*identity.values(), *values))
    return 'inserted'


def _rebuild_fts(conn, locale):
    if locale == 'en':
        conn.execute('DELETE FROM HELP_ARTICLES_FTS')
        conn.execute("""
            INSERT INTO HELP_ARTICLES_FTS(rowid, title, short_answer, body, keywords)
            SELECT id, title, short_answer, body, keywords FROM HELP_ARTICLES
            WHERE is_active = 1 AND approval_status = 'approved' AND tool_visible = 1
        """)
    else:
        conn.execute('DELETE FROM HELP_ARTICLE_VARIANTS_FTS WHERE rowid IN '
                     '(SELECT id FROM HELP_ARTICLE_VARIANTS WHERE locale = ?)', (locale,))
        conn.execute("""
            INSERT INTO HELP_ARTICLE_VARIANTS_FTS(rowid, title, short_answer, body, keywords)
            SELECT id, title, short_answer, body, keywords FROM HELP_ARTICLE_VARIANTS
            WHERE locale = ? AND is_active = 1 AND approval_status = 'approved'
        """, (locale,))


def ingest(rebuild: bool = False, *, locales=None, article_ids=None, tolerant=False,
           docs_dir=None, db_path=None) -> dict:
    """Upsert a validated scope atomically; partial article updates never retire rows.

    A complete locale directory is its inventory. Missing directories are errors,
    and any parse error prevents retirement in that locale, including tolerant runs.
    """
    selected = tuple(dict.fromkeys(locales if locales is not None else LANGUAGES))
    if not selected or any(locale not in LANGUAGES for locale in selected):
        raise ValueError('Select at least one supported locale')
    selected = sorted(selected, key=lambda locale: locale != 'en')
    requested = set(article_ids or ())
    if any(not re.fullmatch(r'[a-z0-9_]+', aid) for aid in requested):
        raise ValueError('Article ids must contain only lowercase letters, digits and underscores')
    root = Path(docs_dir or DOCS_DIR)
    counts = dict(inserted=0, updated=0, skipped=0, errors=0, deactivated=0, stale=0)
    parsed = {}
    invalid = set()
    for locale in selected:
        directory = root if locale == 'en' else root / locale
        if not directory.is_dir():
            raise ValueError(f'Missing help directory: {directory}')
        files = sorted(directory.glob('*.md'))
        if locale == 'en' and not files:
            raise ValueError('English help directory is empty; refusing retirement')
        if requested:
            missing = requested - {path.stem for path in files}
            if missing:
                raise ValueError(f'Missing requested articles in {locale}: {sorted(missing)}')
            files = [path for path in files if path.stem in requested]
        parsed[locale] = []
        for path in files:
            try:
                parsed[locale].append(parse_article(str(path), locale))
            except (ValueError, yaml.YAMLError) as exc:
                counts['errors'] += 1
                invalid.add(locale)
                print(f'ERROR {locale}/{path.name}: {exc}'.encode('ascii', 'backslashreplace').decode())
    if counts['errors'] and not tolerant:
        raise ValueError('Invalid help articles; no content changes applied')

    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        ensure_help_schema(conn)
        with conn:
            for locale in selected:
                for article in parsed[locale]:
                    try:
                        outcome = _save_article(conn, article, locale, rebuild)
                        counts[outcome] += 1
                    except (ValueError, sqlite3.IntegrityError) as exc:
                        if not tolerant:
                            raise
                        counts['errors'] += 1
                        invalid.add(locale)
                        print(f"ERROR {locale}/{article['article_id']}: {exc}".encode('ascii', 'backslashreplace').decode())
                if not requested and locale not in invalid:
                    ids = [article['article_id'] for article in parsed[locale]]
                    marks = ','.join('?' for _ in ids)
                    if locale == 'en':
                        cursor = conn.execute(f'UPDATE HELP_ARTICLES SET is_active = 0 WHERE is_active = 1 '
                                              f'AND article_id NOT IN ({marks})', ids)
                    else:
                        cursor = conn.execute('UPDATE HELP_ARTICLE_VARIANTS SET is_active = 0 '
                                              'WHERE is_active = 1 AND locale = ? AND article_pk IN '
                                              f'(SELECT id FROM HELP_ARTICLES WHERE article_id NOT IN ({marks}))',
                                              (locale, *ids))
                    counts['deactivated'] += cursor.rowcount
                if rebuild:
                    _rebuild_fts(conn, locale)
            counts['stale'] = conn.execute("""
                SELECT count(*) FROM HELP_ARTICLE_VARIANTS v JOIN HELP_ARTICLES a ON a.id = v.article_pk
                WHERE v.is_active = 1 AND a.is_active = 1 AND v.base_source_hash != a.source_hash
            """).fetchone()[0]
    finally:
        conn.close()
    print('Help ingest: ' + ', '.join(f'{key}={value}' for key, value in counts.items()))
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--locale', action='append', choices=tuple(LANGUAGES), dest='locales')
    parser.add_argument('--article', action='append', dest='article_ids')
    parser.add_argument('--rebuild', action='store_true')
    parser.add_argument('--tolerant', action='store_true')
    args = parser.parse_args()
    try:
        ingest(**vars(args))
    except (ValueError, sqlite3.Error) as exc:
        print(f'ERROR: {exc}'.encode('ascii', 'backslashreplace').decode())
        raise SystemExit(1)
