"""One proven phone per application subject; channel links are scoped adapters."""

import hashlib
import secrets

from integrations.embed.models import EmbedError
from integrations.embed.store import _json, _one

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_ACCOUNT_CONTACTS (
 app_id TEXT NOT NULL, subject TEXT NOT NULL, phone_number TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, verified_at INTEGER NOT NULL,
 proof_kind TEXT NOT NULL, proof_ref TEXT NOT NULL,
 PRIMARY KEY(app_id,subject), UNIQUE(app_id,phone_number),
 FOREIGN KEY(app_id,subject) REFERENCES APPLICATION_ACCOUNTS(app_id,subject) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS APPLICATION_TELEGRAM_CONTACT_PROOFS (
 link_id TEXT PRIMARY KEY REFERENCES APPLICATION_CHANNEL_LINKS(link_id) ON DELETE CASCADE,
 phone_number TEXT NOT NULL, verified_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS APPLICATION_CHANNEL_DESTINATIONS (
 link_id TEXT PRIMARY KEY REFERENCES APPLICATION_CHANNEL_LINKS(link_id) ON DELETE CASCADE,
 conversation_id INTEGER NOT NULL REFERENCES APPLICATION_CONVERSATIONS(conversation_id) ON DELETE CASCADE, operation_id TEXT NOT NULL
);
"""


async def contact_state(connection, app_id, subject):
    row = await _one(
        connection,
        "SELECT * FROM APPLICATION_ACCOUNT_CONTACTS WHERE app_id=? AND subject=?",
        (app_id, subject),
    )
    version = hashlib.sha256(_json([app_id, subject, row]).encode()).hexdigest()
    return {
        "phone_number": row["phone_number"] if row else None,
        "verified": bool(row),
        "version": version,
    }


async def check_phone(connection, app_id, subject, phone, *, replacing=False):
    from phone_verification import normalize_phone_number

    phone = normalize_phone_number(phone)
    collision = await _one(
        connection,
        "SELECT 1 FROM APPLICATION_ACCOUNT_CONTACTS WHERE app_id=? AND phone_number=? AND subject<>?",
        (app_id, phone, subject),
    )
    if collision:
        raise EmbedError("contact_unavailable", 409)
    row = await _one(
        connection,
        "SELECT subject,phone_number FROM APPLICATION_ACCOUNT_CONTACTS WHERE app_id=? AND subject=?",
        (app_id, subject),
    )
    if row and row["subject"] != subject:
        raise EmbedError("contact_unavailable", 409)
    if row and row["phone_number"] != phone and not replacing:
        raise EmbedError("phone_replacement_required", 409)
    return phone


async def activate_phone(
    connection, app_id, subject, phone, *, now, proof_kind, proof_ref, replacing=False
):
    phone = await check_phone(connection, app_id, subject, phone, replacing=replacing)
    previous = await _one(
        connection,
        "SELECT * FROM APPLICATION_ACCOUNT_CONTACTS WHERE app_id=? AND subject=?",
        (app_id, subject),
    )
    if previous and previous["phone_number"] == phone:
        return
    if previous:
        from .account_cleanup import fence_account_routes

        actor = await _one(
            connection, "SELECT user_id FROM EMBED_SUBJECTS WHERE subject=?", (subject,)
        )
        await fence_account_routes(
            connection,
            user_id=actor["user_id"],
            app_id=app_id,
            subject=subject,
            version="phone-" + str(previous["version"] + 1),
        )
    await connection.execute(
        """INSERT INTO APPLICATION_ACCOUNT_CONTACTS VALUES (?,?,?,1,?,?,?)
        ON CONFLICT(app_id,subject) DO UPDATE SET phone_number=excluded.phone_number,version=version+1,
        verified_at=excluded.verified_at,proof_kind=excluded.proof_kind,proof_ref=excluded.proof_ref""",
        (app_id, subject, phone, now, proof_kind, proof_ref),
    )
    await ensure_phone_adapters(connection, app_id, subject, phone, now)


async def ensure_phone_adapters(connection, app_id, subject, phone, now):
    """Existing disconnected adapters remain disconnected until explicitly connected."""
    receivers = await (
        await connection.execute(
            """SELECT * FROM APPLICATION_CHANNEL_RECEIVERS
        WHERE app_id=? AND enabled=1 AND channel IN ('phone','whatsapp') AND provider='twilio' """,
            (app_id,),
        )
    ).fetchall()
    for raw in receivers:
        receiver = dict(raw)
        await connection.execute(
            """INSERT OR IGNORE INTO APPLICATION_CHANNEL_LINKS
            (link_id,receiver_id,app_id,subject,provider_identity,verified_at) VALUES (?,?,?,?,?,?)""",
            (
                secrets.token_urlsafe(24),
                receiver["receiver_id"],
                app_id,
                subject,
                phone,
                now,
            ),
        )


async def migrate_contacts(connection, now):
    """Adopt only active scoped Twilio proofs; fail atomically on ambiguity."""
    rows = await (
        await connection.execute("""SELECT l.*,r.channel FROM APPLICATION_CHANNEL_LINKS l
        JOIN APPLICATION_CHANNEL_RECEIVERS r ON r.receiver_id=l.receiver_id AND r.app_id=l.app_id
        JOIN APPLICATION_ACCOUNTS a ON a.app_id=l.app_id AND a.subject=l.subject
        JOIN EMBED_MEMBERSHIPS m ON m.app_id=a.app_id AND m.subject=a.subject
        WHERE l.active=1 AND r.enabled=1 AND m.active=1 AND r.provider='twilio'
        AND r.channel IN ('phone','whatsapp') ORDER BY l.verified_at,l.link_id""")
    ).fetchall()
    by_subject, by_phone = {}, {}
    for raw in rows:
        row = dict(raw)
        from phone_verification import normalize_phone_number

        phone = normalize_phone_number(row["provider_identity"])
        subject_key, phone_key = (row["app_id"], row["subject"]), (row["app_id"], phone)
        if (
            subject_key in by_subject
            and by_subject[subject_key] != phone
            or phone_key in by_phone
            and by_phone[phone_key] != row["subject"]
        ):
            raise EmbedError("contact_migration_conflict", 409)
        by_subject[subject_key], by_phone[phone_key] = phone, row["subject"]
    for raw in rows:
        row = dict(raw)
        await activate_phone(
            connection,
            row["app_id"],
            row["subject"],
            row["provider_identity"],
            now=row["verified_at"],
            proof_kind="channel:" + row["channel"],
            proof_ref=row["link_id"],
        )
        await ensure_phone_adapters(
            connection, row["app_id"], row["subject"], row["provider_identity"], now
        )
    return len(by_subject)
