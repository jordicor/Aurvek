"""Provider-neutral assembly of live STT results into caller turns."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Protocol


MAX_FINAL_SEGMENTS_PER_UTTERANCE = 1_000
MAX_FINAL_UTTERANCE_CHARS = 100_000


class PhoneTranscriptionError(RuntimeError):
    """Live STT produced an unsafe or internally inconsistent utterance."""


@dataclass(frozen=True, slots=True)
class FinalPhoneUtterance:
    """Only this final aggregate may enter the canonical message runtime."""

    text: str
    segment_count: int
    start_seconds: float | None
    end_seconds: float | None
    confidence: float | None
    # Legacy compatibility field. Persistent native Realtime calls use the
    # provider-owned turn_handle below and never copy caller audio here.
    input_audio_pcmu: bytes = field(default=b"", compare=False, repr=False)
    # Provider-owned native turn handle. Persistent Realtime calls attach this
    # to the final transcript so the canonical runtime reuses the same socket
    # and input item without copying caller audio into a second bridge.
    turn_handle: Any | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class _FinalSegment:
    text: str
    start_seconds: float | None
    end_seconds: float | None
    confidence: float | None
    turn_handle: Any | None = field(default=None, compare=False, repr=False)


class _TranscriptEvent(Protocol):
    text: str
    is_final: bool
    speech_final: bool


class PhoneUtteranceAssembler:
    """Accumulate ``is_final`` segments until an endpoint closes the turn.

    Provider interim hypotheses are intentionally exposed nowhere durable and
    never replace an already-final segment.  The structural event contract
    keeps this safety boundary independent from the active STT transport.
    """

    def __init__(self) -> None:
        self._segments: list[_FinalSegment] = []
        self._dedupe: set[tuple[object, ...]] = set()
        self._characters = 0

    @property
    def pending_final_text(self) -> str:
        return _join_segments(self._segments)

    @property
    def pending_segment_count(self) -> int:
        return len(self._segments)

    def feed(self, event: object) -> FinalPhoneUtterance | None:
        if _is_transcript_event(event):
            if event.is_final and event.text.strip():
                self._append(event)
            if event.speech_final:
                return self.flush()
            return None
        if _is_utterance_end_event(event):
            return self.flush()
        return None

    def flush(self) -> FinalPhoneUtterance | None:
        if not self._segments:
            self._reset()
            return None
        text = _join_segments(self._segments)
        starts = [
            segment.start_seconds
            for segment in self._segments
            if segment.start_seconds is not None
        ]
        ends = [
            segment.end_seconds
            for segment in self._segments
            if segment.end_seconds is not None
        ]
        confidences = [
            segment.confidence
            for segment in self._segments
            if segment.confidence is not None
        ]
        utterance = FinalPhoneUtterance(
            text=text,
            segment_count=len(self._segments),
            start_seconds=min(starts) if starts else None,
            end_seconds=max(ends) if ends else None,
            confidence=(
                sum(confidences) / len(confidences) if confidences else None
            ),
            turn_handle=_single_turn_handle(self._segments),
        )
        self._reset()
        return utterance

    def discard(self) -> None:
        """Forget an explicitly suppressed utterance without publishing it."""

        self._reset()

    def _append(self, event: _TranscriptEvent) -> None:
        text = " ".join(event.text.split())
        if not text:
            return
        start = _finite_nonnegative(getattr(event, "start_seconds", None))
        duration = _finite_nonnegative(getattr(event, "duration_seconds", None))
        end = start + duration if start is not None and duration is not None else None
        confidence = _bounded_confidence(getattr(event, "confidence", None))
        turn_handle = getattr(event, "turn_handle", None)
        existing_handles = [
            segment.turn_handle
            for segment in self._segments
            if segment.turn_handle is not None
        ]
        if existing_handles and turn_handle is not existing_handles[0]:
            raise PhoneTranscriptionError(
                "live STT mixed provider turn handles"
            )
        key = (text, start, duration, confidence)
        if key in self._dedupe:
            return
        if len(self._segments) >= MAX_FINAL_SEGMENTS_PER_UTTERANCE:
            raise PhoneTranscriptionError("live STT utterance has too many final segments")
        additional = len(text) + (1 if self._segments else 0)
        if self._characters + additional > MAX_FINAL_UTTERANCE_CHARS:
            raise PhoneTranscriptionError("live STT utterance exceeds its text limit")
        self._segments.append(
            _FinalSegment(
                text=text,
                start_seconds=start,
                end_seconds=end,
                confidence=confidence,
                turn_handle=turn_handle,
            )
        )
        self._dedupe.add(key)
        self._characters += additional

    def _reset(self) -> None:
        self._segments.clear()
        self._dedupe.clear()
        self._characters = 0


def _join_segments(segments: list[_FinalSegment]) -> str:
    return " ".join(segment.text for segment in segments)


def _single_turn_handle(segments: list[_FinalSegment]) -> Any | None:
    handles = [
        segment.turn_handle
        for segment in segments
        if segment.turn_handle is not None
    ]
    return handles[0] if handles else None


def _is_transcript_event(event: object) -> bool:
    return bool(
        isinstance(getattr(event, "text", None), str)
        and isinstance(getattr(event, "is_final", None), bool)
        and isinstance(getattr(event, "speech_final", None), bool)
    )


def _is_utterance_end_event(event: object) -> bool:
    # Realtime adapters expose this field only on their endpoint marker.  A
    # structural check also keeps the narrow pre-Scribe test compatibility
    # path from coupling production assembly back to an old provider module.
    return hasattr(event, "last_word_end_seconds") and not _is_transcript_event(event)


def _finite_nonnegative(value: float | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise PhoneTranscriptionError("live STT timing is invalid")
    return parsed


def _bounded_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise PhoneTranscriptionError("live STT confidence is invalid")
    return parsed


DeepgramUtteranceAssembler = PhoneUtteranceAssembler
"""Deprecated source-compatible alias; production uses the generic name."""


__all__ = [
    "DeepgramUtteranceAssembler",
    "FinalPhoneUtterance",
    "PhoneUtteranceAssembler",
    "PhoneTranscriptionError",
]
