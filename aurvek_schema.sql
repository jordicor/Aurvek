CREATE TABLE USER_ROLES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_name TEXT UNIQUE NOT NULL
);

CREATE TABLE SERVICES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    cost_per_unit REAL NOT NULL DEFAULT 0.00,
    type TEXT NOT NULL
);

-- Fixed media defaults verified on 2026-07-10 against the official provider
-- pricing pages. Zero-priced third-party or variable-model entries deliberately
-- remain disabled until an administrator configures their real unit price.
INSERT INTO SERVICES (id, name, unit, cost_per_unit, type) VALUES
    (8, 'IMAGE-DALL-E-3-STANDARD-SQUARE', 'image', 0.04, 'Images'),
    (9, 'IMAGE-DALL-E-3-STANDARD-WIDE', 'image', 0.08, 'Images'),
    (10, 'IMAGE-GEMINI-2.5-FLASH', 'image', 0.039, 'Images'),
    (11, 'IMAGE-IDEOGRAM-V2', 'image', 0.0, 'Images'),
    (12, 'IMAGE-POE', 'image', 0.0, 'Images'),
    (13, 'IMAGE-OPENAI-GPT-IMAGE', 'image', 0.0, 'Images'),
    (14, 'VIDEO-VEO-3.1-FAST-8S-720P', 'video', 0.80, 'Video'),
    (15, 'VIDEO-VEO-3.1-STANDARD-8S-720P', 'video', 3.20, 'Video'),
    (16, 'VIDEO-VEO-3.1-LITE-8S-720P', 'video', 0.40, 'Video');

CREATE TABLE VOICES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    voice_code TEXT NOT NULL,
    tts_service INTEGER NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    deprecated INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (tts_service) REFERENCES SERVICES(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_voices_single_default
ON VOICES(is_default) WHERE is_default = 1;

CREATE TABLE VOICE_CANONICALIZATION_CONFLICTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mapping_id_snapshot INTEGER, prompt_id INTEGER, prompt_id_snapshot INTEGER NOT NULL,
    legacy_voice_code TEXT NOT NULL, legacy_voice_id INTEGER, canonical_voice_id INTEGER,
    reason TEXT NOT NULL CHECK(reason IN ('explicit_voice_mismatch','legacy_voice_unresolved','legacy_voice_ambiguous')),
    status TEXT NOT NULL DEFAULT 'needs_attention' CHECK(status IN ('needs_attention','resolved')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE SET NULL,
    FOREIGN KEY(legacy_voice_id) REFERENCES VOICES(id) ON DELETE SET NULL,
    FOREIGN KEY(canonical_voice_id) REFERENCES VOICES(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX idx_voice_canonical_conflict_active
ON VOICE_CANONICALIZATION_CONFLICTS(prompt_id_snapshot,legacy_voice_code,reason)
WHERE status='needs_attention';

CREATE TABLE VOICE_CANONICAL_ACTIVATIONS (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('global','prompt')),
    prompt_id INTEGER, current_voice_id INTEGER, target_voice_id INTEGER NOT NULL,
    audio_revision INTEGER NOT NULL CHECK(audio_revision > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready','failed','activated','canceled')),
    last_error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ready_at TEXT, activated_at TEXT,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    FOREIGN KEY(current_voice_id) REFERENCES VOICES(id) ON DELETE SET NULL,
    FOREIGN KEY(target_voice_id) REFERENCES VOICES(id) ON DELETE RESTRICT,
    CHECK((scope='global' AND prompt_id IS NULL) OR (scope='prompt' AND prompt_id IS NOT NULL))
);
CREATE UNIQUE INDEX idx_voice_canonical_activation_global
ON VOICE_CANONICAL_ACTIVATIONS(scope,audio_revision)
WHERE scope='global' AND status IN ('pending','ready');
CREATE UNIQUE INDEX idx_voice_canonical_activation_prompt
ON VOICE_CANONICAL_ACTIVATIONS(prompt_id) WHERE scope='prompt' AND status IN ('pending','ready');

CREATE TABLE LLM (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    model TEXT NOT NULL,
    input_token_cost REAL,
    output_token_cost REAL,
    vision BOOLEAN DEFAULT FALSE,
    provider_key TEXT,
    provider_model_id TEXT,
    display_name TEXT,
    description TEXT,
    context_window_tokens INTEGER,
    max_input_tokens INTEGER,
    max_output_tokens INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    sync_source TEXT,
    sync_status TEXT NOT NULL DEFAULT 'manual',
    last_synced_at TEXT,
    raw_metadata_json TEXT,
    capabilities_json TEXT,
    manual_overrides_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_provider_model
ON LLM(provider_key, provider_model_id)
WHERE provider_key IS NOT NULL AND provider_key != ''
  AND provider_model_id IS NOT NULL AND provider_model_id != '';

CREATE INDEX IF NOT EXISTS idx_llm_enabled ON LLM(enabled);
CREATE INDEX IF NOT EXISTS idx_llm_sync_status ON LLM(sync_status);

CREATE TABLE USERS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT,
    phone_number TEXT,
    phone_verified BOOLEAN DEFAULT FALSE,
    role_id INTEGER,
    is_enabled BOOLEAN, user_info TEXT, profile_picture TEXT, email TEXT,
    google_id TEXT,
    auth_provider TEXT DEFAULT 'local',
    session_version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (role_id) REFERENCES USER_ROLES(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON USERS(google_id);

CREATE TABLE PHONE_VERIFICATION_CHALLENGES (
    id TEXT PRIMARY KEY,
    actor_user_id INTEGER NOT NULL,
    phone_number TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(
        purpose IN ('create_user', 'profile_phone_change')
    ),
    request_ip TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved' CHECK(
        status IN (
            'reserved', 'pending', 'approved', 'consumed',
            'provider_error', 'superseded', 'expired', 'failed'
        )
    ),
    verification_attempts INTEGER NOT NULL DEFAULT 0 CHECK(
        verification_attempts >= 0
    ),
    provider_sid TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    approved_at INTEGER,
    consumed_at INTEGER,
    last_attempt_at INTEGER,
    FOREIGN KEY (actor_user_id) REFERENCES USERS(id) ON DELETE CASCADE
);

CREATE INDEX idx_phone_verification_actor_created
ON PHONE_VERIFICATION_CHALLENGES(actor_user_id, created_at);

CREATE INDEX idx_phone_verification_phone_created
ON PHONE_VERIFICATION_CHALLENGES(phone_number, created_at);

CREATE INDEX idx_phone_verification_ip_created
ON PHONE_VERIFICATION_CHALLENGES(request_ip, created_at);

CREATE INDEX idx_phone_verification_status_expires
ON PHONE_VERIFICATION_CHALLENGES(status, expires_at);

CREATE TABLE PROMPTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    prompt TEXT,
    voice_id INTEGER,
    description TEXT,
    image TEXT,
    created_by_user_id INTEGER,
    created_at TIMESTAMP,
    public BOOLEAN DEFAULT (0),
    public_id TEXT,
    is_unlisted INTEGER DEFAULT 0,
    is_paid BOOLEAN DEFAULT 0,
    markup_per_mtokens DECIMAL DEFAULT 0.00,
    allowed_llms TEXT DEFAULT NULL,
    forced_llm_id INTEGER DEFAULT NULL,
    forced_reasoning_json TEXT DEFAULT NULL,
    phone_llm_id INTEGER DEFAULT NULL,
    phone_reasoning_json TEXT DEFAULT NULL,
    phone_realtime_voice TEXT DEFAULT NULL,
    hide_llm_name BOOLEAN DEFAULT 0,
    landing_registration_config TEXT DEFAULT NULL,
    disable_web_search BOOLEAN DEFAULT 0,
    force_web_search BOOLEAN DEFAULT 0,
    enable_moderation BOOLEAN DEFAULT 0,
    watchdog_config TEXT DEFAULT NULL,
    allow_in_packs BOOLEAN DEFAULT 0,
    pack_notice_period_days INTEGER DEFAULT 0,
    extensions_enabled BOOLEAN DEFAULT 0,
    extensions_auto_advance BOOLEAN DEFAULT 0,
    extensions_free_selection BOOLEAN DEFAULT 1,
    has_welcome_page BOOLEAN DEFAULT 0,
    welcome_bg_image TEXT DEFAULT NULL,
    welcome_accent TEXT DEFAULT NULL,
    purchase_price DECIMAL DEFAULT NULL,
    ranking_score REAL DEFAULT 0,
    has_landing_page BOOLEAN DEFAULT 0,
    landing_trusted INTEGER NOT NULL DEFAULT 0,
    geo_policy TEXT DEFAULT NULL,
    gransabio_enabled INTEGER DEFAULT 0,
    gransabio_config TEXT DEFAULT NULL,
    FOREIGN KEY (voice_id) REFERENCES VOICES (id),
    FOREIGN KEY (created_by_user_id) REFERENCES USERS (id)
);

CREATE UNIQUE INDEX idx_prompts_public_id ON PROMPTS(public_id);

-- =============================================================================
-- PROMPT_EXTENSIONS (levels/phases for prompts)
-- =============================================================================
CREATE TABLE PROMPT_EXTENSIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    description TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0,
    is_default BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    UNIQUE(prompt_id, slug)
);

CREATE INDEX idx_prompt_extensions_prompt_id ON PROMPT_EXTENSIONS(prompt_id);
CREATE INDEX idx_prompt_extensions_order ON PROMPT_EXTENSIONS(prompt_id, display_order);

CREATE TABLE CONVERSATIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role_id INTEGER,
    llm_id INTEGER,
    locked BOOLEAN,
    locked_reason TEXT DEFAULT NULL,
    start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP, chat_name TEXT, stats TEXT, last_analyzed TIMESTAMP, folder_id INTEGER
                REFERENCES CHAT_FOLDERS(id),
    elevenlabs_session_id TEXT,
    elevenlabs_status TEXT
        CHECK(elevenlabs_status IN ('active', 'completed', 'failed')),
    active_extension_id INTEGER DEFAULT NULL,
    branched_from_id INTEGER DEFAULT NULL,
    branched_at_message_id INTEGER DEFAULT NULL,
    FOREIGN KEY (user_id) REFERENCES USERS(id),
    FOREIGN KEY (role_id) REFERENCES PROMPTS(id),
    FOREIGN KEY (llm_id) REFERENCES LLM(id),
    FOREIGN KEY (active_extension_id) REFERENCES PROMPT_EXTENSIONS(id),
    FOREIGN KEY (branched_from_id) REFERENCES CONVERSATIONS(id) ON DELETE SET NULL
);

CREATE TABLE MESSAGES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    type TEXT CHECK(type IN ('user', 'bot')) NOT NULL,
    date TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    input_tokens_used INTEGER DEFAULT 0,
    output_tokens_used INTEGER DEFAULT 0, is_bookmarked INTEGER DEFAULT (0),
    llm_id INTEGER DEFAULT NULL REFERENCES LLM(id),
    citations_json TEXT,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id),
    FOREIGN KEY (user_id) REFERENCES USERS(id)
);

