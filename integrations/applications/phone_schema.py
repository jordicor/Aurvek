"""Small additive extension of native phone bindings."""


def phone_binding_statements(columns):
    if not columns:
        return []
    statements = []
    if 'application_channel_json' not in columns:
        statements.append('ALTER TABLE PHONE_CONVERSATION_BINDINGS ADD COLUMN application_channel_json TEXT')
    statements.extend([
        'DROP INDEX IF EXISTS idx_phone_binding_active_contact',
        '''CREATE UNIQUE INDEX idx_phone_binding_active_contact
           ON PHONE_CONVERSATION_BINDINGS(contact_id)
           WHERE active=1 AND application_channel_json IS NULL''',
    ])
    return statements
