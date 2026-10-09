"""Selection of an account's native chat identity."""


async def set_current_alter_ego(conn, user_id: int, alter_ego_id: int | None) -> bool:
    """Update only an owned identity, within the caller's transaction."""
    if alter_ego_id is not None and (
        type(alter_ego_id) is not int or not 0 < alter_ego_id <= 9223372036854775807
    ):
        return False
    cursor = await conn.execute(
        """UPDATE USER_DETAILS SET current_alter_ego_id = ?
           WHERE user_id = ? AND (? IS NULL OR EXISTS (
               SELECT 1 FROM USER_ALTER_EGOS WHERE id = ? AND user_id = ?
           ))""",
        (alter_ego_id, user_id, alter_ego_id, alter_ego_id, user_id),
    )
    return cursor.rowcount == 1
