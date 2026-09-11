"""Application-owned Twilio credentials and selected Voice numbers."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_TWILIO_INTEGRATIONS (
 integration_id TEXT PRIMARY KEY,app_id TEXT NOT NULL UNIQUE REFERENCES EMBED_APPS(app_id),
 account_sid TEXT NOT NULL,auth_token_encrypted TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),version INTEGER NOT NULL DEFAULT 1,
 inventory_json TEXT NOT NULL DEFAULT '[]',validated_at TEXT,
 selected_number_id INTEGER REFERENCES TELEPHONY_NUMBERS(id),receiver_id TEXT,
 previous_voice_json TEXT,configured_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


async def initialize_twilio_schema(connection):
    await connection.execute(SCHEMA)
    columns = {row[1] for row in await (await connection.execute('PRAGMA table_info(TELEPHONY_NUMBERS)')).fetchall()}
    if columns and 'integration_id' not in columns:
        await connection.execute('ALTER TABLE TELEPHONY_NUMBERS ADD COLUMN integration_id TEXT')
