# database.py
import aiosqlite
from contextlib import asynccontextmanager
import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()
        
dbname = os.getenv('DATABASE')
DEFAULT_DB_TIMEOUT = float(os.getenv('DB_TIMEOUT', '5.0'))
DB_MAX_RETRIES = int(os.getenv('DB_MAX_RETRIES', '3'))
DB_RETRY_DELAY_BASE = float(os.getenv('DB_RETRY_DELAY_BASE', '0.2'))

LOCK_ERROR_MESSAGES = ("database is locked", "database table is locked")

CACHE_SIZE = 1024 * 50  # 50 MB cache

# Per-connection PRAGMAs (must be set on each new connection)
PRAGMA_STATEMENTS_RW = [
    "PRAGMA foreign_keys = ON",
    f"PRAGMA cache_size={-CACHE_SIZE}",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=1073741824",
    "PRAGMA busy_timeout=5000",
]

PRAGMA_STATEMENTS_RO = [
    f"PRAGMA cache_size={-CACHE_SIZE}",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA mmap_size=1073741824",
    "PRAGMA busy_timeout=5000",
]

_wal_initialized = False


async def ensure_wal_mode():
    """Set WAL journal mode once at startup (persistent across connections)."""
    global _wal_initialized
    if _wal_initialized:
        return
    conn = await aiosqlite.connect(f"file:db/{dbname}?mode=rw", uri=True, timeout=DEFAULT_DB_TIMEOUT)
    try:
        await conn.execute("PRAGMA journal_mode=WAL")
    finally:
        await conn.close()
    _wal_initialized = True


@asynccontextmanager
async def get_db_connection(readonly=False):
    mode = 'ro' if readonly else 'rw'
    conn = await aiosqlite.connect(
        f"file:db/{dbname}?mode={mode}",
        uri=True,
        timeout=DEFAULT_DB_TIMEOUT,
    )
    conn.row_factory = aiosqlite.Row

    # Apply per-connection PRAGMA configurations
    pragma_statements = PRAGMA_STATEMENTS_RO if readonly else PRAGMA_STATEMENTS_RW
    for statement in pragma_statements:
        await conn.execute(statement)

    try:
        yield conn
    finally:
        await conn.close()


def is_lock_error(exc: Exception) -> bool:
    """
    Detects if the exception corresponds to a SQLite lock.
    """
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(token in message for token in LOCK_ERROR_MESSAGES)


# ---------------------------------------------------------------------------
# Pack query functions
# ---------------------------------------------------------------------------

