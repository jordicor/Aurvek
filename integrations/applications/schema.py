"""Additive application schema and shared, deterministic v1 adoption statements."""
from __future__ import annotations

import hashlib
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_SETTINGS (
 app_id TEXT PRIMARY KEY REFERENCES EMBED_APPS(app_id), entry_assistant_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS APPLICATION_ASSISTANTS (
 app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id), assistant_id TEXT NOT NULL,
 prompt_id INTEGER NOT NULL, config_json TEXT NOT NULL,
 PRIMARY KEY(app_id,assistant_id)
);
CREATE TABLE IF NOT EXISTS APPLICATION_CONTEXTS (
 context_id TEXT PRIMARY KEY, app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 beneficiary_ref TEXT, active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(app_id,context_id)
);
CREATE TABLE IF NOT EXISTS APPLICATION_PARTICIPANTS (
 app_id TEXT NOT NULL, context_id TEXT NOT NULL, subject TEXT NOT NULL,
 context_ref TEXT, active INTEGER NOT NULL DEFAULT 1,
 capabilities_json TEXT NOT NULL, assistant_ids_json TEXT,
 PRIMARY KEY(app_id,context_id,subject), UNIQUE(app_id,subject,context_ref),
 FOREIGN KEY(app_id,context_id) REFERENCES APPLICATION_CONTEXTS(app_id,context_id),
 FOREIGN KEY(app_id,subject) REFERENCES EMBED_MEMBERSHIPS(app_id,subject) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_personal_context
 ON APPLICATION_PARTICIPANTS(app_id,subject) WHERE context_ref IS NULL;
CREATE TABLE IF NOT EXISTS APPLICATION_CONVERSATIONS (
 conversation_id INTEGER PRIMARY KEY REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
 app_id TEXT NOT NULL, context_id TEXT NOT NULL, subject TEXT NOT NULL,
 user_id INTEGER NOT NULL REFERENCES USERS(id) ON DELETE CASCADE,
 assistant_id TEXT NOT NULL, prompt_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
 FOREIGN KEY(app_id,context_id,subject)
 REFERENCES APPLICATION_PARTICIPANTS(app_id,context_id,subject),
 FOREIGN KEY(app_id,assistant_id) REFERENCES APPLICATION_ASSISTANTS(app_id,assistant_id)
);
CREATE INDEX IF NOT EXISTS idx_application_conversation_scope
 ON APPLICATION_CONVERSATIONS(app_id,subject,context_id,assistant_id,conversation_id);
CREATE TRIGGER IF NOT EXISTS application_conversation_scope_immutable
 BEFORE UPDATE OF app_id,context_id,subject,user_id,assistant_id,prompt_id
 ON APPLICATION_CONVERSATIONS BEGIN SELECT RAISE(ABORT,'immutable application scope'); END;
CREATE TABLE IF NOT EXISTS APPLICATION_OPERATIONS (
 app_id TEXT NOT NULL, subject TEXT NOT NULL, operation_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, conversation_id INTEGER NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(app_id,subject,operation_id),
 FOREIGN KEY(app_id,subject) REFERENCES EMBED_MEMBERSHIPS(app_id,subject) ON DELETE CASCADE
);
"""


def default_statements(app):
    config = json.loads(app["config_json"])
    assistant = {"assistant_id": "default", "display_name": config["display_name"],
                 "prompt_id": config["prompt_id"], "capabilities": {"text": True}}
    return [
        ("INSERT OR IGNORE INTO APPLICATION_ASSISTANTS VALUES (?,?,?,?)",
         (app["app_id"], "default", config["prompt_id"], json.dumps(assistant))),
        ("INSERT OR IGNORE INTO APPLICATION_SETTINGS VALUES (?,?)", (app["app_id"], "default")),
    ]


def legacy_context_id(row):
    key = json.dumps([row["app_id"], row["subject"], row["external_project_id"]])
    return "legacy_" + hashlib.sha256(key.encode()).hexdigest()


INTERVIEW_BINDING_SQL = """SELECT b.*,p.context_ref,
 c.user_id AS native_user_id,c.role_id AS native_prompt_id
 FROM APPLICATION_CONVERSATIONS b
 JOIN APPLICATION_PARTICIPANTS p USING(app_id,context_id,subject)
 JOIN CONVERSATIONS c ON c.id=b.conversation_id WHERE b.conversation_id=?"""


def matches_interview_binding(binding, interview):
    """An imported interview retains its already authorized application context."""
    return (all(binding[key] == interview[key]
                for key in ("app_id", "subject", "user_id", "prompt_id"))
            and binding["assistant_id"] == "default"
            and binding["context_ref"] == interview["external_project_id"]
            and binding["native_user_id"] == interview["user_id"]
            and binding["native_prompt_id"] == interview["prompt_id"])


def legacy_statements(row):
    """Preserve each member's v1 project namespace; never merge by project name."""
    context_id = legacy_context_id(row)
    return [
        ("INSERT OR IGNORE INTO APPLICATION_CONTEXTS VALUES (?,?,NULL,1)",
         (context_id, row["app_id"])),
        ("""INSERT OR IGNORE INTO APPLICATION_PARTICIPANTS
         VALUES (?,?,?,?,1,?,NULL)""",
         (row["app_id"], context_id, row["subject"], row["external_project_id"], '{"text":true}')),
        ("""INSERT OR IGNORE INTO APPLICATION_CONVERSATIONS
         SELECT ?,p.app_id,p.context_id,p.subject,?,'default',?,?
         FROM APPLICATION_PARTICIPANTS p JOIN CONVERSATIONS c ON c.id=?
         WHERE p.app_id=? AND p.subject=? AND p.context_ref=?
         AND c.user_id=? AND c.role_id=?""",
         (row["conversation_id"], row["user_id"], row["prompt_id"], row["created_at"],
          row["conversation_id"], row["app_id"], row["subject"], row["external_project_id"],
          row["user_id"], row["prompt_id"])),
    ]


def migrate_connection(connection):
    """Synchronous migration entry point; caller owns commit/rollback."""
    connection.executescript(SCHEMA)
    connection.row_factory = sqlite3.Row
    for table in ("EMBED_TICKETS", "EMBED_FRAMES"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for sql in column_statements(table, columns):
            connection.execute(sql)
    for app in connection.execute("SELECT * FROM EMBED_APPS").fetchall():
        for sql, args in default_statements(app):
            connection.execute(sql, args)
    for row in connection.execute("SELECT * FROM EMBED_INTERVIEWS").fetchall():
        binding = connection.execute(INTERVIEW_BINDING_SQL, (row["conversation_id"],)).fetchone()
        if binding:
            if not matches_interview_binding(binding, row):
                raise ValueError("Interview conflicts with its application binding")
            continue
        prior = connection.execute("""SELECT context_id FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND subject=? AND context_ref=?""",
            (row["app_id"], row["subject"], row["external_project_id"])).fetchone()
        if prior and prior["context_id"] != legacy_context_id(row):
            raise ValueError("Legacy context alias conflicts with an application grant")
        for sql, args in legacy_statements(row):
            connection.execute(sql, args)


def column_statements(table, columns):
    """Only augment existing v1 transport tables; tolerate a fresh install."""
    if table not in {"EMBED_TICKETS", "EMBED_FRAMES"}:
        raise ValueError("Unsupported transport table")
    if not columns:
        return []
    additions = {"binding_kind": "TEXT NOT NULL DEFAULT 'embed'", "context_id": "TEXT"}
    return [f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            for name, definition in additions.items() if name not in columns]
