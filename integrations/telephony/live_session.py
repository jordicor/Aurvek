"""Continuous GPT-Live audio with Aurvek-owned tools, history and call lifecycle."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field, replace
import json
import time

from fastapi.responses import StreamingResponse

from ai_runtime.channel_turns import VoiceDelegationResult
from integrations.applications.phone import phone_turn_billing_context
from integrations.telephony.billing import PhoneBillingExhausted
from integrations.telephony.foreground import ForegroundCommitGuard, assert_commit_guard_in_transaction
from integrations.telephony.live_billing import OpenAILiveBillingMeter
from integrations.telephony.media_streams import MarkEvent, build_clear_message, build_mark_message, build_media_message
from integrations.telephony.message_audio import persist_live_message_audio_in_transaction
from integrations.telephony.memory_outbox import enqueue_phone_memory_in_transaction
from integrations.telephony.openai_live import OpenAILiveClient, OpenAILiveError, live_session_configuration
from integrations.telephony.phone_context import create_phone_channel_turn
from integrations.telephony.schemas import CALL_TERMINAL_STATUSES
from integrations.telephony.session import PhoneMediaSession, PhoneMediaSessionError
from integrations.telephony.snapshot import realtime_voice_from_snapshot
from integrations.telephony.transport import iter_sse_payloads
from log_config import logger


@dataclass
class LiveTranscriptSegment:
    id: int
    participant: str
    fragments: list = field(default_factory=list)
    updated_at: float = 0.0
    suppressed: bool = False

    @property
    def text(self):
        return "".join(fragment["delta"] for fragment in self.fragments)


def _text(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return value
        return _text(decoded) if isinstance(decoded, (list, dict)) else value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value if isinstance(item, (dict, str)))
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return ""


class OpenAILivePhoneSession(PhoneMediaSession):
    """Reuse PSTN fencing, notices, recording, deadlines and durable hangup.

    Live transcripts are timestamped fragments, not completed model turns.
    Display grouping never starts tools. Only provider delegation events do.
    No Realtime VAD, response.create, truncation or STT/TTS fallback is used.
    """

    def __init__(self, context, *, openai_api_key_provider, billing_service=None, **kwargs):
        client = OpenAILiveClient(api_key_provider=openai_api_key_provider, session_loader=self._configuration)
        super().__init__(
            context, stt=client,
            billing_meter=OpenAILiveBillingMeter(
                context.call_id, client=client, service=billing_service,
                stream_attempt=context.stream_attempt,
            ), **kwargs,
        )
        self._output = asyncio.Queue(maxsize=250)
        self._delegations = asyncio.Queue(maxsize=8)
        self._delegation_ids = set()
        self._segments = []
        self._open_segments = {}
        self._next_segment = 0
        self._transcript_chars = 0
        self._recent = []
        self._flush_lock = asyncio.Lock()
        self._live_suppressed = False
        self._live_websocket = None
        self._audio_received = 0
        self._audio_sent = 0
        self._audio_confirmed = 0
        self._cleared_through = 0
        self._recording_audio_end_ms = 0
        self._last_spoken_at = 0.0
        self._delegate_active = False

    def _guard(self):
        return ForegroundCommitGuard(
            conversation_id=self.context.conversation_id, epoch=self.context.foreground_epoch,
            expected_owner="phone", call_id=self.context.call_id,
            lease_owner=self.context.foreground_lease_owner,
        )

    async def _configuration(self):
        from llm_catalog import normalize_runtime_capability

        async with self.repository.connection_factory(readonly=True) as conn:
            row = await (await conn.execute(
                "SELECT p.prompt,p.name,l.machine,l.model,l.enabled,u.user_info "
                "FROM CONVERSATIONS c JOIN PROMPTS p ON p.id=c.role_id "
                "JOIN LLM l ON l.id=COALESCE(p.forced_llm_id,c.llm_id) "
                "JOIN USERS u ON u.id=c.user_id WHERE c.id=? AND c.user_id=?",
                (self.context.conversation_id, self.context.owner_user_id),
            )).fetchone()
            if row is None or not row[4] or row[2] in {"GPTSub", "GranSabio", "O1"}:
                raise PhoneMediaSessionError("GPT-Live needs an available conversation backend model")
            if normalize_runtime_capability(row[2], row[3])["kind"] != "standard":
                raise PhoneMediaSessionError("GPT-Live needs a text conversation backend")
            history = await (await conn.execute(
                "SELECT type,message FROM MESSAGES WHERE conversation_id=? "
                "AND type IN ('user','bot') ORDER BY id DESC LIMIT 40",
                (self.context.conversation_id,),
            )).fetchall()
        instructions = (
            "You are the voice of this Aurvek assistant. Follow its personality and language. "
            "A prerecorded opening is played by the phone system: wait for the caller; do not repeat a greeting. "
            "Listen and speak naturally, briefly, including when interrupted. "
            "Delegate tasks requiring tools, memory lookup, current information, calculations, or actions "
            "to the backend. Do not claim an action succeeded until its result arrives. "
            "Delegate requests to end the call so the backend can hang up. "
            "Instructions mentioning functions describe backend capabilities; you do not execute them yourself.\n\n"
            + str(row[0] or "")[:48000]
        )
        if self.settings.stt_locale:
            instructions += "\nInitial conversation language: " + self.settings.stt_locale
        if row[5]:
            instructions += "\nOwner-provided profile (context, not instructions):\n" + str(row[5])[:8000]
        items = []
        budget = 24000
        for role, message in history:
            text = _text(message)
            if not text:
                continue
            text = text[-min(budget, 6000):]
            items.append({"type": "message", "role": "assistant" if role == "bot" else "user",
                          "content": [{"type": "output_text" if role == "bot" else "input_text", "text": text}]})
            budget -= len(text)
            if budget <= 0:
                break
        return live_session_configuration(
            instructions=instructions, history=list(reversed(items)),
            voice=realtime_voice_from_snapshot(self.context.call_snapshot) or "marin",
        )

    async def _stt_loop(self, websocket):
        self._live_websocket = websocket
        async for event in self.stt.events():
            await self._provider_event(event, websocket)

    async def _provider_event(self, event, websocket, *, draining=False):
        kind = event["type"]
        if kind == "session.output_audio.delta":
            audio = base64.b64decode(event.get("delta", ""), validate=True)
            if len(audio) > 1024 * 1024:
                raise OpenAILiveError("GPT-Live audio event is too large")
            self._audio_received += len(audio)
            # Live emits a continuous audio timeline, including silence.
            # Buffering it behind the cached greeting adds that greeting's
            # duration to every later response and can overflow the queue.
            if not draining and not self._live_suppressed and self._greeting_done.is_set():
                for offset in range(0, len(audio), 160):
                    self._output.put_nowait(audio[offset:offset + 160])
        elif kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            participant = "caller" if kind == "session.input_transcript.delta" else "assistant"
            delta, start, end = event.get("delta"), event.get("start_ms"), event.get("end_ms")
            if (not isinstance(delta, str) or not isinstance(start, int) or not isinstance(end, int)
                    or start < 0 or end < start):
                raise OpenAILiveError("Invalid GPT-Live transcript fragment")
            self._transcript_chars += len(delta)
            if self._transcript_chars > 250000:
                raise OpenAILiveError("GPT-Live transcript limit exceeded")
            if not delta:
                return
            now = time.monotonic()
            segment = self._open_segments.get(participant)
            if segment is None or start > segment.fragments[-1]["end_ms"] + 1200:
                self._next_segment += 1
                segment = LiveTranscriptSegment(self._next_segment, participant)
                self._segments.append(segment)
                self._open_segments[participant] = segment
            segment.fragments.append({"delta": delta, "start_ms": start, "end_ms": end})
            segment.updated_at = now
            segment.suppressed |= participant == "assistant" and (
                self._live_suppressed or not self._greeting_done.is_set()
            )
            self._recent.append((participant, delta))
            # Bounded working context; canonical history remains in MESSAGES.
            self._recent = self._recent[-160:]
            if participant == "caller":
                self.silence.on_real_participant_speech()
                self.end_controller.on_real_participant_speech()
                if self._active_greeting is not None and not draining:
                    await self._active_greeting.barge_in()
            else:
                self._last_spoken_at = now
        elif kind == "session.delegation.created" and not draining:
            delegation = event.get("delegation") or {}
            delegation_id = delegation.get("id")
            if delegation.get("target") != "client" or not isinstance(delegation_id, str):
                raise OpenAILiveError("Invalid GPT-Live client delegation")
            if delegation_id not in self._delegation_ids:
                if len(self._delegation_ids) >= 128:
                    raise OpenAILiveError("GPT-Live delegation limit exceeded")
                self._delegation_ids.add(delegation_id)
                self._delegations.put_nowait(delegation_id)

    async def _turn_loop(self, websocket):
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._output_loop(websocket))
                group.create_task(self._delegation_loop(websocket))
                group.create_task(self._transcript_loop())
        except* Exception as failures:
            for failure in failures.exceptions:
                logger.error("GPT-Live task failed (call_id=%s, exception_type=%s)",
                             self.context.call_id, type(failure).__name__)
            raise

    async def _output_loop(self, websocket):
        await self._greeting_done.wait()
        next_send = time.monotonic()
        last_mark = 0
        while not self._stopping.is_set():
            try:
                audio = await asyncio.wait_for(self._output.get(), 0.2)
            except TimeoutError:
                continue
            try:
                if self._live_suppressed:
                    continue
                await asyncio.sleep(max(0, next_send - time.monotonic()))
                async with self._audio_lock:
                    if self._live_suppressed:
                        continue
                    await websocket.send_json(build_media_message(stream_sid=str(self._stream_sid), audio=audio))
                    # Scheduling jitter must not make adjacent PCM frames
                    # overlap in the retained recording timeline.
                    recording_start = max(self._recording_audio_end_ms,
                                          int((time.monotonic() - self._call_started_monotonic) * 1000))
                    self.recorder.record_assistant(audio, start_ms=recording_start)
                    self._recording_audio_end_ms = recording_start + (len(audio) + 7) // 8
                    self._audio_sent += len(audio)
                    if self._audio_sent - last_mark >= 800 or self._output.empty():
                        await websocket.send_json(build_mark_message(stream_sid=str(self._stream_sid), name=f"live-audio-{self._audio_sent}"))
                        last_mark = self._audio_sent
                next_send = max(next_send + len(audio) / 8000, time.monotonic())
            finally:
                self._output.task_done()

    async def feed_twilio_message(self, raw, websocket):
        decoded = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if decoded.get("event") == "mark" and str((decoded.get("mark") or {}).get("name", "")).startswith("live-audio-"):
            event = self.parser.parse(raw)
            if isinstance(event, MarkEvent) and not self._live_suppressed:
                value = int(event.name.removeprefix("live-audio-"))
                if self._cleared_through < value <= self._audio_sent:
                    self._audio_confirmed = max(self._audio_confirmed, value)
            return
        await super().feed_twilio_message(raw, websocket)

    async def _delegation_loop(self, websocket):
        while not self._stopping.is_set():
            try:
                delegation_id = await asyncio.wait_for(self._delegations.get(), 0.2)
            except TimeoutError:
                continue
            self._delegate_active = True
            try:
                async with phone_turn_billing_context(
                    self.repository.connection_factory, self.context.call_id,
                    conversation_id=self.context.conversation_id,
                ):
                    await self._delegate(delegation_id)
                directive = self.end_controller.pending
                if directive is not None:
                    # The Live model paraphrases the backend's farewell. Keep
                    # both streams alive before the explicit session close.
                    started = time.monotonic()
                    while time.monotonic() - started < 10:
                        await asyncio.sleep(0.2)
                        if self.end_controller.pending is not directive:
                            break
                        if time.monotonic() - started > 4 and time.monotonic() - self._last_spoken_at > 2:
                            break
                    if self.end_controller.pending is not directive:
                        continue
                    await self.stt.close()
                    await asyncio.wait_for(self._output.join(), 4)
                    async with asyncio.timeout(4):
                        while self._audio_confirmed < self._audio_sent:
                            await asyncio.sleep(0.05)
                    await self._hangup(directive.reason.value)
            finally:
                self._delegate_active = False
                self._delegations.task_done()

    async def _delegate(self, delegation_id):
        from ai_runtime.messages import process_save_message

        # Context comes from timestamped live transcripts, not a fabricated
        # utterance in the delegation event (the API supplies none).
        await self._flush_transcripts(force=True)
        parts = []
        previous = None
        for role, text in self._recent:
            if role != previous:
                parts.append("\nCaller: " if role == "caller" else "\nAssistant: ")
            parts.append(text)
            previous = role
        recent = "".join(parts)[-16000:]
        if not any(role == "caller" for role, _ in self._recent):
            await self.stt.commentary("Ask the caller what they need before requesting backend work.", delegation_id=delegation_id)
            return
        turn = create_phone_channel_turn(
            self._guard(), turn_id=f"live-delegate-{len(self._delegation_ids)}",
            end_controller=self.end_controller,
            phone_direction=self.context.direction, phone_request_source=self.context.request_source,
            phone_request_timing=self.context.request_timing,
            ai_initiation_mode=self.context.call_snapshot["ai_initiation_mode"],
            prompt_id=int(self.context.call_snapshot["prompt_id"]),
            application_channel=self.context.application_channel,
            internal_turn_context=(
                "You are the backend of an ongoing GPT-Live phone conversation. "
                "Use the recent transcript and existing history to handle the current request. "
                "Run the normal permitted tools, including end_call when requested. "
                "Return concise verified results for the voice assistant to communicate. "
                "Your response is backend context, not speech. Transcript fragments may overlap."
            ),
        )
        sink = VoiceDelegationResult()
        context = replace(turn.context, persistence="delegation", delegation_result=sink,
                          on_commit=None, on_commit_in_transaction=None)
        response = await process_save_message(
            None, self.context.conversation_id, self._current_user,
            text_plain=recent, files=[], is_whatsapp=True, prevalidated=False,
            expected_llm_id=int(self.context.call_snapshot["llm_id"]),
            channel_context=context,
        )
        failed = not isinstance(response, StreamingResponse)
        if not failed:
            try:
                async with asyncio.timeout(90):
                    async for event in iter_sse_payloads(response.body_iterator):
                        failed |= bool(event.get("error") or event.get("persistence_error"))
            except TimeoutError:
                failed = True
        if failed or not sink.committed or not sink.content:
            # The canonical backend retains any unresolved charges itself.
            # Report its failure truthfully, without treating a failed web
            # search as a failure of the ongoing voice transport.
            await self.stt.commentary(
                "The backend request failed. Briefly tell the caller that the requested operation "
                "could not be completed or confirmed. Do not claim success or retry it automatically. "
                "Continue the conversation and wait for the caller's next request.",
                delegation_id=delegation_id,
            )
            logger.warning("GPT-Live backend request failed (call_id=%s)", self.context.call_id)
            return
        if not self._stopping.is_set():
            await self.stt.commentary(sink.content, delegation_id=delegation_id)
        logger.info("GPT-Live delegation completed (call_id=%s)", self.context.call_id)

    async def _transcript_loop(self):
        while not self._stopping.is_set():
            await asyncio.sleep(0.5)
            await self._flush_transcripts()

    async def _flush_transcripts(self, *, force=False):
        async with self._flush_lock:
            ready = [s for s in self._segments if force or time.monotonic() - s.updated_at >= 1.5]
            for segment in ready:
                # Freeze before the transaction yields; new fragments get a new
                # display segment. Grouping is never used to trigger an action.
                if self._open_segments.get(segment.participant) is segment:
                    del self._open_segments[segment.participant]
                await self._persist_segment(segment)
                self._segments.remove(segment)

    async def _persist_segment(self, segment):
        async with self.repository.connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                foreground_owned = await assert_commit_guard_in_transaction(conn, self._guard())
                if not foreground_owned:
                    call = await (await conn.execute(
                        "SELECT status,deleted_at FROM PHONE_CALLS WHERE id=? AND conversation_id=?",
                        (self.context.call_id, self.context.conversation_id),
                    )).fetchone()
                    if call is None or call[1] is not None:
                        await conn.rollback()
                        return
                    if call[0] not in CALL_TERMINAL_STATUSES:
                        raise PhoneMediaSessionError("GPT-Live transcript foreground changed")
                existing = await (await conn.execute(
                    "SELECT id FROM PHONE_LIVE_TRANSCRIPTS WHERE call_id=? AND stream_attempt=? AND segment_id=?",
                    (self.context.call_id, self.context.stream_attempt, segment.id),
                )).fetchone()
                if existing is not None:
                    await conn.rollback()
                    return
                message_id = None
                # Keep withheld output as raw private fragments, never as an
                # assistant message. No transcript timestamp is presented as
                # an exact word/audio playback alignment.
                if foreground_owned and (segment.participant == "caller" or (not segment.suppressed and self._audio_sent > 0)):
                    cursor = await conn.execute(
                        "INSERT INTO MESSAGES(conversation_id,user_id,message,type,llm_id) VALUES(?,?,?,?,?)",
                        (self.context.conversation_id, self.context.owner_user_id, segment.text,
                         "user" if segment.participant == "caller" else "bot",
                         self.context.call_snapshot["runtime_llm_id"]),
                    )
                    message_id = int(cursor.lastrowid)
                    await conn.execute(
                        "INSERT INTO PHONE_CALL_MESSAGE_LINKS(call_id,message_id,participant,turn_id,origin_channel,delivery_state) "
                        "VALUES(?,?,?,?,'phone','consumed')",
                        (self.context.call_id, message_id, segment.participant,
                         f"live-{self.context.stream_attempt}-{segment.id}"),
                    )
                    if segment.participant == "caller":
                        await enqueue_phone_memory_in_transaction(conn, call_id=self.context.call_id, message_id=message_id)
                        self._caller_turns += 1
                    await conn.execute("UPDATE CONVERSATIONS SET last_activity=CURRENT_TIMESTAMP WHERE id=?", (self.context.conversation_id,))
                await conn.execute(
                    "INSERT INTO PHONE_LIVE_TRANSCRIPTS(call_id,conversation_id,stream_attempt,segment_id,participant,message_id,fragments_json,"
                    "audio_bytes_sent,audio_bytes_confirmed) VALUES(?,?,?,?,?,?,?,?,?)",
                    (self.context.call_id, self.context.conversation_id, self.context.stream_attempt, segment.id, segment.participant, message_id,
                     json.dumps(segment.fragments, ensure_ascii=False), self._audio_sent, self._audio_confirmed),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def _drain_twilio_stop(self, websocket):
        self._twilio_stop_observed = True
        await self.stt.close()
        for event in self.stt.drain_events():
            await self._provider_event(event, websocket, draining=True)
        await self._flush_transcripts(force=True)
        await self.repository.record_stream_attempt_result(
            call_id=self.context.call_id, provider_call_sid=self.context.provider_call_sid,
            stream_attempt=self.context.stream_attempt, reason="twilio_stop",
            reconnectable=False, internal_failure=False,
        )
        self._attempt_result_published = True
        self._stop_reason = "twilio_stop"
        self._stopping.set()

    async def _hangup(self, reason):
        # Settle while the phone still owns the conversation. The Twilio
        # completion callback can release that ownership immediately.
        try:
            await self.stt.close()
            for event in self.stt.drain_events():
                await self._provider_event(event, self._live_websocket, draining=True)
            await self._flush_transcripts(force=True)
        finally:
            await super()._hangup(reason)

    async def _finalize_session(self, tasks):
        try:
            await super()._finalize_session(tasks)
        finally:
            for event in self.stt.drain_events():
                await self._provider_event(event, self._live_websocket, draining=True)
            await self._flush_transcripts(force=True)
            await self._link_recorded_message_audio()
            logger.info(
                "GPT-Live session finalized (call_id=%s, audio_received=%s, audio_sent=%s, "
                "audio_confirmed=%s, delegations=%s, usage_seconds=%s, final_usage=%s)",
                self.context.call_id, self._audio_received, self._audio_sent,
                self._audio_confirmed, len(self._delegation_ids), self.stt.usage_seconds,
                self.stt.final_usage_confirmed,
            )

    async def _link_recorded_message_audio(self):
        if not self.recorder.enabled:
            return
        # Finalize only after playout and the final transcript drain: Live can
        # deliver text before the corresponding audio has reached the phone.
        try:
            asset = self.recorder.finalize_raw()
            tracks = {
                participant: path.stat().st_size
                for participant, path in (("caller", asset.participant_path),
                                          ("assistant", asset.assistant_path))
                if path is not None and path.is_file()
            }
            if not tracks:
                return
            async with self.repository.connection_factory() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                call = await (await conn.execute(
                    "SELECT 1 FROM PHONE_CALLS c JOIN PHONE_RECORDINGS r ON r.call_id=c.id "
                    "WHERE c.id=? AND c.owner_user_id=? AND c.deleted_at IS NULL "
                    "AND c.recording_enabled=1 AND r.local_deleted_at IS NULL "
                    "AND r.status='available'",
                    (self.context.call_id, self.context.owner_user_id),
                )).fetchone()
                if call is None:
                    await conn.rollback()
                    return
                count = await persist_live_message_audio_in_transaction(
                    conn, call_id=self.context.call_id,
                    stream_attempt=self.context.stream_attempt,
                    timeline_offset_ms=self._recording_attempt_offset_ms,
                    track_bytes=tracks,
                )
                await conn.commit()
            logger.info("GPT-Live recorded message audio linked (call_id=%s, messages=%s)",
                        self.context.call_id, count)
        except Exception as exc:
            # A replay indexing failure must not turn an otherwise completed
            # call into a technical-error hangup. Raw audio remains repairable.
            logger.error("GPT-Live recorded message audio linking failed (call_id=%s, exception_type=%s)",
                         self.context.call_id, type(exc).__name__)

    async def _interrupt_active_output(self, *, reason):
        self._live_suppressed = True
        self._cleared_through = self._audio_sent
        while not self._output.empty():
            self._output.get_nowait()
            self._output.task_done()
        if self._live_websocket is not None and self._stream_sid:
            await self._live_websocket.send_json(build_clear_message(stream_sid=str(self._stream_sid)))
        self._trim_live_recording()
        await super()._interrupt_active_output(reason=reason)

    def _trim_live_recording(self):
        self._recording_audio_end_ms = int((time.monotonic() - self._call_started_monotonic) * 1000)
        self.recorder.truncate_assistant(end_byte=self._recording_audio_end_ms * 8)

    async def _play_notice(self, kind, websocket):
        suppressed = self._live_suppressed
        self._live_suppressed = True
        self._cleared_through = self._audio_sent
        while not self._output.empty():
            self._output.get_nowait()
            self._output.task_done()
        try:
            if self._stream_sid:
                await websocket.send_json(build_clear_message(stream_sid=str(self._stream_sid)))
            self._trim_live_recording()
            await super()._play_notice(kind, websocket)
        finally:
            self._live_suppressed = suppressed

    async def _timer_loop(self, websocket):
        # The inherited timer retains account access checks and notices. Its
        # existing busy flag also covers continuous speech and backend work.
        while not self._stopping.is_set():
            self._starting_runtime = self._delegate_active or time.monotonic() - self._last_spoken_at < 2
            try:
                await self._ensure_live_billing_coverage()
            except PhoneBillingExhausted:
                await self._terminate_balance_exhausted(websocket)
                return
            if self.context.application_channel is not None:
                from integrations.applications.phone import authorize_phone_provider
                await authorize_phone_provider(self.repository.connection_factory, self.context.call_id)
            tick = self.clock.poll_deadline()
            if tick.end_call is not None:
                await self._force_close(tick.end_call, websocket)
                return
            if not self._starting_runtime and self._greeting_done.is_set():
                warning = self.clock.peek_safe_point()
                directive = warning.safe_point_directive
                if directive is not None and directive.source == "milestone":
                    await self.stt.commentary(warning.internal_clock_block())
                    await self.repository.record_delivered_call_milestones(
                        call_id=self.context.call_id, provider_call_sid=self.context.provider_call_sid,
                        fencing_token=self.context.foreground_epoch,
                        lease_owner=self.context.foreground_lease_owner,
                        milestones_seconds=directive.crossed_milestones_seconds,
                    )
                    self.clock.acknowledge_milestones(directive.crossed_milestones_seconds)
                silence = self.silence.at_safe_point()
                if silence is not None:
                    if silence.end_call is not None:
                        await self._force_close(silence.end_call, websocket)
                        return
                    await self._play_notice("silence_check", websocket)
                    self.silence.confirm_presence_check_audible()
            await asyncio.sleep(1)
