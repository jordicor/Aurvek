(function() {
    const tr = (key, params) => AurvekI18n.t(`chat_widgets.voice_call.${key}`, params);

    function onReady(callback) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', callback);
        } else {
            callback();
        }
    }

    function resolveConversation() {
        if (typeof window.resolveConversationGlobal === 'function') {
            const conv = window.resolveConversationGlobal();
            if (conv) {
                return conv;
            }
        }

        const candidates = [
            window.ElevenLabs?.Conversation,
            window.ElevenLabsClient?.Conversation,
            window.elevenlabs?.Conversation,
            window.Conversation,
            window.client?.Conversation,
        ];

        for (let i = 0; i < candidates.length; i++) {
            const candidate = candidates[i];
            if (candidate && typeof candidate.startSession === 'function') {
                return candidate;
            }
        }
        return null;
    }

    function wait(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    onReady(() => {
        const voiceButton = document.getElementById('embed-voice-unavailable') || document.getElementById('plus-voice-call');
        const overlay = document.getElementById('voice-overlay');
        const startStopButton = document.getElementById('voice-start-stop');
        const muteButton = document.getElementById('voice-mute-toggle');
        const closeButton = document.getElementById('close-voice-overlay');
        const statusText = document.getElementById('voice-status-text');
        const statusIcon = document.getElementById('voice-status-icon');
        const promptTag = document.getElementById('voice-overlay-prompt');
        const promptAvatar = document.getElementById('voice-overlay-avatar');
        const promptName = document.getElementById('voice-overlay-prompt-name');
        const helperText = document.getElementById('voice-helper-text');
        const caption = document.getElementById('voice-overlay-caption');
        const incognitoBadge = document.getElementById('incognito-chat-badge');
        const availabilityReason = document.getElementById('embed-voice-reason') || document.getElementById('voice-call-availability-reason');

        if (!voiceButton || !overlay || !startStopButton || !statusText || !statusIcon) {
            return;
        }

        const messageText = document.getElementById('message-text');
        const sendButton = document.getElementById('send-button');
        const fileInput = document.getElementById('chat-files');

        const trackedInputs = [messageText, sendButton, fileInput];
        const previousStates = new Map();

        function lockChatInputs(lock) {
            trackedInputs.forEach((element) => {
                if (!element) {
                    return;
                }
                if (lock) {
                    if (!previousStates.has(element)) {
                        previousStates.set(element, element.disabled);
                    }
                    element.disabled = true;
                } else if (previousStates.has(element)) {
                    const wasDisabled = previousStates.get(element);
                    element.disabled = wasDisabled;
                    previousStates.delete(element);
                }
            });
        }

        let configData = null;
        let conversationRef = null;
        let activeSessionId = null;
        let callConversationId = null;
        let currentState = 'idle';
        let muteState = false;
        let completing = false;
        let loadingConfig = false;
        let availabilityRequestSequence = 0;
        let availabilityConversationId = null;
        let voiceAvailability = null;
        let sessionStartRejected = false;
        let completionRetryPending = false;
        let recentAgentResponses = [];
        let clientCorrectionHints = [];
        let localizedState = null;
        let captionPresentation = {key: 'caption'};
        const maxTrackedVoiceEvents = 100;
        const maxTrackedVoiceTextCharacters = 8000;
        const defaultVoiceButtonTitle = voiceButton.title || tr('button_title');
        const voiceButtonTitle = () => window.AurvekEmbed ? tr('button_title') : defaultVoiceButtonTitle;

        function resetVoiceEventTracking() {
            recentAgentResponses = [];
            clientCorrectionHints = [];
        }

        function normalizeVoiceEventText(value) {
            let candidate = value;
            if (candidate && typeof candidate === 'object') {
                candidate = candidate.text || candidate.content || candidate.message;
            }
            if (typeof candidate !== 'string') {
                return '';
            }
            return candidate.trim().slice(0, maxTrackedVoiceTextCharacters);
        }

        function normalizeVoiceEventId(value) {
            if (value === null || value === undefined || typeof value === 'object') {
                return '';
            }
            return String(value).trim().slice(0, 200);
        }

        function normalizeVoiceEventTime(value) {
            if (value === null || value === undefined || value === '') {
                return null;
            }
            const number = Number(value);
            return Number.isFinite(number) && number >= 0 ? number : null;
        }

        function parsePossibleEvent(value) {
            if (typeof value !== 'string') {
                return value;
            }
            const trimmed = value.trim();
            if (!trimmed.startsWith('{')) {
                return value;
            }
            try {
                return JSON.parse(trimmed);
            } catch (_) {
                return value;
            }
        }

        function extractCorrectionEvent(payload) {
            const root = parsePossibleEvent(payload);
            if (!root || typeof root !== 'object') {
                return null;
            }

            const candidates = [root];
            [
                'agent_response_correction_event',
                'agentResponseCorrectionEvent',
                'value',
                'event',
                'data',
                'payload'
            ].forEach((key) => {
                const nested = parsePossibleEvent(root[key]);
                if (nested && typeof nested === 'object') {
                    candidates.push(nested);
                    if (nested.agent_response_correction_event
                        && typeof nested.agent_response_correction_event === 'object') {
                        candidates.push(nested.agent_response_correction_event);
                    }
                }
            });
            const fallbackEventId = candidates
                .map((candidate) => normalizeVoiceEventId(
                    candidate.event_id || candidate.eventId
                ))
                .find(Boolean) || '';

            for (let index = 0; index < candidates.length; index += 1) {
                const candidate = candidates[index];
                const originalMessage = normalizeVoiceEventText(
                    candidate.original_agent_response
                    || candidate.originalAgentResponse
                    || candidate.original_message
                    || candidate.originalMessage
                );
                const correctedMessage = normalizeVoiceEventText(
                    candidate.corrected_agent_response
                    || candidate.correctedAgentResponse
                    || candidate.corrected_message
                    || candidate.correctedMessage
                );
                if (!originalMessage || !correctedMessage || originalMessage === correctedMessage) {
                    continue;
                }
                return {
                    event_id: normalizeVoiceEventId(
                        candidate.event_id
                        || candidate.eventId
                        || root.event_id
                        || root.eventId
                        || fallbackEventId
                    ),
                    original_message: originalMessage,
                    corrected_message: correctedMessage,
                    time_in_call_secs: normalizeVoiceEventTime(
                        candidate.time_in_call_secs
                        ?? candidate.timeInCallSecs
                        ?? root.time_in_call_secs
                        ?? root.timeInCallSecs
                    )
                };
            }
            return null;
        }

        function rememberCorrection(correction) {
            if (!correction || !correction.original_message || !correction.corrected_message
                || correction.original_message === correction.corrected_message) {
                return;
            }
            const match = clientCorrectionHints.find((item) => {
                if (correction.event_id && item.event_id) {
                    return item.event_id === correction.event_id;
                }
                return item.original_message === correction.original_message
                    && item.corrected_message === correction.corrected_message
                    && item.time_in_call_secs === correction.time_in_call_secs;
            });
            if (match) {
                Object.assign(match, correction);
                return;
            }
            clientCorrectionHints.push(correction);
            if (clientCorrectionHints.length > maxTrackedVoiceEvents) {
                clientCorrectionHints.shift();
            }
        }

        function rememberCorrectionFromEvent(payload) {
            const correction = extractCorrectionEvent(payload);
            if (!correction) {
                return false;
            }
            rememberCorrection(correction);
            return true;
        }

        function handleSdkMessage(payload) {
            if (rememberCorrectionFromEvent(payload)) {
                return;
            }
            if (!payload || typeof payload !== 'object') {
                return;
            }
            const source = String(payload.source || payload.role || '').toLowerCase();
            if (!['ai', 'agent', 'assistant', 'bot'].includes(source)) {
                return;
            }
            const message = normalizeVoiceEventText(payload.message || payload.text);
            if (!message) {
                return;
            }
            recentAgentResponses.push({
                message,
                event_id: normalizeVoiceEventId(payload.event_id || payload.eventId || payload.id)
            });
            if (recentAgentResponses.length > maxTrackedVoiceEvents) {
                recentAgentResponses.shift();
            }
        }

        function handleSdkInterruption(payload) {
            if (rememberCorrectionFromEvent(payload)) {
                return;
            }
            const event = payload && typeof payload === 'object' ? payload : {};
            const latestAgentResponse = recentAgentResponses[
                recentAgentResponses.length - 1
            ];
            const originalMessage = normalizeVoiceEventText(
                event.original_message
                || event.originalMessage
                || event.original_agent_response
                || latestAgentResponse?.message
            );
            const correctedMessage = normalizeVoiceEventText(
                event.corrected_message
                || event.correctedMessage
                || event.corrected_agent_response
                || event.message
                || event.text
            );
            if (!originalMessage || !correctedMessage || originalMessage === correctedMessage) {
                return;
            }
            rememberCorrection({
                event_id: normalizeVoiceEventId(
                    event.event_id || event.eventId || latestAgentResponse?.event_id
                ),
                original_message: originalMessage,
                corrected_message: correctedMessage,
                time_in_call_secs: normalizeVoiceEventTime(
                    event.time_in_call_secs ?? event.timeInCallSecs
                )
            });
        }

        function handleAgentResponseCorrection(payload, correctedMessage) {
            if (typeof payload === 'string' && typeof correctedMessage === 'string') {
                rememberCorrection({
                    event_id: '',
                    original_message: normalizeVoiceEventText(payload),
                    corrected_message: normalizeVoiceEventText(correctedMessage),
                    time_in_call_secs: null
                });
                return;
            }
            rememberCorrectionFromEvent(payload);
        }

        function handleSdkDebug(payload) {
            rememberCorrectionFromEvent(payload);
        }

        function buildClientCorrectionsPayload() {
            return clientCorrectionHints.map((item) => ({
                event_id: item.event_id || undefined,
                original_message: item.original_message,
                corrected_message: item.corrected_message,
                time_in_call_secs: item.time_in_call_secs
            }));
        }

        function getSelectedConversationId() {
            if (typeof currentConversationId === 'undefined' || currentConversationId === null) {
                return null;
            }
            return String(currentConversationId);
        }

        function isSameConversation(first, second) {
            return first !== null && second !== null && String(first) === String(second);
        }

        function isIncognitoConversationActive() {
            if (typeof currentConversationIncognito !== 'undefined') {
                return Boolean(currentConversationIncognito);
            }
            return Boolean(incognitoBadge && !incognitoBadge.hidden);
        }

        function localizedVoiceErrorKey(code, status) {
            const keys = {
                conversation_required: 'select_conversation',
                conversation_incognito: 'incognito_unavailable',
                insufficient_balance: 'insufficient_balance',
                storage_quota_exceeded: 'storage_limit',
                wellbeing_pause_active: 'pause_required',
                wellbeing_pause_required: 'pause_required',
                microphone_permission_denied: 'microphone_required',
                voice_recording_unavailable: 'recording_unavailable',
                sdk_unavailable: 'sdk_unavailable',
                availability_pending: 'checking_availability',
                availability_check_failed: 'availability_failed'
            };
            if (keys[String(code || '').toLowerCase()]) return keys[String(code).toLowerCase()];
            if (status === 401) return typeof window !== 'undefined' && window.AurvekEmbed
                ? 'embed.expired' : 'common.session.expired_message';
            return 'request_failed';
        }

        function localizedVoiceError(code, status) {
            const key = localizedVoiceErrorKey(code, status);
            return key.includes('.') ? AurvekI18n.t(key) : tr(key);
        }

        function voiceErrorCode(payload) {
            return payload?.error_code || payload?.error || '';
        }

        function applyVoiceAvailability(available, reason = '', errorCode = '') {
            const canonicalReason = String(reason || localizedVoiceError(errorCode)).trim();
            const incognito = errorCode === 'conversation_incognito';
            voiceButton.disabled = !available;
            voiceButton.hidden = incognito;
            voiceButton.setAttribute('aria-disabled', available ? 'false' : 'true');
            voiceButton.dataset.availabilityCode = errorCode || '';
            voiceButton.title = available ? voiceButtonTitle() : canonicalReason;
            voiceButton.setAttribute(
                'aria-label',
                available ? voiceButtonTitle() : tr('unavailable_label', {reason: canonicalReason})
            );
            if (availabilityReason) {
                availabilityReason.textContent = available ? '' : canonicalReason;
                availabilityReason.hidden = available || !canonicalReason || incognito;
            }
            if (!available && !completionRetryPending
                && !['connecting', 'active', 'updating'].includes(currentState)) {
                setOverlayVisible(false);
            }
        }

        async function syncVoiceAvailability(force = false) {
            const conversationId = getSelectedConversationId();
            if (!force && voiceAvailability
                && isSameConversation(availabilityConversationId, conversationId)) {
                return voiceAvailability;
            }

            const requestSequence = ++availabilityRequestSequence;
            availabilityConversationId = conversationId;
            voiceAvailability = null;
            if (conversationId === null) {
                const unavailable = {
                    available: false,
                    error_code: 'conversation_required',
                    reason: tr('select_conversation')
                };
                applyVoiceAvailability(false, unavailable.reason, unavailable.error_code);
                return unavailable;
            }

            const pendingReason = isIncognitoConversationActive()
                ? tr('incognito_unavailable') : tr('checking_availability');
            applyVoiceAvailability(
                false,
                pendingReason,
                isIncognitoConversationActive() ? 'conversation_incognito' : 'availability_pending'
            );

            try {
                const response = await secureFetch(
                    `/api/conversations/${conversationId}/voice/availability`
                );
                if (!response) {
                    throw new Error(tr('request_failed'));
                }
                const payload = await response.json();
                if (requestSequence !== availabilityRequestSequence
                    || !isSameConversation(getSelectedConversationId(), conversationId)) {
                    return null;
                }
                if (!response.ok) {
                    const reason = localizedVoiceError(payload.error_code, response.status);
                    voiceAvailability = {
                        available: false,
                        error_code: payload.error_code || 'availability_check_failed',
                        reason
                    };
                } else {
                    voiceAvailability = payload;
                }
                window.setApplicationConversationFunding?.(
                    conversationId, response.ok ? (payload.application_funding ?? null) : null
                );
                applyVoiceAvailability(
                    Boolean(voiceAvailability.available),
                    voiceAvailability.reason,
                    voiceAvailability.error_code
                );
                return voiceAvailability;
            } catch (error) {
                console.error('Error checking ElevenLabs availability:', error);
                if (requestSequence !== availabilityRequestSequence) {
                    return null;
                }
                voiceAvailability = {
                    available: false,
                    error_code: 'availability_check_failed',
                    reason: tr('availability_failed')
                };
                applyVoiceAvailability(false, voiceAvailability.reason, voiceAvailability.error_code);
                return voiceAvailability;
            }
        }

        window.syncElevenLabsVoiceAvailability = () => syncVoiceAvailability(true);

        function clearCallBinding() {
            activeSessionId = null;
            callConversationId = null;
            completionRetryPending = false;
            configData = null;
            resetVoiceEventTracking();
        }

        function setOverlayVisible(show) {
            if (!overlay) {
                return;
            }
            if (show) {
                overlay.classList.remove('hidden');
                overlay.setAttribute('aria-hidden', 'false');
                voiceButton.classList.add('active');
                voiceButton.setAttribute('aria-pressed', 'true');
                startStopButton.focus({ preventScroll: true });
            } else {
                overlay.classList.add('hidden');
                overlay.setAttribute('aria-hidden', 'true');
                voiceButton.classList.remove('active');
                voiceButton.setAttribute('aria-pressed', 'false');
            }
        }

        function updateMuteUI() {
            if (!muteButton) {
                return;
            }
            const icon = muteButton.querySelector('i');
            muteButton.classList.toggle('muted', muteState);
            muteButton.setAttribute('aria-pressed', muteState ? 'true' : 'false');
            muteButton.setAttribute('title', tr(muteState ? 'activate_microphone' : 'mute_microphone'));
            if (icon) {
                icon.className = muteState ? 'fas fa-microphone-slash' : 'fas fa-microphone';
            }
        }

        function message(presentation) {
            if (!presentation?.key) return presentation?.text || '';
            return presentation.key.includes('.')
                ? AurvekI18n.t(presentation.key, presentation.params)
                : tr(presentation.key, presentation.params);
        }

        function repaintLocalizedState() {
            if (localizedState?.message) statusText.textContent = message(localizedState.message);
            if (helperText && localizedState?.helper) helperText.textContent = message(localizedState.helper);
            if (caption && captionPresentation) caption.textContent = message(captionPresentation);
            const actionKey = {loading: 'loading', ready: 'start_call', connecting: 'connecting',
                active: 'end_call', updating: 'saving', error: 'retry'}[currentState];
            if (actionKey) startStopButton.textContent = tr(actionKey);
            updateMuteUI();
            if (voiceAvailability) {
                const reason = voiceAvailability.available ? '' : localizedVoiceError(voiceAvailability.error_code);
                applyVoiceAvailability(Boolean(voiceAvailability.available), reason, voiceAvailability.error_code);
            }
        }

        function setLocalizedState(state, messageKey, options = {}) {
            const defaultHelpers = {loading: 'getting_configuration', ready: 'press_start',
                connecting: 'establishing', active: 'speak_normally',
                updating: 'retrieving_transcript', error: 'check_connection'};
            localizedState = {
                message: messageKey ? {key: messageKey, params: options.messageParams} : null,
                helper: {key: options.helperKey || defaultHelpers[state], params: options.helperParams}
            };
            setState(state, message(localizedState.message), {
                helper: localizedState.helper ? message(localizedState.helper) : undefined,
                localized: true
            });
        }

        function setState(state, message, options = {}) {
            if (!options.localized) localizedState = null;
            currentState = state;
            window.WellbeingVoiceActive = ['connecting', 'active', 'updating'].includes(state);
            if (message) {
                statusText.textContent = message;
            }
            statusIcon.className = 'voice-status-icon';
            if (helperText && options.helper) {
                helperText.textContent = options.helper;
            }

            switch (state) {
                case 'loading':
                    startStopButton.disabled = true;
                    startStopButton.textContent = tr('loading');
                    if (helperText && !options.helper) {
                        helperText.textContent = tr('getting_configuration');
                    }
                    closeButton.disabled = false;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('loading');
                    statusIcon.innerHTML = '<i class="fas fa-clock"></i>';
                    lockChatInputs(false);
                    break;
                case 'ready':
                    startStopButton.disabled = false;
                    startStopButton.textContent = tr('start_call');
                    if (helperText && !options.helper) {
                        helperText.textContent = tr('press_start');
                    }
                    closeButton.disabled = false;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('ready');
                    statusIcon.innerHTML = '<i class="fas fa-check"></i>';
                    lockChatInputs(false);
                    break;
                case 'connecting':
                    startStopButton.disabled = true;
                    startStopButton.textContent = tr('connecting');
                    if (helperText && !options.helper) {
                        helperText.textContent = tr('establishing');
                    }
                    closeButton.disabled = true;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('connecting');
                    statusIcon.innerHTML = '<i class="fas fa-plug"></i>';
                    lockChatInputs(true);
                    break;
                case 'active':
                    startStopButton.disabled = false;
                    startStopButton.textContent = tr('end_call');
                    if (helperText && !options.helper) {
                        helperText.textContent = tr('speak_normally');
                    }
                    closeButton.disabled = true;
                    muteButton.classList.remove('hidden');
                    statusIcon.classList.add('active');
                    statusIcon.innerHTML = '<i class="voice-wave-icon"></i>';
                    lockChatInputs(true);
                    break;
                case 'updating':
                    startStopButton.disabled = true;
                    startStopButton.textContent = tr('saving');
                    if (helperText && !options.helper) {
                        helperText.textContent = tr('retrieving_transcript');
                    }
                    closeButton.disabled = true;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('updating');
                    statusIcon.innerHTML = '<i class="fas fa-sync-alt"></i>';
                    lockChatInputs(true);
                    break;
                case 'error':
                    startStopButton.disabled = false;
                    startStopButton.textContent = tr('retry');
                    if (helperText && !options.helper) {
                        helperText.textContent = tr('check_connection');
                    }
                    closeButton.disabled = false;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('error');
                    statusIcon.innerHTML = '<i class="fas fa-exclamation-triangle"></i>';
                    lockChatInputs(false);
                    break;
            }
        }

        async function ensureMicPermission() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                return false;
            }
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                stream.getTracks().forEach((track) => track.stop());
                return true;
            } catch (error) {
                console.error('Microphone permission denied:', error);
                return false;
            }
        }

        function extractSessionId(info) {
            if (!info) {
                return '';
            }
            if (typeof info === 'string') {
                return info;
            }
            if (typeof info === 'object') {
                // ElevenLabs SDK passes an object with conversationId property
                // or sometimes the ID is nested in conversationId.conversationId
                const id = info.conversationId || info.sessionId || info.id || info.conversation_id || '';

                // Handle nested case where conversationId is itself an object
                if (typeof id === 'object' && id.conversationId) {
                    return id.conversationId;
                }

                return id || '';
            }
            return '';
        }

        function buildConversationConfig() {
            if (!configData || !isSameConversation(configData.conversation_id, callConversationId)) {
                return null;
            }
            const Conversation = resolveConversation();
            if (!Conversation) {
                return null;
            }

            const dynamicVariables = {};

            // Add context if available (will replace {{context}} in agent template)
            if (configData.context) {
                dynamicVariables.context = configData.context;
            } else {
                dynamicVariables.context = "";
            }

            // Add personality template (will replace {{personality_template}} in agent template)
            // This is the actual prompt text from AURVEK's database
            if (configData.prompt_text) {
                dynamicVariables.personality_template = configData.prompt_text;
            } else {
                dynamicVariables.personality_template = "";
            }

            // Add all other variables that might be in the agent template (matching ConvAI exactly)
            // The backend resolves this from the user's primary language. An
            // English remains the platform fallback for users who have not
            // configured a preference yet.
            dynamicVariables.language = configData.language || "English";
            dynamicVariables.prev_persona = "";
            dynamicVariables.persona_transition_instruction = "Continue the previous conversation naturally, picking up exactly where it left off.";
            dynamicVariables.persona_name = configData.prompt_name || "";
            dynamicVariables.conversation_chain_id = `aurvek_${configData.conversation_id}_${Date.now()}`;
            dynamicVariables.accumulated_context = "";
            dynamicVariables.voice_id = configData.voice_id || "";
            if (configData.voice_id) {
                dynamicVariables.voice = configData.voice_id;
            }

            // Add conversation metadata
            dynamicVariables.aurvek_conversation_id = String(configData.conversation_id);
            if (configData.prompt_id) {
                dynamicVariables.aurvek_prompt_id = String(configData.prompt_id);
            }

            // Add user ID for ElevenLabs agent tracking
            if (configData.user_id) {
                dynamicVariables.user_id = String(configData.user_id);
                dynamicVariables.aurvek_user_id = String(configData.user_id);
            }

            // Watchdog: inject steering hint as dynamic variable if available
            if (configData.watchdog_steering_hint) {
                dynamicVariables.watchdog_steering_hint = configData.watchdog_steering_hint;
            } else {
                dynamicVariables.watchdog_steering_hint = "";
            }

            const options = {
                agentId: configData.agent_id,
                dynamicVariables,
                clientData: {
                    aurvek_conversation_id: String(configData.conversation_id),
                    aurvek_prompt_name: configData.prompt_name || '',
                    aurvek_user_id: configData.user_id ? String(configData.user_id) : ''
                },
                onConnect: handleConnected,
                onDisconnect: handleDisconnected,
                onError: handleSessionError,
                onMessage: handleSdkMessage,
                // Unknown callback keys are ignored by older SDK bundles. Newer
                // releases can expose either a dedicated correction callback or
                // the raw client event through onDebug.
                onInterruption: handleSdkInterruption,
                onAgentResponseCorrection: handleAgentResponseCorrection,
                onDebug: handleSdkDebug
            };

            if (configData.signed_url) {
                options.signedUrl = configData.signed_url;
            }
            if (configData.voice_id) {
                options.voiceId = configData.voice_id;
                options.overrides = {
                    voiceId: configData.voice_id,
                    voice: configData.voice_id,
                    tts: {
                        voiceId: configData.voice_id,
                        voice_id: configData.voice_id
                    }
                };
            }
            return options;
        }

        async function fetchConfig(force = false, conversationId = getSelectedConversationId()) {
            if (voiceAvailability?.transport === 'aurvek') {
                configData = {transport: 'aurvek', conversation_id: conversationId};
                captionPresentation = {key: 'caption'};
                if (caption) caption.textContent = message(captionPresentation);
                setLocalizedState('ready', 'ready');
                return configData;
            }
            if (loadingConfig) {
                return null;
            }
            if (configData && !force && isSameConversation(configData.conversation_id, conversationId)) {
                return configData;
            }
            if (conversationId === null) {
                setLocalizedState('error', 'select_chat', {helperKey: 'choose_conversation'});
                return null;
            }

            loadingConfig = true;
            setLocalizedState('loading', 'requesting_configuration');
            try {
                const response = await secureFetch(`/api/conversations/${conversationId}/elevenlabs/config`);
                if (!response) {
                    throw new Error(tr('request_failed'));
                }
                if (!response.ok) {
                    let errorKey = 'configuration_failed';
                    try {
                        const errorPayload = await response.json();
                        errorKey = localizedVoiceErrorKey(voiceErrorCode(errorPayload), response.status);
                    } catch (_) {
                        // ignore
                    }
                    setLocalizedState('error', errorKey, {
                        helperKey: response.status === 402 ? 'add_credit' : errorKey
                    });
                    return null;
                }
                const data = await response.json();
                if (!isSameConversation(data.conversation_id, conversationId)) {
                    throw new Error(tr('configuration_mismatch'));
                }
                configData = data;
                if (promptTag && promptAvatar && promptName) {
                    if (data.prompt_name) {
                        // Create large avatar similar to prompt-info
                        promptAvatar.innerHTML = '';

                        // First set the initial letter as background
                        const botInitial = (data.prompt_name && data.prompt_name.length > 0)
                            ? data.prompt_name.charAt(0).toUpperCase()
                            : 'A';
                        promptAvatar.textContent = botInitial;
                        promptAvatar.title = data.prompt_name;

                        // If there's a bot profile picture, overlay it
                        const voiceAvatarUrl = (
                            typeof botProfilePictureFullsize !== 'undefined' && botProfilePictureFullsize
                        ) || (
                            typeof botProfilePicture128 !== 'undefined' && botProfilePicture128
                        ) || (
                            typeof botProfilePicture !== 'undefined' && botProfilePicture
                        ) || '';
                        if (voiceAvatarUrl) {
                            const avatarImg = document.createElement('img');
                            avatarImg.src = voiceAvatarUrl;
                            avatarImg.alt = data.prompt_name;
                            avatarImg.title = data.prompt_name;
                            promptAvatar.appendChild(avatarImg);
                        }

                        promptName.textContent = data.prompt_name;
                        promptTag.classList.remove('hidden');
                    } else {
                        promptAvatar.innerHTML = '';
                        promptName.textContent = '';
                        promptTag.classList.add('hidden');
                    }
                }
                if (caption) {
                    captionPresentation = data.conversation_name
                        ? {key: 'conversation_caption', params: {name: data.conversation_name}}
                        : {key: 'activate_to_start'};
                    caption.textContent = message(captionPresentation);
                }
                setLocalizedState('ready', 'ready');
                return data;
            } catch (error) {
                console.error('Error fetching ElevenLabs configuration:', error);
                setLocalizedState('error', 'configuration_failed', {helperKey: 'check_connection'});
                return null;
            } finally {
                loadingConfig = false;
            }
        }

        async function markSessionStarted(sessionId) {
            const conversationId = callConversationId;
            if (conversationId === null || !sessionId) {
                return {
                    ok: false,
                    message: tr('missing_binding'), message_key: 'missing_binding'
                };
            }
            try {
                const sessionBody = { session_id: sessionId };
                // Watchdog: send CAS token so backend can consume the hint
                if (configData && configData.watchdog_hint_eval_id != null) {
                    sessionBody.watchdog_hint_eval_id = configData.watchdog_hint_eval_id;
                }
                const response = await secureFetch(`/api/conversations/${conversationId}/elevenlabs/session`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(sessionBody)
                });
                if (!response) {
                    return {
                        ok: false,
                        message: tr('start_no_response'), message_key: 'start_no_response'
                    };
                }
                if (!response.ok) {
                    let message = tr('start_failed');
                    let messageKey = 'start_failed';
                    let error = null;
                    try {
                        const payload = await response.json();
                        error = voiceErrorCode(payload);
                        message = localizedVoiceError(error, response.status);
                        messageKey = localizedVoiceErrorKey(error, response.status);
                    } catch (_) {
                        // ignore
                    }
                    console.warn('Failed to mark ElevenLabs session as active');
                    return { ok: false, error, message, message_key: messageKey };
                }
                return { ok: true };
            } catch (error) {
                console.error('Error notifying session start:', error);
                return {
                    ok: false,
                    message: tr('start_notify_failed'), message_key: 'start_notify_failed'
                };
            }
        }

        async function markSessionStatus(
            status,
            conversationId = callConversationId,
            sessionId = activeSessionId
        ) {
            if (conversationId === null || !sessionId) {
                return;
            }
            try {
                const response = await secureFetch(`/api/conversations/${conversationId}/elevenlabs/stop`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        session_id: sessionId,
                        status
                    })
                });
                if (response && !response.ok) {
                    console.warn('Failed to update ElevenLabs session status');
                }
            } catch (error) {
                console.error('Error updating ElevenLabs status:', error);
            }
        }

        async function cancelPendingCompletion() {
            if (completionRetryPending && activeSessionId && callConversationId !== null) {
                await markSessionStatus('failed', callConversationId, activeSessionId);
            }
            conversationRef = null;
            sessionStartRejected = false;
            clearCallBinding();
        }

        async function startCall() {
            if (completionRetryPending) {
                await completeSession();
                return;
            }
            if (voiceAvailability?.transport === 'aurvek') {
                callConversationId = getSelectedConversationId();
                setLocalizedState('connecting', 'connecting_voice', {helperKey: 'allow_microphone'});
                let destinationUrl = null;
                let recordingNoticeKey = null;
                try {
                    conversationRef = await window.AurvekBrowserVoice.startSession({
                        conversationId: callConversationId,
                        onState(state, message) {
                            if (message === 'voice_recording_unavailable') {
                                recordingNoticeKey = localizedVoiceErrorKey(message);
                            }
                            setLocalizedState('active', ({listening: 'listening',
                                thinking: 'thinking', speaking: 'assistant_speaking'}[state] || 'call_active'),
                                {helperKey: recordingNoticeKey});
                        },
                        onHandoff(result) {
                            callConversationId = result.conversation_id;
                            destinationUrl = result.reload_url;
                            if (promptName) promptName.textContent = result.assistant_id;
                        },
                        onError() { setLocalizedState('error', 'call_error'); },
                        async onDisconnect() {
                            conversationRef = null;
                            clearCallBinding();
                            muteState = false; updateMuteUI();
                            setLocalizedState('ready', 'call_ended', {helperKey: recordingNoticeKey});
                            if (destinationUrl) window.location.assign(destinationUrl);
                            else if (typeof window.refreshActiveConversation === 'function') await window.refreshActiveConversation();
                        }
                    });
                } catch (error) {
                    conversationRef = null;
                    clearCallBinding();
                    setLocalizedState('error', 'start_failed');
                }
                return;
            }
            const Conversation = resolveConversation();
            if (!Conversation) {
                setLocalizedState('error', 'sdk_unavailable', {helperKey: 'reload_page'});
                return;
            }

            const wellbeingStatus = window.WellbeingReminders && window.WellbeingReminders.latestStatus;
            if (wellbeingStatus && (wellbeingStatus.active_pause || wellbeingStatus.reminder?.requires_pause)) {
                setLocalizedState('error', 'pause_required', {helperKey: 'use_break_reminder'});
                return;
            }

            if (window.ChatWarmup && typeof window.ChatWarmup.signal === 'function') {
                window.ChatWarmup.signal('voice_call', {});
            }

            const targetConversationId = getSelectedConversationId();
            if (targetConversationId === null) {
                setLocalizedState('error', 'select_chat');
                return;
            }
            callConversationId = targetConversationId;
            resetVoiceEventTracking();

            // Force refresh to get fresh watchdog hint from backend
            const config = await fetchConfig(true, targetConversationId);
            if (!config) {
                callConversationId = null;
                return;
            }
            const hasMic = await ensureMicPermission();
            if (!hasMic) {
                setLocalizedState('ready', 'microphone_required', {helperKey: 'grant_permissions'});
                callConversationId = null;
                return;
            }
            if (!isSameConversation(getSelectedConversationId(), targetConversationId)) {
                configData = null;
                callConversationId = null;
                setLocalizedState('error', 'chat_changed', {helperKey: 'reopen_panel'});
                return;
            }
            const sessionConfig = buildConversationConfig();
            if (!sessionConfig) {
                setLocalizedState('error', 'prepare_failed');
                callConversationId = null;
                return;
            }

            muteState = false;
            sessionStartRejected = false;
            completionRetryPending = false;
            updateMuteUI();
            setLocalizedState('connecting', 'connecting_service');

            try {
                const startedConversation = await Conversation.startSession(sessionConfig);
                conversationRef = startedConversation;
                // Some SDK versions invoke onConnect before startSession resolves.
                // If server-side binding rejected that callback, close the handle
                // as soon as it becomes available instead of orphaning the call.
                if (sessionStartRejected && startedConversation?.endSession) {
                    await startedConversation.endSession();
                    conversationRef = null;
                }
            } catch (error) {
                console.error('Error starting ElevenLabs conversation:', error);
                setLocalizedState('error', 'start_failed', {helperKey: 'check_agent_configuration'});
                await markSessionStatus('failed');
                conversationRef = null;
                clearCallBinding();
                sessionStartRejected = false;
            }
        }

        async function stopCall() {
            if (!conversationRef || !conversationRef.endSession) {
                await completeSession();
                return;
            }
            setLocalizedState('updating', 'closing_call');
            try {
                await conversationRef.endSession();
            } catch (error) {
                console.error('Error stopping ElevenLabs session:', error);
                await completeSession();
            }
        }

        async function completeSession() {
            if (!activeSessionId || callConversationId === null) {
                setLocalizedState('ready', 'call_ended');
                conversationRef = null;
                clearCallBinding();
                sessionStartRejected = false;
                lockChatInputs(false);
                return;
            }
            if (completing) {
                return;
            }
            const completedConversationId = callConversationId;
            const completedSessionId = activeSessionId;
            let clearBindingWhenDone = false;
            completing = true;
            setLocalizedState('updating', 'saving_transcript', {helperKey: 'may_take_seconds'});
            try {
                const completionBody = { session_id: completedSessionId };
                if (typeof buildClientCorrectionsPayload === 'function') {
                    const corrections = buildClientCorrectionsPayload();
                    if (corrections.length > 0) {
                        completionBody.client_corrections = corrections;
                    }
                }
                const response = await secureFetch(`/api/conversations/${completedConversationId}/elevenlabs/complete`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(completionBody)
                });
                if (!response) {
                    throw new Error(tr('request_failed'));
                }
                if (!response.ok) {
                    let errorKey = 'save_transcript_failed';
                    try {
                        const payload = await response.json();
                        errorKey = localizedVoiceErrorKey(voiceErrorCode(payload), response.status);
                    } catch (_) {
                        // ignore
                    }
                    const retryable = response.status === 425 || response.status === 502;
                    setLocalizedState('error', errorKey, {
                        helperKey: retryable ? 'retry_same_call' : 'cannot_complete'
                    });
                    if (retryable) {
                        completionRetryPending = true;
                    } else {
                        await markSessionStatus('failed', completedConversationId, completedSessionId);
                        clearBindingWhenDone = true;
                    }
                    return;
                }
                const data = await response.json();
                const saved = data && typeof data.messages_saved === 'number' ? data.messages_saved : 0;
                completionRetryPending = false;
                clearBindingWhenDone = true;

                // Refresh the chat messages if we saved something
                if (saved > 0 && isSameConversation(getSelectedConversationId(), completedConversationId)) {
                    // Show loading state in overlay
                    setLocalizedState('updating', 'updating_chat', {helperKey: 'syncing_messages'});

                    // Wait a bit for visual feedback
                    await wait(500);

                    let refreshed = false;

                    // Try to refresh messages
                    if (typeof window.refreshActiveConversation === 'function') {
                        try {
                            // Use helper to reset pagination and pull fresh messages
                            await window.refreshActiveConversation();
                            refreshed = true;
                        } catch (error) {
                            console.error('Error refreshing chat via refreshActiveConversation:', error);
                        }
                    }

                    if (!refreshed && typeof loadMessages === 'function') {
                        try {
                            // Force reload messages without clearing the chat when helper is unavailable
                            await loadMessages(completedConversationId);
                            refreshed = true;
                        } catch (error) {
                            console.error('Error refreshing messages after voice call:', error);
                        }
                    }

                    if (refreshed) {
                        // Scroll to bottom to show new messages
                        const windowChat = document.getElementById('window-chat');
                        if (windowChat) {
                            setTimeout(() => {
                                windowChat.scrollTop = windowChat.scrollHeight;
                            }, 200);
                        }
                    }
                }
                // Success state with visual feedback
                if (saved > 0) {
                    // Create success state with animation
                    statusText.replaceChildren();
                    const savedIcon = document.createElement('i');
                    savedIcon.className = 'fas fa-check-circle';
                    savedIcon.style.color = '#10b981';
                    savedIcon.style.marginRight = '8px';
                    const savedText = document.createElement('span');
                    AurvekI18n.bindText(savedText, 'chat_widgets.voice_call.transcript_saved', {count: saved});
                    statusText.append(savedIcon, savedText);
                    statusText.style.transform = 'scale(1.1)';
                    statusText.style.transition = 'transform 0.3s ease';

                    setTimeout(() => {
                        statusText.style.transform = 'scale(1)';
                    }, 300);

                    setLocalizedState('ready', null, {helperKey: 'close_or_new'});
                } else {
                    setLocalizedState('ready', 'call_ended', {helperKey: 'start_another'});
                }
            } catch (error) {
                console.error('Error completing ElevenLabs session:', error);
                if (clearBindingWhenDone) {
                    setLocalizedState('ready', 'call_ended', {helperKey: 'saved_refresh_failed'});
                } else {
                    setLocalizedState('error', 'save_transcript_failed', {helperKey: 'retry_same_call'});
                    completionRetryPending = true;
                }
            } finally {
                completing = false;
                conversationRef = null;
                lockChatInputs(false);
                muteState = false;
                updateMuteUI();
                if (clearBindingWhenDone) {
                    // Reset config to avoid reusing stale watchdog hints on consecutive calls.
                    clearCallBinding();
                }
                closeButton.disabled = false;
            }
        }

        async function handleConnected(info) {
            activeSessionId = extractSessionId(info);
            const sessionResult = await markSessionStarted(activeSessionId);
            if (!sessionResult.ok) {
                sessionStartRejected = true;
                const shouldRefreshWellbeing = sessionResult.error === 'wellbeing_pause_active'
                    || sessionResult.error === 'wellbeing_pause_required';
                if (shouldRefreshWellbeing && window.WellbeingReminders && typeof window.WellbeingReminders.refresh === 'function') {
                    await window.WellbeingReminders.refresh();
                }
                const ref = conversationRef;
                clearCallBinding();
                conversationRef = null;
                setLocalizedState('error', sessionResult.message_key || localizedVoiceErrorKey(sessionResult.error), {
                    helperKey: shouldRefreshWellbeing ? 'use_break_reminder' : 'try_when_ready'
                });
                lockChatInputs(false);
                closeButton.disabled = false;
                if (ref && typeof ref.endSession === 'function') {
                    try {
                        await ref.endSession();
                    } catch (error) {
                        console.error('Error closing rejected ElevenLabs session:', error);
                    }
                }
                return;
            }
            setLocalizedState('active', 'call_active');
        }

        async function handleDisconnected() {
            await completeSession();
        }

        async function handleSessionError(error) {
            console.error('ElevenLabs session error:', error);
            setLocalizedState('error', 'call_error', {helperKey: 'restart_when_ready'});
            await markSessionStatus('failed');
            conversationRef = null;
            clearCallBinding();
            lockChatInputs(false);
        }

        muteButton.addEventListener('click', () => {
            if (!conversationRef || !conversationRef.setMicMuted) {
                return;
            }
            muteState = !muteState;
            try {
                conversationRef.setMicMuted(muteState);
            } catch (error) {
                console.error('Error toggling ElevenLabs microphone:', error);
                muteState = !muteState;
            }
            updateMuteUI();
        });

        startStopButton.addEventListener('click', async () => {
            if (currentState === 'ready') {
                await startCall();
            } else if (currentState === 'error') {
                if (completionRetryPending && activeSessionId && callConversationId !== null) {
                    await completeSession();
                } else {
                    await fetchConfig(true);
                }
            } else if (currentState === 'active') {
                await stopCall();
            }
        });

        async function openVoicePanel(toggle = false) {
            const isVisible = !overlay.classList.contains('hidden');
            if (isVisible) {
                if (!toggle) return;
                if (currentState === 'active' || currentState === 'connecting' || currentState === 'updating') {
                    return;
                }
                await cancelPendingCompletion();
                setOverlayVisible(false);
                return;
            }
            const availability = await syncVoiceAvailability(true);
            if (!availability || !availability.available) {
                return;
            }
            setOverlayVisible(true);
            if (completionRetryPending) {
                setLocalizedState('error', 'transcript_pending', {helperKey: 'retry_same_call'});
                return;
            }
            await fetchConfig(false);
        }
        window.AurvekVoice = Object.freeze({ open: openVoicePanel });
        voiceButton.addEventListener('click', () => { void openVoicePanel(true); });

        closeButton.addEventListener('click', async () => {
            if (currentState === 'active' || currentState === 'connecting' || currentState === 'updating') {
                return;
            }
            await cancelPendingCompletion();
            setOverlayVisible(false);
            setLocalizedState('ready', 'ready', {helperKey: 'press_voice_icon'});
        });

        setLocalizedState('ready', 'ready', {helperKey: 'press_voice_icon'});
        setOverlayVisible(false);
        updateMuteUI();
        AurvekI18n.onChange(repaintLocalizedState);
        void syncVoiceAvailability(true);
        if (incognitoBadge && typeof MutationObserver !== 'undefined') {
            const observer = new MutationObserver(syncVoiceAvailability);
            observer.observe(incognitoBadge, {
                attributes: true,
                attributeFilter: ['hidden']
            });
        }
    });
})();