CREATE TABLE MESSAGE_CHANNEL_PROVENANCE (
    message_id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL CHECK(channel IN ('whatsapp', 'telegram')),
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    external_message_id TEXT,
    content_kind TEXT NOT NULL CHECK(content_kind IN (
        'text', 'voice_note', 'audio', 'image', 'mixed', 'voice_reply'
    )),
    response_mode TEXT CHECK(response_mode IN ('text', 'voice')),
    delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK(delivery_state IN (
        'pending', 'sent', 'failed', 'received'
    )),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE
);

CREATE TABLE MESSAGE_INPUT_PROVENANCE (
    message_id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK(origin IN (
        'web.message', 'web.live_voice',
        'whatsapp.message', 'whatsapp.voice_note', 'whatsapp.audio',
        'telegram.message', 'telegram.voice_note',
        'device.message', 'phone.live_call'
    )),
    perception TEXT NOT NULL CHECK(perception IN ('text', 'transcript_only')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(
        (perception = 'text' AND origin IN (
            'web.message', 'whatsapp.message',
            'telegram.message', 'device.message'
        ))
        OR
        (perception = 'transcript_only' AND origin IN (
            'web.live_voice', 'whatsapp.voice_note', 'whatsapp.audio',
            'telegram.voice_note', 'phone.live_call'
        ))
    ),
    FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_message_channel_provenance_external
ON MESSAGE_CHANNEL_PROVENANCE(channel, external_message_id, direction)
WHERE external_message_id IS NOT NULL;

CREATE INDEX idx_message_channel_provenance_delivery
ON MESSAGE_CHANNEL_PROVENANCE(channel, delivery_state, updated_at);

CREATE TABLE MESSAGE_VOICE_NOTES (
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
);

CREATE INDEX idx_message_voice_notes_retention
ON MESSAGE_VOICE_NOTES(retention_status, created_at);

CREATE TABLE MESSAGE_TRANSCRIPTION_REVISIONS (
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
);

CREATE UNIQUE INDEX idx_message_transcription_revisions_active_job
ON MESSAGE_TRANSCRIPTION_REVISIONS(message_id)
WHERE status IN ('queued', 'transcribing', 'comparing');

CREATE INDEX idx_message_transcription_revisions_message
ON MESSAGE_TRANSCRIPTION_REVISIONS(message_id, created_at DESC, id DESC);

CREATE INDEX idx_message_transcription_revisions_status
ON MESSAGE_TRANSCRIPTION_REVISIONS(status, updated_at);

CREATE TABLE EXTERNAL_DEVICES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    device_type TEXT NOT NULL DEFAULT 'custom',
    icon_class TEXT,
    icon_asset_path TEXT,
    notes TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    token_hash TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_external_devices_user_id
ON EXTERNAL_DEVICES(user_id);

CREATE INDEX IF NOT EXISTS idx_external_devices_token_prefix
ON EXTERNAL_DEVICES(token_prefix);

CREATE INDEX IF NOT EXISTS idx_external_devices_enabled
ON EXTERNAL_DEVICES(enabled);

CREATE TABLE EXTERNAL_DEVICE_GROUPS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    icon_class TEXT,
    notes TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_external_device_groups_user_id
ON EXTERNAL_DEVICE_GROUPS(user_id);

CREATE TABLE EXTERNAL_DEVICE_GROUP_MEMBERS (
    device_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    is_primary_route_group INTEGER NOT NULL DEFAULT 0,
    routing_priority INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(device_id, group_id),
    FOREIGN KEY (device_id) REFERENCES EXTERNAL_DEVICES(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES EXTERNAL_DEVICE_GROUPS(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_external_device_group_members_group_id
ON EXTERNAL_DEVICE_GROUP_MEMBERS(group_id);

CREATE INDEX IF NOT EXISTS idx_external_device_group_members_device_id
ON EXTERNAL_DEVICE_GROUP_MEMBERS(device_id);

CREATE TABLE EXTERNAL_DEVICE_BINDINGS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('device', 'group')),
    target_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    response_mode TEXT NOT NULL DEFAULT 'text',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    UNIQUE(target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_external_device_bindings_user_id
ON EXTERNAL_DEVICE_BINDINGS(user_id);

CREATE INDEX IF NOT EXISTS idx_external_device_bindings_conversation_id
ON EXTERNAL_DEVICE_BINDINGS(conversation_id);

CREATE TABLE EXTERNAL_DEVICE_EVENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    conversation_id INTEGER,
    external_message_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('in', 'out', 'system')),
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (device_id) REFERENCES EXTERNAL_DEVICES(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_external_device_events_device_created
ON EXTERNAL_DEVICE_EVENTS(device_id, created_at);

CREATE INDEX IF NOT EXISTS idx_external_device_events_conversation_created
ON EXTERNAL_DEVICE_EVENTS(conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_external_device_events_created_id
ON EXTERNAL_DEVICE_EVENTS(created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_external_device_events_type_direction_created
ON EXTERNAL_DEVICE_EVENTS(event_type, direction, created_at, device_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_external_device_events_message_id
ON EXTERNAL_DEVICE_EVENTS(device_id, external_message_id)
WHERE external_message_id IS NOT NULL;

-- =============================================================================
-- MESSAGES_FTS (full-text search index for message content)
-- Virtual table using FTS5 for fast text search. Rowid maps to MESSAGES.id.
-- =============================================================================
-- CREATE VIRTUAL TABLE IF NOT EXISTS MESSAGES_FTS USING fts5(
--   search_text,
--   tokenize = 'unicode61 remove_diacritics 2'
-- );

-- Triggers to keep FTS index in sync with MESSAGES table:
-- trg_messages_fts_insert  - AFTER INSERT: extracts text from plain or JSON multimodal messages
-- trg_messages_fts_update  - AFTER UPDATE: removes old entry, inserts updated text
-- trg_messages_fts_delete  - AFTER DELETE: removes entry from FTS index

-- Complementary performance indices:
-- CREATE INDEX IF NOT EXISTS idx_messages_conv_id_id ON MESSAGES(conversation_id, id DESC);
-- CREATE INDEX IF NOT EXISTS idx_messages_user_bookmark ON MESSAGES(user_id, is_bookmarked);

CREATE TABLE MAGIC_LINKS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    token TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY (user_id) REFERENCES USERS(id)
);

CREATE TABLE SERVICE_USAGE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    usage_quantity REAL NOT NULL,
    cost REAL NOT NULL, 
    usage_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    FOREIGN KEY (user_id) REFERENCES USERS(id),
    FOREIGN KEY (service_id) REFERENCES SERVICES(id)
);

CREATE TABLE BILLING_USAGE_RESERVATIONS (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    billing_account_id INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('ai', 'image', 'stt', 'tts', 'video', 'phone')),
    service_id INTEGER,
    usage_quantity REAL CHECK(usage_quantity IS NULL OR usage_quantity > 0),
    amount REAL NOT NULL CHECK(amount > 0),
    settled_amount REAL CHECK(settled_amount IS NULL OR settled_amount >= 0),
    accumulated_input_tokens INTEGER NOT NULL DEFAULT 0
        CHECK(accumulated_input_tokens >= 0),
    accumulated_output_tokens INTEGER NOT NULL DEFAULT 0
        CHECK(accumulated_output_tokens >= 0),
    accumulated_components TEXT NOT NULL DEFAULT '[]',
    billing_limit_delta REAL NOT NULL DEFAULT 0
        CHECK(billing_limit_delta >= 0),
    billing_refill_count_delta INTEGER NOT NULL DEFAULT 0
        CHECK(billing_refill_count_delta >= 0),
    billing_month TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'settled', 'refunded')),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    provider_started_at TIMESTAMP,
    provider_succeeded_at TIMESTAMP,
    settled_at TIMESTAMP,
    refunded_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (billing_account_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (service_id) REFERENCES SERVICES(id)
);

CREATE INDEX idx_billing_usage_reservations_active
    ON BILLING_USAGE_RESERVATIONS(status, created_at);

CREATE INDEX idx_billing_usage_reservations_account
    ON BILLING_USAGE_RESERVATIONS(billing_account_id, created_at);

CREATE TABLE DISCOUNTS (
    code TEXT PRIMARY KEY,
    discount_value REAL,
    active BOOLEAN,
    validity_date DATE, 
    usage_count INTEGER, 
    unlimited_usage BOOLEAN DEFAULT FALSE, 
    unlimited_validity BOOLEAN DEFAULT FALSE,
    created_by_user_id INTEGER,
    scope TEXT NOT NULL DEFAULT 'marketplace'
        CHECK(scope IN ('marketplace', 'wallet')),
    wallet_grant_amount REAL
        CHECK(
            wallet_grant_amount IS NULL OR
            (wallet_grant_amount >= 5 AND wallet_grant_amount <= 500)
        ),
    FOREIGN KEY (created_by_user_id) REFERENCES USERS(id)
);

CREATE TABLE DISCOUNT_REDEMPTIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discount_code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    grant_amount REAL NOT NULL CHECK(
        grant_amount >= 5 AND grant_amount <= 500
    ),
    transaction_reference TEXT,
    redeemed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    UNIQUE(discount_code, user_id, purpose)
);

CREATE INDEX idx_discount_redemptions_user_date
    ON DISCOUNT_REDEMPTIONS(user_id, redeemed_at);
CREATE INDEX idx_discount_redemptions_code
    ON DISCOUNT_REDEMPTIONS(discount_code);

CREATE TABLE "PROMPT_PERMISSIONS" (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    prompt_id INTEGER NOT NULL,

    user_id INTEGER NOT NULL,

    permission_level TEXT NOT NULL CHECK(permission_level IN ('owner', 'edit', 'access')),

    FOREIGN KEY (prompt_id) REFERENCES PROMPTS (id),

    FOREIGN KEY (user_id) REFERENCES USERS(id),

    UNIQUE(prompt_id, user_id, permission_level)

);

CREATE TABLE PROMPT_SECTION_CONFIGS (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    prompt_id INTEGER NOT NULL,

    section VARCHAR(50) NOT NULL,

    use_default BOOLEAN NOT NULL DEFAULT 0,

    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id),

    UNIQUE (prompt_id, section)

);

CREATE INDEX idx_prompt_section ON PROMPT_SECTION_CONFIGS (prompt_id, section);

CREATE TABLE USER_ALTER_EGOS (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER NOT NULL,

    name TEXT NOT NULL,

    description TEXT,

    profile_picture TEXT,

    FOREIGN KEY (user_id) REFERENCES USERS(id)

);

CREATE TABLE USER_DETAILS (
    user_id INTEGER,
    llm_id INTEGER,
    input_tokens INTEGER DEFAULT (0),
    output_tokens INTEGER DEFAULT (0),
    tokens_spent INTEGER DEFAULT (0),
    input_token_cost DECIMAL DEFAULT (0.00),
    output_token_cost DECIMAL DEFAULT (0.00),
    total_tts_cost DECIMAL DEFAULT (0.00),
    total_stt_cost DECIMAL DEFAULT (0.00),
    total_image_cost DECIMAL DEFAULT (0.00),
    total_cost DECIMAL DEFAULT (0.00),
    balance DECIMAL DEFAULT (0.00),
    current_prompt_id INTEGER,
    all_prompts_access BOOLEAN DEFAULT (FALSE),
    allow_file_upload BOOLEAN DEFAULT (FALSE),
    allow_image_generation BOOLEAN DEFAULT (FALSE),
    external_platforms TEXT,
    created_by INTEGER,
    voice_id INTEGER,
    public_prompts_access BOOLEAN DEFAULT (FALSE),
    voice_code TEXT,
    current_alter_ego_id INTEGER,
    authentication_mode VARCHAR(20) DEFAULT 'magic_link_only',
    can_change_password BOOLEAN DEFAULT FALSE,
    user_api_keys TEXT DEFAULT NULL,
    api_key_mode VARCHAR(20) DEFAULT 'both_prefer_own',
    category_access TEXT DEFAULT NULL,
    referral_markup_per_mtokens DECIMAL DEFAULT 0.00,
    pending_earnings DECIMAL DEFAULT 0.00,
    billing_account_id INTEGER DEFAULT NULL,
    billing_limit DECIMAL DEFAULT NULL,
    billing_limit_action TEXT DEFAULT 'block',
    billing_current_month_spent DECIMAL DEFAULT 0.00,
    billing_month_reset_date TEXT DEFAULT NULL,
    billing_auto_refill_amount DECIMAL DEFAULT 10.00,
    billing_max_limit DECIMAL DEFAULT NULL,
    billing_auto_refill_count INTEGER DEFAULT 0,
    home_preferences TEXT DEFAULT NULL,
    stripe_connect_account_id TEXT,
    stripe_connect_onboarding_complete INTEGER DEFAULT 0,
    stripe_connect_charges_enabled INTEGER DEFAULT 0,
    stripe_connect_payouts_enabled INTEGER DEFAULT 0,
    web_search_enabled BOOLEAN DEFAULT 1,
    web_search_mode TEXT DEFAULT 'native',
    web_search_show_sources BOOLEAN DEFAULT 1,
    web_search_inline_citations BOOLEAN DEFAULT 0,
    domain_slots_purchased INTEGER DEFAULT 0,
    storage_quota_bytes INTEGER DEFAULT NULL CHECK(storage_quota_bytes IS NULL OR storage_quota_bytes >= 0),
    subscription_auth TEXT DEFAULT NULL,
    CONSTRAINT USER_DETAILS_PK PRIMARY KEY (user_id),
    CONSTRAINT FK_USER_DETAILS_LLM FOREIGN KEY (llm_id) REFERENCES LLM(id),
    CONSTRAINT FK_USER_DETAILS_PROMPTS_2 FOREIGN KEY (current_prompt_id) REFERENCES PROMPTS(id),
    CONSTRAINT FK_USER_DETAILS_USERS_3 FOREIGN KEY (user_id) REFERENCES USERS(id),
    CONSTRAINT USER_DETAILS_USERS_FK FOREIGN KEY (created_by) REFERENCES USERS(id),
    CONSTRAINT USER_DETAILS_VOICES_FK FOREIGN KEY (voice_id) REFERENCES VOICES(id),
    CONSTRAINT USER_DETAILS_USER_ALTER_EGOS_FK FOREIGN KEY (current_alter_ego_id) REFERENCES USER_ALTER_EGOS(id)
);

CREATE INDEX idx_user_details_category_access ON USER_DETAILS(category_access);
CREATE INDEX idx_user_details_created_by ON USER_DETAILS(created_by);
CREATE INDEX idx_user_details_billing_account ON USER_DETAILS(billing_account_id);
CREATE INDEX IF NOT EXISTS idx_user_details_stripe_connect ON USER_DETAILS(stripe_connect_account_id) WHERE stripe_connect_account_id IS NOT NULL;

CREATE TABLE FAVORITE_PROMPTS (
    user_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, prompt_id),
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE
);

CREATE INDEX idx_favorite_prompts_user ON FAVORITE_PROMPTS(user_id);
CREATE INDEX idx_favorite_prompts_prompt ON FAVORITE_PROMPTS(prompt_id);

CREATE TABLE CHAT_FOLDERS (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                color TEXT DEFAULT '#3B82F6',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES USERS(id),
                UNIQUE(name, user_id)
            );

CREATE INDEX idx_conversations_folder_id 
            ON CONVERSATIONS(folder_id)
        ;

CREATE INDEX idx_chat_folders_user_id
            ON CHAT_FOLDERS(user_id)
        ;

CREATE INDEX idx_users_email ON USERS(email);

-- =============================================================================
-- CATEGORIES (for prompt categorization)
-- =============================================================================
CREATE TABLE CATEGORIES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    icon TEXT DEFAULT 'fa-tag',
    is_age_restricted INTEGER DEFAULT 0,
    display_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_categories_display_order ON CATEGORIES(display_order);
CREATE INDEX idx_categories_is_age_restricted ON CATEGORIES(is_age_restricted);

-- =============================================================================
-- PROMPT_CATEGORIES (many-to-many relationship)
-- =============================================================================
CREATE TABLE PROMPT_CATEGORIES (
    prompt_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (prompt_id, category_id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES CATEGORIES(id) ON DELETE CASCADE
);

CREATE INDEX idx_prompt_categories_prompt_id ON PROMPT_CATEGORIES(prompt_id);
CREATE INDEX idx_prompt_categories_category_id ON PROMPT_CATEGORIES(category_id);

-- =============================================================================
-- PENDING_REGISTRATIONS (email verification flow)
-- =============================================================================
CREATE TABLE PENDING_REGISTRATIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash BLOB,
    token TEXT UNIQUE NOT NULL,
    target_role TEXT NOT NULL DEFAULT 'user',
    prompt_id INTEGER,
    pack_id INTEGER DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id),
    FOREIGN KEY (pack_id) REFERENCES PACKS(id)
);

