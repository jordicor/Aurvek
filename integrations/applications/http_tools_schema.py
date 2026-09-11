"""Operator HTTP registrations and durable mutation receipts."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_HTTP_TOOLS (
 app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id), name TEXT NOT NULL,
 config_json TEXT NOT NULL, PRIMARY KEY(app_id,name)
);
CREATE TABLE IF NOT EXISTS APPLICATION_HTTP_OPERATIONS (
 app_id TEXT NOT NULL, subject TEXT NOT NULL, operation_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','succeeded','ambiguous')),
 result_json TEXT, created_at INTEGER NOT NULL,
 PRIMARY KEY(app_id,subject,operation_id),
 FOREIGN KEY(app_id,subject) REFERENCES EMBED_MEMBERSHIPS(app_id,subject)
);
"""