async def create_pack(conn, name, slug, description, created_by_user_id,
                      cover_image=None, is_public=False, is_paid=False,
                      price=0.00, tags=None, public_id=None,
                      landing_reg_config=None):
    """Insert a new pack and return its ID."""
    await conn.execute(
        """INSERT INTO PACKS
           (name, slug, description, cover_image, created_by_user_id,
            is_public, is_paid, price, tags, public_id, status,
            landing_reg_config)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (name, slug, description, cover_image, created_by_user_id,
         is_public, is_paid, price, tags, public_id, landing_reg_config),
    )
    await conn.commit()
    cursor = await conn.execute("SELECT last_insert_rowid()")
    row = await cursor.fetchone()
    return row[0]


async def get_pack(conn, pack_id):
    """Return a single pack row by ID (with creator username and item count)."""
    cursor = await conn.execute(
        """SELECT p.*, u.username AS created_by_username,
                  (SELECT COUNT(*) FROM PACK_ITEMS WHERE pack_id = p.id AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))) AS item_count
           FROM PACKS p
           LEFT JOIN USERS u ON p.created_by_user_id = u.id
           WHERE p.id = ?""",
        (pack_id,),
    )
    return await cursor.fetchone()


async def get_pack_by_public_id(conn, public_id):
    """Return a pack by its public_id (for landing pages). Includes creator username and item count."""
    cursor = await conn.execute(
        """SELECT p.*, u.username AS created_by_username,
                  (SELECT COUNT(*) FROM PACK_ITEMS WHERE pack_id = p.id AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))) AS item_count
           FROM PACKS p
           LEFT JOIN USERS u ON p.created_by_user_id = u.id
           WHERE p.public_id = ?""",
        (public_id,),
    )
    return await cursor.fetchone()


async def get_user_packs(conn, user_id, is_admin=False):
    """Return all packs for a user (or all packs if admin)."""
    if is_admin:
        cursor = await conn.execute(
            """SELECT p.*, u.username AS created_by_username,
                      (SELECT COUNT(*) FROM PACK_ITEMS WHERE pack_id = p.id AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))) AS item_count
               FROM PACKS p
               LEFT JOIN USERS u ON p.created_by_user_id = u.id
               ORDER BY p.created_at DESC"""
        )
    else:
        cursor = await conn.execute(
            """SELECT p.*, u.username AS created_by_username,
                      (SELECT COUNT(*) FROM PACK_ITEMS WHERE pack_id = p.id AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))) AS item_count
               FROM PACKS p
               LEFT JOIN USERS u ON p.created_by_user_id = u.id
               WHERE p.created_by_user_id = ?
               ORDER BY p.created_at DESC""",
            (user_id,),
        )
    return await cursor.fetchall()


async def update_pack(conn, pack_id, **fields):
    """Update pack fields. Only provided fields are changed."""
    if not fields:
        return
    allowed = {
        "name", "slug", "description", "cover_image", "is_public",
        "is_paid", "price", "status", "tags", "max_items",
        "has_custom_landing", "landing_reg_config", "public_id",
    }
    filtered = {k: v for k, v in fields.items() if k in allowed}
    if not filtered:
        return
    filtered["updated_at"] = "CURRENT_TIMESTAMP"
    set_clauses = []
    params = []
    for key, val in filtered.items():
        if val == "CURRENT_TIMESTAMP":
            set_clauses.append(f"{key} = CURRENT_TIMESTAMP")
        else:
            set_clauses.append(f"{key} = ?")
            params.append(val)
    params.append(pack_id)
    await conn.execute(
        f"UPDATE PACKS SET {', '.join(set_clauses)} WHERE id = ?",
        params,
    )
    await conn.commit()


async def delete_pack(conn, pack_id):
    """Delete a pack and its items (CASCADE handles PACK_ITEMS)."""
    await conn.execute("DELETE FROM PACKS WHERE id = ?", (pack_id,))
    await conn.commit()


async def get_pack_items(conn, pack_id):
    """Return items in a pack with joined prompt info, ordered by display_order."""
    cursor = await conn.execute(
        """SELECT pi.*, p.name AS prompt_name, p.description AS prompt_description,
                  p.image AS prompt_image, u.username AS prompt_owner_username
           FROM PACK_ITEMS pi
           JOIN PROMPTS p ON pi.prompt_id = p.id
           LEFT JOIN PROMPT_PERMISSIONS pp ON p.id = pp.prompt_id AND pp.permission_level = 'owner'
           LEFT JOIN USERS u ON pp.user_id = u.id
           WHERE pi.pack_id = ? AND pi.is_active = 1
             AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
           ORDER BY pi.display_order ASC""",
        (pack_id,),
    )
    return await cursor.fetchall()


async def get_public_pack_items(conn, pack_id):
    """Return items for a pack with only public-safe fields (for the explorer modal)."""
    cursor = await conn.execute(
        """SELECT pi.id, pi.prompt_id, pi.display_order,
                  p.name AS prompt_name, p.description AS prompt_description,
                  p.image AS prompt_image
           FROM PACK_ITEMS pi
           JOIN PROMPTS p ON pi.prompt_id = p.id
           WHERE pi.pack_id = ? AND pi.is_active = 1
             AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
           ORDER BY pi.display_order ASC""",
        (pack_id,),
    )
    return await cursor.fetchall()


async def add_pack_item(conn, pack_id, prompt_id):
    """Add a prompt to a pack. Returns the new item ID.
    Copies notice_period_snapshot from the prompt's current value."""
    # Get current notice period from prompt
    cursor = await conn.execute(
        "SELECT pack_notice_period_days FROM PROMPTS WHERE id = ?", (prompt_id,)
    )
    prompt_row = await cursor.fetchone()
    notice_snapshot = prompt_row["pack_notice_period_days"] if prompt_row else 0

    # Get next display_order
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(display_order), -1) + 1 FROM PACK_ITEMS WHERE pack_id = ? AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))",
        (pack_id,),
    )
    next_order = (await cursor.fetchone())[0]

    await conn.execute(
        """INSERT INTO PACK_ITEMS (pack_id, prompt_id, display_order, notice_period_snapshot)
           VALUES (?, ?, ?, ?)""",
        (pack_id, prompt_id, next_order, notice_snapshot),
    )
    await conn.commit()
    cursor = await conn.execute("SELECT last_insert_rowid()")
    row = await cursor.fetchone()
    return row[0]