CREATE UNIQUE INDEX idx_pending_token ON PENDING_REGISTRATIONS(token);
CREATE INDEX idx_pending_email ON PENDING_REGISTRATIONS(email);
CREATE INDEX idx_pending_expires ON PENDING_REGISTRATIONS(expires_at);

-- =============================================================================
-- PENDING_ENTITLEMENTS (claim access for existing users from landing pages)
-- =============================================================================
CREATE TABLE PENDING_ENTITLEMENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token TEXT UNIQUE NOT NULL,
    prompt_id INTEGER,
    pack_id INTEGER,
    expires_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id),
    FOREIGN KEY (pack_id) REFERENCES PACKS(id)
);

CREATE UNIQUE INDEX idx_pending_entitlements_token ON PENDING_ENTITLEMENTS(token);
CREATE INDEX idx_pending_entitlements_user ON PENDING_ENTITLEMENTS(user_id);
CREATE INDEX idx_pending_entitlements_expires ON PENDING_ENTITLEMENTS(expires_at);

-- =============================================================================
-- LANDING_PAGE_ANALYTICS (visitor tracking for landing pages)
-- =============================================================================
CREATE TABLE LANDING_PAGE_ANALYTICS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER,
    pack_id INTEGER DEFAULT NULL,
    visitor_id TEXT,
    page_path TEXT,
    referrer TEXT,
    user_agent TEXT,
    ip_hash TEXT,
    converted BOOLEAN DEFAULT 0,
    converted_user_id INTEGER,
    visit_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    FOREIGN KEY (pack_id) REFERENCES PACKS(id) ON DELETE CASCADE
);

CREATE INDEX idx_analytics_prompt ON LANDING_PAGE_ANALYTICS(prompt_id);
CREATE INDEX idx_analytics_pack ON LANDING_PAGE_ANALYTICS(pack_id);
CREATE INDEX idx_analytics_timestamp ON LANDING_PAGE_ANALYTICS(visit_timestamp);
CREATE INDEX idx_analytics_visitor ON LANDING_PAGE_ANALYTICS(visitor_id);
CREATE INDEX idx_analytics_converted ON LANDING_PAGE_ANALYTICS(converted);

-- =============================================================================
-- CREATOR_EARNINGS (paid prompts earnings tracking)
-- =============================================================================
CREATE TABLE CREATOR_EARNINGS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creator_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL,
    consumer_id INTEGER NOT NULL,
    referral_id INTEGER,
    tokens_consumed INTEGER NOT NULL,
    gross_amount DECIMAL NOT NULL,
    platform_commission DECIMAL NOT NULL,
    net_earnings DECIMAL NOT NULL,
    earning_type TEXT NOT NULL DEFAULT 'markup',
    source_ref TEXT DEFAULT NULL,
    pack_id INTEGER DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (creator_id) REFERENCES USERS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id),
    FOREIGN KEY (consumer_id) REFERENCES USERS(id),
    FOREIGN KEY (referral_id) REFERENCES USERS(id)
);

CREATE INDEX idx_creator_earnings_creator ON CREATOR_EARNINGS(creator_id);
CREATE INDEX idx_creator_earnings_prompt ON CREATOR_EARNINGS(prompt_id);
CREATE INDEX idx_creator_earnings_consumer ON CREATOR_EARNINGS(consumer_id);
CREATE INDEX idx_creator_earnings_created ON CREATOR_EARNINGS(created_at);
-- Partial unique index so a retried Stripe webhook cannot double-record a sale
-- (markup rows have NULL source_ref and are excluded from the constraint).
-- Must stay identical to migration_creator_earnings_purchase_type.py.
CREATE UNIQUE INDEX IF NOT EXISTS idx_creator_earnings_source_ref
    ON CREATOR_EARNINGS(source_ref)
    WHERE source_ref IS NOT NULL;

-- =============================================================================
-- TRANSACTIONS (balance changes history)
-- =============================================================================
CREATE TABLE TRANSACTIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    amount DECIMAL NOT NULL,
    balance_before DECIMAL NOT NULL,
    balance_after DECIMAL NOT NULL,
    description TEXT,
    reference_id TEXT,
    discount_code TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id)
);

CREATE INDEX idx_transactions_user_id ON TRANSACTIONS(user_id);
CREATE INDEX idx_transactions_created_at ON TRANSACTIONS(created_at);
CREATE INDEX idx_transactions_type ON TRANSACTIONS(type);
CREATE INDEX idx_transactions_reference_id ON TRANSACTIONS(reference_id);

