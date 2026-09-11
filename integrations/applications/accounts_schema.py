"""Additive account bindings. No migration provisions or admits a member."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_ACCOUNT_POLICIES (
 app_id TEXT PRIMARY KEY REFERENCES EMBED_APPS(app_id), config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS APPLICATION_ACCOUNTS (
 app_id TEXT NOT NULL, external_user_id TEXT NOT NULL, subject TEXT NOT NULL,
 created_here INTEGER NOT NULL DEFAULT 0, display_name TEXT,
 preferences_json TEXT NOT NULL DEFAULT '{}', pending_phone TEXT, pending_email TEXT,
 version INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 PRIMARY KEY(app_id,external_user_id), UNIQUE(app_id,subject),
 FOREIGN KEY(app_id,subject) REFERENCES EMBED_MEMBERSHIPS(app_id,subject) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS APPLICATION_ACCOUNT_OPERATIONS (
 app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id), external_user_id TEXT NOT NULL,
 operation_id TEXT NOT NULL, request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(app_id,external_user_id,operation_id),
 FOREIGN KEY(app_id,external_user_id) REFERENCES APPLICATION_ACCOUNTS(app_id,external_user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS APPLICATION_PHONE_CHALLENGES (
 challenge_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, external_user_id TEXT NOT NULL,
 initial_contact INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(app_id,external_user_id) REFERENCES APPLICATION_ACCOUNTS(app_id,external_user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS APPLICATION_ACCOUNT_CLEANUP (
 cleanup_key TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES USERS(id) ON DELETE CASCADE,
 app_id TEXT, subject TEXT, payload_json TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS application_account_binding_immutable
 BEFORE UPDATE OF app_id,external_user_id,subject,created_here ON APPLICATION_ACCOUNTS
 BEGIN SELECT RAISE(ABORT,'immutable application account binding'); END;
"""
