"""Generate and atomically activate private phone greeting audio caches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Protocol
import uuid

from ai_runtime.voice_resolution import CanonicalVoice, provider_from_service_name
from integrations.telephony.audio import (
    describe_pcmu_cache,
    materialize_pcmu_cache,
    pcmu_duration_ceiling_ms,
)
from integrations.telephony.greetings import (
    DEFAULT_PRIVATE_CACHE_ROOT,
    GLOBAL_AUDIO_REVISION_CONFIG_KEY,
    GLOBAL_TECHNICAL_NOTICE_KEYS,
    GREETING_DIRECTIONS,
    PHONE_CACHE_MP3_FORMAT,
    PROMPT_TECHNICAL_NOTICE_KEYS,
    GreetingDefinition,
    PhoneTextAlignment,
    PhoneGreetingConfigurationError,
    build_audio_content_hash,
    build_cache_key,
    normalize_literal_text,
    normalize_notice_key,
    normalize_phone_cache_tts_profile,
    parse_phone_text_alignment,
    serialize_tts_profile,
    validate_phone_text_alignment,
)
from tools.tts_config import TTSProfile


class PhoneAudioCacheBuildError(RuntimeError):
    """A pending cache revision failed and was not activated."""


PHONE_AUDIO_ALIGNER_VERSION = "character-timing-v1"
PHONE_CACHE_OPENAI_MODEL = "tts-1"


@dataclass(frozen=True, slots=True)
class RenderedMp3:
    path: Path
    alignment: PhoneTextAlignment


class Mp3Renderer(Protocol):
    async def __call__(
        self,
        *,
        literal_text: str,
        voice: CanonicalVoice,
        profile: TTSProfile,
        billing_user_id: int,
        activation_id: str,
        cache_key: str,
        render_fingerprint: str,
    ) -> RenderedMp3: ...


@dataclass(frozen=True, slots=True)
class AudioCacheActivationPlan:
    scope: str
    prompt_id: int | None
    revision: int
    voice: CanonicalVoice
    profile: TTSProfile
    greetings: tuple[GreetingDefinition, ...]
    technical_notices: Mapping[str, str]
    billing_user_id: int
    greeting_modes: Mapping[str, str] | None = None
    commit_voice_change: bool = False
    expected_active_revision: int | None = None
    enforce_prompt_state_fence: bool = False


@dataclass(frozen=True, slots=True)
class AudioCacheActivationResult:
    activation_id: str
    scope: str
    prompt_id: int | None
    revision: int
    cache_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AudioCacheRecoveryResult:
    canceled_activation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _AssetPlan:
    asset_kind: str
    identity_key: str
    literal_text: str
    greeting_id: int | None
    direction: str | None
    content_hash: str
    cache_key: str
    render_fingerprint: str


class PhoneAudioCacheService:
    """Two-phase cache builder with an injected, provider-exact MP3 renderer.

    Network/provider work happens while a revision remains ``pending``.  The
    final database transaction first revalidates every ready row and private
    file, then switches the active revision (and optionally canonical voice).
    Any failure marks only the pending revision failed; the previous active
    revision and voice are left untouched.
    """

    def __init__(
        self,
        *,
        renderer: Mp3Renderer,
        cache_root: str | Path = DEFAULT_PRIVATE_CACHE_ROOT,
    ) -> None:
        self._renderer = renderer
        self._cache_root = Path(cache_root).resolve()

    async def generate_and_activate(
        self,
        conn: Any,
        plan: AudioCacheActivationPlan,
        *,
        activation_id: str | None = None,
    ) -> AudioCacheActivationResult:
        normalized = _validate_plan(plan)
        identifier = activation_id or uuid.uuid4().hex
        if not identifier or len(identifier) > 128:
            raise PhoneGreetingConfigurationError("activation identity is invalid")
        assets = _build_asset_plans(normalized)

        generated_paths: list[Path] = []
        begun = False
        try:
            await self._begin_activation_owned(conn, normalized, identifier)
            begun = True
            for asset in assets:
                paths = await self._render_asset(
                    conn, normalized, asset, activation_id=identifier
                )
                generated_paths.extend(paths)
            await self._activate(conn, normalized, identifier, assets)
        except asyncio.CancelledError:
            await _await_non_abandonable(
                self._fail_builds(
                    conn,
                    ((normalized, identifier),) if begun else (),
                    generated_paths,
                    "phone audio cache generation was canceled",
                )
            )
            raise
        except Exception as exc:
            await _await_non_abandonable(
                self._fail_builds(
                    conn,
                    ((normalized, identifier),) if begun else (),
                    generated_paths,
                    str(exc),
                )
            )
            if not begun:
                raise
            if isinstance(exc, PhoneAudioCacheBuildError):
                raise
            raise PhoneAudioCacheBuildError(
                "phone audio cache generation failed; prior revision remains active"
            ) from exc
        return AudioCacheActivationResult(
            activation_id=identifier,
            scope=normalized.scope,
            prompt_id=normalized.prompt_id,
            revision=normalized.revision,
            cache_keys=tuple(asset.cache_key for asset in assets),
        )

    async def generate_and_activate_global_bundle(
        self,
        conn: Any,
        global_plan: AudioCacheActivationPlan,
        prompt_plans: Sequence[AudioCacheActivationPlan],
        *,
        activation_id: str | None = None,
    ) -> tuple[AudioCacheActivationResult, ...]:
        """Build every globally-dependent prompt and switch them atomically.

        Global notices are consumed by every prompt, so every prompt must have
        a complete prompt-local cache in the bundle even when its voice and
        both greeting lists are explicit. Provider work is still outside the
        final transaction; only the single final switch can make the bundle
        visible.
        """

        normalized_global = _validate_plan(global_plan)
        if normalized_global.scope != "global":
            raise PhoneGreetingConfigurationError(
                "global bundle requires a global plan"
            )
        normalized_prompts = tuple(_validate_plan(item) for item in prompt_plans)
        base_id = activation_id or uuid.uuid4().hex
        if not base_id or len(base_id) > 80:
            raise PhoneGreetingConfigurationError("activation identity is invalid")
        await _assert_global_prompt_state_fences(
            conn,
            normalized_prompts,
            expect_plan_pending=False,
        )
        await _validate_global_bundle(
            conn,
            normalized_global,
            normalized_prompts,
        )
        plans = (normalized_global, *normalized_prompts)
        identities = (
            base_id,
            *(f"{base_id}:prompt:{plan.prompt_id}" for plan in normalized_prompts),
        )
        asset_sets = tuple(_build_asset_plans(plan) for plan in plans)
        begun: list[tuple[AudioCacheActivationPlan, str]] = []
        generated_paths: list[Path] = []
        try:
            for plan, identifier in zip(plans, identities, strict=True):
                await self._begin_activation_owned(
                    conn,
                    plan,
                    identifier,
                    global_bundle_target_voice_id=normalized_global.voice.id,
                )
                begun.append((plan, identifier))
            for plan, identifier, assets in zip(
                plans, identities, asset_sets, strict=True
            ):
                for asset in assets:
                    paths = await self._render_asset(
                        conn, plan, asset, activation_id=identifier
                    )
                    generated_paths.extend(paths)
            await self._activate_global_bundle(
                conn,
                normalized_global,
                normalized_prompts,
                identities,
                asset_sets,
            )
        except asyncio.CancelledError:
            await _await_non_abandonable(
                self._fail_builds(
                    conn,
                    tuple(begun),
                    generated_paths,
                    "phone audio cache bundle generation was canceled",
                )
            )
            raise
        except Exception as exc:
            await _await_non_abandonable(
                self._fail_builds(
                    conn,
                    tuple(begun),
                    generated_paths,
                    str(exc),
                )
            )
            if isinstance(exc, PhoneAudioCacheBuildError):
                raise
            raise PhoneAudioCacheBuildError(
                "phone audio cache bundle failed; prior revisions remain active"
            ) from exc
        return tuple(
            AudioCacheActivationResult(
                activation_id=identifier,
                scope=plan.scope,
                prompt_id=plan.prompt_id,
                revision=plan.revision,
                cache_keys=tuple(asset.cache_key for asset in assets),
            )
            for plan, identifier, assets in zip(
                plans,
                identities,
                asset_sets,
                strict=True,
            )
        )

    async def recover_stale_activations(
        self,
        conn: Any,
        *,
        created_before: datetime,
        protected_activation_ids: Sequence[str],
    ) -> AudioCacheRecoveryResult:
        """Cancel old orphaned builds without guessing whether a worker is live.

        The caller supplies both a strict age cutoff and every locally active
        activation identity.  This method never activates a revision and never
        touches the current active revision, even if its bookkeeping is stale.
        It is intended for controlled startup/admin recovery, not a timer.
        """

        cutoff = _validated_recovery_cutoff(created_before)
        protected = _validated_activation_identities(protected_activation_ids)
        removed_paths: list[Path] = []
        canceled: list[str] = []
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                """
                SELECT id,scope,prompt_id,audio_revision,created_at
                FROM VOICE_CANONICAL_ACTIVATIONS
                WHERE status IN ('pending','ready') AND created_at < ?
                ORDER BY created_at,id
                """,
                (cutoff,),
            )
            candidates = [dict(row) for row in await cursor.fetchall()]
            global_active = await _global_active_revision_in_transaction(conn)
            for row in candidates:
                identifier = str(row["id"])
                if identifier in protected:
                    continue
                revision = int(row["audio_revision"])
                prompt_id = row["prompt_id"]
                if row["scope"] == "global":
                    if global_active == revision:
                        continue
                elif (
                    await _prompt_active_revision_in_transaction(conn, prompt_id)
                    == revision
                ):
                    continue
                path_cursor = await conn.execute(
                    """
                    SELECT source_mp3_path,pcmu_path,content_hash,voice_id
                    FROM PHONE_PROMPT_AUDIO_CACHE
                    WHERE prompt_id IS ? AND revision=? AND status IN ('pending','ready')
                    """,
                    (prompt_id, revision),
                )
                for cache_row in await path_cursor.fetchall():
                    for key in ("source_mp3_path", "pcmu_path"):
                        if cache_row[key]:
                            path = Path(str(cache_row[key])).resolve()
                            if self._cache_root in path.parents:
                                removed_paths.append(path)
                    cache_directory = (
                        self._cache_root
                        / (
                            "global"
                            if prompt_id is None
                            else f"prompt-{int(prompt_id)}"
                        )
                        / f"voice-{int(cache_row['voice_id'])}"
                        / f"revision-{revision}"
                    )
                    digest = str(cache_row["content_hash"])
                    removed_paths.extend(
                        (
                            cache_directory / f"{digest}.mp3",
                            cache_directory / f"{digest}.mulaw",
                        )
                    )
                update = await conn.execute(
                    """
                    UPDATE VOICE_CANONICAL_ACTIVATIONS
                    SET status='canceled',last_error=?
                    WHERE id=? AND status IN ('pending','ready') AND created_at < ?
                    """,
                    ("stale activation recovered explicitly", identifier, cutoff),
                )
                if update.rowcount != 1:
                    continue
                await conn.execute(
                    """
                    UPDATE PHONE_PROMPT_AUDIO_CACHE
                    SET status='failed',last_error=?,source_mp3_path=NULL,pcmu_path=NULL
                    WHERE prompt_id IS ? AND revision=? AND status IN ('pending','ready')
                    """,
                    (
                        "stale activation recovered explicitly",
                        prompt_id,
                        revision,
                    ),
                )
                if row["scope"] == "prompt":
                    await conn.execute(
                        """
                        UPDATE PROMPT_PHONE_SETTINGS
                        SET pending_audio_revision=NULL,audio_cache_status='failed',
                            updated_at=CURRENT_TIMESTAMP
                        WHERE prompt_id=? AND pending_audio_revision=?
                        """,
                        (prompt_id, revision),
                    )
                canceled.append(identifier)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        await asyncio.to_thread(_remove_paths, removed_paths)
        return AudioCacheRecoveryResult(tuple(canceled))

    async def _begin_activation_owned(
        self,
        conn: Any,
        plan: AudioCacheActivationPlan,
        activation_id: str,
        *,
        global_bundle_target_voice_id: int | None = None,
    ) -> None:
        """Finish ownership acquisition before honoring task cancellation."""

        begin_task = asyncio.create_task(
            self._begin_activation(
                conn,
                plan,
                activation_id,
                global_bundle_target_voice_id=global_bundle_target_voice_id,
            )
        )
        try:
            await asyncio.shield(begin_task)
        except asyncio.CancelledError:
            acquired = False
            try:
                await _await_non_abandonable(begin_task)
                acquired = True
            except Exception:
                pass
            if acquired:
                await _await_non_abandonable(
                    self._mark_failed(
                        conn,
                        plan,
                        activation_id,
                        "phone audio cache generation was canceled",
                    )
                )
            raise

    async def _begin_activation(
        self,
        conn: Any,
        plan: AudioCacheActivationPlan,
        activation_id: str,
        *,
        global_bundle_target_voice_id: int | None = None,
    ) -> None:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if plan.scope == "global" and global_bundle_target_voice_id is None:
                dependent_ids = await _global_dependent_prompt_ids(conn)
                if dependent_ids:
                    raise PhoneGreetingConfigurationError(
                        "global cache activation requires a complete dependent-prompt bundle"
                    )
            cursor = await conn.execute(
                """
                SELECT COUNT(*) AS count FROM PHONE_PROMPT_AUDIO_CACHE
                WHERE prompt_id IS ? AND revision=?
                """,
                (plan.prompt_id, plan.revision),
            )
            if int((await cursor.fetchone())["count"]) != 0:
                raise PhoneGreetingConfigurationError(
                    "audio revision already exists and is immutable"
                )
            if plan.scope == "prompt":
                cursor = await conn.execute(
                    """
                    SELECT active_audio_revision,pending_audio_revision
                    FROM PROMPT_PHONE_SETTINGS WHERE prompt_id=?
                    """,
                    (plan.prompt_id,),
                )
                settings = await cursor.fetchone()
                pending_revision = (
                    None if settings is None else settings["pending_audio_revision"]
                )
                if plan.enforce_prompt_state_fence:
                    active_revision = (
                        None
                        if settings is None or settings["active_audio_revision"] is None
                        else int(settings["active_audio_revision"])
                    )
                    if (
                        active_revision != plan.expected_active_revision
                        or pending_revision is not None
                    ):
                        raise PhoneGreetingConfigurationError(
                            "dependent prompt state changed during global audio publication"
                        )
                if (
                    pending_revision is not None
                    and int(pending_revision) != plan.revision
                ):
                    raise PhoneGreetingConfigurationError(
                        "another phone audio activation is already pending"
                    )
            current_voice_id = await _current_voice_id(conn, plan.scope, plan.prompt_id)
            if not plan.commit_voice_change and current_voice_id != plan.voice.id:
                inherited_target = False
                if (
                    plan.scope == "prompt"
                    and global_bundle_target_voice_id == plan.voice.id
                ):
                    cursor = await conn.execute(
                        "SELECT voice_id FROM PROMPTS WHERE id=?",
                        (plan.prompt_id,),
                    )
                    prompt_row = await cursor.fetchone()
                    inherited_target = (
                        prompt_row is not None and prompt_row["voice_id"] is None
                    )
                if not inherited_target:
                    raise PhoneGreetingConfigurationError(
                        "cache voice is not the active canonical voice"
                    )
            await conn.execute(
                """
                INSERT INTO VOICE_CANONICAL_ACTIVATIONS(
                    id,scope,prompt_id,current_voice_id,target_voice_id,
                    audio_revision,status
                ) VALUES(?,?,?,?,?,?,'pending')
                """,
                (
                    activation_id,
                    plan.scope,
                    plan.prompt_id,
                    current_voice_id,
                    plan.voice.id,
                    plan.revision,
                ),
            )
            if plan.scope == "prompt":
                await conn.execute(
                    """
                    INSERT INTO PROMPT_PHONE_SETTINGS(
                        prompt_id,pending_audio_revision,audio_cache_status,updated_at
                    ) VALUES(?,?,'pending',CURRENT_TIMESTAMP)
                    ON CONFLICT(prompt_id) DO UPDATE SET
                        pending_audio_revision=excluded.pending_audio_revision,
                        audio_cache_status='pending',updated_at=CURRENT_TIMESTAMP
                    """,
                    (plan.prompt_id, plan.revision),
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    async def _render_asset(
        self,
        conn: Any,
        plan: AudioCacheActivationPlan,
        asset: _AssetPlan,
        *,
        activation_id: str,
    ) -> tuple[Path, Path]:
        root = self._cache_root
        await asyncio.to_thread(_ensure_private_directory, root)
        target_directory = (
            root
            / ("global" if plan.prompt_id is None else f"prompt-{plan.prompt_id}")
            / f"voice-{plan.voice.id}"
            / f"revision-{plan.revision}"
        )
        await asyncio.to_thread(_ensure_private_directory, target_directory)
        mp3_path = target_directory / f"{asset.content_hash}.mp3"
        pcmu_path = target_directory / f"{asset.content_hash}.mulaw"

        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                """
                INSERT INTO PHONE_PROMPT_AUDIO_CACHE(
                    cache_key,prompt_id,greeting_id,asset_kind,direction,
                    literal_text,revision,voice_id,provider_key,provider_voice_id,
                    tts_profile_json,content_hash,status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
                """,
                (
                    asset.cache_key,
                    plan.prompt_id,
                    asset.greeting_id,
                    asset.asset_kind,
                    asset.direction,
                    asset.literal_text,
                    plan.revision,
                    plan.voice.id,
                    plan.voice.provider,
                    plan.voice.voice_code,
                    serialize_tts_profile(plan.profile),
                    asset.content_hash,
                ),
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            await asyncio.to_thread(_remove_paths, (mp3_path, pcmu_path))
            raise

        try:
            rendered = await self._renderer(
                literal_text=asset.literal_text,
                voice=plan.voice,
                profile=plan.profile,
                billing_user_id=plan.billing_user_id,
                activation_id=activation_id,
                cache_key=asset.cache_key,
                render_fingerprint=asset.render_fingerprint,
            )
            await _run_file_operation(
                _materialize_private_mp3, rendered.path, mp3_path
            )
            pcmu = await _run_file_operation(
                materialize_pcmu_cache, mp3_path, pcmu_path
            )
            duration_ms = pcmu_duration_ceiling_ms(pcmu.byte_length)
            if duration_ms <= 0:
                raise PhoneAudioCacheBuildError("rendered phone audio has no duration")
            alignment = validate_phone_text_alignment(
                rendered.alignment,
                literal_text=asset.literal_text,
                duration_ms=duration_ms,
            )
            alignment_json = alignment.as_json()
        except BaseException as exc:
            await _await_non_abandonable(
                self._cleanup_failed_render(
                    conn,
                    asset=asset,
                    paths=(mp3_path, pcmu_path),
                    error=exc,
                )
            )
            raise

        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                """
                UPDATE PHONE_PROMPT_AUDIO_CACHE
                SET source_mp3_path=?,pcmu_path=?,duration_ms=?,alignment_json=?,
                    status='ready',last_error=NULL,ready_at=?
                WHERE cache_key=? AND status='pending'
                """,
                (
                    str(mp3_path),
                    str(pcmu_path),
                    duration_ms,
                    alignment_json,
                    _utc_now(),
                    asset.cache_key,
                ),
            )
            if cursor.rowcount != 1:
                raise PhoneAudioCacheBuildError("pending phone cache row changed")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            await asyncio.to_thread(_remove_paths, (mp3_path, pcmu_path))
            raise
        return mp3_path, pcmu_path

    async def _cleanup_failed_render(
        self,
        conn: Any,
        *,
        asset: _AssetPlan,
        paths: Sequence[Path],
        error: BaseException,
    ) -> None:
        await asyncio.to_thread(_remove_paths, paths)
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                """
                UPDATE PHONE_PROMPT_AUDIO_CACHE
                SET status='failed',last_error=?,source_mp3_path=NULL,pcmu_path=NULL
                WHERE cache_key=? AND status='pending'
                """,
                (_bounded_error(error), asset.cache_key),
            )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    async def _activate(
        self,
        conn: Any,
        plan: AudioCacheActivationPlan,
        activation_id: str,
        assets: Sequence[_AssetPlan],
    ) -> None:
        # File checks happen immediately before the switching transaction.
        cursor = await conn.execute(
            """
            SELECT cache_key,source_mp3_path,pcmu_path,status,literal_text,
                   duration_ms,alignment_json
            FROM PHONE_PROMPT_AUDIO_CACHE
            WHERE prompt_id IS ? AND revision=? ORDER BY cache_key
            """,
            (plan.prompt_id, plan.revision),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        expected_keys = sorted(asset.cache_key for asset in assets)
        if [str(row["cache_key"]) for row in rows] != expected_keys:
            raise PhoneAudioCacheBuildError("phone cache revision is incomplete")
        if any(str(row["status"]) != "ready" for row in rows):
            raise PhoneAudioCacheBuildError("phone cache revision is not ready")
        for row in rows:
            _validate_ready_cache_row(row, self._cache_root)

        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                """
                SELECT status,current_voice_id,target_voice_id,audio_revision
                FROM VOICE_CANONICAL_ACTIVATIONS
                WHERE id=? AND scope=? AND prompt_id IS ?
                """,
                (activation_id, plan.scope, plan.prompt_id),
            )
            row = await cursor.fetchone()
            current_voice_id = await _current_voice_id(
                conn,
                plan.scope,
                plan.prompt_id,
            )
            if (
                row is None
                or row["status"] != "pending"
                or (
                    None
                    if row["current_voice_id"] is None
                    else int(row["current_voice_id"])
                )
                != current_voice_id
                or int(row["target_voice_id"]) != plan.voice.id
                or int(row["audio_revision"]) != plan.revision
            ):
                raise PhoneAudioCacheBuildError("phone cache activation was superseded")
            await _assert_plan_voice_unchanged(conn, plan.voice)

            cursor = await conn.execute(
                """
                SELECT cache_key,source_mp3_path,pcmu_path,status,literal_text,
                       duration_ms,alignment_json
                FROM PHONE_PROMPT_AUDIO_CACHE
                WHERE prompt_id IS ? AND revision=? ORDER BY cache_key
                """,
                (plan.prompt_id, plan.revision),
            )
            transaction_rows = [dict(item) for item in await cursor.fetchall()]
            if [str(item["cache_key"]) for item in transaction_rows] != expected_keys:
                raise PhoneAudioCacheBuildError("phone cache activation is incomplete")
            if any(str(item["status"]) != "ready" for item in transaction_rows):
                raise PhoneAudioCacheBuildError("phone cache activation is incomplete")
            for item in transaction_rows:
                _validate_ready_cache_row(item, self._cache_root)

            if plan.scope == "global" and await _global_dependent_prompt_ids(conn):
                raise PhoneAudioCacheBuildError(
                    "global cache dependencies changed; a complete bundle is required"
                )
            if plan.commit_voice_change:
                await _activate_canonical_voice(conn, plan)
            if plan.scope == "prompt":
                cursor = await conn.execute(
                    """
                    UPDATE PROMPT_PHONE_SETTINGS
                    SET inbound_greeting_mode=?,outbound_greeting_mode=?,
                        active_audio_revision=?,pending_audio_revision=NULL,
                        audio_cache_status='ready',updated_at=CURRENT_TIMESTAMP
                    WHERE prompt_id=? AND pending_audio_revision=?
                    """,
                    (
                        plan.greeting_modes["inbound"],
                        plan.greeting_modes["outbound"],
                        plan.revision,
                        plan.prompt_id,
                        plan.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PhoneAudioCacheBuildError(
                        "pending prompt cache was superseded"
                    )
            else:
                await conn.execute(
                    """
                    INSERT INTO SYSTEM_CONFIG(key,value) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (GLOBAL_AUDIO_REVISION_CONFIG_KEY, str(plan.revision)),
                )
            cursor = await conn.execute(
                """
                UPDATE VOICE_CANONICAL_ACTIVATIONS
                SET status='activated',ready_at=?,activated_at=?,last_error=NULL
                WHERE id=? AND status='pending'
                """,
                (_utc_now(), _utc_now(), activation_id),
            )
            if cursor.rowcount != 1:
                raise PhoneAudioCacheBuildError("phone cache activation was superseded")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    async def _activate_global_bundle(
        self,
        conn: Any,
        global_plan: AudioCacheActivationPlan,
        prompt_plans: Sequence[AudioCacheActivationPlan],
        activation_ids: Sequence[str],
        asset_sets: Sequence[Sequence[_AssetPlan]],
    ) -> None:
        plans = (global_plan, *prompt_plans)
        for plan, assets in zip(plans, asset_sets, strict=True):
            await _assert_plan_assets_ready(
                conn,
                plan,
                assets,
                cache_root=self._cache_root,
            )

        await conn.execute("BEGIN IMMEDIATE")
        try:
            await _validate_global_bundle(conn, global_plan, tuple(prompt_plans))
            active_global_revision = await _global_active_revision_in_transaction(conn)
            if (
                active_global_revision is not None
                and global_plan.revision <= active_global_revision
            ):
                raise PhoneAudioCacheBuildError(
                    "phone cache bundle activation was superseded"
                )
            await _assert_global_prompt_state_fences(
                conn,
                tuple(prompt_plans),
                expect_plan_pending=True,
                error_type=PhoneAudioCacheBuildError,
            )
            for plan, identifier, assets in zip(
                plans,
                activation_ids,
                asset_sets,
                strict=True,
            ):
                cursor = await conn.execute(
                    """
                    SELECT status,current_voice_id,target_voice_id,audio_revision
                    FROM VOICE_CANONICAL_ACTIVATIONS
                    WHERE id=? AND scope=? AND prompt_id IS ?
                    """,
                    (identifier, plan.scope, plan.prompt_id),
                )
                activation = await cursor.fetchone()
                current_voice_id = await _current_voice_id(
                    conn,
                    plan.scope,
                    plan.prompt_id,
                )
                if (
                    activation is None
                    or activation["status"] != "pending"
                    or (
                        None
                        if activation["current_voice_id"] is None
                        else int(activation["current_voice_id"])
                    )
                    != current_voice_id
                    or int(activation["target_voice_id"]) != plan.voice.id
                    or int(activation["audio_revision"]) != plan.revision
                ):
                    raise PhoneAudioCacheBuildError(
                        "phone cache bundle activation was superseded"
                    )
                await _assert_plan_voice_unchanged(conn, plan.voice)
                expected_keys = sorted(asset.cache_key for asset in assets)
                cursor = await conn.execute(
                    """
                    SELECT cache_key,source_mp3_path,pcmu_path,status,literal_text,
                           duration_ms,alignment_json
                    FROM PHONE_PROMPT_AUDIO_CACHE
                    WHERE prompt_id IS ? AND revision=? ORDER BY cache_key
                    """,
                    (plan.prompt_id, plan.revision),
                )
                rows = [dict(row) for row in await cursor.fetchall()]
                if [str(row["cache_key"]) for row in rows] != expected_keys or any(
                    str(row["status"]) != "ready" for row in rows
                ):
                    raise PhoneAudioCacheBuildError("phone cache bundle is incomplete")
                for cache_row in rows:
                    _validate_ready_cache_row(cache_row, self._cache_root)

            if global_plan.commit_voice_change:
                await _activate_canonical_voice(conn, global_plan)
            for plan in prompt_plans:
                cursor = await conn.execute(
                    """
                    UPDATE PROMPT_PHONE_SETTINGS
                    SET inbound_greeting_mode=?,outbound_greeting_mode=?,
                        active_audio_revision=?,pending_audio_revision=NULL,
                        audio_cache_status='ready',updated_at=CURRENT_TIMESTAMP
                    WHERE prompt_id=? AND pending_audio_revision=?
                    """,
                    (
                        plan.greeting_modes["inbound"],
                        plan.greeting_modes["outbound"],
                        plan.revision,
                        plan.prompt_id,
                        plan.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PhoneAudioCacheBuildError(
                        "dependent prompt cache was superseded"
                    )
            await conn.execute(
                """
                INSERT INTO SYSTEM_CONFIG(key,value) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (GLOBAL_AUDIO_REVISION_CONFIG_KEY, str(global_plan.revision)),
            )
            now = _utc_now()
            for identifier in activation_ids:
                cursor = await conn.execute(
                    """
                    UPDATE VOICE_CANONICAL_ACTIVATIONS
                    SET status='activated',ready_at=?,activated_at=?,last_error=NULL
                    WHERE id=? AND status='pending'
                    """,
                    (now, now, identifier),
                )
                if cursor.rowcount != 1:
                    raise PhoneAudioCacheBuildError(
                        "phone cache bundle activation was superseded"
                    )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise

    async def _mark_failed(
        self,
        conn: Any,
        plan: AudioCacheActivationPlan,
        activation_id: str,
        error: str,
    ) -> bool:
        try:
            await conn.execute("BEGIN IMMEDIATE")
            activation = await conn.execute(
                """
                UPDATE VOICE_CANONICAL_ACTIVATIONS
                SET status='failed',last_error=?
                WHERE id=? AND status IN ('pending','ready')
                """,
                (_bounded_error(error), activation_id),
            )
            if activation.rowcount != 1:
                await conn.rollback()
                return False
            cursor = await conn.execute(
                """
                SELECT 1 FROM VOICE_CANONICAL_ACTIVATIONS
                WHERE id<>? AND scope=? AND prompt_id IS ? AND audio_revision=?
                  AND status IN ('pending','ready','activated')
                LIMIT 1
                """,
                (
                    activation_id,
                    plan.scope,
                    plan.prompt_id,
                    plan.revision,
                ),
            )
            if await cursor.fetchone() is not None:
                # The failed identity does not own revision-scoped cache rows.
                # Preserve the other activation and its files fail-closed.
                await conn.commit()
                return False
            if plan.scope == "prompt":
                await conn.execute(
                    """
                    UPDATE PROMPT_PHONE_SETTINGS
                    SET pending_audio_revision=NULL,audio_cache_status='failed',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE prompt_id=? AND pending_audio_revision=?
                    """,
                    (plan.prompt_id, plan.revision),
                )
            await conn.execute(
                """
                UPDATE PHONE_PROMPT_AUDIO_CACHE
                SET status='failed',last_error=?,source_mp3_path=NULL,pcmu_path=NULL
                WHERE prompt_id IS ? AND revision=? AND status IN ('pending','ready')
                """,
                (_bounded_error(error), plan.prompt_id, plan.revision),
            )
            await conn.commit()
            return True
        except BaseException:
            await conn.rollback()
            raise

    async def _fail_builds(
        self,
        conn: Any,
        builds: Sequence[tuple[AudioCacheActivationPlan, str]],
        generated_paths: Sequence[Path],
        error: str,
    ) -> None:
        marked: list[bool] = []
        for plan, identifier in reversed(tuple(builds)):
            marked.append(await self._mark_failed(conn, plan, identifier, error))
        if not builds or all(marked):
            await asyncio.to_thread(_remove_paths, generated_paths)


def build_audio_render_fingerprint(
    *,
    literal_text: str,
    voice: CanonicalVoice,
    tts_profile_json: str,
    audio_format: str,
    aligner_version: str = PHONE_AUDIO_ALIGNER_VERSION,
) -> str:
    """Identify reusable provider work without cache revision semantics."""

    literal = normalize_literal_text(literal_text)
    normalized_format = str(audio_format or "").strip()
    normalized_aligner = str(aligner_version or "").strip()
    if not normalized_format or not normalized_aligner:
        raise PhoneGreetingConfigurationError(
            "audio format and aligner version are required"
        )
    if int(voice.id) <= 0 or not voice.provider or not voice.voice_code:
        raise PhoneGreetingConfigurationError("canonical voice is invalid")
    try:
        profile_value = json.loads(str(tts_profile_json))
    except (TypeError, ValueError) as exc:
        raise PhoneGreetingConfigurationError("TTS profile is invalid") from exc
    if not isinstance(profile_value, dict):
        raise PhoneGreetingConfigurationError("TTS profile is invalid")
    if str(voice.provider).strip().lower() == "openai":
        # OpenAI's existing canonical TTS path uses tts-1 and accepts only the
        # MP3 response format here.  ElevenLabs-only stability/chunk settings
        # are not provider inputs and must not define OpenAI artifact reuse.
        profile_value = {
            "model_id": PHONE_CACHE_OPENAI_MODEL,
            "response_format": "mp3",
        }
    canonical = json.dumps(
        {
            "literal_text": literal,
            "voice": {
                "provider": str(voice.provider),
                "provider_voice_id": str(voice.voice_code),
            },
            "tts_profile": profile_value,
            "audio_format": normalized_format,
            "aligner_version": normalized_aligner,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_plan(plan: AudioCacheActivationPlan) -> AudioCacheActivationPlan:
    scope = str(plan.scope or "").strip().lower()
    if scope == "global":
        if plan.prompt_id is not None:
            raise PhoneGreetingConfigurationError("global cache cannot have a prompt")
        required_notices = GLOBAL_TECHNICAL_NOTICE_KEYS
    elif scope == "prompt":
        if plan.prompt_id is None or int(plan.prompt_id) <= 0:
            raise PhoneGreetingConfigurationError("prompt cache requires a prompt")
        required_notices = PROMPT_TECHNICAL_NOTICE_KEYS
    else:
        raise PhoneGreetingConfigurationError("audio cache scope is invalid")
    if int(plan.revision) <= 0:
        raise PhoneGreetingConfigurationError("audio revision must be positive")
    if int(plan.voice.id) <= 0 or not plan.voice.voice_code or not plan.voice.provider:
        raise PhoneGreetingConfigurationError("canonical voice is invalid")
    if str(plan.profile.output_format) != PHONE_CACHE_MP3_FORMAT:
        raise PhoneGreetingConfigurationError(
            "phone cache renderer requires MP3 output"
        )
    normalized_profile = normalize_phone_cache_tts_profile(plan.profile)

    notices = {
        normalize_notice_key(key): normalize_literal_text(text)
        for key, text in plan.technical_notices.items()
    }
    if set(notices) != set(required_notices):
        missing = sorted(set(required_notices) - set(notices))
        extra = sorted(set(notices) - set(required_notices))
        raise PhoneGreetingConfigurationError(
            f"technical notice set is incomplete (missing={missing}, extra={extra})"
        )

    enabled_greetings = tuple(item for item in plan.greetings if item.enabled)
    inferred_modes: dict[str, str] = {}
    supplied_modes = dict(plan.greeting_modes or {})
    greetings_list: list[GreetingDefinition] = []
    for direction in sorted(GREETING_DIRECTIONS):
        direction_items = [
            item for item in enabled_greetings if item.direction == direction
        ]
        fixed_items = [item for item in direction_items if item.fixed]
        if len(fixed_items) > 1:
            raise PhoneGreetingConfigurationError(
                "fixed greeting selection is ambiguous"
            )
        inferred_mode = (
            "inherit"
            if scope == "prompt"
            and direction_items
            and all(item.scope == "global" for item in direction_items)
            else ("fixed" if fixed_items else "random")
        )
        mode = str(supplied_modes.get(direction, inferred_mode)).strip().lower()
        allowed_modes = {"fixed", "random"} | (
            {"inherit"} if scope == "prompt" else set()
        )
        if mode not in allowed_modes:
            raise PhoneGreetingConfigurationError("greeting activation mode is invalid")
        if mode == "inherit":
            if not direction_items or any(
                item.scope != "global" for item in direction_items
            ):
                raise PhoneGreetingConfigurationError(
                    "inherited greeting list is invalid"
                )
            greetings_list.extend(fixed_items or direction_items)
        elif mode == "fixed":
            if len(fixed_items) != 1 or fixed_items[0].scope != scope:
                raise PhoneGreetingConfigurationError(
                    "fixed greeting selection is invalid"
                )
            greetings_list.extend(fixed_items)
        else:
            if (
                not direction_items
                or fixed_items
                or any(item.scope != scope for item in direction_items)
            ):
                raise PhoneGreetingConfigurationError(
                    "random greeting selection is invalid"
                )
            greetings_list.extend(direction_items)
        inferred_modes[direction] = mode
    if set(supplied_modes) - set(GREETING_DIRECTIONS):
        raise PhoneGreetingConfigurationError("unknown greeting activation direction")
    greetings = tuple(greetings_list)
    if not greetings:
        raise PhoneGreetingConfigurationError(
            "audio activation has no enabled greetings"
        )
    if {item.direction for item in greetings} != set(GREETING_DIRECTIONS):
        raise PhoneGreetingConfigurationError(
            "audio activation requires inbound and outbound greetings"
        )
    allowed_scopes = {"global"} if scope == "global" else {"global", "prompt"}
    if any(item.scope not in allowed_scopes for item in greetings):
        raise PhoneGreetingConfigurationError(
            "greeting scope does not match activation"
        )
    if any(item.prompt_id not in {None, plan.prompt_id} for item in greetings):
        raise PhoneGreetingConfigurationError("greeting belongs to another prompt")
    if len({item.id for item in greetings}) != len(greetings):
        raise PhoneGreetingConfigurationError("audio activation repeats a greeting")
    for item in greetings:
        normalize_literal_text(item.literal_text)

    return AudioCacheActivationPlan(
        scope=scope,
        prompt_id=None if plan.prompt_id is None else int(plan.prompt_id),
        revision=int(plan.revision),
        voice=plan.voice,
        profile=normalized_profile,
        greetings=greetings,
        technical_notices=notices,
        greeting_modes=inferred_modes,
        commit_voice_change=bool(plan.commit_voice_change),
        expected_active_revision=_normalize_expected_active_revision(
            plan.expected_active_revision,
            enabled=bool(plan.enforce_prompt_state_fence),
            scope=scope,
        ),
        enforce_prompt_state_fence=bool(plan.enforce_prompt_state_fence),
        billing_user_id=_positive_billing_user_id(plan.billing_user_id),
    )


def _build_asset_plans(plan: AudioCacheActivationPlan) -> tuple[_AssetPlan, ...]:
    profile_json = serialize_tts_profile(plan.profile)
    assets: list[_AssetPlan] = []
    for greeting in plan.greetings:
        identity = str(greeting.id)
        digest = build_audio_content_hash(
            asset_kind="greeting",
            identity_key=identity,
            literal_text=greeting.literal_text,
            revision=plan.revision,
            voice=plan.voice,
            tts_profile_json=profile_json,
        )
        assets.append(
            _AssetPlan(
                asset_kind="greeting",
                identity_key=identity,
                literal_text=greeting.literal_text,
                greeting_id=greeting.id,
                direction=greeting.direction,
                content_hash=digest,
                cache_key=build_cache_key(
                    prompt_id=plan.prompt_id,
                    revision=plan.revision,
                    asset_kind="greeting",
                    identity_key=identity,
                    content_hash=digest,
                ),
                render_fingerprint=build_audio_render_fingerprint(
                    literal_text=greeting.literal_text,
                    voice=plan.voice,
                    tts_profile_json=profile_json,
                    audio_format=plan.profile.output_format,
                ),
            )
        )
    for key in sorted(plan.technical_notices):
        text = plan.technical_notices[key]
        digest = build_audio_content_hash(
            asset_kind="technical_notice",
            identity_key=key,
            literal_text=text,
            revision=plan.revision,
            voice=plan.voice,
            tts_profile_json=profile_json,
        )
        assets.append(
            _AssetPlan(
                asset_kind="technical_notice",
                identity_key=key,
                literal_text=text,
                greeting_id=None,
                direction=None,
                content_hash=digest,
                cache_key=build_cache_key(
                    prompt_id=plan.prompt_id,
                    revision=plan.revision,
                    asset_kind="technical_notice",
                    identity_key=key,
                    content_hash=digest,
                ),
                render_fingerprint=build_audio_render_fingerprint(
                    literal_text=text,
                    voice=plan.voice,
                    tts_profile_json=profile_json,
                    audio_format=plan.profile.output_format,
                ),
            )
        )
    return tuple(assets)


async def _validate_global_bundle(
    conn: Any,
    global_plan: AudioCacheActivationPlan,
    prompt_plans: Sequence[AudioCacheActivationPlan],
) -> None:
    if global_plan.scope != "global":
        raise PhoneGreetingConfigurationError("global bundle requires a global plan")
    dependent_ids = await _global_dependent_prompt_ids(conn)
    if any(plan.scope != "prompt" or plan.prompt_id is None for plan in prompt_plans):
        raise PhoneGreetingConfigurationError(
            "global cache bundle contains a non-prompt dependency"
        )
    supplied_ids = [int(plan.prompt_id) for plan in prompt_plans]
    if len(supplied_ids) != len(set(supplied_ids)):
        raise PhoneGreetingConfigurationError(
            "global cache bundle repeats a dependent prompt"
        )
    if set(supplied_ids) != set(dependent_ids):
        missing = sorted(set(dependent_ids) - set(supplied_ids))
        extra = sorted(set(supplied_ids) - set(dependent_ids))
        raise PhoneGreetingConfigurationError(
            f"global cache bundle dependencies are incomplete (missing={missing}, extra={extra})"
        )
    fenced_plans = [plan for plan in prompt_plans if plan.enforce_prompt_state_fence]
    if fenced_plans and len(fenced_plans) != len(prompt_plans):
        raise PhoneGreetingConfigurationError(
            "global cache bundle has incomplete prompt state fencing"
        )
    state_cursor = await conn.execute("SELECT id,voice_id FROM PROMPTS ORDER BY id")
    prompt_voice_ids = {
        int(row["id"]): None if row["voice_id"] is None else int(row["voice_id"])
        for row in await state_cursor.fetchall()
    }
    for plan in prompt_plans:
        if plan.commit_voice_change:
            raise PhoneGreetingConfigurationError(
                "dependent prompt voice inheritance cannot be made explicit"
            )
        configured_voice_id = prompt_voice_ids[int(plan.prompt_id)]
        expected_voice_id = (
            global_plan.voice.id if configured_voice_id is None else configured_voice_id
        )
        if plan.voice.id != expected_voice_id:
            raise PhoneGreetingConfigurationError(
                "dependent prompt cache does not use its post-activation canonical voice"
            )
        for direction in sorted(GREETING_DIRECTIONS):
            if plan.greeting_modes[direction] != "inherit":
                continue
            dependent_definitions = _greeting_signatures(plan, direction)
            global_definitions = _greeting_signatures(global_plan, direction)
            if dependent_definitions != global_definitions:
                raise PhoneGreetingConfigurationError(
                    "dependent prompt inherited greetings do not match the new global revision"
                )


async def _global_dependent_prompt_ids(conn: Any) -> tuple[int, ...]:
    cursor = await conn.execute(
        "SELECT id FROM PROMPTS ORDER BY id"
    )
    return tuple(int(row["id"]) for row in await cursor.fetchall())


async def _assert_global_prompt_state_fences(
    conn: Any,
    prompt_plans: Sequence[AudioCacheActivationPlan],
    *,
    expect_plan_pending: bool,
    error_type: type[Exception] = PhoneGreetingConfigurationError,
) -> None:
    """Fail if a staged global bundle no longer owns the prompt state snapshot."""

    for plan in prompt_plans:
        if not plan.enforce_prompt_state_fence:
            continue
        cursor = await conn.execute(
            """
            SELECT active_audio_revision,pending_audio_revision
            FROM PROMPT_PHONE_SETTINGS WHERE prompt_id=?
            """,
            (int(plan.prompt_id),),
        )
        row = await cursor.fetchone()
        active_revision = (
            None
            if row is None or row["active_audio_revision"] is None
            else int(row["active_audio_revision"])
        )
        pending_revision = (
            None
            if row is None or row["pending_audio_revision"] is None
            else int(row["pending_audio_revision"])
        )
        expected_pending_revision = plan.revision if expect_plan_pending else None
        if (
            active_revision != plan.expected_active_revision
            or pending_revision != expected_pending_revision
        ):
            raise error_type(
                "dependent prompt state changed during global audio publication"
            )


def _greeting_signatures(
    plan: AudioCacheActivationPlan,
    direction: str,
) -> tuple[tuple[Any, ...], ...]:
    definitions = [item for item in plan.greetings if item.direction == direction]
    definitions.sort(key=lambda item: (item.display_order, item.id))
    return tuple(
        (
            item.id,
            item.scope,
            item.prompt_id,
            item.direction,
            item.literal_text,
            item.enabled,
            item.fixed,
            item.display_order,
            item.definition_revision,
        )
        for item in definitions
    )


async def _assert_plan_assets_ready(
    conn: Any,
    plan: AudioCacheActivationPlan,
    assets: Sequence[_AssetPlan],
    *,
    cache_root: Path,
) -> None:
    cursor = await conn.execute(
        """
        SELECT cache_key,source_mp3_path,pcmu_path,status,literal_text,
               duration_ms,alignment_json
        FROM PHONE_PROMPT_AUDIO_CACHE
        WHERE prompt_id IS ? AND revision=? ORDER BY cache_key
        """,
        (plan.prompt_id, plan.revision),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    expected_keys = sorted(asset.cache_key for asset in assets)
    if [str(row["cache_key"]) for row in rows] != expected_keys:
        raise PhoneAudioCacheBuildError("phone cache bundle revision is incomplete")
    if any(str(row["status"]) != "ready" for row in rows):
        raise PhoneAudioCacheBuildError("phone cache bundle revision is not ready")
    for row in rows:
        _validate_ready_cache_row(row, cache_root)


async def _current_voice_id(conn: Any, scope: str, prompt_id: int | None) -> int | None:
    if scope == "prompt":
        cursor = await conn.execute(
            "SELECT voice_id FROM PROMPTS WHERE id=?", (prompt_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise PhoneGreetingConfigurationError("prompt does not exist")
        if row["voice_id"] is not None:
            return int(row["voice_id"])
    cursor = await conn.execute(
        "SELECT id FROM VOICES WHERE COALESCE(is_default,0)=1 ORDER BY id"
    )
    rows = await cursor.fetchall()
    if len(rows) != 1:
        return None
    return int(rows[0]["id"])


async def _activate_canonical_voice(conn: Any, plan: AudioCacheActivationPlan) -> None:
    if plan.scope == "prompt":
        cursor = await conn.execute(
            "UPDATE PROMPTS SET voice_id=? WHERE id=?",
            (plan.voice.id, plan.prompt_id),
        )
        if cursor.rowcount != 1:
            raise PhoneAudioCacheBuildError(
                "prompt disappeared during voice activation"
            )
    else:
        await conn.execute(
            "UPDATE VOICES SET is_default=0 WHERE COALESCE(is_default,0)=1"
        )
        await conn.execute(
            "UPDATE VOICES SET is_default=1 WHERE id=?", (plan.voice.id,)
        )


async def _assert_plan_voice_unchanged(conn: Any, voice: CanonicalVoice) -> None:
    cursor = await conn.execute(
        """
        SELECT v.voice_code,v.tts_service,
               COALESCE(v.deprecated,0) AS deprecated,
               s.name AS service_name
        FROM VOICES v
        LEFT JOIN SERVICES s ON s.id=v.tts_service
        WHERE v.id=?
        """,
        (voice.id,),
    )
    row = await cursor.fetchone()
    service_name = "" if row is None else str(row["service_name"] or "").strip()
    if (
        row is None
        or bool(row["deprecated"])
        or str(row["voice_code"] or "").strip() != voice.voice_code
        or row["tts_service"] is None
        or int(row["tts_service"]) != voice.tts_service
        or provider_from_service_name(service_name) != voice.provider
    ):
        raise PhoneAudioCacheBuildError(
            "target canonical voice changed during audio generation"
        )


def _materialize_private_mp3(source: Path, destination: Path) -> None:
    source_path = Path(source)
    if not source_path.is_file() or source_path.stat().st_size <= 3:
        raise PhoneAudioCacheBuildError("TTS renderer returned no MP3")
    with source_path.open("rb") as handle:
        signature = handle.read(3)
    if signature != b"ID3" and not (signature[:2] and signature[0] == 0xFF):
        raise PhoneAudioCacheBuildError("TTS renderer did not return MP3 audio")
    _ensure_private_directory(destination.parent)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        shutil.copyfile(source_path, temporary_path)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Windows does not implement POSIX mode bits; ACL inheritance remains
        # authoritative there.  Unix deployments get the restrictive mode.
        pass


def _require_generated_path(value: Any, root: Path) -> Path:
    if value is None:
        raise PhoneAudioCacheBuildError("generated phone cache path is missing")
    path = Path(str(value)).resolve()
    if root not in path.parents or not path.is_file() or path.stat().st_size <= 0:
        raise PhoneAudioCacheBuildError("generated phone cache path is invalid")
    return path


def _validate_ready_cache_row(row: Mapping[str, Any], root: Path) -> None:
    _require_generated_path(row["source_mp3_path"], root)
    pcmu_path = _require_generated_path(row["pcmu_path"], root)
    try:
        pcmu = describe_pcmu_cache(pcmu_path)
        actual_duration = pcmu_duration_ceiling_ms(pcmu.byte_length)
        stored_duration = row["duration_ms"]
        if (
            stored_duration is None
            or isinstance(stored_duration, bool)
            or abs(int(stored_duration) - actual_duration) > 1
        ):
            raise ValueError("duration mismatch")
        parse_phone_text_alignment(
            row["alignment_json"],
            literal_text=str(row["literal_text"]),
            duration_ms=actual_duration,
        )
    except Exception as exc:
        raise PhoneAudioCacheBuildError(
            "phone cache alignment or duration is invalid"
        ) from exc


def _remove_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


async def _run_file_operation(function: Any, *args: Any) -> Any:
    """Let an atomic file worker finish before propagating cancellation."""

    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _await_non_abandonable(task)
        except Exception:
            pass
        raise


async def _await_non_abandonable(operation: Awaitable[Any]) -> Any:
    """Finish ownership/cleanup work despite repeated caller cancellation."""

    task = asyncio.ensure_future(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            continue
        except Exception:
            break
    return task.result()


def _validated_recovery_cutoff(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PhoneGreetingConfigurationError(
            "stale activation cutoff must be timezone-aware"
        )
    normalized = value.astimezone(UTC)
    if normalized >= datetime.now(UTC):
        raise PhoneGreetingConfigurationError(
            "stale activation cutoff must be in the past"
        )
    return normalized.strftime("%Y-%m-%d %H:%M:%S")


def _validated_activation_identities(values: Sequence[str]) -> frozenset[str]:
    identities: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise PhoneGreetingConfigurationError(
                "protected activation identity is invalid"
            )
        identities.add(value)
    return frozenset(identities)


async def _global_active_revision_in_transaction(conn: Any) -> int | None:
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key=?",
        (GLOBAL_AUDIO_REVISION_CONFIG_KEY,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        value = int(row["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


async def _prompt_active_revision_in_transaction(
    conn: Any,
    prompt_id: int,
) -> int | None:
    cursor = await conn.execute(
        "SELECT active_audio_revision FROM PROMPT_PHONE_SETTINGS WHERE prompt_id=?",
        (int(prompt_id),),
    )
    row = await cursor.fetchone()
    if row is None or row["active_audio_revision"] is None:
        return None
    return int(row["active_audio_revision"])


def _bounded_error(value: Any) -> str:
    text = str(value or "phone audio cache generation failed").strip()
    return text[:2_000]


def _normalize_expected_active_revision(
    value: Any,
    *,
    enabled: bool,
    scope: str,
) -> int | None:
    if scope != "prompt":
        if enabled or value is not None:
            raise PhoneGreetingConfigurationError(
                "prompt state fencing is only valid for prompt caches"
            )
        return None
    if not enabled:
        if value is not None:
            raise PhoneGreetingConfigurationError(
                "expected prompt revision requires prompt state fencing"
            )
        return None
    if value is None:
        return None
    if isinstance(value, bool):
        raise PhoneGreetingConfigurationError(
            "expected prompt audio revision is invalid"
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PhoneGreetingConfigurationError(
            "expected prompt audio revision is invalid"
        ) from exc
    if normalized <= 0 or normalized != value:
        raise PhoneGreetingConfigurationError(
            "expected prompt audio revision is invalid"
        )
    return normalized


def _positive_billing_user_id(value: Any) -> int:
    if isinstance(value, bool):
        raise PhoneGreetingConfigurationError("billing user is invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise PhoneGreetingConfigurationError("billing user is invalid") from exc
    if normalized <= 0 or normalized != value:
        raise PhoneGreetingConfigurationError("billing user is invalid")
    return normalized


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "AudioCacheActivationPlan",
    "AudioCacheRecoveryResult",
    "AudioCacheActivationResult",
    "Mp3Renderer",
    "PHONE_AUDIO_ALIGNER_VERSION",
    "PHONE_CACHE_OPENAI_MODEL",
    "PhoneAudioCacheBuildError",
    "PhoneAudioCacheService",
    "RenderedMp3",
    "build_audio_render_fingerprint",
]