-- =============================================================================
-- FINANCIAL_ARCHIVE (pseudonymized ledger of deleted users' wallet rows)
-- =============================================================================
-- Single-party TRANSACTIONS rows are snapshotted here (identity columns
-- excluded, deleted_user_ref = keyed HMAC pseudonym) when an account is
-- deleted, so financial reconciliation survives account erasure.
CREATE TABLE FINANCIAL_ARCHIVE (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deleted_user_ref TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_row_id INTEGER NOT NULL,
    amount DECIMAL,
    currency TEXT,
    payment_reference TEXT,
    occurred_at TIMESTAMP,
    row_snapshot TEXT NOT NULL,
    archived_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_financial_archive_user_ref ON FINANCIAL_ARCHIVE(deleted_user_ref);

-- =============================================================================
-- USAGE_DAILY (daily usage summaries per user)
-- =============================================================================
-- Aggregates consumption by day and type to avoid millions of individual records.
-- Types: 'ai_tokens', 'tts', 'stt', 'image', 'video', 'domain'
CREATE TABLE USAGE_DAILY (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    date DATE NOT NULL,
    type TEXT NOT NULL,

    -- Counters
    operations INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    units REAL DEFAULT 0,

    -- Cost
    total_cost DECIMAL DEFAULT 0,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (user_id) REFERENCES USERS(id),
    UNIQUE(user_id, date, type)
);

CREATE INDEX idx_usage_daily_user_date ON USAGE_DAILY(user_id, date);
CREATE INDEX idx_usage_daily_date ON USAGE_DAILY(date);
CREATE INDEX idx_usage_daily_type ON USAGE_DAILY(type);

-- =============================================================================
-- SYSTEM_CONFIG (global system configuration)
-- =============================================================================
CREATE TABLE SYSTEM_CONFIG (
    key TEXT PRIMARY KEY,
    value TEXT,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- MEMORY PROVIDERS (generic memory provider preferences and sync metadata)
-- =============================================================================
CREATE TABLE MEMORY_USER_PREFERENCES (
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    remember_across_chats INTEGER NOT NULL DEFAULT 1,
    memory_scope TEXT NOT NULL DEFAULT 'prompt',
    settings_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, provider)
);

CREATE TABLE MEMORY_PROVIDER_MESSAGE_LINKS (
    message_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_message_id TEXT NOT NULL,
    provider_event_id TEXT,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    source TEXT NOT NULL DEFAULT 'live',
    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (message_id, provider),
    FOREIGN KEY (message_id) REFERENCES MESSAGES(id)
);

CREATE TABLE MEMORY_PROVIDER_CONVERSATION_LINKS (
    provider TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (provider, conversation_id)
);

CREATE TABLE MEMORY_PROVIDER_SYNC_RUNS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    total_messages INTEGER NOT NULL DEFAULT 0,
    processed_messages INTEGER NOT NULL DEFAULT 0,
    linked_messages INTEGER NOT NULL DEFAULT 0,
    skipped_messages INTEGER NOT NULL DEFAULT 0,
    failed_messages INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    recent_errors TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE MEMORY_PROVIDER_SYNC_STATE (
    provider TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, conversation_id)
);

CREATE INDEX idx_memory_provider_links_conversation
ON MEMORY_PROVIDER_MESSAGE_LINKS(provider, conversation_id, message_id);

-- =============================================================================
-- USER_BRANDING (white-label customization)
-- =============================================================================
CREATE TABLE USER_BRANDING (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    company_name TEXT,
    logo_url TEXT,
    brand_color_primary TEXT DEFAULT '#6366f1',
    brand_color_secondary TEXT DEFAULT '#10B981',
    footer_text TEXT,
    email_signature TEXT,
    hide_platform_branding BOOLEAN DEFAULT 0,
    forced_theme TEXT,
    disable_theme_selector BOOLEAN DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE
);

CREATE INDEX idx_user_branding_user ON USER_BRANDING(user_id);

-- =============================================================================
-- ELEVENLABS_AGENTS (voice call agents)
-- =============================================================================
CREATE TABLE ELEVENLABS_AGENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL UNIQUE,
    agent_name TEXT,
    is_default BOOLEAN NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_elevenlabs_agents_default
    ON ELEVENLABS_AGENTS (is_default)
    WHERE is_default = 1;

-- =============================================================================
-- PROMPT_AGENT_MAPPING (prompt to voice agent mapping)
-- =============================================================================
CREATE TABLE PROMPT_AGENT_MAPPING (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    voice_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id),
    FOREIGN KEY (agent_id) REFERENCES ELEVENLABS_AGENTS(agent_id),
    UNIQUE(prompt_id)
);

CREATE INDEX idx_prompt_agent_mapping_agent ON PROMPT_AGENT_MAPPING(agent_id);

-- =============================================================================
-- ELEVENLABS_CALL_SESSIONS (immutable call ownership and completion marker)
-- =============================================================================
CREATE TABLE ELEVENLABS_CALL_SESSIONS (
    session_id TEXT PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active', 'completed', 'failed')),
    transcript_saved_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE
);

CREATE INDEX idx_elevenlabs_call_sessions_conversation
    ON ELEVENLABS_CALL_SESSIONS(conversation_id, created_at);

CREATE UNIQUE INDEX idx_elevenlabs_one_active_call
    ON ELEVENLABS_CALL_SESSIONS(conversation_id)
    WHERE status = 'active';

-- =============================================================================
-- PROMPT_CUSTOM_DOMAINS (custom domain configuration)
-- =============================================================================
CREATE TABLE PROMPT_CUSTOM_DOMAINS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER NOT NULL UNIQUE,
    custom_domain TEXT NOT NULL UNIQUE,
    verification_status INTEGER DEFAULT 0
        CHECK(verification_status IN (0, 1, 2, 3)),
    verification_token TEXT,
    last_verification_attempt TIMESTAMP,
    last_verification_success TIMESTAMP,
    verification_error TEXT,
    is_active BOOLEAN DEFAULT FALSE,
    activated_by_admin BOOLEAN DEFAULT FALSE,
    activated_at TIMESTAMP,
    activated_by_user_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    FOREIGN KEY (activated_by_user_id) REFERENCES USERS(id)
);

CREATE INDEX idx_custom_domain ON PROMPT_CUSTOM_DOMAINS(custom_domain);
CREATE INDEX idx_prompt_custom_domain ON PROMPT_CUSTOM_DOMAINS(prompt_id);
CREATE INDEX idx_domain_is_active ON PROMPT_CUSTOM_DOMAINS(is_active);


-- =============================================================================
-- ADMIN_AUDIT_LOG (tracks admin access to user data for transparency/compliance)
-- =============================================================================
-- This table logs when administrators access user conversations or other
-- sensitive data. Required for support, moderation, and legal compliance.
-- All admin actions on user data should be logged here.
-- =============================================================================
CREATE TABLE ADMIN_AUDIT_LOG (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    target_user_id INTEGER,
    target_resource_type TEXT,
    target_resource_id INTEGER,
    details TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (admin_id) REFERENCES USERS(id),
    FOREIGN KEY (target_user_id) REFERENCES USERS(id)
);

CREATE INDEX idx_admin_audit_admin_id ON ADMIN_AUDIT_LOG(admin_id);
CREATE INDEX idx_admin_audit_target_user ON ADMIN_AUDIT_LOG(target_user_id);
CREATE INDEX idx_admin_audit_created_at ON ADMIN_AUDIT_LOG(created_at);
CREATE INDEX idx_admin_audit_action_type ON ADMIN_AUDIT_LOG(action_type);

-- =============================================================================
-- WATCHDOG_EVENTS (audit log of watchdog evaluations)
-- =============================================================================
CREATE TABLE WATCHDOG_EVENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    prompt_id INTEGER,
    user_message_id INTEGER,
    bot_message_id INTEGER,
    event_type TEXT NOT NULL CHECK(event_type IN ('drift','rabbit_hole','stuck','inconsistency','saturation','none','error','security','role_breach','role_breach_hard','role_breach_soft')),
    severity TEXT NOT NULL CHECK(severity IN ('info','nudge','redirect','alert')),
    analysis TEXT,
    hint TEXT,
    action_taken TEXT DEFAULT 'none' CHECK(action_taken IN ('hint_generated','none','error','blocked','force_locked','takeover','takeover_locked')),
    source TEXT DEFAULT 'post' CHECK(source IN ('pre','post')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id)
);

CREATE INDEX idx_watchdog_events_conv_date ON WATCHDOG_EVENTS(conversation_id, created_at);
CREATE INDEX idx_watchdog_events_type_severity ON WATCHDOG_EVENTS(event_type, severity);
CREATE INDEX idx_watchdog_events_prompt ON WATCHDOG_EVENTS(prompt_id);

-- =============================================================================
-- WATCHDOG_STATE (pending hints per conversation for steering injection)
-- =============================================================================
CREATE TABLE WATCHDOG_STATE (
    conversation_id INTEGER PRIMARY KEY,
    prompt_id INTEGER,
    pending_hint TEXT,
    hint_severity TEXT,
    last_evaluated_message_id INTEGER NOT NULL DEFAULT 0,
    consecutive_hint_count INTEGER NOT NULL DEFAULT 0,
    pending_hint_event_type TEXT DEFAULT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id)
);

-- Composite index for cadence calculation (used by watchdog actor)
CREATE INDEX idx_messages_conv_type_id ON MESSAGES(conversation_id, type, id);

-- =============================================================================
-- PACKS (curated collections of prompts sold as products)
-- =============================================================================
CREATE TABLE PACKS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    cover_image TEXT,
    created_by_user_id INTEGER NOT NULL,
    is_public BOOLEAN DEFAULT 0,
    is_paid BOOLEAN DEFAULT 0,
    price DECIMAL DEFAULT 0.00,
    status TEXT DEFAULT 'draft',
    public_id TEXT UNIQUE,
    landing_reg_config TEXT DEFAULT NULL,
    tags TEXT DEFAULT NULL,
    max_items INTEGER DEFAULT 50,
    has_custom_landing BOOLEAN DEFAULT 0,
    rejection_reason TEXT DEFAULT NULL,
    has_welcome_page BOOLEAN DEFAULT 0,
    welcome_bg_image TEXT DEFAULT NULL,
    welcome_accent TEXT DEFAULT NULL,
    ranking_score REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by_user_id) REFERENCES USERS(id)
);

CREATE UNIQUE INDEX idx_packs_public_id ON PACKS(public_id);
CREATE INDEX idx_packs_status ON PACKS(status);
CREATE INDEX idx_packs_created_by ON PACKS(created_by_user_id);

-- =============================================================================
-- PACK_ITEMS (prompts included in a pack)
-- =============================================================================
CREATE TABLE PACK_ITEMS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL,
    display_order INTEGER DEFAULT 0,
    notice_period_snapshot INTEGER DEFAULT 0,
    disable_at TIMESTAMP DEFAULT NULL,
    is_active BOOLEAN DEFAULT 1,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (pack_id) REFERENCES PACKS(id) ON DELETE CASCADE,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id),
    UNIQUE(pack_id, prompt_id)
);

CREATE INDEX idx_pack_items_pack ON PACK_ITEMS(pack_id);
CREATE INDEX idx_pack_items_prompt ON PACK_ITEMS(prompt_id);

-- =============================================================================
-- PACK_ACCESS (user entitlements to packs)
-- =============================================================================
CREATE TABLE PACK_ACCESS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    granted_via TEXT NOT NULL,
    granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP DEFAULT NULL,
    FOREIGN KEY (pack_id) REFERENCES PACKS(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    UNIQUE(pack_id, user_id)
);

CREATE INDEX idx_pack_access_user ON PACK_ACCESS(user_id);
CREATE INDEX idx_pack_access_pack ON PACK_ACCESS(pack_id);

-- =============================================================================
-- USER_CREATOR_RELATIONSHIPS (N:N user-creator tracking)
-- =============================================================================
CREATE TABLE USER_CREATOR_RELATIONSHIPS (
    user_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    relationship_type TEXT NOT NULL CHECK (relationship_type IN (
        'registered_via', 'purchased_from', 'assigned_by'
    )),
    source_type TEXT CHECK (source_type IN ('prompt', 'pack', 'manual', 'oauth')),
    source_id INTEGER,
    is_primary BOOLEAN NOT NULL DEFAULT 0,
    first_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_interaction_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, creator_id, relationship_type),
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (creator_id) REFERENCES USERS(id) ON DELETE CASCADE
);

CREATE INDEX idx_ucr_creator ON USER_CREATOR_RELATIONSHIPS(creator_id);
CREATE UNIQUE INDEX idx_ucr_single_primary ON USER_CREATOR_RELATIONSHIPS(user_id) WHERE is_primary = 1;

