"""Application funding attribution; native reservations remain the money ledger."""

GRANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_FUNDING_GRANTS (
 grant_id TEXT PRIMARY KEY, app_id TEXT NOT NULL, context_id TEXT NOT NULL DEFAULT '',
 subject TEXT NOT NULL DEFAULT '',
 beneficiary_ref TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL CHECK(mode IN ('native','sponsored')),
 payer_user_id INTEGER, monthly_limit REAL CHECK(monthly_limit IS NULL OR monthly_limit >= 0),
 version INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(app_id,context_id,beneficiary_ref,subject),
 CHECK(subject='' OR (context_id='' AND beneficiary_ref='')),
 CHECK((mode='native' AND payer_user_id IS NULL) OR (mode='sponsored' AND payer_user_id > 0))
);
"""

GRANT_SCOPE_UPGRADE = [
    'ALTER TABLE APPLICATION_FUNDING_GRANTS RENAME TO APPLICATION_FUNDING_GRANTS_OLD_SCOPE',
    GRANTS_SCHEMA,
    '''INSERT INTO APPLICATION_FUNDING_GRANTS
       (grant_id,app_id,context_id,beneficiary_ref,mode,payer_user_id,monthly_limit,version,active)
       SELECT grant_id,app_id,context_id,beneficiary_ref,mode,payer_user_id,monthly_limit,version,active
       FROM APPLICATION_FUNDING_GRANTS_OLD_SCOPE''',
    'DROP TABLE APPLICATION_FUNDING_GRANTS_OLD_SCOPE',
]

SCHEMA = GRANTS_SCHEMA + """
CREATE TABLE IF NOT EXISTS APPLICATION_BILLING_OPERATIONS (
 operation_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, grant_version INTEGER NOT NULL,
 app_id TEXT NOT NULL, user_id INTEGER NOT NULL, payer_user_id INTEGER NOT NULL,
 mode TEXT NOT NULL, budget_month TEXT NOT NULL, monthly_limit REAL,
 scope_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS APPLICATION_BILLING_RESERVATIONS (
 reservation_id TEXT PRIMARY KEY REFERENCES BILLING_USAGE_RESERVATIONS(id) ON DELETE CASCADE,
 operation_id TEXT NOT NULL REFERENCES APPLICATION_BILLING_OPERATIONS(operation_id)
);
CREATE INDEX IF NOT EXISTS idx_application_billing_budget
 ON APPLICATION_BILLING_OPERATIONS(grant_id,budget_month);
CREATE INDEX IF NOT EXISTS idx_application_billing_operation_reservations
 ON APPLICATION_BILLING_RESERVATIONS(operation_id);
CREATE TRIGGER IF NOT EXISTS application_billing_operation_immutable
 BEFORE UPDATE ON APPLICATION_BILLING_OPERATIONS
 BEGIN SELECT RAISE(ABORT,'immutable application billing operation'); END;
CREATE TRIGGER IF NOT EXISTS application_billing_reservation_immutable
 BEFORE UPDATE ON APPLICATION_BILLING_RESERVATIONS
 BEGIN SELECT RAISE(ABORT,'immutable application billing attribution'); END;
"""


async def initialize_billing_schema(connection, *, seed_existing_native=False):
    await connection.executescript(SCHEMA)
    if seed_existing_native:
        await connection.execute("""INSERT OR IGNORE INTO APPLICATION_FUNDING_GRANTS
            (grant_id,app_id,mode) SELECT 'legacy-native:' || app_id,app_id,'native'
            FROM EMBED_APPS""")
