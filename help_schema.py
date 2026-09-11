"""SQLite storage for the platform help articles and their editorial variants."""

import sqlite3


def ensure_help_schema(conn: sqlite3.Connection) -> None:
    """Create missing help storage without replacing existing articles or logs.

    Called before ingestion transactions, by fresh database setup and migration.
    English remains in HELP_ARTICLES; all access controls belong to that row.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS HELP_ARTICLES (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN
                ('whatsapp','telegram','voice','search','chat','settings','media','auth','billing','limitations')),
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
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
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
