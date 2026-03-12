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

CREATE TABLE VOICES (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    voice_code TEXT NOT NULL,
    tts_service INTEGER NOT NULL,
    FOREIGN KEY (tts_service) REFERENCES SERVICES(id)
);

CREATE TABLE LLM (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    model TEXT NOT NULL,
    input_token_cost REAL,
    output_token_cost REAL,
    vision BOOLEAN DEFAULT FALSE
);

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
    FOREIGN KEY (role_id) REFERENCES USER_ROLES(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON USERS(google_id);

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
    geo_policy TEXT DEFAULT NULL,
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
    start_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP, chat_name TEXT, stats TEXT, last_analyzed TIMESTAMP, folder_id INTEGER
                REFERENCES CHAT_FOLDERS(id),
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

CREATE TABLE DISCOUNTS (
    code TEXT PRIMARY KEY,
    discount_value REAL,
    active BOOLEAN,
    validity_date DATE, 
    usage_count INTEGER, 
    unlimited_usage BOOLEAN DEFAULT FALSE, 
    unlimited_validity BOOLEAN DEFAULT FALSE,
    created_by_user_id INTEGER,
    FOREIGN KEY (created_by_user_id) REFERENCES USERS(id)
);

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
-- MANAGER_BRANDING (white-label customization)
-- =============================================================================
CREATE TABLE MANAGER_BRANDING (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manager_id INTEGER NOT NULL UNIQUE,
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
    FOREIGN KEY (manager_id) REFERENCES USERS(id) ON DELETE CASCADE
);

CREATE INDEX idx_manager_branding_manager ON MANAGER_BRANDING(manager_id);

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
    event_type TEXT NOT NULL CHECK(event_type IN ('drift','rabbit_hole','stuck','inconsistency','saturation','none','error','security','role_breach')),
    severity TEXT NOT NULL CHECK(severity IN ('info','nudge','redirect','alert')),
    analysis TEXT,
    hint TEXT,
    action_taken TEXT DEFAULT 'none' CHECK(action_taken IN ('hint_generated','none','error','blocked','force_locked','takeover')),
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
    FOREIGN KEY (branding_id) REFERENCES MANAGER_BRANDING(id) ON DELETE SET NULL
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

CREATE UNIQUE INDEX idx_prompt_purchases_unique ON PROMPT_PURCHASES(buyer_user_id, prompt_id);
CREATE INDEX idx_prompt_purchases_prompt ON PROMPT_PURCHASES(prompt_id);
CREATE UNIQUE INDEX idx_prompt_purchases_reference ON PROMPT_PURCHASES(payment_reference) WHERE payment_reference IS NOT NULL;

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
-- PERFORMANCE INDEXES (hot-path queries)
-- =============================================================================
CREATE UNIQUE INDEX idx_prompt_permissions_single_owner ON PROMPT_PERMISSIONS(prompt_id) WHERE permission_level = 'owner';

CREATE INDEX idx_prompt_permissions_user_prompt ON PROMPT_PERMISSIONS(user_id, prompt_id);
CREATE INDEX idx_magic_links_token ON MAGIC_LINKS(token);
CREATE INDEX idx_users_phone_number ON USERS(phone_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone_unique ON USERS(phone_number) WHERE phone_number IS NOT NULL;
CREATE INDEX idx_conversations_user_id ON CONVERSATIONS(user_id);
CREATE INDEX idx_conversations_role_id ON CONVERSATIONS(role_id);
CREATE INDEX idx_conversations_last_activity ON CONVERSATIONS(user_id, last_activity DESC);
CREATE INDEX idx_prompts_public_feed ON PROMPTS(public, is_unlisted, created_at DESC);
CREATE INDEX idx_prompts_landing_candidates ON PROMPTS(public, is_unlisted, has_landing_page);
