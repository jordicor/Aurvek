"""Literal phone greetings and fail-closed cached-audio selection.

Greeting definitions are versioned independently from their rendered audio.
Calls only consume an activated audio revision whose complete candidate set
still matches the prompt's canonical voice and TTS profile.  This module does
not render TTS or expose files over HTTP; generation lives in
``audio_cache_service`` and transport playback remains a separate concern.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import re
from types import MappingProxyType
from typing import Any, Protocol

from ai_runtime.voice_resolution import CanonicalVoice, resolve_prompt_voice
from integrations.telephony.audio import (
    describe_pcmu_cache,
    pcmu_duration_ceiling_ms,
)
from integrations.telephony.clock import EndCallReason
from tools.tts_config import TTSProfile, get_tts_profile


GREETING_DIRECTIONS = frozenset({"inbound", "outbound"})
GREETING_MODES = frozenset({"inherit", "fixed", "random"})
PHONE_CACHE_MP3_FORMAT = "mp3_44100_128"

# These are stable machine keys, not user-facing copy.  The actual phrases are
# deliberately configured by administration/prompt settings and are never
# invented by this backend.
GLOBAL_TECHNICAL_NOTICE_KEYS = frozenset(
    {"inbound_unavailable", "unknown_caller"}
)
DEFAULT_GLOBAL_TECHNICAL_NOTICE_TEXT: Mapping[str, str] = MappingProxyType(
    {
        "inbound_unavailable": (
            "I'm sorry, incoming calls are not available for this number."
        ),
    }
)
PROMPT_TECHNICAL_NOTICE_KEYS = frozenset(
    {
        "silence_check",
        "silence_hangup",
        "deadline",
        "reconnect_notice",
        "reconnect_failed",
        "technical_failure",
        "balance_exhausted",
    }
)
TECHNICAL_NOTICE_KEYS = GLOBAL_TECHNICAL_NOTICE_KEYS | PROMPT_TECHNICAL_NOTICE_KEYS
END_CALL_NOTICE_KEYS: Mapping[EndCallReason, str] = MappingProxyType(
    {
        EndCallReason.DEADLINE: "deadline",
        EndCallReason.SILENCE: "silence_hangup",
        EndCallReason.BALANCE: "balance_exhausted",
        EndCallReason.ERROR: "technical_failure",
    }
)

GLOBAL_AUDIO_REVISION_CONFIG_KEY = "telephony_global_audio_revision"
DEFAULT_PRIVATE_CACHE_ROOT = (
    Path(__file__).resolve().parents[2] / "data" / "phone_prompt_audio_cache"
)

_CACHE_KEY_RE = re.compile(r"^[a-z0-9:_-]{1,220}$")
_NOTICE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MAX_LITERAL_CHARS = 2_000


class PhoneGreetingError(RuntimeError):
    """Base error for invalid or unavailable phone greeting configuration."""


class PhoneGreetingConfigurationError(PhoneGreetingError):
    """An administrator/prompt revision is structurally invalid."""


class PhoneGreetingCacheUnavailable(PhoneGreetingError):
    """Audio must not play because the active cache cannot be proven valid."""


class ChoiceSource(Protocol):
    def choice(self, values: Sequence[Any]) -> Any: ...


@dataclass(frozen=True, slots=True)
class GreetingInput:
    literal_text: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class GreetingDefinition:
    id: int
    scope: str
    prompt_id: int | None
    direction: str
    literal_text: str
    enabled: bool
    fixed: bool
    display_order: int
    definition_revision: int


@dataclass(frozen=True, slots=True)
class PhoneTextAlignment:
    """Exact literal-character timing for one cached phone asset.

    A completed character is considered audible only after its ``end_ms``.
    This deliberately conservative boundary lets barge-in persist a prefix
    that Twilio can actually have played, without guessing from word counts.
    """

    text: str
    character_start_ms: tuple[int, ...]
    character_end_ms: tuple[int, ...]

    def audible_prefix(self, played_ms: int) -> str:
        if isinstance(played_ms, bool) or not isinstance(played_ms, int):
            raise PhoneGreetingConfigurationError("played time must be an integer")
        if played_ms < 0:
            raise PhoneGreetingConfigurationError("played time cannot be negative")
        completed = bisect_right(self.character_end_ms, played_ms)
        return self.text[:completed]

    def as_json(self) -> str:
        return json.dumps(
            {
                "character_end_ms": list(self.character_end_ms),
                "character_start_ms": list(self.character_start_ms),
                "text": self.text,
                "version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class CachedPhoneAudio:
    cache_id: int
    cache_key: str
    prompt_id: int | None
    greeting_id: int | None
    asset_kind: str
    technical_notice_key: str | None
    direction: str | None
    literal_text: str
    audio_revision: int
    voice_id: int
    provider_key: str
    provider_voice_id: str
    tts_profile_json: str
    content_hash: str
    mp3_path: Path
    pcmu_path: Path
    duration_ms: int
    alignment_json: str
    alignment: PhoneTextAlignment

    def read_pcmu(self) -> bytes:
        audio = self.pcmu_path.read_bytes()
        if not audio:
            raise PhoneGreetingCacheUnavailable("cached phone audio is empty")
        return audio

    def audible_prefix(self, played_ms: int) -> str:
        return self.alignment.audible_prefix(played_ms)


def validate_phone_text_alignment(
    value: PhoneTextAlignment,
    *,
    literal_text: str,
    duration_ms: int,
) -> PhoneTextAlignment:
    """Return one canonical, fully covered character-timing alignment."""

    if not isinstance(value, PhoneTextAlignment):
        raise PhoneGreetingConfigurationError(
            "phone audio alignment must use PhoneTextAlignment"
        )
    text = normalize_literal_text(literal_text)
    if value.text != text:
        raise PhoneGreetingConfigurationError(
            "phone audio alignment text does not match"
        )
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
    ):
        raise PhoneGreetingConfigurationError(
            "phone audio alignment duration is invalid"
        )
    starts = value.character_start_ms
    ends = value.character_end_ms
    if not isinstance(starts, tuple) or not isinstance(ends, tuple):
        raise PhoneGreetingConfigurationError(
            "phone audio alignment timings must be tuples"
        )
    if len(starts) != len(text) or len(ends) != len(text):
        raise PhoneGreetingConfigurationError(
            "phone audio alignment must cover every literal character"
        )
    previous_end = 0
    for index, (start, end) in enumerate(zip(starts, ends, strict=True)):
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise PhoneGreetingConfigurationError(
                "phone audio alignment timings must be integer milliseconds"
            )
        if start < 0 or start > end or end < previous_end or end > duration_ms:
            raise PhoneGreetingConfigurationError(
                f"phone audio alignment is not monotonic at character {index}"
            )
        previous_end = end
    return PhoneTextAlignment(
        text=text,
        character_start_ms=tuple(starts),
        character_end_ms=tuple(ends),
    )


def parse_phone_text_alignment(
    value: Any,
    *,
    literal_text: str,
    duration_ms: int,
) -> PhoneTextAlignment:
    """Parse only the canonical persisted alignment schema; fail closed."""

    if not isinstance(value, str) or not value:
        raise PhoneGreetingCacheUnavailable("cached phone audio alignment is missing")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise PhoneGreetingCacheUnavailable(
            "cached phone audio alignment is invalid"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "text",
        "character_start_ms",
        "character_end_ms",
    }:
        raise PhoneGreetingCacheUnavailable("cached phone audio alignment is invalid")
    if payload["version"] != 1 or not isinstance(payload["text"], str):
        raise PhoneGreetingCacheUnavailable("cached phone audio alignment is invalid")
    starts = payload["character_start_ms"]
    ends = payload["character_end_ms"]
    if not isinstance(starts, list) or not isinstance(ends, list):
        raise PhoneGreetingCacheUnavailable("cached phone audio alignment is invalid")
    try:
        return validate_phone_text_alignment(
            PhoneTextAlignment(
                text=payload["text"],
                character_start_ms=tuple(starts),
                character_end_ms=tuple(ends),
            ),
            literal_text=literal_text,
            duration_ms=duration_ms,
        )
    except PhoneGreetingConfigurationError as exc:
        raise PhoneGreetingCacheUnavailable(
            "cached phone audio alignment is invalid"
        ) from exc


def normalize_literal_text(value: Any) -> str:
    """Validate one deliberately literal phrase.

    Curly braces are rejected rather than interpreted later.  That makes the
    no-placeholders contract enforceable at the write boundary and prevents a
    future caller from accidentally adding conversation data to cached audio.
    """

    if not isinstance(value, str):
        raise PhoneGreetingConfigurationError("phone audio text must be a string")
    text = value.strip()
    if not text or len(text) > _MAX_LITERAL_CHARS:
        raise PhoneGreetingConfigurationError("phone audio text is empty or too long")
    if "{" in text or "}" in text:
        raise PhoneGreetingConfigurationError(
            "phone audio text is literal and cannot contain placeholders"
        )
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in text):
        raise PhoneGreetingConfigurationError("phone audio text contains controls")
    return text


def normalize_notice_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not _NOTICE_KEY_RE.fullmatch(key) or key not in TECHNICAL_NOTICE_KEYS:
        raise PhoneGreetingConfigurationError("unsupported technical notice key")
    return key


def serialize_tts_profile(profile: TTSProfile) -> str:
    payload = {
        "chunk_schedule": [int(value) for value in profile.chunk_schedule],
        "model_id": str(profile.model_id),
        "output_format": str(profile.output_format),
        "similarity_boost": float(profile.similarity_boost),
        "stability": float(profile.stability),
        "ws_enabled": bool(profile.ws_enabled),
    }
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def normalize_phone_cache_tts_profile(profile: TTSProfile) -> TTSProfile:
    """Use deterministic MP3 cache output while retaining voice settings."""

    if not isinstance(profile, TTSProfile):
        raise PhoneGreetingConfigurationError("phone cache TTS profile is invalid")
    model_id = str(profile.model_id or "").strip()
    if not model_id:
        raise PhoneGreetingConfigurationError("phone cache TTS model is invalid")
    try:
        stability = float(profile.stability)
        similarity = float(profile.similarity_boost)
        schedule = [int(value) for value in profile.chunk_schedule]
    except (TypeError, ValueError) as exc:
        raise PhoneGreetingConfigurationError(
            "phone cache TTS settings are invalid"
        ) from exc
    if (
        not 0 <= stability <= 1
        or not 0 <= similarity <= 1
        or not schedule
        or any(value <= 0 for value in schedule)
    ):
        raise PhoneGreetingConfigurationError(
            "phone cache TTS settings are invalid"
        )
    return TTSProfile(
        model_id=model_id,
        output_format=PHONE_CACHE_MP3_FORMAT,
        stability=stability,
        similarity_boost=similarity,
        ws_enabled=bool(profile.ws_enabled),
        chunk_schedule=schedule,
    )


def build_audio_content_hash(
    *,
    asset_kind: str,
    identity_key: str,
    literal_text: str,
    revision: int,
    voice: CanonicalVoice,
    tts_profile_json: str,
) -> str:
    """Hash every value whose change invalidates rendered phone audio."""

    if asset_kind not in {"greeting", "technical_notice"}:
        raise PhoneGreetingConfigurationError("invalid phone cache asset kind")
    if int(revision) <= 0:
        raise PhoneGreetingConfigurationError("audio revision must be positive")
    identity = str(identity_key or "").strip()
    if not identity:
        raise PhoneGreetingConfigurationError("audio cache identity is missing")
    payload = {
        "asset_kind": asset_kind,
        "identity_key": identity,
        "literal_text": normalize_literal_text(literal_text),
        "provider_key": voice.provider,
        "provider_voice_id": voice.voice_code,
        "revision": int(revision),
        "tts_profile": json.loads(tts_profile_json),
        "voice_id": int(voice.id),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_cache_key(
    *,
    prompt_id: int | None,
    revision: int,
    asset_kind: str,
    identity_key: str,
    content_hash: str,
) -> str:
    scope = "global" if prompt_id is None else f"prompt:{int(prompt_id)}"
    kind = "greeting" if asset_kind == "greeting" else "notice"
    key = f"phone:{scope}:r{int(revision)}:{kind}:{identity_key}:{content_hash[:20]}"
    if not _CACHE_KEY_RE.fullmatch(key):
        raise PhoneGreetingConfigurationError("generated cache key is invalid")
    return key


async def stage_greeting_revision(
    conn: Any,
    *,
    scope: str,
    prompt_id: int | None,
    revision: int,
    direction: str,
    mode: str,
    greetings: Sequence[GreetingInput],
    fixed_index: int | None = None,
) -> tuple[int, ...]:
    """Insert one immutable list revision; activation happens separately."""

    normalized_scope, normalized_prompt = _validate_scope(scope, prompt_id)
    normalized_direction = _validate_direction(direction)
    normalized_mode = _validate_mode(mode, allow_inherit=normalized_scope == "prompt")
    if int(revision) <= 0:
        raise PhoneGreetingConfigurationError("greeting revision must be positive")
    cursor = await conn.execute(
        """
        SELECT COUNT(*) AS count FROM PROMPT_PHONE_GREETINGS
        WHERE scope=? AND prompt_id IS ? AND direction=? AND revision=?
        """,
        (normalized_scope, normalized_prompt, normalized_direction, int(revision)),
    )
    if int((await cursor.fetchone())["count"]) != 0:
        raise PhoneGreetingConfigurationError("greeting revision is immutable")
    if normalized_mode == "inherit":
        if greetings or fixed_index is not None:
            raise PhoneGreetingConfigurationError(
                "inherited greetings cannot define phrases"
            )
        return ()
    if not greetings:
        raise PhoneGreetingConfigurationError(
            "a replacement greeting list cannot be empty"
        )

    normalized = [normalize_literal_text(item.literal_text) for item in greetings]
    if not any(bool(item.enabled) for item in greetings):
        raise PhoneGreetingConfigurationError("at least one greeting must be enabled")
    if normalized_mode == "fixed":
        if fixed_index is None or not 0 <= int(fixed_index) < len(greetings):
            raise PhoneGreetingConfigurationError("fixed greeting selection is missing")
        if not bool(greetings[int(fixed_index)].enabled):
            raise PhoneGreetingConfigurationError("the fixed greeting must be enabled")
    elif fixed_index is not None:
        raise PhoneGreetingConfigurationError(
            "random greeting selection cannot be fixed"
        )

    inserted: list[int] = []
    for index, (item, literal_text) in enumerate(
        zip(greetings, normalized, strict=True)
    ):
        cursor = await conn.execute(
            """
            INSERT INTO PROMPT_PHONE_GREETINGS(
                scope,prompt_id,direction,literal_text,enabled,is_fixed_selection,
                display_order,revision
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                normalized_scope,
                normalized_prompt,
                normalized_direction,
                literal_text,
                int(bool(item.enabled)),
                int(normalized_mode == "fixed" and index == int(fixed_index)),
                index,
                int(revision),
            ),
        )
        inserted.append(int(cursor.lastrowid))
    return tuple(inserted)