async def remove_pack_item(conn, pack_id, prompt_id):
    """Remove a prompt from a pack (hard delete)."""
    await conn.execute(
        "DELETE FROM PACK_ITEMS WHERE pack_id = ? AND prompt_id = ?",
        (pack_id, prompt_id),
    )
    await conn.commit()


async def reorder_pack_items(conn, pack_id, ordered_prompt_ids):
    """Reorder items in a pack. ordered_prompt_ids is a list of prompt IDs in desired order."""
    for idx, prompt_id in enumerate(ordered_prompt_ids):
        await conn.execute(
            "UPDATE PACK_ITEMS SET display_order = ? WHERE pack_id = ? AND prompt_id = ?",
            (idx, pack_id, prompt_id),
        )
    await conn.commit()


async def get_available_prompts_for_pack(conn, pack_id, search="", limit=30):
    """Return prompts eligible for inclusion in a pack (allow_in_packs=1, not already in pack)."""
    cursor = await conn.execute(
        """SELECT p.id, p.name, p.description, p.image,
                  COALESCE(u.username, uc.username) AS owner_username,
                  p.pack_notice_period_days
           FROM PROMPTS p
           LEFT JOIN PROMPT_PERMISSIONS pp ON p.id = pp.prompt_id AND pp.permission_level = 'owner'
           LEFT JOIN USERS u ON pp.user_id = u.id
           LEFT JOIN USERS uc ON p.created_by_user_id = uc.id
           WHERE p.allow_in_packs = 1
             AND p.id NOT IN (SELECT prompt_id FROM PACK_ITEMS WHERE pack_id = ? AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now')))
             AND (p.name LIKE ? OR p.description LIKE ?)
           ORDER BY p.name ASC
           LIMIT ?""",
        (pack_id, f"%{search}%", f"%{search}%", limit),
    )
    return await cursor.fetchall()


async def grant_pack_access(conn, pack_id, user_id, granted_via="admin_grant"):
    """Grant a user access to a pack. Ignores if already granted (UNIQUE constraint)."""
    await conn.execute(
        """INSERT OR IGNORE INTO PACK_ACCESS (pack_id, user_id, granted_via)
           VALUES (?, ?, ?)""",
        (pack_id, user_id, granted_via),
    )
    await conn.commit()


async def revoke_pack_access(conn, pack_id, user_id):
    """Revoke a user's access to a pack."""
    await conn.execute(
        "DELETE FROM PACK_ACCESS WHERE pack_id = ? AND user_id = ?",
        (pack_id, user_id),
    )
    await conn.commit()


async def check_pack_access(conn, pack_id, user_id):
    """Check if a user has active access to a pack."""
    cursor = await conn.execute(
        """SELECT 1 FROM PACK_ACCESS
           WHERE pack_id = ? AND user_id = ?
             AND (expires_at IS NULL OR expires_at > datetime('now'))""",
        (pack_id, user_id),
    )
    return await cursor.fetchone() is not None


