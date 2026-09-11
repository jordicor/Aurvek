from fastapi import Request

from database import get_db_connection
from log_config import logger
from middleware.security import get_client_ip


async def log_admin_action(
    admin_id: int,
    action_type: str,
    request: Request = None,
    target_user_id: int = None,
    target_resource_type: str = None,
    target_resource_id: int = None,
    details: str = None,
    *,
    connection=None,
):
    """Standalone logs are best-effort; borrowed transactions require the audit."""
    async def insert(conn):
        ip_address = None
        user_agent = None

        if request:
            ip_address = get_client_ip(request)
            user_agent = request.headers.get("User-Agent", "")[:500]

        await conn.execute(
            """
            INSERT INTO ADMIN_AUDIT_LOG
            (admin_id, action_type, target_user_id, target_resource_type,
             target_resource_id, details, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                admin_id,
                action_type,
                target_user_id,
                target_resource_type,
                target_resource_id,
                details,
                ip_address,
                user_agent,
            ),
        )
    if connection is not None:
        await insert(connection)
        return
    try:
        async with get_db_connection() as conn:
            await insert(conn)
            await conn.commit()

        logger.debug(
            "[AUDIT] Admin %s performed %s on %s:%s",
            admin_id,
            action_type,
            target_resource_type,
            target_resource_id,
        )
    except Exception as exc:
        logger.error("[AUDIT] Failed to log admin action: %s", exc)
