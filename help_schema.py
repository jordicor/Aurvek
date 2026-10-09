"""SQLite storage for the platform help articles and their editorial variants."""

import sqlite3


ARTICLE_SCHEMA = """
    CREATE TABLE {table} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id TEXT NOT NULL,
        title TEXT NOT NULL,
        category TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '[]',
        prerequisites TEXT DEFAULT '[]',
        short_answer TEXT NOT NULL,
        tool_text TEXT NOT NULL,
        body TEXT NOT NULL,
        tool_visible INTEGER NOT NULL DEFAULT 0,
        approval_status TEXT NOT NULL DEFAULT 'draft'
            CHECK(approval_status IN ('draft','review','approved')),
        required_role TEXT CHECK(required_role IS NULL OR required_role IN ('user','admin')),
        last_reviewed_at TEXT,
        source_hash TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        knowledge_scope TEXT NOT NULL DEFAULT 'platform',
        required_capabilities TEXT NOT NULL DEFAULT '[]',
        required_tools TEXT NOT NULL DEFAULT '[]',
        UNIQUE(knowledge_scope, article_id)
    )
"""


def _ensure_article_scope(conn: sqlite3.Connection) -> None:
    columns = [row[1] for row in conn.execute('PRAGMA table_info(HELP_ARTICLES)')]
    if not columns:
        conn.execute(ARTICLE_SCHEMA.format(table='HELP_ARTICLES'))
        return
    if 'knowledge_scope' in columns:
        return
    # Rebuild only the help table to remove the old global ID/category limits.
    # Preserve primary keys so translations and FTS rows retain their identity.
    if conn.in_transaction:
        raise ValueError('Help schema migration must run before content transactions')
    foreign_keys = conn.execute('PRAGMA foreign_keys').fetchone()[0]
    conn.execute('PRAGMA foreign_keys = OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(ARTICLE_SCHEMA.format(table='HELP_ARTICLES_SCOPED'))
        names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
        conn.execute(f'INSERT INTO HELP_ARTICLES_SCOPED ({names}) SELECT {names} FROM HELP_ARTICLES')
        conn.execute('DROP TABLE HELP_ARTICLES')
        conn.execute('ALTER TABLE HELP_ARTICLES_SCOPED RENAME TO HELP_ARTICLES')
        variants_exist = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='HELP_ARTICLE_VARIANTS'").fetchone()
        if variants_exist and conn.execute('PRAGMA foreign_key_check(HELP_ARTICLE_VARIANTS)').fetchone():
            raise ValueError('Help translation references failed migration integrity checks')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute(f'PRAGMA foreign_keys = {foreign_keys}')


def ensure_help_schema(conn: sqlite3.Connection) -> None:
    """Create missing help storage without replacing existing articles or logs.

    Called before ingestion transactions, by fresh database setup and migration.
    English remains in HELP_ARTICLES; all access controls belong to that row.
    """
    _ensure_article_scope(conn)
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS HELP_ARTICLES_FTS USING fts5(
            title, short_answer, body, keywords, tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS trg_help_fts_insert AFTER INSERT ON HELP_ARTICLES
        WHEN NEW.is_active = 1 AND NEW.approval_status = 'approved' AND NEW.tool_visible = 1
        BEGIN
            INSERT INTO HELP_ARTICLES_FTS(rowid, title, short_answer, body, keywords)
            VALUES (NEW.id, NEW.title, NEW.short_answer, NEW.body, NEW.keywords);
        END;
        CREATE TRIGGER IF NOT EXISTS trg_help_fts_delete AFTER DELETE ON HELP_ARTICLES
        BEGIN
            DELETE FROM HELP_ARTICLES_FTS WHERE rowid = OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_help_fts_update AFTER UPDATE ON HELP_ARTICLES
        BEGIN
            DELETE FROM HELP_ARTICLES_FTS WHERE rowid = OLD.id;
            INSERT INTO HELP_ARTICLES_FTS(rowid, title, short_answer, body, keywords)
            SELECT NEW.id, NEW.title, NEW.short_answer, NEW.body, NEW.keywords
            WHERE NEW.is_active = 1 AND NEW.approval_status = 'approved' AND NEW.tool_visible = 1;
        END;
        CREATE TABLE IF NOT EXISTS HELP_QUERY_LOG (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_hash TEXT NOT NULL,
            user_query_hash TEXT,
            category TEXT,
            results_count INTEGER NOT NULL DEFAULT 0,
            top_article_id TEXT,
            prompt_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_help_query_log_created ON HELP_QUERY_LOG(created_at);
        CREATE TABLE IF NOT EXISTS HELP_ARTICLE_VARIANTS (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_pk INTEGER NOT NULL REFERENCES HELP_ARTICLES(id) ON DELETE CASCADE,
            locale TEXT NOT NULL CHECK(locale IN ('es','ja','fr','pt','it','de')),
            title TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '[]',
            prerequisites TEXT NOT NULL DEFAULT '[]',
            short_answer TEXT NOT NULL,
            tool_text TEXT NOT NULL,
            body TEXT NOT NULL,
            approval_status TEXT NOT NULL DEFAULT 'draft'
                CHECK(approval_status IN ('draft','review','approved')),
            last_reviewed_at TEXT,
            source_hash TEXT NOT NULL,
            base_source_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(article_pk, locale)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS HELP_ARTICLE_VARIANTS_FTS USING fts5(
            title, short_answer, body, keywords, tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS trg_help_variant_fts_insert AFTER INSERT ON HELP_ARTICLE_VARIANTS
        WHEN NEW.is_active = 1 AND NEW.approval_status = 'approved'
        BEGIN
            INSERT INTO HELP_ARTICLE_VARIANTS_FTS(rowid, title, short_answer, body, keywords)
            VALUES (NEW.id, NEW.title, NEW.short_answer, NEW.body, NEW.keywords);
        END;
        CREATE TRIGGER IF NOT EXISTS trg_help_variant_fts_delete AFTER DELETE ON HELP_ARTICLE_VARIANTS
        BEGIN
            DELETE FROM HELP_ARTICLE_VARIANTS_FTS WHERE rowid = OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS trg_help_variant_fts_update AFTER UPDATE ON HELP_ARTICLE_VARIANTS
        BEGIN
            DELETE FROM HELP_ARTICLE_VARIANTS_FTS WHERE rowid = OLD.id;
            INSERT INTO HELP_ARTICLE_VARIANTS_FTS(rowid, title, short_answer, body, keywords)
            SELECT NEW.id, NEW.title, NEW.short_answer, NEW.body, NEW.keywords
            WHERE NEW.is_active = 1 AND NEW.approval_status = 'approved';
        END;
    """)
