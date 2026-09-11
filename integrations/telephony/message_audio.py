"""Per-message ranges and browser playback for retained phone audio.

Telephone calls already keep one exact, call-aligned 8 kHz PCMU track for
each participant when private audio retention is enabled.  A message therefore
needs only a bounded byte range into the appropriate track; copying every turn
to another file would double storage and complicate deletion.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import struct
from typing import Any, BinaryIO

from integrations.telephony.audio import PCMU_SAMPLE_RATE_HZ


_WAV_HEADER_BYTES = 44
_PCM16_BYTES_PER_SAMPLE = 2
_READ_CHUNK_BYTES = 64 * 1024
_MAX_PCMU_TRACK_BYTES = 24 * 60 * 60 * PCMU_SAMPLE_RATE_HZ


@dataclass(frozen=True, slots=True)
class PhoneMessageAudioRange:
    """Half-open byte range in a call-aligned raw PCMU track."""

    start_byte: int
    end_byte: int

    def __post_init__(self) -> None:
        for name, value in (
            ("start_byte", self.start_byte),
            ("end_byte", self.end_byte),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if (
            self.start_byte < 0
            or self.end_byte <= self.start_byte
            or self.end_byte > _MAX_PCMU_TRACK_BYTES
        ):
            raise ValueError("phone message audio range is invalid")

    @property
    def byte_length(self) -> int:
        return self.end_byte - self.start_byte


def live_transcript_audio_range(
    fragments: list[dict[str, Any]],
    *,
    timeline_offset_ms: int,
    track_bytes: int,
) -> PhoneMessageAudioRange | None:
    """Return a replay window, not a word-level delivery confirmation.

    Live transcript intervals use the session timeline, while our retained
    tracks use the call timeline. Include context on either side for native
    transcript timing and output buffering, and never reference bytes beyond
    the finalized recording. Overlapping replay windows are intentional:
    splitting at a transcript boundary can cut off speech in this duplex API.
    No audio is synthesized and no played_ms/confirmed_text is inferred here.
    """

    intervals = [
        (part["start_ms"], part["end_ms"])
        for part in fragments
        if isinstance(part.get("start_ms"), int)
        and isinstance(part.get("end_ms"), int)
        and 0 <= part["start_ms"] < part["end_ms"]
    ]
    if not intervals or track_bytes <= 0:
        return None
    start_ms = min(start for start, _ in intervals)
    end_ms = max(end for _, end in intervals)
    start_byte = max(timeline_offset_ms, timeline_offset_ms + start_ms - 2000) * 8
    end_byte = min(track_bytes, (timeline_offset_ms + end_ms + 3500) * 8)
    if end_byte <= start_byte:
        return None
    return PhoneMessageAudioRange(start_byte, end_byte)


async def persist_live_message_audio_in_transaction(
    conn: Any,
    *,
    call_id: str,
    stream_attempt: int,
    timeline_offset_ms: int,
    track_bytes: dict[str, int],
) -> int:
    """Link saved Live messages to the existing, finalized participant tracks.

    The caller owns the transaction and verifies the recording/call scope.
    The shared range writer below preserves deletion tombstones and checks
    each parent link. Existing ranges, including the cached greeting, stay put.
    """

    import json

    rows = await (await conn.execute(
        "SELECT t.message_id,t.participant,t.fragments_json "
        "FROM PHONE_LIVE_TRANSCRIPTS t "
        "JOIN PHONE_CALL_MESSAGE_LINKS l ON l.call_id=t.call_id AND l.message_id=t.message_id "
        "LEFT JOIN PHONE_CALL_MESSAGE_AUDIO_RANGES a ON a.message_id=t.message_id "
        "WHERE t.call_id=? AND t.stream_attempt=? AND a.message_id IS NULL "
        "AND l.participant=t.participant AND l.origin_channel='phone'",
        (call_id, stream_attempt),
    )).fetchall()
    linked = 0
    for message_id, participant, fragments_json in rows:
        audio_range = live_transcript_audio_range(
            json.loads(fragments_json), timeline_offset_ms=timeline_offset_ms,
            track_bytes=track_bytes.get(participant, 0),
        )
        if audio_range is not None:
            linked += await persist_message_audio_range_in_transaction(
                conn, call_id=call_id, message_id=message_id,
                participant=participant, audio_range=audio_range,
            )
    return linked


class _OwnedPcmuWavChunks(Iterator[bytes]):
    """Iterator that owns its descriptor even before its first iteration."""

    def __init__(
        self,
        source: BinaryIO,
        audio_range: PhoneMessageAudioRange,
        *,
        chunk_bytes: int,
    ) -> None:
        self._source = source
        self._chunks = _pcmu_source_as_wav_chunks(
            source,
            audio_range,
            chunk_bytes=chunk_bytes,
            include_header=True,
        )
        self._closed = False

    def __iter__(self) -> _OwnedPcmuWavChunks:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            return next(self._chunks)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._chunks.close()
        finally:
            self._source.close()


async def persist_message_audio_range_in_transaction(
    conn: Any,
    *,
    call_id: str,
    message_id: int,
    participant: str,
    audio_range: PhoneMessageAudioRange,
) -> bool:
    """Insert one range once and reject incompatible retries.

    The parent message link is checked explicitly as well as by the composite
    foreign key.  This prevents a caller-controlled message id from selecting
    another call or the wrong participant track.
    """

    # Audio deletion wins over a late message commit.  The canonical text may
    # still be committed, but its range must not reappear after the user asked
    # to remove retained call audio.  Older/rolling schemas deliberately
    # degrade to text-only rather than breaking the live call.
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('PHONE_CALL_MESSAGE_AUDIO_RANGES','PHONE_RECORDING_TOMBSTONES')"
    )
    available_tables = {str(row[0]) for row in await cursor.fetchall()}
    if "PHONE_CALL_MESSAGE_AUDIO_RANGES" not in available_tables:
        return False
    if "PHONE_RECORDING_TOMBSTONES" in available_tables:
        cursor = await conn.execute(
            "SELECT 1 FROM PHONE_RECORDING_TOMBSTONES "
            "WHERE call_id_snapshot=? LIMIT 1",
            (str(call_id),),
        )
        if await cursor.fetchone() is not None:
            return False

    normalized_participant = str(participant)
    if normalized_participant not in {"caller", "assistant"}:
        raise ValueError("only caller and assistant phone messages have audio")
    cursor = await conn.execute(
        """
        SELECT participant,origin_channel
        FROM PHONE_CALL_MESSAGE_LINKS
        WHERE call_id=? AND message_id=?
        """,
        (str(call_id), int(message_id)),
    )
    link = await cursor.fetchone()
    if link is None or tuple(link) != (normalized_participant, "phone"):
        raise RuntimeError("Phone message audio does not match its call link")

    await conn.execute(
        """
        INSERT INTO PHONE_CALL_MESSAGE_AUDIO_RANGES(
            message_id,call_id,start_byte,end_byte
        ) VALUES(?,?,?,?)
        ON CONFLICT(message_id) DO NOTHING
        """,
        (
            int(message_id),
            str(call_id),
            audio_range.start_byte,
            audio_range.end_byte,
        ),
    )
    cursor = await conn.execute(
        """
        SELECT call_id,start_byte,end_byte
        FROM PHONE_CALL_MESSAGE_AUDIO_RANGES
        WHERE message_id=?
        """,
        (int(message_id),),
    )
    row = await cursor.fetchone()
    expected = (
        str(call_id),
        audio_range.start_byte,
        audio_range.end_byte,
    )
    if row is None or tuple(row) != expected:
        raise RuntimeError("Phone message audio was already stored incompatibly")
    return True


def pcmu_range_as_wav_chunks(
    path: str | Path,
    audio_range: PhoneMessageAudioRange,
    *,
    chunk_bytes: int = _READ_CHUNK_BYTES,
) -> Iterator[bytes]:
    """Stream one PCMU range as mono 16-bit PCM WAV without a temp file."""

    _validate_chunk_bytes(chunk_bytes)
    yield _wav_header(audio_range.byte_length)
    with Path(path).open("rb") as source:
        yield from _pcmu_source_as_wav_chunks(
            source,
            audio_range,
            chunk_bytes=chunk_bytes,
            include_header=False,
        )


def open_pcmu_range_as_wav_chunks(
    path: str | Path,
    audio_range: PhoneMessageAudioRange,
    *,
    chunk_bytes: int = _READ_CHUNK_BYTES,
) -> Iterator[bytes]:
    """Open and validate a retained track before returning its WAV iterator.

    Opening the descriptor here (with ``O_NOFOLLOW`` where the platform
    supports it) closes the stat/open race with concurrent privacy deletion or
    final-component symlink replacement.  The returned iterator owns and
    closes the descriptor, including on streaming errors.
    """

    _validate_chunk_bytes(chunk_bytes)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise OSError("retained phone audio is not a regular file")
        if audio_range.end_byte > details.st_size:
            raise OSError("retained phone audio ended before its message range")
        source = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    return _OwnedPcmuWavChunks(
        source,
        audio_range,
        chunk_bytes=chunk_bytes,
    )


def _pcmu_source_as_wav_chunks(
    source: BinaryIO,
    audio_range: PhoneMessageAudioRange,
    *,
    chunk_bytes: int,
    include_header: bool,
) -> Iterator[bytes]:
    _validate_chunk_bytes(chunk_bytes)
    if include_header:
        yield _wav_header(audio_range.byte_length)
    source.seek(audio_range.start_byte)
    remaining = audio_range.byte_length
    while remaining:
        encoded = source.read(min(chunk_bytes, remaining))
        if not encoded:
            raise OSError("retained phone audio ended before its message range")
        remaining -= len(encoded)
        yield _decode_pcmu_to_pcm16le(encoded)


def _validate_chunk_bytes(chunk_bytes: int) -> None:
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int):
        raise ValueError("chunk_bytes must be an integer")
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")


def wav_content_length(audio_range: PhoneMessageAudioRange) -> int:
    return _WAV_HEADER_BYTES + audio_range.byte_length * _PCM16_BYTES_PER_SAMPLE


def _wav_header(pcmu_byte_length: int) -> bytes:
    pcm_byte_length = pcmu_byte_length * _PCM16_BYTES_PER_SAMPLE
    byte_rate = PCMU_SAMPLE_RATE_HZ * _PCM16_BYTES_PER_SAMPLE
    block_align = _PCM16_BYTES_PER_SAMPLE
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_byte_length,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        PCMU_SAMPLE_RATE_HZ,
        byte_rate,
        block_align,
        16,
        b"data",
        pcm_byte_length,
    )


def _decode_pcmu_to_pcm16le(encoded: bytes) -> bytes:
    decoded = bytearray(len(encoded) * _PCM16_BYTES_PER_SAMPLE)
    offset = 0
    for value in encoded:
        inverted = (~value) & 0xFF
        sign = inverted & 0x80
        exponent = (inverted >> 4) & 0x07
        mantissa = inverted & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if sign:
            sample = -sample
        struct.pack_into("<h", decoded, offset, sample)
        offset += _PCM16_BYTES_PER_SAMPLE
    return bytes(decoded)


__all__ = [
    "PhoneMessageAudioRange",
    "open_pcmu_range_as_wav_chunks",
    "pcmu_range_as_wav_chunks",
    "persist_message_audio_range_in_transaction",
    "wav_content_length",
]