-- =============================================================================
-- CREATOR_PROFILES (public creator identity and storefront configuration)
-- =============================================================================
CREATE TABLE CREATOR_PROFILES (
    user_id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    bio TEXT,
    avatar_url TEXT,
    social_links TEXT,
    custom_domain TEXT,
    domain_verification_status INTEGER NOT NULL DEFAULT 0 CHECK(domain_verification_status IN (0,1,2,3)),
    domain_verification_token TEXT,
    branding_id INTEGER,
    is_public BOOLEAN NOT NULL DEFAULT 0,
    is_verified BOOLEAN NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (branding_id) REFERENCES USER_BRANDING(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX idx_creator_profiles_slug ON CREATOR_PROFILES(slug);
CREATE UNIQUE INDEX idx_creator_profiles_domain ON CREATOR_PROFILES(custom_domain) WHERE custom_domain IS NOT NULL;

-- =============================================================================
-- USER_CAPTIVE_DOMAINS (captive users linked to assigned domain/prompt)
-- =============================================================================
CREATE TABLE USER_CAPTIVE_DOMAINS (
    user_id    INTEGER NOT NULL,
    domain_id  INTEGER NOT NULL,
    prompt_id  INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, domain_id),
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (domain_id) REFERENCES PROMPT_CUSTOM_DOMAINS(id) ON DELETE CASCADE,
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE
);

CREATE INDEX idx_captive_domain_id ON USER_CAPTIVE_DOMAINS(domain_id);
CREATE INDEX idx_captive_prompt_id ON USER_CAPTIVE_DOMAINS(prompt_id);

-- =============================================================================
-- PACK_PURCHASES (purchase tracking)
-- =============================================================================
CREATE TABLE PACK_PURCHASES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_user_id INTEGER NOT NULL,
    pack_id INTEGER NOT NULL,
    amount DECIMAL NOT NULL,
    currency TEXT DEFAULT 'USD',
    payment_method TEXT,
    payment_reference TEXT,
    status TEXT DEFAULT 'completed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (buyer_user_id) REFERENCES USERS(id),
    FOREIGN KEY (pack_id) REFERENCES PACKS(id)
);

CREATE INDEX idx_pack_purchases_buyer ON PACK_PURCHASES(buyer_user_id);
CREATE INDEX idx_pack_purchases_pack ON PACK_PURCHASES(pack_id);

-- =============================================================================
-- PROMPT_PURCHASES (individual prompt purchase tracking)
-- =============================================================================
CREATE TABLE PROMPT_PURCHASES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_user_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL,
    amount DECIMAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    payment_method TEXT,
    payment_reference TEXT,
    discount_code TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (buyer_user_id) REFERENCES USERS(id),
    FOREIGN KEY (prompt_id) REFERENCES PROMPTS(id)
);

CREATE INDEX idx_prompt_purchases_prompt ON PROMPT_PURCHASES(prompt_id);
CREATE UNIQUE INDEX idx_prompt_purchases_reference ON PROMPT_PURCHASES(payment_reference) WHERE payment_reference IS NOT NULL;

-- =============================================================================
-- ENTITLEMENTS (generic use-access ledger for prompts, packs, and future assets)
-- =============================================================================
CREATE TABLE ENTITLEMENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    asset_type TEXT NOT NULL CHECK(asset_type IN ('prompt', 'pack', 'skill_pack', 'memory_pack', 'plugin', 'workflow')),
    asset_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_ref_type TEXT,
    source_ref_id TEXT,
    starts_at DATETIME,
    expires_at DATETIME,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'pending', 'expired', 'revoked', 'refunded', 'suspended')),
    metadata_json TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by_user_id INTEGER,
    revoked_at DATETIME,
    revoked_by_user_id INTEGER,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES USERS(id) ON DELETE SET NULL,
    FOREIGN KEY (revoked_by_user_id) REFERENCES USERS(id) ON DELETE SET NULL
);

CREATE INDEX idx_entitlements_user_asset_status ON ENTITLEMENTS(user_id, asset_type, asset_id, status);
CREATE INDEX idx_entitlements_asset ON ENTITLEMENTS(asset_type, asset_id, status);
CREATE INDEX idx_entitlements_user_status ON ENTITLEMENTS(user_id, status, starts_at, expires_at);
CREATE INDEX idx_entitlements_expiry ON ENTITLEMENTS(status, expires_at);
CREATE UNIQUE INDEX idx_entitlements_source_ref_unique
    ON ENTITLEMENTS(user_id, asset_type, asset_id, source_ref_type, source_ref_id)
    WHERE source_ref_type IS NOT NULL AND source_ref_id IS NOT NULL;

-- =============================================================================
-- WELCOME_MESSAGES (welcome message content per prompt or pack)
-- =============================================================================
CREATE TABLE WELCOME_MESSAGES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('prompt', 'pack')),
    entity_id INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(entity_type, entity_id)
);

-- =============================================================================
-- WELCOME_MESSAGE_READS (per-user read/mute tracking)
-- =============================================================================
CREATE TABLE WELCOME_MESSAGE_READS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    welcome_message_id INTEGER NOT NULL REFERENCES WELCOME_MESSAGES(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES USERS(id) ON DELETE CASCADE,
    read_at TIMESTAMP DEFAULT NULL,
    muted BOOLEAN DEFAULT 0,
    UNIQUE(welcome_message_id, user_id)
);

CREATE INDEX idx_welcome_reads_user ON WELCOME_MESSAGE_READS(user_id);

-- =============================================================================
-- GENERATED MEDIA FILES (storage-quota ledger for AI-generated media on disk)
-- =============================================================================
CREATE TABLE GENERATED_MEDIA_FILES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('image', 'video', 'pdf', 'mp3', 'wav')),
    rel_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE
);

CREATE INDEX idx_generated_media_user ON GENERATED_MEDIA_FILES(user_id);
CREATE INDEX idx_generated_media_conversation ON GENERATED_MEDIA_FILES(conversation_id);

-- =============================================================================
-- PERFORMANCE INDEXES (hot-path queries)
-- =============================================================================
CREATE UNIQUE INDEX idx_prompt_permissions_single_owner ON PROMPT_PERMISSIONS(prompt_id) WHERE permission_level = 'owner';

CREATE INDEX idx_prompt_permissions_user_prompt ON PROMPT_PERMISSIONS(user_id, prompt_id);
CREATE INDEX idx_magic_links_token ON MAGIC_LINKS(token);
CREATE INDEX idx_users_phone_number ON USERS(phone_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON USERS(phone_number) WHERE phone_number IS NOT NULL;

-- =============================================================================
-- NATIVE TELEPHONE CHANNEL
-- =============================================================================
CREATE TABLE TELEPHONY_NUMBERS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, provider_number_sid TEXT NOT NULL UNIQUE,
    e164 TEXT NOT NULL UNIQUE, friendly_name TEXT, iso_country TEXT, region TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    inbound_enabled INTEGER NOT NULL DEFAULT 0 CHECK(inbound_enabled IN (0,1)),
    is_outbound_default INTEGER NOT NULL DEFAULT 0 CHECK(is_outbound_default IN (0,1)),
    voice_url TEXT, voice_method TEXT CHECK(voice_method IS NULL OR voice_method IN ('GET','POST')),
    status_callback_url TEXT,
    status_callback_method TEXT CHECK(status_callback_method IS NULL OR status_callback_method IN ('GET','POST')),
    voice_application_sid TEXT,
    trunk_sid TEXT,
    synced_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK(is_outbound_default = 0 OR enabled = 1)
);
CREATE UNIQUE INDEX idx_telephony_numbers_outbound_default ON TELEPHONY_NUMBERS(is_outbound_default) WHERE is_outbound_default = 1;
CREATE INDEX idx_telephony_numbers_enabled ON TELEPHONY_NUMBERS(enabled, inbound_enabled);

CREATE TABLE PHONE_CONTACTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL, e164 TEXT NOT NULL, timezone_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    UNIQUE(owner_user_id, e164)
);
CREATE INDEX idx_phone_contacts_owner_active ON PHONE_CONTACTS(owner_user_id, active, display_name);

