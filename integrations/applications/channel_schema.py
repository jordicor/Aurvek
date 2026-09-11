"""Four additive tables; native channel assignments remain independent."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_CHANNEL_RECEIVERS (
 receiver_id TEXT PRIMARY KEY,app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 channel TEXT NOT NULL,provider TEXT NOT NULL,provider_account_id TEXT NOT NULL DEFAULT '',
 receiver_key TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
 config_json TEXT NOT NULL,
 UNIQUE(channel,provider,provider_account_id,receiver_key),UNIQUE(receiver_id,app_id)
);
CREATE TABLE IF NOT EXISTS APPLICATION_CHANNEL_LINKS (
 link_id TEXT PRIMARY KEY,receiver_id TEXT NOT NULL,app_id TEXT NOT NULL,subject TEXT NOT NULL,
 provider_identity TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
 default_context_id TEXT,proof_secret_hash TEXT,verified_at INTEGER NOT NULL,
 UNIQUE(receiver_id,provider_identity,subject),UNIQUE(link_id,receiver_id),
 FOREIGN KEY(receiver_id,app_id) REFERENCES APPLICATION_CHANNEL_RECEIVERS(receiver_id,app_id),
 FOREIGN KEY(app_id,subject) REFERENCES EMBED_MEMBERSHIPS(app_id,subject) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS APPLICATION_CHANNEL_ROUTES (
 route_id TEXT PRIMARY KEY,receiver_id TEXT NOT NULL,link_id TEXT NOT NULL,
 provider_identity TEXT NOT NULL,session_key TEXT NOT NULL,conversation_id INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,active INTEGER NOT NULL DEFAULT 1,
 response_mode TEXT NOT NULL DEFAULT 'text' CHECK(response_mode IN ('text','voice')),
 last_operation_id TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,
 UNIQUE(receiver_id,provider_identity,session_key),
 FOREIGN KEY(link_id,receiver_id) REFERENCES APPLICATION_CHANNEL_LINKS(link_id,receiver_id) ON DELETE CASCADE,
 FOREIGN KEY(conversation_id) REFERENCES APPLICATION_CONVERSATIONS(conversation_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS APPLICATION_CHANNEL_CHALLENGES (
 challenge_id TEXT PRIMARY KEY,receiver_id TEXT NOT NULL REFERENCES APPLICATION_CHANNEL_RECEIVERS(receiver_id),
 provider_identity TEXT NOT NULL,session_key TEXT NOT NULL,purpose TEXT NOT NULL,
 app_id TEXT NOT NULL,subject TEXT,link_id TEXT,secret_hash TEXT,payload_json TEXT NOT NULL DEFAULT '{}',
 attempts INTEGER NOT NULL DEFAULT 0,expires_at INTEGER NOT NULL,created_at INTEGER NOT NULL,
 consumed_at INTEGER,consumed_event_id TEXT,result_route_id TEXT,
 operation_id TEXT,request_hash TEXT,
 UNIQUE(receiver_id,operation_id),
 FOREIGN KEY(app_id,subject) REFERENCES EMBED_MEMBERSHIPS(app_id,subject) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_application_channel_proofs
 ON APPLICATION_CHANNEL_CHALLENGES(receiver_id,provider_identity,purpose,expires_at);
"""


async def initialize_channel_schema(connection):
    # A borrowed connection may already contain a native job transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            await connection.execute(statement)
