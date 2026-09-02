from database import get_db_connection


async def ensure_integration_schema() -> None:
    """Create lightweight integration tables that predate formal migrations."""
    async with get_db_connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS WHATSAPP_PROCESSED_MESSAGES (
                message_sid TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS WHATSAPP_LOG (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                phone_number TEXT,
                direction TEXT CHECK(direction IN ('in', 'out')),
                message_type TEXT CHECK(message_type IN ('text', 'audio', 'image', 'error', 'system')),
                response_mode TEXT CHECK(response_mode IN ('text', 'voice')),
                FOREIGN KEY (user_id) REFERENCES USERS(id)
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_whatsapp_log_user_timestamp
            ON WHATSAPP_LOG(user_id, timestamp)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS TELEGRAM_PROCESSED_UPDATES (
                update_id INTEGER PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS TELEGRAM_LOG (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER,
                chat_id INTEGER,
                direction TEXT CHECK(direction IN ('in', 'out')),
                message_type TEXT CHECK(message_type IN ('text', 'audio', 'image', 'contact', 'error', 'system')),
                response_mode TEXT CHECK(response_mode IN ('text', 'voice'))
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_telegram_log_timestamp
            ON TELEGRAM_LOG(timestamp)
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS MESSAGE_CHANNEL_PROVENANCE (
                message_id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL CHECK(channel IN ('whatsapp', 'telegram')),
                direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
                external_message_id TEXT,
                content_kind TEXT NOT NULL CHECK(content_kind IN (
                    'text', 'voice_note', 'audio', 'image', 'mixed', 'voice_reply'
                )),
                response_mode TEXT CHECK(response_mode IN ('text', 'voice')),
                delivery_state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(delivery_state IN ('pending', 'sent', 'failed', 'received')),
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS MESSAGE_VOICE_NOTES (
                message_id INTEGER PRIMARY KEY,
                audio_attachment_ref TEXT UNIQUE,
                original_transcript TEXT NOT NULL,
                active_transcript TEXT NOT NULL,
                initial_stt_provider TEXT,
                initial_stt_model TEXT,
                duration_seconds REAL CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
                retention_status TEXT NOT NULL CHECK(retention_status IN (
                    'stored', 'disabled', 'quota_skipped', 'failed'
                )),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
                FOREIGN KEY(audio_attachment_ref) REFERENCES FILE_ATTACHMENTS(public_id)
                    ON DELETE SET NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS MESSAGE_TRANSCRIPTION_REVISIONS (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                requested_by_user_id INTEGER,
                old_transcript TEXT NOT NULL,
                new_transcript TEXT,
                stt_provider TEXT,
                stt_model TEXT,
                comparison_llm_id INTEGER,
                comparison_machine TEXT,
                comparison_model TEXT,
                comparison_display_name TEXT,
                comparison_input_token_cost REAL CHECK(
                    comparison_input_token_cost IS NULL OR comparison_input_token_cost >= 0
                ),
                comparison_output_token_cost REAL CHECK(
                    comparison_output_token_cost IS NULL OR comparison_output_token_cost >= 0
                ),
                verdict TEXT CHECK(verdict IN ('better', 'equal', 'worse', 'uncertain')),
                confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                rationale TEXT,
                comparison_json TEXT,
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'transcribing', 'comparing', 'ready', 'accepted',
                    'rejected', 'failed', 'stale'
                )),
                error_message TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TIMESTAMP,
                FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
                FOREIGN KEY(requested_by_user_id) REFERENCES USERS(id) ON DELETE SET NULL,
                FOREIGN KEY(comparison_llm_id) REFERENCES LLM(id) ON DELETE SET NULL
            )
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_message_channel_provenance_external
            ON MESSAGE_CHANNEL_PROVENANCE(channel, external_message_id, direction)
            WHERE external_message_id IS NOT NULL
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_message_transcription_revisions_active_job
            ON MESSAGE_TRANSCRIPTION_REVISIONS(message_id)
            WHERE status IN ('queued', 'transcribing', 'comparing')
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_voice_notes_retention
            ON MESSAGE_VOICE_NOTES(retention_status, created_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_transcription_revisions_message
            ON MESSAGE_TRANSCRIPTION_REVISIONS(message_id, created_at DESC, id DESC)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_channel_provenance_delivery
            ON MESSAGE_CHANNEL_PROVENANCE(channel, delivery_state, updated_at)
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_message_transcription_revisions_status
            ON MESSAGE_TRANSCRIPTION_REVISIONS(status, updated_at)
            """
        )
        await conn.commit()
