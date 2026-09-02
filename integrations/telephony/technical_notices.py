"""Versioned literal copy for cached telephone technical notices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
import uuid

from integrations.telephony.greetings import (
    PROMPT_TECHNICAL_NOTICE_KEYS,
    TECHNICAL_NOTICE_KEYS,
    PhoneGreetingConfigurationError,
    normalize_literal_text,
    normalize_notice_key,
)


@dataclass(frozen=True, slots=True)
class TechnicalNoticeRevision:
    revision: int
    updated_by: int
    notices: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PromptTechnicalNoticeRevision:
    prompt_id: int
    revision: int
    updated_by: int
    notices: Mapping[str, str]


async def stage_technical_notice_revision(
    conn: Any,
    *,
    revision: int,
    notices: Mapping[str, str],
    updated_by: int,
) -> TechnicalNoticeRevision:
    """Insert one immutable, complete eight-key revision without inventing copy."""

    normalized_revision = _positive_integer(revision, "technical notice revision")
    normalized_updated_by = _positive_integer(updated_by, "technical notice updater")
    normalized = _normalize_complete_notice_set(
        notices,
        expected_keys=TECHNICAL_NOTICE_KEYS,
        label="technical notice",
    )

    savepoint = f"phone_notice_{uuid.uuid4().hex}"
    await conn.execute(f"SAVEPOINT {savepoint}")
    try:
        cursor = await conn.execute(
            "SELECT 1 FROM USERS WHERE id=?", (normalized_updated_by,)
        )
        if await cursor.fetchone() is None:
            raise PhoneGreetingConfigurationError(
                "technical notice updater does not exist"
            )
        cursor = await conn.execute(
            "SELECT 1 FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS WHERE revision=? LIMIT 1",
            (normalized_revision,),
        )
        if await cursor.fetchone() is not None:
            raise PhoneGreetingConfigurationError(
                "technical notice revision is immutable"
            )
        for key in sorted(normalized):
            await conn.execute(
                """
                INSERT INTO PHONE_TECHNICAL_NOTICE_DEFINITIONS(
                    revision, notice_key, literal_text, updated_by
                ) VALUES(?,?,?,?)
                """,
                (
                    normalized_revision,
                    key,
                    normalized[key],
                    normalized_updated_by,
                ),
            )
    except BaseException:
        await conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    return TechnicalNoticeRevision(
        revision=normalized_revision,
        updated_by=normalized_updated_by,
        notices=MappingProxyType(dict(normalized)),
    )


async def load_latest_complete_technical_notice_revision(
    conn: Any,
) -> TechnicalNoticeRevision:
    """Return the newest revision only when it is exact and complete."""

    cursor = await conn.execute(
        "SELECT MAX(revision) FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS"
    )
    row = await cursor.fetchone()
    if row is None or row[0] is None:
        raise PhoneGreetingConfigurationError(
            "technical notice definitions have not been configured"
        )
    return await load_technical_notice_revision(conn, revision=int(row[0]))


async def load_technical_notice_revision(
    conn: Any,
    *,
    revision: int,
) -> TechnicalNoticeRevision:
    """Return one explicit complete global revision without selecting newer copy."""

    normalized_revision = _positive_integer(revision, "technical notice revision")
    cursor = await conn.execute(
        """
        SELECT notice_key,literal_text,updated_by
        FROM PHONE_TECHNICAL_NOTICE_DEFINITIONS
        WHERE revision=? ORDER BY notice_key
        """,
        (normalized_revision,),
    )
    rows = await cursor.fetchall()
    if not rows:
        raise PhoneGreetingConfigurationError(
            "technical notice revision is not configured"
        )
    updated_by_values = {int(row[2]) for row in rows}
    try:
        notices = _normalize_complete_notice_set(
            {str(row[0]): str(row[1]) for row in rows},
            expected_keys=TECHNICAL_NOTICE_KEYS,
            label="technical notice",
        )
    except PhoneGreetingConfigurationError as exc:
        raise PhoneGreetingConfigurationError(
            "technical notice revision is incomplete"
        ) from exc
    if len(rows) != len(TECHNICAL_NOTICE_KEYS) or len(updated_by_values) != 1:
        raise PhoneGreetingConfigurationError(
            "technical notice revision is incomplete"
        )
    return TechnicalNoticeRevision(
        revision=normalized_revision,
        updated_by=next(iter(updated_by_values)),
        notices=MappingProxyType(dict(notices)),
    )


async def stage_prompt_technical_notice_revision(
    conn: Any,
    *,
    prompt_id: int,
    revision: int,
    notices: Mapping[str, str],
    updated_by: int,
) -> PromptTechnicalNoticeRevision:
    """Insert one immutable, complete seven-key prompt override revision."""

    normalized_prompt_id = _positive_integer(prompt_id, "prompt id")
    normalized_revision = _positive_integer(
        revision, "prompt technical notice revision"
    )
    normalized_updated_by = _positive_integer(
        updated_by, "prompt technical notice updater"
    )
    normalized = _normalize_complete_notice_set(
        notices,
        expected_keys=PROMPT_TECHNICAL_NOTICE_KEYS,
        label="prompt technical notice",
    )

    savepoint = f"phone_prompt_notice_{uuid.uuid4().hex}"
    await conn.execute(f"SAVEPOINT {savepoint}")
    try:
        cursor = await conn.execute(
            "SELECT 1 FROM USERS WHERE id=?", (normalized_updated_by,)
        )
        if await cursor.fetchone() is None:
            raise PhoneGreetingConfigurationError(
                "prompt technical notice updater does not exist"
            )
        cursor = await conn.execute(
            "SELECT 1 FROM PROMPTS WHERE id=?", (normalized_prompt_id,)
        )
        if await cursor.fetchone() is None:
            raise PhoneGreetingConfigurationError("prompt does not exist")
        cursor = await conn.execute(
            """
            SELECT 1 FROM PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS
            WHERE prompt_id=? AND revision=? LIMIT 1
            """,
            (normalized_prompt_id, normalized_revision),
        )
        if await cursor.fetchone() is not None:
            raise PhoneGreetingConfigurationError(
                "prompt technical notice revision is immutable"
            )
        for key in sorted(normalized):
            await conn.execute(
                """
                INSERT INTO PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS(
                    prompt_id, revision, notice_key, literal_text, updated_by
                ) VALUES(?,?,?,?,?)
                """,
                (
                    normalized_prompt_id,
                    normalized_revision,
                    key,
                    normalized[key],
                    normalized_updated_by,
                ),
            )
    except BaseException:
        await conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    await conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    return PromptTechnicalNoticeRevision(
        prompt_id=normalized_prompt_id,
        revision=normalized_revision,
        updated_by=normalized_updated_by,
        notices=MappingProxyType(dict(normalized)),
    )


async def load_prompt_technical_notice_revision(
    conn: Any,
    *,
    prompt_id: int,
    revision: int,
) -> PromptTechnicalNoticeRevision | None:
    """Load an exact prompt revision, or ``None`` when it inherits globals."""

    normalized_prompt_id = _positive_integer(prompt_id, "prompt id")
    normalized_revision = _positive_integer(
        revision, "prompt technical notice revision"
    )
    cursor = await conn.execute(
        """
        SELECT notice_key,literal_text,updated_by
        FROM PROMPT_PHONE_TECHNICAL_NOTICE_DEFINITIONS
        WHERE prompt_id=? AND revision=? ORDER BY notice_key
        """,
        (normalized_prompt_id, normalized_revision),
    )
    rows = await cursor.fetchall()
    if not rows:
        return None
    updated_by_values = {int(row[2]) for row in rows}
    try:
        notices = _normalize_complete_notice_set(
            {str(row[0]): str(row[1]) for row in rows},
            expected_keys=PROMPT_TECHNICAL_NOTICE_KEYS,
            label="prompt technical notice",
        )
    except PhoneGreetingConfigurationError as exc:
        raise PhoneGreetingConfigurationError(
            "prompt technical notice revision is incomplete"
        ) from exc
    if (
        len(rows) != len(PROMPT_TECHNICAL_NOTICE_KEYS)
        or len(updated_by_values) != 1
    ):
        raise PhoneGreetingConfigurationError(
            "prompt technical notice revision is incomplete"
        )
    return PromptTechnicalNoticeRevision(
        prompt_id=normalized_prompt_id,
        revision=normalized_revision,
        updated_by=next(iter(updated_by_values)),
        notices=MappingProxyType(dict(notices)),
    )


def _normalize_complete_notice_set(
    notices: Mapping[str, str],
    *,
    expected_keys: frozenset[str],
    label: str,
) -> dict[str, str]:
    try:
        normalized: dict[str, str] = {}
        for key, value in notices.items():
            normalized_key = normalize_notice_key(key)
            if normalized_key in normalized:
                raise PhoneGreetingConfigurationError(
                    f"{label} keys collide after normalization"
                )
            normalized[normalized_key] = normalize_literal_text(value)
    except (AttributeError, TypeError) as exc:
        raise PhoneGreetingConfigurationError(
            f"{label} set is invalid"
        ) from exc
    if set(normalized) != set(expected_keys):
        missing = sorted(set(expected_keys) - set(normalized))
        extra = sorted(set(normalized) - set(expected_keys))
        raise PhoneGreetingConfigurationError(
            f"{label} set is incomplete (missing={missing}, extra={extra})"
        )
    return normalized


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise PhoneGreetingConfigurationError(f"{label} must be positive")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PhoneGreetingConfigurationError(f"{label} must be positive") from exc
    if normalized <= 0 or normalized != value:
        raise PhoneGreetingConfigurationError(f"{label} must be positive")
    return normalized


__all__ = [
    "PromptTechnicalNoticeRevision",
    "TechnicalNoticeRevision",
    "load_latest_complete_technical_notice_revision",
    "load_prompt_technical_notice_revision",
    "load_technical_notice_revision",
    "stage_prompt_technical_notice_revision",
    "stage_technical_notice_revision",
]
