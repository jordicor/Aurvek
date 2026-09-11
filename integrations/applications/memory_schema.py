"""Provider destinations retained independently of current application policy."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_MEMORY_ACTIVATIONS (
 conversation_id INTEGER PRIMARY KEY,
 after_message_id INTEGER NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS APPLICATION_MEMORY_DESTINATIONS (
 provider TEXT NOT NULL, conversation_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 namespace_key TEXT NOT NULL, namespace_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(provider,conversation_id,namespace_key)
);
CREATE INDEX IF NOT EXISTS idx_application_memory_destination_user
 ON APPLICATION_MEMORY_DESTINATIONS(user_id,provider);
CREATE TABLE IF NOT EXISTS APPLICATION_MEMORY_MESSAGE_DESTINATIONS (
 provider TEXT NOT NULL, message_id INTEGER NOT NULL, conversation_id INTEGER NOT NULL,
 user_id INTEGER NOT NULL, namespace_json TEXT NOT NULL,
 PRIMARY KEY(provider,message_id)
);
CREATE INDEX IF NOT EXISTS idx_application_memory_message_conversation
 ON APPLICATION_MEMORY_MESSAGE_DESTINATIONS(conversation_id,provider);
"""
