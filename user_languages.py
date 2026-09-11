"""Canonical user conversation-language preferences.

Profiles store one small ordered JSON array of ISO 639-1 language codes.  The
first entry is the primary language and any remaining entries are languages
the user also understands.  Keeping this representation channel-neutral lets
chat, voice, and future integrations share one preference without adding a
matrix of per-feature switches.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Iterable, Mapping

from babel import Locale

logger = logging.getLogger(__name__)

MAX_PREFERRED_LANGUAGES = 6
_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2}$")

# English labels remain stable for internal prompts and legacy inputs. The
# list is the shared base-language set supported by the current recognition
# and telephone-synthesis models (Scribe, Nova-2, and Flash v2.5), so every
# choice remains usable on every channel. Locale variants (es-ES, es-MX, ...)
# are normalized away rather than turning one simple preference into a
# provider/region matrix.
LANGUAGE_LABELS: dict[str, str] = {
    "bg": "Bulgarian",
    "zh": "Chinese",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",
    "hi": "Hindi",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "ms": "Malay",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "es": "Spanish",
    "sv": "Swedish",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
}

_LEGACY_CODE_ALIASES = {"in": "id", "mo": "ro"}
_LABEL_TO_CODE = {label.casefold(): code for code, label in LANGUAGE_LABELS.items()}


def normalize_language_code(value: object) -> str:
    """Return one supported base language code.

    Locale-shaped historic values such as ``es-ES`` and ``EN_us`` are reduced
    to their base language.  English display labels are also accepted so older
    form experiments can be read and re-serialized canonically.
    """

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Language codes cannot be empty.")

    label_code = _LABEL_TO_CODE.get(raw.casefold())
    if label_code:
        return label_code

    code = raw.replace("_", "-").split("-", 1)[0].lower()
    code = _LEGACY_CODE_ALIASES.get(code, code)
    if not _LANGUAGE_CODE_RE.fullmatch(code) or code not in LANGUAGE_LABELS:
        raise ValueError(f"Unsupported language code: {raw}")
    return code


def _legacy_mapping_items(value: Mapping[str, object]) -> list[object]:
    ordered = value.get("preferred_languages") or value.get("languages")
    if ordered:
        if isinstance(ordered, str):
            return [
                part for part in re.split(r"[,;]", ordered) if part.strip()
            ]
        if isinstance(ordered, Iterable) and not isinstance(
            ordered, (bytes, bytearray, Mapping)
        ):
            return list(ordered)
        raise ValueError("Languages must be a list of language codes.")

    primary = (
        value.get("primary")
        or value.get("primary_language")
        or value.get("main")
        or value.get("language")
    )
    secondary = (
        value.get("secondary")
        or value.get("secondary_languages")
        or value.get("additional")
        or value.get("other_languages")
        or []
    )
    items: list[object] = []
    if primary:
        items.append(primary)
    if isinstance(secondary, str):
        items.extend(part for part in re.split(r"[,;]", secondary) if part.strip())
    elif isinstance(secondary, Iterable) and not isinstance(
        secondary, (bytes, bytearray, Mapping)
    ):
        items.extend(secondary)
    elif secondary:
        raise ValueError("Secondary languages must be a list of language codes.")
    return items


def _preference_items(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Languages must be valid UTF-8.") from exc
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.casefold() in {"none", "null"}:
            return []
        if raw[:1] in "[{\"":
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Languages must be a valid JSON array of language codes."
                ) from exc
            return _preference_items(decoded)
        return [part for part in re.split(r"[,;]", raw) if part.strip()]
    if isinstance(value, Mapping):
        return _legacy_mapping_items(value)
    if isinstance(value, Iterable):
        return list(value)
    raise ValueError("Languages must be a list of language codes.")


def normalize_preferred_languages(value: object) -> tuple[str, ...]:
    """Validate, de-duplicate, and order a profile language preference."""

    normalized: list[str] = []
    for item in _preference_items(value):
        code = normalize_language_code(item)
        if code not in normalized:
            normalized.append(code)
    if len(normalized) > MAX_PREFERRED_LANGUAGES:
        raise ValueError(
            f"Choose at most {MAX_PREFERRED_LANGUAGES} conversation languages."
        )
    return tuple(normalized)


def serialize_preferred_languages(value: object) -> str:
    """Serialize any accepted preference value as compact canonical JSON."""

    return json.dumps(
        list(normalize_preferred_languages(value)),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def language_display_name(code: object, locale: str = "en") -> str | None:
    """Return a UI label, keeping English as the internal callers' default."""

    try:
        normalized = normalize_language_code(code)
    except ValueError:
        return None
    if locale == "en":
        return LANGUAGE_LABELS[normalized]
    return Locale.parse(locale, sep="-").languages.get(normalized, LANGUAGE_LABELS[normalized])


def selectable_user_languages(locale: str = "en") -> tuple[dict[str, str], ...]:
    """Return the same supported codes with labels in the requested UI locale."""

    labels = LANGUAGE_LABELS if locale == "en" else Locale.parse(locale, sep="-").languages
    return tuple(
        {"code": code, "label": label}
        for code, label in sorted(
            ((code, labels.get(code, fallback)) for code, fallback in LANGUAGE_LABELS.items()),
            key=lambda item: item[1],
        )
    )


def _missing_preference_schema(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return "no such column" in message or "no such table" in message


async def load_user_preferred_languages(conn: Any, user_id: int) -> tuple[str, ...]:
    """Load one user's preference, tolerating rolling/legacy schemas.

    Invalid stored content is treated as absent.  In particular, it must never
    be reflected into a trusted prompt block or provider request.
    """

    try:
        cursor = await conn.execute(
            "SELECT preferred_languages_json FROM USERS WHERE id = ?",
            (int(user_id),),
        )
        row = await cursor.fetchone()
    except sqlite3.OperationalError as exc:
        if not _missing_preference_schema(exc):
            raise
        logger.debug("USERS.preferred_languages_json is not available yet")
        return ()

    if not row or row[0] in (None, ""):
        return ()
    try:
        return normalize_preferred_languages(row[0])
    except ValueError:
        logger.warning(
            "Ignoring invalid stored conversation languages for user_id=%s",
            user_id,
        )
        return ()


__all__ = [
    "LANGUAGE_LABELS",
    "MAX_PREFERRED_LANGUAGES",
    "language_display_name",
    "load_user_preferred_languages",
    "normalize_language_code",
    "normalize_preferred_languages",
    "selectable_user_languages",
    "serialize_preferred_languages",
]