CREATE TABLE PHONE_CONVERSATION_BINDINGS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL, contact_id INTEGER NOT NULL, preferred_number_id INTEGER,
    allow_inbound INTEGER NOT NULL DEFAULT 1 CHECK(allow_inbound IN (0,1)),
    allow_outbound INTEGER NOT NULL DEFAULT 1 CHECK(allow_outbound IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deactivated_at TEXT,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(contact_id) REFERENCES PHONE_CONTACTS(id) ON DELETE CASCADE,
    FOREIGN KEY(preferred_number_id) REFERENCES TELEPHONY_NUMBERS(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX idx_phone_binding_active_conversation ON PHONE_CONVERSATION_BINDINGS(conversation_id) WHERE active = 1;
CREATE UNIQUE INDEX idx_phone_binding_active_contact ON PHONE_CONVERSATION_BINDINGS(contact_id) WHERE active = 1;
CREATE INDEX idx_phone_bindings_owner ON PHONE_CONVERSATION_BINDINGS(owner_user_id, active);

CREATE TABLE PHONE_ACTIVE_ROUTES (
    e164 TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, binding_id INTEGER NOT NULL UNIQUE,
    contact_id INTEGER NOT NULL UNIQUE, conversation_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY(binding_id) REFERENCES PHONE_CONVERSATION_BINDINGS(id) ON DELETE CASCADE,
    FOREIGN KEY(contact_id) REFERENCES PHONE_CONTACTS(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE
);
CREATE INDEX idx_phone_active_routes_owner ON PHONE_ACTIVE_ROUTES(owner_user_id);

CREATE TABLE PROMPT_PHONE_SETTINGS (
    prompt_id INTEGER PRIMARY KEY, stt_locale TEXT NOT NULL DEFAULT 'auto',
    endpointing_ms INTEGER CHECK(endpointing_ms IS NULL OR endpointing_ms BETWEEN 300 AND 3000),
    interruptible INTEGER NOT NULL DEFAULT 1 CHECK(interruptible IN (0,1)),
    interrupt_sensitivity TEXT NOT NULL DEFAULT 'normal' CHECK(interrupt_sensitivity IN ('low','normal','high')),
    ignore_backchannels INTEGER NOT NULL DEFAULT 1 CHECK(ignore_backchannels IN (0,1)),
    max_duration_seconds INTEGER CHECK(max_duration_seconds IS NULL OR max_duration_seconds > 0),
    warning_milestones_json TEXT NOT NULL DEFAULT '[900,300,180,60]',
    silence_prompt_seconds INTEGER CHECK(silence_prompt_seconds IS NULL OR silence_prompt_seconds > 0),
    silence_hangup_seconds INTEGER CHECK(silence_hangup_seconds IS NULL OR silence_hangup_seconds > 0),
    ai_initiation_mode TEXT NOT NULL DEFAULT 'on_request' CHECK(ai_initiation_mode IN ('on_request','proactive','disabled')),
    inbound_greeting_mode TEXT NOT NULL DEFAULT 'inherit' CHECK(inbound_greeting_mode IN ('inherit','fixed','random')),
    outbound_greeting_mode TEXT NOT NULL DEFAULT 'inherit' CHECK(outbound_greeting_mode IN ('inherit','fixed','random')),
    recording_default INTEGER NOT NULL DEFAULT 0 CHECK(recording_default IN (0,1)),
    amd_default INTEGER NOT NULL DEFAULT 0 CHECK(amd_default IN (0,1)),
    active_audio_revision INTEGER, pending_audio_revision INTEGER,
    audio_cache_status TEXT NOT NULL DEFAULT 'not_generated' CHECK(audio_cache_status IN ('not_generated','pending','ready','failed','needs_attention')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE
);

CREATE TABLE PROMPT_PHONE_GREETINGS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL CHECK(scope IN ('global','prompt')),
    prompt_id INTEGER, direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    literal_text TEXT NOT NULL CHECK(length(trim(literal_text)) > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    is_fixed_selection INTEGER NOT NULL DEFAULT 0 CHECK(is_fixed_selection IN (0,1)),
    display_order INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    CHECK((scope='global' AND prompt_id IS NULL) OR (scope='prompt' AND prompt_id IS NOT NULL))
);
CREATE INDEX idx_phone_greetings_scope_direction ON PROMPT_PHONE_GREETINGS(scope,prompt_id,direction,enabled,display_order);
CREATE UNIQUE INDEX idx_phone_greetings_fixed_global ON PROMPT_PHONE_GREETINGS(direction,revision) WHERE scope='global' AND prompt_id IS NULL AND is_fixed_selection=1;
CREATE UNIQUE INDEX idx_phone_greetings_fixed_prompt ON PROMPT_PHONE_GREETINGS(prompt_id,direction,revision) WHERE scope='prompt' AND is_fixed_selection=1;

CREATE TABLE PHONE_TECHNICAL_NOTICE_DEFINITIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision INTEGER NOT NULL CHECK(revision > 0),
    notice_key TEXT NOT NULL CHECK(notice_key IN (
        'silence_check','silence_hangup','deadline','reconnect_notice',
        'reconnect_failed','technical_failure','balance_exhausted',
        'inbound_unavailable','unknown_caller'
    )),
    literal_text TEXT NOT NULL CHECK(length(trim(literal_text)) > 0),
    updated_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(revision,notice_key)
);
CREATE INDEX idx_phone_technical_notices_revision ON PHONE_TECHNICAL_NOTICE_DEFINITIONS(revision,notice_key);

CREATE TABLE PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    notice_key TEXT NOT NULL CHECK(notice_key IN (
        'silence_check','silence_hangup','deadline','reconnect_notice',
        'reconnect_failed','technical_failure','balance_exhausted'
    )),
    literal_text TEXT NOT NULL CHECK(length(trim(literal_text)) > 0),
    updated_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    UNIQUE(prompt_id,revision,notice_key)
);
CREATE INDEX idx_prompt_phone_technical_notices_revision ON PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS(prompt_id,revision,notice_key);

CREATE TABLE PHONE_PROMPT_AUDIO_CACHE (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cache_key TEXT NOT NULL UNIQUE, prompt_id INTEGER,
    greeting_id INTEGER, asset_kind TEXT NOT NULL CHECK(asset_kind IN ('greeting','technical_notice')),
    direction TEXT CHECK(direction IS NULL OR direction IN ('inbound','outbound')),
    literal_text TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0), voice_id INTEGER NOT NULL,
    provider_key TEXT NOT NULL, provider_voice_id TEXT NOT NULL, tts_profile_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL, source_mp3_path TEXT, pcmu_path TEXT,
    duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
    alignment_json TEXT, status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','ready','failed','retired')),
    last_error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ready_at TEXT,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    FOREIGN KEY(greeting_id) REFERENCES PROMPT_PHONE_GREETINGS(id) ON DELETE CASCADE,
    FOREIGN KEY(voice_id) REFERENCES VOICES(id) ON DELETE RESTRICT,
    CHECK((asset_kind='greeting' AND greeting_id IS NOT NULL) OR asset_kind='technical_notice')
);
CREATE INDEX idx_phone_audio_cache_activation ON PHONE_PROMPT_AUDIO_CACHE(prompt_id,revision,status);

CREATE TABLE PHONE_AUDIO_RENDER_ATTEMPTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL,
    render_fingerprint TEXT NOT NULL CHECK(length(render_fingerprint)=64 AND render_fingerprint NOT GLOB '*[^0-9a-f]*'),
    billing_user_id INTEGER NOT NULL,
    activation_id TEXT NOT NULL,
    tts_reservation_id TEXT,
    alignment_reservation_id TEXT,
    provider_state TEXT NOT NULL DEFAULT 'pending' CHECK(provider_state IN ('pending','in_flight','succeeded','failed','ambiguous')),
    alignment_state TEXT NOT NULL DEFAULT 'pending' CHECK(alignment_state IN ('pending','in_flight','succeeded','failed','ambiguous','not_required')),
    needs_attention INTEGER NOT NULL DEFAULT 0 CHECK(needs_attention IN (0,1)),
    last_error TEXT,
    provider_started_at TEXT,
    provider_succeeded_at TEXT,
    alignment_started_at TEXT,
    alignment_succeeded_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(cache_key) REFERENCES PHONE_PROMPT_AUDIO_CACHE(cache_key) ON DELETE CASCADE,
    FOREIGN KEY(activation_id) REFERENCES VOICE_CANONICAL_ACTIVATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(tts_reservation_id) REFERENCES BILLING_USAGE_RESERVATIONS(id) ON DELETE SET NULL,
    FOREIGN KEY(alignment_reservation_id) REFERENCES BILLING_USAGE_RESERVATIONS(id) ON DELETE SET NULL,
    UNIQUE(cache_key,render_fingerprint),
    CHECK((provider_state!='ambiguous' AND alignment_state!='ambiguous') OR needs_attention=1)
);
CREATE INDEX idx_phone_audio_render_attention ON PHONE_AUDIO_RENDER_ATTEMPTS(needs_attention,provider_state,alignment_state,updated_at);
CREATE INDEX idx_phone_audio_render_activation ON PHONE_AUDIO_RENDER_ATTEMPTS(activation_id,created_at);
CREATE INDEX idx_phone_audio_render_fingerprint ON PHONE_AUDIO_RENDER_ATTEMPTS(render_fingerprint,provider_state,alignment_state);

CREATE TABLE PHONE_CALL_JOBS (
    id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, conversation_id INTEGER NOT NULL,
    binding_id INTEGER NOT NULL, contact_id INTEGER NOT NULL, telephony_number_id INTEGER NOT NULL,
    scheduled_at_utc TEXT NOT NULL, timezone_name TEXT NOT NULL,
    origin TEXT NOT NULL CHECK(origin IN ('ui','assistant','api')), origin_message_id INTEGER,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','dispatching','completed','canceled','missed','conflict','needs_attention')),
    recording_override INTEGER CHECK(recording_override IS NULL OR recording_override IN (0,1)),
    amd_override INTEGER CHECK(amd_override IS NULL OR amd_override IN (0,1)),
    binding_snapshot_json TEXT NOT NULL, config_snapshot_json TEXT NOT NULL,
    lease_owner TEXT, lease_token TEXT, lease_until TEXT, dispatch_started_at TEXT, completed_at TEXT,
    last_error_code TEXT, last_error_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(binding_id) REFERENCES PHONE_CONVERSATION_BINDINGS(id) ON DELETE RESTRICT,
    FOREIGN KEY(contact_id) REFERENCES PHONE_CONTACTS(id) ON DELETE CASCADE,
    FOREIGN KEY(telephony_number_id) REFERENCES TELEPHONY_NUMBERS(id) ON DELETE RESTRICT,
    FOREIGN KEY(origin_message_id) REFERENCES MESSAGES(id) ON DELETE SET NULL,
    UNIQUE(owner_user_id,origin,idempotency_key)
);
CREATE INDEX idx_phone_call_jobs_due ON PHONE_CALL_JOBS(status,scheduled_at_utc,lease_until);
CREATE INDEX idx_phone_call_jobs_binding ON PHONE_CALL_JOBS(binding_id,status,scheduled_at_utc);

