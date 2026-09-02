"""Private, traversal-safe access and deletion for retained call audio."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
from typing import Iterable

from integrations.telephony.recording import DEFAULT_RECORDING_ROOT


_CALL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_TRACK_NAMES = frozenset({"participant.mulaw", "assistant.mulaw", "mixed.mp3"})


class PrivateRecordingPathError(ValueError):
    """A persisted recording path escaped its canonical private call directory."""


def private_call_directory(
    call_id: str,
    *,
    root: str | os.PathLike[str] = DEFAULT_RECORDING_ROOT,
) -> Path:
    normalized = str(call_id or "").strip()
    if _CALL_ID.fullmatch(normalized) is None:
        raise PrivateRecordingPathError("call_id is invalid for private audio")
    private_root = Path(root).resolve()
    return private_root / normalized[:2].lower() / normalized


def resolve_private_recording_path(
    call_id: str,
    value: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] = DEFAULT_RECORDING_ROOT,
) -> Path:
    expected_directory = private_call_directory(call_id, root=root).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = Path(root).resolve() / candidate
    resolved = candidate.resolve(strict=False)
    if resolved.parent != expected_directory or resolved.name not in _TRACK_NAMES:
        raise PrivateRecordingPathError("recording path is outside private storage")
    return resolved


def delete_private_call_audio(
    call_id: str,
    persisted_paths: Iterable[str | os.PathLike[str] | None],
    *,
    root: str | os.PathLike[str] = DEFAULT_RECORDING_ROOT,
) -> None:
    """Delete one validated call directory idempotently.

    Every non-empty persisted path must resolve to the directory derived from the
    validated call id.  The directory itself is constructed rather than accepted
    from storage, so recursive deletion cannot follow a database-controlled path.
    """

    directory = private_call_directory(call_id, root=root).resolve()
    for value in persisted_paths:
        if value:
            resolve_private_recording_path(call_id, value, root=root)
    if directory.exists():
        if not directory.is_dir() or directory.is_symlink():
            raise PrivateRecordingPathError("private recording directory is invalid")
        shutil.rmtree(directory)


__all__ = [
    "PrivateRecordingPathError",
    "delete_private_call_audio",
    "private_call_directory",
    "resolve_private_recording_path",
]