async def load_greeting_revision(
    conn: Any,
    *,
    scope: str,
    prompt_id: int | None,
    revision: int,
    direction: str,
) -> tuple[GreetingDefinition, ...]:
    normalized_scope, normalized_prompt = _validate_scope(scope, prompt_id)
    normalized_direction = _validate_direction(direction)
    cursor = await conn.execute(
        """
        SELECT id,scope,prompt_id,direction,literal_text,enabled,
               is_fixed_selection,display_order,revision
        FROM PROMPT_PHONE_GREETINGS
        WHERE scope=? AND prompt_id IS ? AND revision=? AND direction=?
        ORDER BY display_order,id
        """,
        (normalized_scope, normalized_prompt, int(revision), normalized_direction),
    )
    rows = await cursor.fetchall()
    return tuple(_greeting_from_row(row) for row in rows)


async def select_cached_greeting(
    conn: Any,
    *,
    prompt_id: int,
    direction: str,
    revision: int,
    greeting_mode: str,
    previous_greeting_id: int | None = None,
    rng: ChoiceSource = random,
    cache_root: str | Path = DEFAULT_PRIVATE_CACHE_ROOT,
    voice: CanonicalVoice | None = None,
    profile: TTSProfile | None = None,
) -> CachedPhoneAudio:
    """Select from the call-captured revision and prove its entire cache set.

    The previous greeting comes from the caller's last durable call.  With two
    or more enabled candidates it is excluded before the uniform draw.  Both
    revision and mode are required caller-snapshot data; this function never
    follows a newer administrative activation.
    """

    normalized_direction = _validate_direction(direction)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise PhoneGreetingConfigurationError(
            "captured phone audio revision is required"
        )
    mode = _validate_mode(greeting_mode, allow_inherit=True)
    effective_voice = voice or await resolve_prompt_voice(int(prompt_id), conn=conn)
    effective_profile = normalize_phone_cache_tts_profile(
        profile or await get_tts_profile("external")
    )
    profile_json = serialize_tts_profile(effective_profile)

    expected_scope = "global" if mode == "inherit" else "prompt"
    cursor = await conn.execute(
        """
        SELECT c.*,g.scope AS greeting_scope,g.prompt_id AS definition_prompt_id,
               g.literal_text AS greeting_literal_text,
               g.enabled,g.is_fixed_selection,g.display_order,
               g.revision AS definition_revision
        FROM PHONE_PROMPT_AUDIO_CACHE c
        JOIN PROMPT_PHONE_GREETINGS g ON g.id=c.greeting_id
        WHERE c.asset_kind='greeting' AND c.revision=? AND c.status='ready'
          AND c.direction=? AND g.scope=? AND g.enabled=1
          AND (c.prompt_id=? OR c.prompt_id IS NULL)
        ORDER BY CASE WHEN c.prompt_id=? THEN 0 ELSE 1 END,g.display_order,g.id
        """,
        (
            revision,
            normalized_direction,
            expected_scope,
            int(prompt_id),
            int(prompt_id),
        ),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    rows = _prefer_prompt_specific_rows(rows)
    if not rows:
        raise PhoneGreetingCacheUnavailable("active phone greeting cache is missing")
    await _assert_complete_greeting_candidate_set(conn, rows=rows, mode=mode)

    if mode == "fixed":
        fixed = [row for row in rows if bool(row["is_fixed_selection"])]
        if len(fixed) == 1:
            candidates = fixed
        elif not fixed and len(rows) == 1:
            # Compatibility for active cache rows created before fixed
            # selections became immutable per definition revision.
            candidates = rows
        else:
            raise PhoneGreetingCacheUnavailable("active fixed greeting is ambiguous")
    elif mode in {"inherit", "random"}:
        fixed = [row for row in rows if bool(row["is_fixed_selection"])]
        candidates = fixed if mode == "inherit" and fixed else rows
        if mode == "inherit" and len(fixed) > 1:
            raise PhoneGreetingCacheUnavailable("global fixed greeting is ambiguous")
    else:
        raise PhoneGreetingCacheUnavailable("active greeting mode is invalid")

    validated = [
        _cached_audio_from_row(
            row,
            voice=effective_voice,
            tts_profile_json=profile_json,
            cache_root=cache_root,
        )
        for row in candidates
    ]
    selectable = validated
    if len(validated) >= 2 and previous_greeting_id is not None:
        without_previous = [
            item for item in validated if item.greeting_id != int(previous_greeting_id)
        ]
        if without_previous:
            selectable = without_previous
    return rng.choice(selectable)


async def load_cached_technical_notice(
    conn: Any,
    *,
    prompt_id: int | None,
    notice_key: str,
    cache_root: str | Path = DEFAULT_PRIVATE_CACHE_ROOT,
    voice: CanonicalVoice,
    profile: TTSProfile,
    revision: int,
) -> CachedPhoneAudio:
    """Load exactly one notice from the call-captured revision, or fail closed."""

    key = normalize_notice_key(notice_key)
    if prompt_id is None and key not in GLOBAL_TECHNICAL_NOTICE_KEYS:
        raise PhoneGreetingConfigurationError("prompt notice requires a prompt")
    if prompt_id is not None and key not in PROMPT_TECHNICAL_NOTICE_KEYS:
        raise PhoneGreetingConfigurationError("global notice cannot use a prompt cache")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise PhoneGreetingConfigurationError(
            "captured phone audio revision is required"
        )
    active_revision = revision

    prefix = build_cache_key_prefix(
        prompt_id=prompt_id,
        revision=int(active_revision),
        asset_kind="technical_notice",
        identity_key=key,
    )
    cursor = await conn.execute(
        """
        SELECT * FROM PHONE_PROMPT_AUDIO_CACHE
        WHERE cache_key LIKE ? ESCAPE '\\' AND asset_kind='technical_notice'
          AND prompt_id IS ? AND revision=? AND status='ready'
        ORDER BY id
        """,
        (_escape_like(prefix) + "%", prompt_id, int(active_revision)),
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        raise PhoneGreetingCacheUnavailable(
            "technical notice cache is missing or ambiguous"
        )
    return _cached_audio_from_row(
        dict(rows[0]),
        voice=voice,
        tts_profile_json=serialize_tts_profile(
            normalize_phone_cache_tts_profile(profile)
        ),
        cache_root=cache_root,
        technical_notice_key=key,
    )


def technical_notice_key_for_end_reason(reason: EndCallReason) -> str | None:
    """Map forced clock reasons explicitly; voluntary ``end_call`` uses its farewell."""

    if not isinstance(reason, EndCallReason):
        raise PhoneGreetingConfigurationError("invalid end-call reason")
    return END_CALL_NOTICE_KEYS.get(reason)


def build_cache_key_prefix(
    *,
    prompt_id: int | None,
    revision: int,
    asset_kind: str,
    identity_key: str,
) -> str:
    scope = "global" if prompt_id is None else f"prompt:{int(prompt_id)}"
    kind = "greeting" if asset_kind == "greeting" else "notice"
    prefix = f"phone:{scope}:r{int(revision)}:{kind}:{identity_key}:"
    if not _CACHE_KEY_RE.fullmatch(prefix.rstrip(":")):
        raise PhoneGreetingConfigurationError("generated cache key prefix is invalid")
    return prefix


def _cached_audio_from_row(
    row: Mapping[str, Any],
    *,
    voice: CanonicalVoice,
    tts_profile_json: str,
    cache_root: str | Path,
    technical_notice_key: str | None = None,
) -> CachedPhoneAudio:
    if int(row["voice_id"]) != voice.id:
        raise PhoneGreetingCacheUnavailable("cached audio voice is stale")
    if str(row["provider_key"]) != voice.provider:
        raise PhoneGreetingCacheUnavailable("cached audio provider is stale")
    if str(row["provider_voice_id"]) != voice.voice_code:
        raise PhoneGreetingCacheUnavailable("cached provider voice is stale")
    if str(row["tts_profile_json"]) != tts_profile_json:
        raise PhoneGreetingCacheUnavailable("cached TTS profile is stale")
    if row.get("greeting_literal_text") is not None and str(
        row["greeting_literal_text"]
    ) != str(row["literal_text"]):
        raise PhoneGreetingCacheUnavailable("cached greeting text is stale")

    asset_kind = str(row["asset_kind"])
    identity_key = (
        str(int(row["greeting_id"]))
        if asset_kind == "greeting"
        else normalize_notice_key(technical_notice_key)
    )
    expected_hash = build_audio_content_hash(
        asset_kind=asset_kind,
        identity_key=identity_key,
        literal_text=str(row["literal_text"]),
        revision=int(row["revision"]),
        voice=voice,
        tts_profile_json=tts_profile_json,
    )
    if str(row["content_hash"]) != expected_hash:
        raise PhoneGreetingCacheUnavailable("cached audio content hash is stale")
    expected_key = build_cache_key(
        prompt_id=row["prompt_id"],
        revision=int(row["revision"]),
        asset_kind=asset_kind,
        identity_key=identity_key,
        content_hash=expected_hash,
    )
    if str(row["cache_key"]) != expected_key:
        raise PhoneGreetingCacheUnavailable("cached audio identity is stale")

    root = Path(cache_root).resolve()
    mp3_path = _private_path(row["source_mp3_path"], root, "MP3")
    pcmu_path = _private_path(row["pcmu_path"], root, "PCMU")
    if not mp3_path.is_file() or mp3_path.stat().st_size <= 0:
        raise PhoneGreetingCacheUnavailable("cached MP3 is unavailable")
    try:
        pcmu = describe_pcmu_cache(pcmu_path)
    except Exception as exc:
        raise PhoneGreetingCacheUnavailable("cached PCMU is unavailable") from exc
    duration_ms = pcmu_duration_ceiling_ms(pcmu.byte_length)
    stored_duration = row["duration_ms"]
    if stored_duration is None or abs(int(stored_duration) - duration_ms) > 1:
        raise PhoneGreetingCacheUnavailable("cached audio duration is stale")
    alignment = parse_phone_text_alignment(
        row["alignment_json"],
        literal_text=str(row["literal_text"]),
        duration_ms=duration_ms,
    )
    return CachedPhoneAudio(
        cache_id=int(row["id"]),
        cache_key=expected_key,
        prompt_id=None if row["prompt_id"] is None else int(row["prompt_id"]),
        greeting_id=(None if row["greeting_id"] is None else int(row["greeting_id"])),
        asset_kind=asset_kind,
        technical_notice_key=technical_notice_key,
        direction=row["direction"],
        literal_text=str(row["literal_text"]),
        audio_revision=int(row["revision"]),
        voice_id=int(row["voice_id"]),
        provider_key=str(row["provider_key"]),
        provider_voice_id=str(row["provider_voice_id"]),
        tts_profile_json=str(row["tts_profile_json"]),
        content_hash=expected_hash,
        mp3_path=mp3_path,
        pcmu_path=pcmu_path,
        duration_ms=duration_ms,
        alignment_json=alignment.as_json(),
        alignment=alignment,
    )


def _private_path(value: Any, root: Path, label: str) -> Path:
    if value is None:
        raise PhoneGreetingCacheUnavailable(f"cached {label} path is missing")
    path = Path(str(value)).resolve()
    if path == root or root not in path.parents:
        raise PhoneGreetingCacheUnavailable(
            f"cached {label} path escapes private storage"
        )
    return path


def _prefer_prompt_specific_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt_rows = [row for row in rows if row["prompt_id"] is not None]
    return prompt_rows or rows


async def _assert_complete_greeting_candidate_set(
    conn: Any,
    *,
    rows: Sequence[Mapping[str, Any]],
    mode: str,
) -> None:
    for direction in GREETING_DIRECTIONS:
        direction_rows = [row for row in rows if row["direction"] == direction]
        if not direction_rows:
            # Selection requests one direction, so the other is normally not
            # present in the query.  Only validate the direction returned.
            continue
        scopes = {
            (str(row["greeting_scope"]), row["definition_revision"])
            for row in direction_rows
        }
        if len(scopes) != 1:
            raise PhoneGreetingCacheUnavailable("active greeting definitions are mixed")
        if mode == "fixed":
            if len(direction_rows) != 1:
                raise PhoneGreetingCacheUnavailable(
                    "active fixed greeting is incomplete"
                )
            continue
        if mode == "inherit" and len(direction_rows) == 1:
            # A global list with one enabled phrase and a fixed global list are
            # both complete with one cached candidate.
            continue
        scope, definition_revision = next(iter(scopes))
        definition_prompt_id = direction_rows[0]["definition_prompt_id"]
        cursor = await conn.execute(
            """
            SELECT id FROM PROMPT_PHONE_GREETINGS
            WHERE scope=? AND prompt_id IS ? AND direction=? AND revision=?
              AND enabled=1
            ORDER BY id
            """,
            (scope, definition_prompt_id, direction, definition_revision),
        )
        expected = {int(row["id"]) for row in await cursor.fetchall()}
        cached = {int(row["greeting_id"]) for row in direction_rows}
        if not expected or expected != cached:
            raise PhoneGreetingCacheUnavailable("active greeting cache is incomplete")


def _greeting_from_row(row: Mapping[str, Any]) -> GreetingDefinition:
    values = dict(row)
    return GreetingDefinition(
        id=int(values["id"]),
        scope=str(values["scope"]),
        prompt_id=None if values["prompt_id"] is None else int(values["prompt_id"]),
        direction=str(values["direction"]),
        literal_text=str(values["literal_text"]),
        enabled=bool(values["enabled"]),
        fixed=bool(values["is_fixed_selection"]),
        display_order=int(values["display_order"]),
        definition_revision=int(values["revision"]),
    )


def _validate_scope(scope: str, prompt_id: int | None) -> tuple[str, int | None]:
    normalized = str(scope or "").strip().lower()
    if normalized == "global" and prompt_id is None:
        return normalized, None
    if normalized == "prompt" and prompt_id is not None and int(prompt_id) > 0:
        return normalized, int(prompt_id)
    raise PhoneGreetingConfigurationError("phone greeting scope is invalid")


def _validate_direction(direction: str) -> str:
    normalized = str(direction or "").strip().lower()
    if normalized not in GREETING_DIRECTIONS:
        raise PhoneGreetingConfigurationError("phone greeting direction is invalid")
    return normalized


def _validate_mode(mode: str, *, allow_inherit: bool) -> str:
    normalized = str(mode or "").strip().lower()
    allowed = GREETING_MODES if allow_inherit else GREETING_MODES - {"inherit"}
    if normalized not in allowed:
        raise PhoneGreetingConfigurationError("phone greeting mode is invalid")
    return normalized


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = [
    "CachedPhoneAudio",
    "DEFAULT_GLOBAL_TECHNICAL_NOTICE_TEXT",
    "DEFAULT_PRIVATE_CACHE_ROOT",
    "END_CALL_NOTICE_KEYS",
    "GLOBAL_AUDIO_REVISION_CONFIG_KEY",
    "GLOBAL_TECHNICAL_NOTICE_KEYS",
    "GREETING_DIRECTIONS",
    "GREETING_MODES",
    "GreetingDefinition",
    "GreetingInput",
    "PhoneTextAlignment",
    "PHONE_CACHE_MP3_FORMAT",
    "PROMPT_TECHNICAL_NOTICE_KEYS",
    "PhoneGreetingCacheUnavailable",
    "PhoneGreetingConfigurationError",
    "PhoneGreetingError",
    "TECHNICAL_NOTICE_KEYS",
    "build_audio_content_hash",
    "build_cache_key",
    "build_cache_key_prefix",
    "load_cached_technical_notice",
    "load_greeting_revision",
    "normalize_literal_text",
    "normalize_notice_key",
    "normalize_phone_cache_tts_profile",
    "parse_phone_text_alignment",
    "select_cached_greeting",
    "serialize_tts_profile",
    "stage_greeting_revision",
    "technical_notice_key_for_end_reason",
    "validate_phone_text_alignment",
]