CREATE TABLE PHONE_CALLS (
    id TEXT PRIMARY KEY, job_id TEXT UNIQUE, owner_user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL, binding_id INTEGER, contact_id INTEGER NOT NULL,
    telephony_number_id INTEGER NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    from_e164 TEXT NOT NULL, to_e164 TEXT NOT NULL,
    transport TEXT CHECK(transport IS NULL OR transport='media_streams'),
    status TEXT NOT NULL DEFAULT 'created' CHECK(status IN ('created','dispatching','dispatch_unknown','queued','initiated','ringing','in_progress','completed','busy','no_answer','machine','failed','canceled','unresolved')),
    provider_call_sid TEXT UNIQUE, provider_session_id TEXT UNIQUE, provider_stream_sid TEXT UNIQUE,
    dispatch_token TEXT NOT NULL UNIQUE, provider_request_started_at TEXT, reconcile_deadline TEXT,
    answered_by TEXT, binding_snapshot_json TEXT NOT NULL, config_snapshot_json TEXT NOT NULL,
    foreground_fencing_token INTEGER NOT NULL DEFAULT 1 CHECK(foreground_fencing_token > 0),
    foreground_lease_owner TEXT, foreground_lease_until TEXT,
    reconnect_count INTEGER NOT NULL DEFAULT 0 CHECK(reconnect_count BETWEEN 0 AND 2),
    initiated_at TEXT, ringing_at TEXT, answered_at TEXT, ended_at TEXT, deadline_at TEXT,
    warning_milestones_json TEXT NOT NULL DEFAULT '[]', duration_seconds INTEGER CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
    termination_reason TEXT, estimated_cost REAL NOT NULL DEFAULT 0 CHECK(estimated_cost >= 0),
    final_cost REAL CHECK(final_cost IS NULL OR final_cost >= 0), currency TEXT NOT NULL DEFAULT 'USD',
    recording_enabled INTEGER NOT NULL DEFAULT 0 CHECK(recording_enabled IN (0,1)),
    amd_enabled INTEGER NOT NULL DEFAULT 0 CHECK(amd_enabled IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT,
    FOREIGN KEY(job_id) REFERENCES PHONE_CALL_JOBS(id) ON DELETE SET NULL,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(binding_id) REFERENCES PHONE_CONVERSATION_BINDINGS(id) ON DELETE SET NULL,
    FOREIGN KEY(contact_id) REFERENCES PHONE_CONTACTS(id) ON DELETE CASCADE,
    FOREIGN KEY(telephony_number_id) REFERENCES TELEPHONY_NUMBERS(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX idx_phone_calls_one_incompatible_conversation ON PHONE_CALLS(conversation_id)
WHERE deleted_at IS NULL AND status IN ('created','dispatching','dispatch_unknown','queued','initiated','ringing','in_progress','unresolved');
CREATE INDEX idx_phone_calls_owner_created ON PHONE_CALLS(owner_user_id,created_at DESC);
CREATE INDEX idx_phone_calls_status ON PHONE_CALLS(status,updated_at);

CREATE TABLE PHONE_HANGUP_ATTEMPTS (
    call_id TEXT PRIMARY KEY,
    provider_call_sid TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK(state IN ('in_flight', 'unresolved', 'accepted', 'confirmed')),
    attempt_count INTEGER NOT NULL CHECK(attempt_count > 0),
    attempt_token TEXT UNIQUE,
    lease_owner TEXT,
    lease_until TEXT,
    origin TEXT NOT NULL,
    reason TEXT NOT NULL,
    target_status TEXT NOT NULL
        CHECK(target_status IN ('completed', 'busy', 'no_answer', 'machine', 'failed', 'canceled')),
    last_error_code TEXT,
    last_error_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,
    FOREIGN KEY (call_id) REFERENCES PHONE_CALLS(id) ON DELETE CASCADE,
    CHECK(
        (state = 'in_flight' AND attempt_token IS NOT NULL
            AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (state IN ('unresolved', 'accepted', 'confirmed') AND attempt_token IS NULL
            AND lease_owner IS NULL AND lease_until IS NULL)
    )
);

CREATE INDEX idx_phone_hangup_attempts_state_lease
ON PHONE_HANGUP_ATTEMPTS(state, lease_until, updated_at);

CREATE TABLE PHONE_CONVERSATION_FOREGROUND (
    conversation_id INTEGER PRIMARY KEY,
    epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch >= 0),
    current_call_id TEXT, lease_owner TEXT, lease_until TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(current_call_id) REFERENCES PHONE_CALLS(id) ON DELETE SET NULL
);

CREATE TABLE PHONE_CALL_EVENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, provider_call_sid TEXT,
    provider_event_id TEXT, dedupe_key TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
    signature_valid INTEGER NOT NULL CHECK(signature_valid IN (0,1)), provider_occurred_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}', received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(call_id) REFERENCES PHONE_CALLS(id) ON DELETE CASCADE
);
CREATE INDEX idx_phone_call_events_call_received ON PHONE_CALL_EVENTS(call_id,received_at,id);
CREATE INDEX idx_phone_call_events_provider_sid ON PHONE_CALL_EVENTS(provider_call_sid,received_at);

CREATE TABLE PHONE_CALL_MESSAGE_LINKS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, message_id INTEGER NOT NULL,
    participant TEXT NOT NULL CHECK(participant IN ('caller','assistant','other_channel')),
    turn_id TEXT, origin_channel TEXT NOT NULL CHECK(origin_channel IN ('phone','web','whatsapp','telegram','device')),
    interrupted INTEGER NOT NULL DEFAULT 0 CHECK(interrupted IN (0,1)),
    played_ms INTEGER CHECK(played_ms IS NULL OR played_ms >= 0),
    confirmed_text TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'consumed' CHECK(delivery_state IN ('queued','consumed','released')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(call_id) REFERENCES PHONE_CALLS(id) ON DELETE CASCADE,
    FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
    UNIQUE(call_id,message_id)
);
CREATE UNIQUE INDEX idx_phone_call_turn_participant ON PHONE_CALL_MESSAGE_LINKS(call_id,turn_id,participant) WHERE turn_id IS NOT NULL;
CREATE INDEX idx_phone_call_message_delivery ON PHONE_CALL_MESSAGE_LINKS(call_id,delivery_state,message_id);

CREATE TABLE PHONE_MEMORY_OUTBOX (
    message_id INTEGER PRIMARY KEY,
    call_id TEXT NOT NULL,
    conversation_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    prompt_id INTEGER,
    message_text TEXT NOT NULL,
    occurred_at TEXT,
    provider TEXT CHECK(provider IS NULL OR provider IN ('atagia', 'mem0', 'none')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'retry', 'completed', 'needs_attention')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_token TEXT UNIQUE,
    lease_until TEXT,
    provider_started_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY(message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
    FOREIGN KEY(call_id) REFERENCES PHONE_CALLS(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE SET NULL,
    CHECK(
        (status = 'processing' AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status != 'processing' AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_until IS NULL)
    )
);
CREATE INDEX idx_phone_memory_outbox_claim
ON PHONE_MEMORY_OUTBOX(status, next_attempt_at, lease_until, created_at);
CREATE INDEX idx_phone_memory_outbox_attention
ON PHONE_MEMORY_OUTBOX(status, updated_at)
WHERE status = 'needs_attention';

CREATE TABLE PHONE_AI_CALL_OUTBOX (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    conversation_id INTEGER NOT NULL,
    binding_id INTEGER NOT NULL,
    prompt_id INTEGER NOT NULL,
    origin_channel TEXT NOT NULL
        CHECK(origin_channel IN ('web','whatsapp','telegram','device')),
    initiation_mode TEXT NOT NULL
        CHECK(initiation_mode IN ('on_request','proactive')),
    user_message_id INTEGER NOT NULL UNIQUE,
    assistant_message_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN (
            'pending','processing','retry','completed','canceled','needs_attention'
        )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at TEXT,
    expires_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_token TEXT UNIQUE,
    lease_until TEXT,
    call_job_id TEXT,
    error_code TEXT,
    error_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE CASCADE,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
    FOREIGN KEY(binding_id) REFERENCES PHONE_CONVERSATION_BINDINGS(id) ON DELETE CASCADE,
    FOREIGN KEY(prompt_id) REFERENCES PROMPTS(id) ON DELETE CASCADE,
    FOREIGN KEY(user_message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
    FOREIGN KEY(assistant_message_id) REFERENCES MESSAGES(id) ON DELETE CASCADE,
    FOREIGN KEY(call_job_id) REFERENCES PHONE_CALL_JOBS(id) ON DELETE CASCADE,
    CHECK(
        (status='processing' AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status!='processing' AND lease_owner IS NULL
            AND lease_token IS NULL AND lease_until IS NULL)
    ),
    CHECK(status!='completed' OR call_job_id IS NOT NULL)
);
CREATE INDEX idx_phone_ai_call_outbox_claim
ON PHONE_AI_CALL_OUTBOX(status,next_attempt_at,expires_at,created_at,id);
CREATE INDEX idx_phone_ai_call_outbox_attention
ON PHONE_AI_CALL_OUTBOX(status,updated_at)
WHERE status='needs_attention';

CREATE TABLE PHONE_BILLING_RATES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    component_type TEXT NOT NULL CHECK(component_type IN (
        'pstn','transport','stt','tts','amd','recording'
    )),
    direction TEXT NOT NULL DEFAULT '' CHECK(direction IN ('','inbound','outbound')),
    from_country TEXT NOT NULL DEFAULT '',
    to_country TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL CHECK(unit IN ('minute','character','call')),
    provider_rate_per_unit REAL NOT NULL CHECK(provider_rate_per_unit >= 0),
    customer_rate_per_unit REAL NOT NULL CHECK(customer_rate_per_unit >= 0),
    currency TEXT NOT NULL,
    service_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(service_id) REFERENCES SERVICES(id) ON DELETE RESTRICT,
    CHECK(length(provider) BETWEEN 1 AND 100),
    CHECK(length(currency) BETWEEN 3 AND 8),
    CHECK(from_country='' OR (length(from_country)=2 AND from_country=upper(from_country))),
    CHECK(to_country='' OR (length(to_country)=2 AND to_country=upper(to_country))),
    CHECK(
        (component_type='pstn' AND direction IN ('inbound','outbound')
         AND length(from_country)=2 AND length(to_country)=2)
        OR
        (component_type<>'pstn' AND direction=''
         AND from_country='' AND to_country='')
    ),
    CHECK(customer_rate_per_unit=0 OR service_id IS NOT NULL),
    UNIQUE(provider,component_type,direction,from_country,to_country)
);
CREATE INDEX idx_phone_billing_rates_lookup
ON PHONE_BILLING_RATES(
    active,provider,component_type,direction,from_country,to_country
);

CREATE TABLE PHONE_CALL_COST_COMPONENTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    billing_reservation_id TEXT,
    rate_id INTEGER,
    provider TEXT NOT NULL,
    component_type TEXT NOT NULL CHECK(component_type IN (
        'pstn','transport','stt','tts','amd','recording'
    )),
    dedupe_key TEXT NOT NULL,
    external_usage_id TEXT,
    quantity REAL NOT NULL CHECK(quantity >= 0),
    reserved_quantity REAL NOT NULL CHECK(reserved_quantity >= 0),
    unit TEXT NOT NULL CHECK(unit IN ('minute','character','call')),
    provider_rate_per_unit REAL NOT NULL CHECK(provider_rate_per_unit >= 0),
    customer_rate_per_unit REAL NOT NULL CHECK(customer_rate_per_unit >= 0),
    estimated_provider_cost REAL NOT NULL CHECK(estimated_provider_cost >= 0),
    estimated_customer_charge REAL NOT NULL CHECK(estimated_customer_charge >= 0),
    final_provider_cost REAL CHECK(final_provider_cost IS NULL OR final_provider_cost >= 0),
    final_customer_charge REAL CHECK(final_customer_charge IS NULL OR final_customer_charge >= 0),
    provider_cost REAL NOT NULL CHECK(provider_cost >= 0),
    customer_charge REAL NOT NULL CHECK(customer_charge >= 0),
    currency TEXT,
    state TEXT NOT NULL CHECK(state IN (
        'reserved','provider_started','settled','refunded','needs_attention'
    )),
    platform_absorbed INTEGER NOT NULL DEFAULT 0 CHECK(platform_absorbed IN (0,1)),
    rate_missing INTEGER NOT NULL DEFAULT 0 CHECK(rate_missing IN (0,1)),
    occurred_at TEXT,
    provider_started_at TEXT,
    provider_confirmed_at TEXT,
    settled_at TEXT,
    refunded_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(call_id) REFERENCES PHONE_CALLS(id) ON DELETE CASCADE,
    FOREIGN KEY(billing_reservation_id) REFERENCES BILLING_USAGE_RESERVATIONS(id) ON DELETE SET NULL,
    FOREIGN KEY(rate_id) REFERENCES PHONE_BILLING_RATES(id) ON DELETE SET NULL,
    CHECK(
        (rate_missing=0 AND currency IS NOT NULL)
        OR
        (rate_missing=1 AND currency IS NULL AND rate_id IS NULL
         AND billing_reservation_id IS NULL AND state='needs_attention')
    ),
    UNIQUE(call_id,dedupe_key)
);
CREATE INDEX idx_phone_call_costs_call_state ON PHONE_CALL_COST_COMPONENTS(call_id,state,component_type);
CREATE INDEX idx_phone_call_costs_reservation ON PHONE_CALL_COST_COMPONENTS(billing_reservation_id);

CREATE TABLE PHONE_RECORDINGS (
    id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL, provider_recording_sid TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','available','failed','deleting','deleted','needs_attention')),
    participant_path TEXT, assistant_path TEXT, mixed_path TEXT,
    duration_seconds INTEGER CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
    remote_deleted_at TEXT, local_deleted_at TEXT, last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(call_id) REFERENCES PHONE_CALLS(id) ON DELETE CASCADE
);
CREATE INDEX idx_phone_recordings_call ON PHONE_RECORDINGS(call_id,status);

CREATE TABLE PHONE_DATA_PURGE_JOBS (
    id TEXT PRIMARY KEY, owner_user_id INTEGER, conversation_id INTEGER,
    call_id TEXT, recording_id INTEGER,
    owner_user_id_snapshot INTEGER NOT NULL, conversation_id_snapshot INTEGER NOT NULL,
    call_id_snapshot TEXT, recording_id_snapshot INTEGER,
    purge_scope TEXT NOT NULL CHECK(purge_scope IN ('call','recording')),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','running','needs_attention','completed')),
    conversation_revision INTEGER NOT NULL CHECK(conversation_revision > 0),
    provider_call_sid_snapshot TEXT, provider_recording_sid_snapshot TEXT,
    source_snapshot_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}', next_attempt_at TEXT,
    lease_owner TEXT, lease_token TEXT, lease_until TEXT, runtime_lease_token TEXT,
    content_revision_snapshot INTEGER NOT NULL DEFAULT 0,
    source_revision INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0), last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
    FOREIGN KEY(owner_user_id) REFERENCES USERS(id) ON DELETE SET NULL,
    FOREIGN KEY(conversation_id) REFERENCES CONVERSATIONS(id) ON DELETE SET NULL,
    FOREIGN KEY(call_id) REFERENCES PHONE_CALLS(id) ON DELETE SET NULL,
    FOREIGN KEY(recording_id) REFERENCES PHONE_RECORDINGS(id) ON DELETE SET NULL,
    CHECK((purge_scope='call' AND call_id_snapshot IS NOT NULL) OR
          (purge_scope='recording' AND recording_id_snapshot IS NOT NULL))
);
CREATE UNIQUE INDEX idx_phone_purge_active_call ON PHONE_DATA_PURGE_JOBS(call_id_snapshot,purge_scope) WHERE purge_scope='call' AND status IN ('scheduled','running','needs_attention');
CREATE UNIQUE INDEX idx_phone_purge_active_recording ON PHONE_DATA_PURGE_JOBS(recording_id_snapshot,purge_scope) WHERE purge_scope='recording' AND status IN ('scheduled','running','needs_attention');
CREATE INDEX idx_phone_purge_jobs_claim ON PHONE_DATA_PURGE_JOBS(status,lease_until,created_at);