async def create_pack_purchase(conn, buyer_user_id, pack_id, amount, currency="USD",
                                payment_method="stripe", payment_reference=None, status="completed"):
    """Record a pack purchase in PACK_PURCHASES. Returns the new row ID."""
    cursor = await conn.execute(
        """INSERT INTO PACK_PURCHASES
           (buyer_user_id, pack_id, amount, currency, payment_method, payment_reference, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (buyer_user_id, pack_id, amount, currency, payment_method, payment_reference, status),
    )
    await conn.commit()
    return cursor.lastrowid


async def get_pack_purchase_by_reference(conn, payment_reference):
    """Look up a purchase by its payment_reference (Stripe session ID). For idempotency."""
    cursor = await conn.execute(
        "SELECT * FROM PACK_PURCHASES WHERE payment_reference = ?",
        (payment_reference,),
    )
    return await cursor.fetchone()


async def get_pack_purchases(conn, pack_id):
    """Return all purchases for a pack, joined with buyer user info, newest first."""
    cursor = await conn.execute(
        """SELECT pp.id, pp.buyer_user_id, u.username, u.email,
                  pp.amount, pp.currency, pp.payment_method,
                  pp.payment_reference, pp.status, pp.created_at
           FROM PACK_PURCHASES pp
           JOIN USERS u ON pp.buyer_user_id = u.id
           WHERE pp.pack_id = ?
           ORDER BY pp.created_at DESC""",
        (pack_id,),
    )
    return await cursor.fetchall()


async def count_user_packs_today(conn, user_id):
    """Count packs created by user today (for rate limiting)."""
    cursor = await conn.execute(
        """SELECT COUNT(*) FROM PACKS
           WHERE created_by_user_id = ? AND created_at >= date('now') AND created_at < date('now', '+1 day')""",
        (user_id,),
    )
    return (await cursor.fetchone())[0]


async def count_user_packs(conn, user_id):
    """Count total packs owned by a user."""
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM PACKS WHERE created_by_user_id = ?",
        (user_id,),
    )
    return (await cursor.fetchone())[0]


async def get_public_packs(conn, search="", page=1, limit=24, user_id=None, mine_only=False):
    """Return published public packs for the explorer, paginated.
    Only exposes fields safe for public consumption (no internal config)."""
    offset = (page - 1) * limit
    uid = user_id if user_id is not None else -1

    if mine_only and user_id is not None:
        base_where = "p.created_by_user_id = ?"
        base_params = (uid,)
        count_where = "created_by_user_id = ?"
        count_params = (uid,)
    else:
        base_where = "p.status = 'published' AND p.is_public = 1"
        base_params = ()
        count_where = "status = 'published' AND is_public = 1"
        count_params = ()

    order_clause = "ORDER BY p.ranking_score DESC, p.created_at DESC" if not mine_only else "ORDER BY p.created_at DESC"
    cursor = await conn.execute(
        f"""SELECT p.id, p.name, p.slug, p.description,
                  CASE WHEN p.cover_image IS NOT NULL AND p.cover_image != '' THEN 1 ELSE 0 END AS has_cover_image,
                  p.is_paid, p.price, p.tags, p.public_id, p.created_at,
                  u.username AS created_by_username,
                  (SELECT COUNT(*) FROM PACK_ITEMS WHERE pack_id = p.id AND is_active = 1 AND (disable_at IS NULL OR disable_at > datetime('now'))) AS item_count,
                  CASE WHEN p.created_by_user_id = ? THEN 1 ELSE 0 END AS is_mine,
                  p.is_public, p.status, p.has_custom_landing, p.ranking_score
           FROM PACKS p
           LEFT JOIN USERS u ON p.created_by_user_id = u.id
           WHERE {base_where}
             AND (p.name LIKE ? OR p.description LIKE ?)
           {order_clause}
           LIMIT ? OFFSET ?""",
        (uid,) + base_params + (f"%{search}%", f"%{search}%", limit, offset),
    )
    packs = await cursor.fetchall()

    cursor = await conn.execute(
        f"""SELECT COUNT(*) FROM PACKS
           WHERE {count_where}
             AND (name LIKE ? OR description LIKE ?)""",
        count_params + (f"%{search}%", f"%{search}%"),
    )
    total = (await cursor.fetchone())[0]
    return packs, total
