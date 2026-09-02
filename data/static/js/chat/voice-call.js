(function() {
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
        const voiceButton = document.getElementById('plus-voice-call');
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
        const availabilityReason = document.getElementById('voice-call-availability-reason');

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
        const defaultVoiceButtonTitle = voiceButton.title || 'AI voice assistant';

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

        function applyVoiceAvailability(available, reason = '', errorCode = '') {
            const canonicalReason = String(reason || '').trim();
            const incognito = errorCode === 'conversation_incognito';
            voiceButton.disabled = !available;
            voiceButton.hidden = incognito;
            voiceButton.setAttribute('aria-disabled', available ? 'false' : 'true');
            voiceButton.dataset.availabilityCode = errorCode || '';
            voiceButton.title = available ? defaultVoiceButtonTitle : canonicalReason;
            voiceButton.setAttribute(
                'aria-label',
                available ? defaultVoiceButtonTitle : `AI Voice unavailable. ${canonicalReason}`
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
                    reason: 'Select a conversation to check browser voice availability.'
                };
                applyVoiceAvailability(false, unavailable.reason, unavailable.error_code);
                return unavailable;
            }

            const pendingReason = isIncognitoConversationActive()
                ? 'Browser voice calls are unavailable in incognito chats.'
                : 'Checking browser voice availability…';
            applyVoiceAvailability(
                false,
                pendingReason,
                isIncognitoConversationActive() ? 'conversation_incognito' : 'availability_pending'
            );

            try {
                const response = await secureFetch(
                    `/api/conversations/${conversationId}/elevenlabs/availability`
                );
                if (!response) {
                    throw new Error('No response received');
                }
                const payload = await response.json();
                if (requestSequence !== availabilityRequestSequence
                    || !isSameConversation(getSelectedConversationId(), conversationId)) {
                    return null;
                }
                if (!response.ok) {
                    const reason = payload.reason || payload.message || payload.error
                        || 'Browser voice availability could not be checked.';
                    voiceAvailability = {
                        available: false,
                        error_code: payload.error_code || 'availability_check_failed',
                        reason
                    };
                } else {
                    voiceAvailability = payload;
                }
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
                    reason: 'Browser voice availability could not be checked. Check your connection and try again.'
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
            muteButton.setAttribute('title', muteState ? 'Activate microphone' : 'Mute microphone');
            if (icon) {
                icon.className = muteState ? 'fas fa-microphone-slash' : 'fas fa-microphone';
            }
        }

        function setState(state, message, options = {}) {
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
                    startStopButton.textContent = 'Loading...';
                    if (helperText && !options.helper) {
                        helperText.textContent = 'Getting ElevenLabs configuration.';
                    }
                    closeButton.disabled = false;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('loading');
                    statusIcon.innerHTML = '<i class="fas fa-clock"></i>';
                    lockChatInputs(false);
                    break;
                case 'ready':
                    startStopButton.disabled = false;
                    startStopButton.textContent = 'Start call';
                    if (helperText && !options.helper) {
                        helperText.textContent = 'Press Start call to begin.';
                    }
                    closeButton.disabled = false;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('ready');
                    statusIcon.innerHTML = '<i class="fas fa-check"></i>';
                    lockChatInputs(false);
                    break;
                case 'connecting':
                    startStopButton.disabled = true;
                    startStopButton.textContent = 'Connecting...';
                    if (helperText && !options.helper) {
                        helperText.textContent = 'Establishing session with ElevenLabs.';
                    }
                    closeButton.disabled = true;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('connecting');
                    statusIcon.innerHTML = '<i class="fas fa-plug"></i>';
                    lockChatInputs(true);
                    break;
                case 'active':
                    startStopButton.disabled = false;
                    startStopButton.textContent = 'End call';
                    if (helperText && !options.helper) {
                        helperText.textContent = 'Speak normally. Use End call when finished.';
                    }
                    closeButton.disabled = true;
                    muteButton.classList.remove('hidden');
                    statusIcon.classList.add('active');
                    statusIcon.innerHTML = '<i class="voice-wave-icon"></i>';
                    lockChatInputs(true);
                    break;
                case 'updating':
                    startStopButton.disabled = true;
                    startStopButton.textContent = 'Saving...';
                    if (helperText && !options.helper) {
                        helperText.textContent = 'Retrieving transcript from ElevenLabs.';
                    }
                    closeButton.disabled = true;
                    muteButton.classList.add('hidden');
                    statusIcon.classList.add('updating');
                    statusIcon.innerHTML = '<i class="fas fa-sync-alt"></i>';
                    lockChatInputs(true);
                    break;
                case 'error':
                    startStopButton.disabled = false;
                    startStopButton.textContent = 'Retry';
                    if (helperText && !options.helper) {
                        helperText.textContent = 'Check your connection and try again.';
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
            dynamicVariables.language = "English";
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
                onMessage: () => {}
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
            if (loadingConfig) {
                return null;
            }
            if (configData && !force && isSameConversation(configData.conversation_id, conversationId)) {
                return configData;
            }
            if (conversationId === null) {
                setState('error', 'Select a chat before starting the call.', {
                    helper: 'Choose a conversation and try again.'
                });
                return null;
            }

            loadingConfig = true;
            setState('loading', 'Requesting ElevenLabs configuration...');
            try {
                const response = await secureFetch(`/api/conversations/${conversationId}/elevenlabs/config`);
                if (!response) {
                    throw new Error('No response received');
                }
                if (!response.ok) {
                    let detail = 'Could not get ElevenLabs configuration.';
                    let errorPayload = null;
                    try {
                        errorPayload = await response.json();
                        if (errorPayload && (errorPayload.reason || errorPayload.message || errorPayload.error)) {
                            detail = errorPayload.reason || errorPayload.message || errorPayload.error;
                        }
                    } catch (_) {
                        // ignore
                    }
                    const helper = response.status === 402
                        ? 'Add credit to your account and try again.'
                        : detail;
                    setState('error', detail, { helper: helper });
                    return null;
                }
                const data = await response.json();
                if (!isSameConversation(data.conversation_id, conversationId)) {
                    throw new Error('Configuration does not match the requested conversation');
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
                    caption.textContent = data.conversation_name
                        ? `Conversation ${data.conversation_name}`
                        : 'Activate your microphone to start the ElevenLabs session.';
                }
                setState('ready', 'Ready to start the call.');
                return data;
            } catch (error) {
                console.error('Error fetching ElevenLabs configuration:', error);
                setState('error', 'Could not get ElevenLabs configuration.', {
                    helper: 'Check your connection and try again.'
                });
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
                    message: 'The voice session is missing its conversation binding.'
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
                        message: 'No response received when starting the voice session.'
                    };
                }
                if (!response.ok) {
                    let message = 'Failed to start the voice session.';
                    let error = null;
                    try {
                        const payload = await response.json();
                        error = payload && payload.error;
                        message = (payload && (payload.message || payload.error)) || message;
                    } catch (_) {
                        // ignore
                    }
                    console.warn('Failed to mark ElevenLabs session as active');
                    return { ok: false, error, message };
                }
                return { ok: true };
            } catch (error) {
                console.error('Error notifying session start:', error);
                return {
                    ok: false,
                    message: 'Could not notify the server that the voice session started.'
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
            const Conversation = resolveConversation();
            if (!Conversation) {
                setState('error', 'ElevenLabs SDK not available on this page.', {
                    helper: 'Reload the page or verify /sdk/elevenlabs-client.js'
                });
                return;
            }

            const wellbeingStatus = window.WellbeingReminders && window.WellbeingReminders.latestStatus;
            if (wellbeingStatus && (wellbeingStatus.active_pause || wellbeingStatus.reminder?.requires_pause)) {
                setState('error', 'A break pause is required before starting a voice call.', {
                    helper: 'Use the break reminder prompt before continuing.'
                });
                return;
            }

            if (window.ChatWarmup && typeof window.ChatWarmup.signal === 'function') {
                window.ChatWarmup.signal('voice_call', {});
            }

            const targetConversationId = getSelectedConversationId();
            if (targetConversationId === null) {
                setState('error', 'Select a chat before starting the call.');
                return;
            }
            callConversationId = targetConversationId;

            // Force refresh to get fresh watchdog hint from backend
            const config = await fetchConfig(true, targetConversationId);
            if (!config) {
                callConversationId = null;
                return;
            }
            const hasMic = await ensureMicPermission();
            if (!hasMic) {
                setState('ready', 'Microphone access required.', {
                    helper: 'Grant browser permissions and try again.'
                });
                callConversationId = null;
                return;
            }
            if (!isSameConversation(getSelectedConversationId(), targetConversationId)) {
                configData = null;
                callConversationId = null;
                setState('error', 'The selected chat changed before the call started.', {
                    helper: 'Open the voice panel again in the chat you want to call.'
                });
                return;
            }
            const sessionConfig = buildConversationConfig();
            if (!sessionConfig) {
                setState('error', 'Could not prepare call configuration.');
                callConversationId = null;
                return;
            }

            muteState = false;
            sessionStartRejected = false;
            completionRetryPending = false;
            updateMuteUI();
            setState('connecting', 'Connecting to ElevenLabs...');

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
                setState('error', 'Could not start ElevenLabs call.', {
                    helper: 'Try again or check the agent configuration.'
                });
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
            setState('updating', 'Closing call...');
            try {
                await conversationRef.endSession();
            } catch (error) {
                console.error('Error stopping ElevenLabs session:', error);
                await completeSession();
            }
        }

        async function completeSession() {
            if (!activeSessionId || callConversationId === null) {
                setState('ready', 'The call has ended.');
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
            setState('updating', 'Saving call transcript...', {
                helper: 'This may take a few seconds.'
            });
            try {
                const response = await secureFetch(`/api/conversations/${completedConversationId}/elevenlabs/complete`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ session_id: completedSessionId })
                });
                if (!response) {
                    throw new Error('No response received');
                }
                if (!response.ok) {
                    let detail = 'Could not save transcript.';
                    try {
                        const payload = await response.json();
                        if (payload && payload.error) {
                            detail = payload.error;
                        }
                    } catch (_) {
                        // ignore
                    }
                    const retryable = response.status === 425 || response.status === 502;
                    setState('error', detail, {
                        helper: retryable
                            ? 'Retry to fetch the transcript from this same call.'
                            : 'This call cannot be completed. Close the panel and try a new call.'
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
                    setState('updating', 'Updating chat...', {
                        helper: 'Syncing messages in main chat.'
                    });

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
                    statusText.innerHTML = `<i class="fas fa-check-circle" style="color: #10b981; margin-right: 8px;"></i>Transcript saved (${saved} messages)`;
                    statusText.style.transform = 'scale(1.1)';
                    statusText.style.transition = 'transform 0.3s ease';

                    setTimeout(() => {
                        statusText.style.transform = 'scale(1)';
                    }, 300);

                    if (helperText) {
                        helperText.innerHTML = 'You can close this window or start a new conversation';
                    }

                    setState('ready', '', {
                        helper: 'You can close this window or start a new conversation'
                    });
                } else {
                    setState('ready', 'The call has ended.', {
                        helper: 'You can start another call whenever you want.'
                    });
                }
            } catch (error) {
                console.error('Error completing ElevenLabs session:', error);
                if (clearBindingWhenDone) {
                    setState('ready', 'The call has ended.', {
                        helper: 'The transcript was saved, but the chat could not be refreshed.'
                    });
                } else {
                    setState('error', 'Could not save call transcript.', {
                        helper: 'Retry to fetch the transcript from this same call.'
                    });
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
                setState('error', sessionResult.message || 'A break pause is required before starting a voice call.', {
                    helper: shouldRefreshWellbeing ? 'Use the break reminder prompt before continuing.' : 'Try again when ready.'
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
            setState('active', 'Call active. Speak normally.');
        }

        async function handleDisconnected() {
            await completeSession();
        }

        async function handleSessionError(error) {
            console.error('ElevenLabs session error:', error);
            setState('error', 'An error occurred in the ElevenLabs call.', {
                helper: 'Restart the call when ready.'
            });
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

        voiceButton.addEventListener('click', async () => {
            const isVisible = !overlay.classList.contains('hidden');
            if (isVisible) {
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
                setState('error', 'The previous call transcript is still pending.', {
                    helper: 'Retry to fetch the transcript from that same call.'
                });
                return;
            }
            await fetchConfig(false);
        });

        closeButton.addEventListener('click', async () => {
            if (currentState === 'active' || currentState === 'connecting' || currentState === 'updating') {
                return;
            }
            await cancelPendingCompletion();
            setOverlayVisible(false);
            setState('ready', 'Ready to start the call.', {
                helper: 'Press the voice icon to open this panel.'
            });
        });

        setState('ready', 'Ready to start the call.', {
            helper: 'Press the voice icon to open this panel.'
        });
        setOverlayVisible(false);
        updateMuteUI();
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