-- Durable telephone data deletion.  Tombstones deliberately keep only provider
-- correlation identifiers after the mutable call row has been purged so late
-- signed callbacks cannot recreate deleted data.
CREATE INDEX idx_phone_purge_jobs_ready
ON PHONE_DATA_PURGE_JOBS(status,next_attempt_at,lease_until,created_at);

CREATE TABLE PHONE_CALL_TOMBSTONES (
    call_id TEXT PRIMARY KEY,
    owner_user_id_snapshot INTEGER NOT NULL,
    conversation_id_snapshot INTEGER NOT NULL,
    dispatch_token TEXT NOT NULL UNIQUE,
    provider_call_sid TEXT UNIQUE,
    purge_job_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (purge_job_id) REFERENCES PHONE_DATA_PURGE_JOBS(id) ON DELETE RESTRICT
);
CREATE INDEX idx_phone_call_tombstones_conversation
ON PHONE_CALL_TOMBSTONES(conversation_id_snapshot,deleted_at);

CREATE TABLE PHONE_RECORDING_TOMBSTONES (
    call_id_snapshot TEXT PRIMARY KEY,
    owner_user_id_snapshot INTEGER NOT NULL,
    conversation_id_snapshot INTEGER NOT NULL,
    purge_job_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (purge_job_id) REFERENCES PHONE_DATA_PURGE_JOBS(id) ON DELETE RESTRICT
);

CREATE TABLE PHONE_CONVERSATION_DATA_REVISIONS (
    conversation_id_snapshot INTEGER PRIMARY KEY,
    owner_user_id_snapshot INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
    content_revision INTEGER NOT NULL DEFAULT 0 CHECK(content_revision >= 0),
    memory_state TEXT NOT NULL DEFAULT 'ready'
        CHECK(memory_state IN ('ready','rebuilding','needs_attention')),
    memory_blocked INTEGER NOT NULL DEFAULT 0 CHECK(memory_blocked IN (0,1)),
    active_job_id TEXT,
    lease_owner TEXT,
    lease_token TEXT UNIQUE,
    lease_until TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY (active_job_id) REFERENCES PHONE_DATA_PURGE_JOBS(id) ON DELETE SET NULL,
    CHECK(
        (memory_state='ready' AND memory_blocked=0 AND active_job_id IS NULL
            AND lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL)
        OR
        (memory_state IN ('rebuilding','needs_attention') AND memory_blocked=1)
    )
);
CREATE INDEX idx_phone_conversation_data_state
ON PHONE_CONVERSATION_DATA_REVISIONS(memory_state,lease_until,updated_at);

CREATE TABLE PHONE_DATA_PURGE_RUNTIME (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    worker_id TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    lease_until TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE PHONE_MEMORY_OPERATION_LEASES (
    id TEXT PRIMARY KEY,
    conversation_id_snapshot INTEGER NOT NULL,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    content_revision INTEGER NOT NULL CHECK(content_revision >= 0),
    provider_started INTEGER NOT NULL DEFAULT 0 CHECK(provider_started IN (0,1)),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','completed','needs_attention')),
    lease_until TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE INDEX idx_phone_memory_operation_leases_active
ON PHONE_MEMORY_OPERATION_LEASES(conversation_id_snapshot,status,lease_until,provider_started);

CREATE TABLE PHONE_PURGED_MESSAGE_TOMBSTONES (
    message_id INTEGER PRIMARY KEY,
    conversation_id_snapshot INTEGER NOT NULL,
    purge_job_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(purge_job_id) REFERENCES PHONE_DATA_PURGE_JOBS(id) ON DELETE RESTRICT
);
CREATE INDEX idx_phone_purged_messages_conversation
ON PHONE_PURGED_MESSAGE_TOMBSTONES(conversation_id_snapshot,deleted_at);

CREATE TRIGGER trg_phone_content_revision_message_insert
AFTER INSERT ON MESSAGES BEGIN
    INSERT INTO PHONE_CONVERSATION_DATA_REVISIONS(
        conversation_id_snapshot,owner_user_id_snapshot,content_revision
    ) VALUES(
        NEW.conversation_id,
        COALESCE(NEW.user_id,(SELECT user_id FROM CONVERSATIONS WHERE id=NEW.conversation_id),0),1
    ) ON CONFLICT(conversation_id_snapshot) DO UPDATE SET
        content_revision=content_revision+1,updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER trg_phone_content_revision_message_update
AFTER UPDATE ON MESSAGES BEGIN
    UPDATE PHONE_CONVERSATION_DATA_REVISIONS
    SET content_revision=content_revision+1,updated_at=CURRENT_TIMESTAMP
    WHERE conversation_id_snapshot=OLD.conversation_id;
    INSERT INTO PHONE_CONVERSATION_DATA_REVISIONS(
        conversation_id_snapshot,owner_user_id_snapshot,content_revision
    ) SELECT NEW.conversation_id,
        COALESCE(NEW.user_id,(SELECT user_id FROM CONVERSATIONS WHERE id=NEW.conversation_id),0),1
    WHERE NEW.conversation_id<>OLD.conversation_id
    ON CONFLICT(conversation_id_snapshot) DO UPDATE SET
        content_revision=content_revision+1,updated_at=CURRENT_TIMESTAMP;
END;
CREATE TRIGGER trg_phone_content_revision_message_delete
AFTER DELETE ON MESSAGES BEGIN
    UPDATE PHONE_CONVERSATION_DATA_REVISIONS
    SET content_revision=content_revision+1,updated_at=CURRENT_TIMESTAMP
    WHERE conversation_id_snapshot=OLD.conversation_id;
END;
CREATE TRIGGER trg_phone_block_purged_watchdog_event_insert
BEFORE INSERT ON WATCHDOG_EVENTS
WHEN EXISTS(SELECT 1 FROM PHONE_PURGED_MESSAGE_TOMBSTONES
            WHERE message_id=NEW.user_message_id OR message_id=NEW.bot_message_id)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER trg_phone_block_purged_watchdog_event_update
BEFORE UPDATE OF user_message_id,bot_message_id ON WATCHDOG_EVENTS
WHEN EXISTS(SELECT 1 FROM PHONE_PURGED_MESSAGE_TOMBSTONES
            WHERE message_id=NEW.user_message_id OR message_id=NEW.bot_message_id)
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER trg_phone_clear_purged_watchdog_state_insert
AFTER INSERT ON WATCHDOG_STATE
WHEN EXISTS(SELECT 1 FROM PHONE_PURGED_MESSAGE_TOMBSTONES
            WHERE message_id=NEW.last_evaluated_message_id)
BEGIN DELETE FROM WATCHDOG_STATE WHERE conversation_id=NEW.conversation_id; END;
CREATE TRIGGER trg_phone_clear_purged_watchdog_state_update
AFTER UPDATE OF last_evaluated_message_id ON WATCHDOG_STATE
WHEN EXISTS(SELECT 1 FROM PHONE_PURGED_MESSAGE_TOMBSTONES
            WHERE message_id=NEW.last_evaluated_message_id)
BEGIN DELETE FROM WATCHDOG_STATE WHERE conversation_id=NEW.conversation_id; END;

INSERT OR IGNORE INTO SYSTEM_CONFIG(key, value, description) VALUES
('telephony_enabled', '0', 'Enable Aurvek native telephone calls'),
('telephony_transport', 'media_streams', 'Selected Twilio voice transport'),
('telephony_stt_provider', 'elevenlabs', 'Streaming speech-to-text provider'),
('telephony_stt_model', 'scribe_v2_realtime', 'Streaming speech-to-text model'),
('telephony_stt_language', 'multi', 'Default automatic speech language mode'),
('telephony_endpointing_ms', '700', 'Default STT end-of-speech pause in milliseconds'),
('telephony_barge_in_confirmation_ms', '350', 'Base speech confirmation before barge-in'),
('telephony_max_call_seconds', '14400', 'Administrative maximum call duration'),
('telephony_allowed_countries', '["US","ES"]', 'Allowed outbound destination countries'),
('telephony_recording_default', '0', 'Default call recording mode'),
('telephony_amd_default', '0', 'Default answering-machine detection mode'),
('telephony_reconnect_attempts', '2', 'Maximum voice transport reconnections'),
('telephony_silence_check_seconds', '60', 'Silence interval before checking presence'),
('telephony_silence_hangup_seconds', '60', 'Second silence interval before hangup'),
('telephony_scheduler_jitter_seconds', '10', 'Scheduler polling and network jitter allowance');
INSERT OR IGNORE INTO SYSTEM_CONFIG(key, value, description) VALUES
('telephony_max_concurrent_dispatches', '10', 'Maximum simultaneous outbound call dispatches');
INSERT OR IGNORE INTO SYSTEM_CONFIG(key, value, description) VALUES
('whatsapp_retain_voice_notes', '0', 'Retain original WhatsApp voice-note audio for future playback and processing'),
('telegram_retain_voice_notes', '0', 'Retain original Telegram voice-note audio for future playback and processing');
CREATE INDEX idx_conversations_user_id ON CONVERSATIONS(user_id);
CREATE INDEX idx_conversations_role_id ON CONVERSATIONS(role_id);
CREATE INDEX idx_conversations_last_activity ON CONVERSATIONS(user_id, last_activity DESC);
CREATE INDEX idx_prompts_public_feed ON PROMPTS(public, is_unlisted, created_at DESC);
CREATE INDEX idx_prompts_landing_candidates ON PROMPTS(public, is_unlisted, has_landing_page);
