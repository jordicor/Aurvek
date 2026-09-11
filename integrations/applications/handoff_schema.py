"""Separate handoff notes and explicit future entry preferences."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_HANDOFFS (
 handoff_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, subject TEXT NOT NULL,
 context_id TEXT NOT NULL, operation_id TEXT NOT NULL, source_conversation_id INTEGER NOT NULL,
 conversation_id INTEGER NOT NULL, source_assistant_id TEXT NOT NULL, assistant_id TEXT NOT NULL,
 note TEXT, note_origin TEXT NOT NULL, after_message_id INTEGER NOT NULL DEFAULT 0,
 frame_instance_id TEXT, created_at INTEGER NOT NULL,
 UNIQUE(app_id,subject,operation_id)
);
CREATE INDEX IF NOT EXISTS idx_application_handoff_destination
 ON APPLICATION_HANDOFFS(app_id,subject,conversation_id,created_at);
CREATE TABLE IF NOT EXISTS APPLICATION_ENTRY_PREFERENCES (
 app_id TEXT NOT NULL, subject TEXT NOT NULL, context_id TEXT NOT NULL,
 channel TEXT NOT NULL, assistant_id TEXT NOT NULL,
 PRIMARY KEY(app_id,subject,context_id,channel),
 FOREIGN KEY(app_id,context_id,subject)
 REFERENCES APPLICATION_PARTICIPANTS(app_id,context_id,subject) ON DELETE CASCADE
);
"""
