"""Duration billing for the GPT-Live voice transport, separate from its agent."""

from integrations.telephony.billing import PhoneLiveBillingMeter


class OpenAILiveBillingMeter(PhoneLiveBillingMeter):
    def __init__(self, call_id, *, client, **kwargs):
        super().__init__(call_id, include_stt=False, **kwargs)
        self.client = client
        self._voice_index = -1
        self._voice_components = []

    async def ensure_live_coverage(self, **kwargs):
        components = await super().ensure_live_coverage(**kwargs)
        elapsed = max(float(kwargs.get("stream_elapsed_seconds", 0)), self.client.usage_seconds)
        index = int((elapsed + self.close_buffer_seconds) // self.stream_tranche_seconds)
        # The base coverage lock is already released; voice has its own ledger
        # keys but shares the session's serialized coverage calls.
        async with self._coverage_lock:
            for tranche in range(self._voice_index + 1, index + 1):
                component = await self.service.reserve_time_tranche(
                    call_id=self.call_id, provider="openai_live", component_type="transport",
                    tranche_index=tranche, seconds=self.stream_tranche_seconds,
                    stream_attempt=self.stream_attempt,
                )
                try:
                    await self.service.mark_provider_started(component.id)
                except BaseException:
                    await self.service.mark_ambiguous(component.id, reason="live_voice_boundary_uncertain")
                    raise
                self._voice_components.append(component.id)
                self._voice_index = tranche
        return components

    async def finalize_stt_usage(self):
        # The inherited finalizer invokes this after closing the voice client.
        # GPT-Live has no separate STT/TTS bill, and silence is billable too.
        if self._voice_index < 0:
            return None
        if not self.client.final_usage_confirmed:
            for component_id in self._voice_components:
                await self.service.mark_ambiguous(component_id, reason="live_final_usage_unconfirmed")
            return None
        return await self.service.settle_duration_components(
            call_id=self.call_id, provider="openai_live", component_type="transport",
            duration_seconds=self.client.usage_seconds, external_usage_id=self.client.session_id,
            stream_attempt=self.stream_attempt,
        )
