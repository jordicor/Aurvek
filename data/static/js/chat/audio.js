let audioContext;
let ws;
let isPlaying = false; // Indicates if any audio is playing
let isBuffering = false;
let isFinished = false;
let isWaiting = false;
let sourceNode;
let audioQueue = [];
let bufferSize = 1;
let bufferTimer = null;
let isRecording = false;
let isCanceled = false;
let ttsGeneration = 0;
let ttsFetchController = null;
let currentAudioObjectUrl = null;

// Ensure currentAudioIcon is initialized
Config.currentAudioIcon = null;

// Function to automatically build WebSocket URL
function getWebSocketURL() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host; // includes port if exists
    return `${protocol}//${host}/ws`;
}

// WebSocket and AudioContext initialization
function connect(generation) {
    const wsURL = getWebSocketURL();

    const socket = new WebSocket(wsURL);
    socket.binaryType = "arraybuffer";
    ws = socket;
    initAudio();

    socket.onmessage = function(event) {
        if (socket !== ws || generation !== ttsGeneration || socket.readyState !== WebSocket.OPEN) {
            return;
        }

        if (typeof event.data === 'string') {
            const message = JSON.parse(event.data);

            switch (message.action) {
                case 'insufficient-balance':
                    showInsufficientBalancePopup();
                    break;
                case 'stopped':
                    handleStoppedMessage();
                    break;
                case 'no-content':
                    handleNoContentMessage();
                    break;
                case 'finished':
                    handleFinishedMessage();
                    break;
                default:
                    break;
            }
        } else {
            queueAudioChunk(event.data, generation);
        }
    };

    socket.onerror = function(event) {
        if (socket !== ws || generation !== ttsGeneration) return;
        console.error('WebSocket connection error', event);
        isPlaying = false;
        isWaiting = false;
        if (Config.currentAudioIcon) {
            toggleIcons(Config.currentAudioIcon, 'stopped');
        }
    };

    socket.onclose = function() {
        if (socket !== ws || generation !== ttsGeneration) return;
        if (audioQueue.length > 0 && !isPlaying) {
            playNextInQueue(generation);
        }
    };

    return socket;
}

function handleStoppedMessage() {
    stopAudioAndCloseWebSocket();
}

function handleNoContentMessage() {
    stopAudioAndCloseWebSocket();
}

function handleFinishedMessage() {
    isFinished = true;
    if (!isPlaying && audioQueue.length > 0) {
        playNextInQueue();
    }
}

function initAudio() {
    if (!audioContext || audioContext.state === 'closed') {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
}

function queueAudioChunk(arrayBuffer, generation = ttsGeneration) {
    audioContext.decodeAudioData(arrayBuffer, (audioBuffer) => {
        if (generation !== ttsGeneration) return;
        audioQueue.push(audioBuffer);
        if (!isPlaying && !isBuffering) {
            if (audioQueue.length >= bufferSize) {
                isWaiting = false;
                playNextInQueue(generation);
            } else {
                isBuffering = true;
                isWaiting = true;
                toggleIcons(Config.currentAudioIcon, 'waiting');
                scheduleBufferCheck(generation);
            }
        }
    }, (error) => {
        console.error('Error decoding audio data', error);
    });
}

function scheduleBufferCheck(generation) {
    if (bufferTimer) clearTimeout(bufferTimer);
    bufferTimer = setTimeout(() => checkBuffer(generation), 100);
}

function checkBuffer(generation = ttsGeneration) {
    bufferTimer = null;
    if (generation !== ttsGeneration) return;
    if (audioQueue.length >= bufferSize || isFinished) {
        isBuffering = false;
        isWaiting = false;
        playNextInQueue(generation);
    } else {
        scheduleBufferCheck(generation);
    }
}

function playNextInQueue(generation = ttsGeneration) {
    if (generation !== ttsGeneration) return;
    if (audioQueue.length === 0) {
        if (isFinished) {
            stopAudioAndCloseWebSocket();
        } else {
            isBuffering = true;
            isWaiting = true;
            toggleIcons(Config.currentAudioIcon, 'waiting');
            scheduleBufferCheck(generation);
        }
        return;
    }

    isPlaying = true;
    isWaiting = false;
    const audioBuffer = audioQueue.shift();
    sourceNode = audioContext.createBufferSource();
    sourceNode.buffer = audioBuffer;
    sourceNode.connect(audioContext.destination);
    sourceNode.onended = () => playNextInQueue(generation);
    sourceNode.start();
    toggleIcons(Config.currentAudioIcon, 'playing');
}

function ensureWebSocketConnection(generation) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        return Promise.resolve(ws);
    }

    return new Promise((resolve, reject) => {
        const socket = connect(generation);
        let settled = false;
        const timeout = setTimeout(() => {
            if (settled) return;
            settled = true;
            if (socket === ws) ws = null;
            try { socket.close(); } catch (error) { /* already closed */ }
            reject(new Error('WebSocket connection timeout'));
        }, 10000);

        socket.onopen = () => {
            if (settled) return;
            settled = true;
            clearTimeout(timeout);
            resolve(socket);
        };

        const handleSocketFailure = socket.onerror;
        socket.onerror = event => {
            if (typeof handleSocketFailure === 'function') {
                handleSocketFailure.call(socket, event);
            }
            if (settled) return;
            settled = true;
            clearTimeout(timeout);
            reject(new Error('WebSocket connection failed'));
        };

        const handleSocketClose = socket.onclose;
        socket.onclose = event => {
            if (typeof handleSocketClose === 'function') {
                handleSocketClose.call(socket, event);
            }
            if (settled) return;
            settled = true;
            clearTimeout(timeout);
            reject(new Error('WebSocket closed before opening'));
        };
    });
}

function start_tts(text, audioIcon, author, conversationId, generation = ttsGeneration) {
    Config.currentAudioIcon = audioIcon;
    isWaiting = true;
    toggleIcons(audioIcon, 'waiting');

    ensureWebSocketConnection(generation).then(socket => {
        if (generation !== ttsGeneration || socket !== ws) return;

        audioQueue = [];
        isFinished = false;
        socket.send(JSON.stringify({
            action: 'start_tts_ws',
            text: text,
            author: author,
            conversationId: conversationId,
        }));
    }).catch(error => {
        if (generation !== ttsGeneration) return;
        console.error('TTS connection failed:', error);
        toggleIcons(audioIcon, 'stopped');
        isWaiting = false;
    });
}

function revokeAudioObjectUrl(url) {
    if (!url) return;
    URL.revokeObjectURL(url);
    if (currentAudioObjectUrl === url) currentAudioObjectUrl = null;
}

function disposeCachedAudio(audio = Config.currentAudio, objectUrl = currentAudioObjectUrl) {
    if (audio) {
        audio.onended = null;
        audio.onerror = null;
        audio.onplaying = null;
        audio.onwaiting = null;
        try { audio.pause(); } catch (error) { /* no-op */ }
        try { audio.currentTime = 0; } catch (error) { /* not seekable yet */ }
        audio.src = '';
    }
    if (Config.currentAudio === audio) Config.currentAudio = null;
    revokeAudioObjectUrl(objectUrl);
}

function closeTtsSocket(sendStop) {
    const socket = ws;
    ws = null;
    if (!socket) return;

    if (sendStop && socket.readyState === WebSocket.OPEN) {
        try { socket.send(JSON.stringify({ action: 'stop' })); } catch (error) { /* closing */ }
    }
    try { socket.close(); } catch (error) { /* already closed */ }
}

function stopAudio(audioIcon) {
    stopAllAudio();
    if (audioIcon) toggleIcons(audioIcon, 'stopped');
}

function stopAudioAndCloseWebSocket() {
    stopAllAudio({ sendStop: false });
}

function stopAllAudio(options = {}) {
    ttsGeneration += 1;

    if (ttsFetchController) {
        ttsFetchController.abort();
        ttsFetchController = null;
    }

    disposeCachedAudio();
    closeTtsSocket(options.sendStop !== false);

    if (sourceNode) {
        sourceNode.onended = null;
        try { sourceNode.stop(); } catch (error) { /* may already be stopped */ }
        sourceNode = null;
    }

    if (bufferTimer) {
        clearTimeout(bufferTimer);
        bufferTimer = null;
    }
    isPlaying = false;
    isWaiting = false;
    isBuffering = false;
    isFinished = false;
    audioQueue = [];

    if (Config.currentAudioIcon) {
        toggleIcons(Config.currentAudioIcon, 'stopped');
    }
    Config.currentAudioIcon = null;
}

function finishCachedAudio(audio, objectUrl, generation, audioIcon) {
    const isCurrent = generation === ttsGeneration && Config.currentAudio === audio;
    disposeCachedAudio(audio, objectUrl);
    if (!isCurrent) return;

    isPlaying = false;
    isWaiting = false;
    toggleIcons(audioIcon, 'stopped');
    Config.currentAudioIcon = null;
}

function resolveOriginalAudioUrl(url) {
    if (typeof url !== 'string' || !url.trim()) {
        throw new Error('Original audio URL is missing');
    }
    if (window.AurvekEmbed) {
        const scoped = window.AurvekEmbed.resourceUrl(url);
        if (!scoped) throw new Error('Original audio is unavailable in this conversation');
        return scoped;
    }
    const resolved = new URL(url, window.location.origin);
    if (resolved.origin !== window.location.origin ||
            (resolved.protocol !== 'http:' && resolved.protocol !== 'https:')) {
        throw new Error('Original audio URL must use the current origin');
    }
    const isAttachment = /^\/api\/attachments\/[^/]+\/content$/.test(resolved.pathname);
    const isPhoneMessage = /^\/api\/phone-messages\/\d+\/audio$/.test(resolved.pathname);
    const isWebRecording = /^\/api\/conversations\/\d+\/voice\/audio$/.test(resolved.pathname);
    if (!isAttachment && !isPhoneMessage && !isWebRecording) {
        throw new Error('Original audio URL must use an approved audio endpoint');
    }
    return resolved.href;
}

function markAudioPlaybackError(audioIcon, label) {
    if (!audioIcon) return;
    const message = AurvekI18n.t('chat_widgets.audio.playback_failed', {label});
    audioIcon.classList.add('audio-playback-error');
    audioIcon.title = message;
    audioIcon.setAttribute('aria-label', message);
    audioIcon.setAttribute('aria-pressed', 'false');
}

function playOriginalAudio(url, audioIcon, label = AurvekI18n.t('chat_widgets.audio.original')) {
    audioIcon?.classList.remove('audio-playback-error');
    let resolvedUrl;
    try {
        resolvedUrl = resolveOriginalAudioUrl(url);
    } catch (error) {
        console.error('Could not play original audio:', error);
        toggleIcons(audioIcon, 'stopped');
        markAudioPlaybackError(audioIcon, label);
        return;
    }

    if (isPlaying || isWaiting) {
        stopAllAudio();
        return;
    }
    stopAllAudio();

    const generation = ttsGeneration;
    let audio;
    try {
        audio = new Audio();
        audio.preload = 'metadata';
        audio.setAttribute('playsinline', '');
        audio.src = resolvedUrl;
    } catch (error) {
        console.error('Could not initialize original audio:', error);
        toggleIcons(audioIcon, 'stopped');
        markAudioPlaybackError(audioIcon, label);
        return;
    }

    Config.currentAudio = audio;
    Config.currentAudioIcon = audioIcon;
    isWaiting = true;
    isPlaying = false;
    toggleIcons(audioIcon, 'waiting');

    const isCurrentAudio = () => (
        generation === ttsGeneration && Config.currentAudio === audio
    );
    audio.onplaying = () => {
        if (!isCurrentAudio()) return;
        isWaiting = false;
        isPlaying = true;
        toggleIcons(audioIcon, 'playing');
    };
    audio.onwaiting = () => {
        if (!isCurrentAudio()) return;
        isWaiting = true;
        isPlaying = false;
        toggleIcons(audioIcon, 'waiting');
    };
    audio.onended = () => finishCachedAudio(
        audio,
        null,
        generation,
        audioIcon
    );
    audio.onerror = () => {
        const wasCurrent = isCurrentAudio();
        if (wasCurrent) {
            console.error('Error playing original audio');
        }
        finishCachedAudio(audio, null, generation, audioIcon);
        if (wasCurrent) markAudioPlaybackError(audioIcon, label);
    };

    const playPromise = audio.play();
    if (playPromise && typeof playPromise.catch === 'function') {
        playPromise.catch(error => {
            const wasCurrent = isCurrentAudio();
            if (wasCurrent) {
                console.error('Error playing original audio:', error);
            }
            finishCachedAudio(audio, null, generation, audioIcon);
            if (wasCurrent) markAudioPlaybackError(audioIcon, label);
        });
    }
}

function audioApplication(conversationId) {
    const embed = window.AurvekEmbed?.config;
    if (embed?.application && String(embed.conversation_id) === String(conversationId)) {
        return {capabilities: embed.capabilities};
    }
    const current = window.applicationConversation;
    return current && String(current.id) === String(conversationId) ? current.application : null;
}

async function fetchAudio(url, options, application) {
    if (!application) return fetch(url, options);
    const token = window.AurvekEmbed?.config.csrf_token
        || document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
    if (!token || typeof window.secureFetch !== 'function') {
        throw new Error(AurvekI18n.t('chat_widgets.error.security_unavailable'));
    }
    const response = await window.secureFetch(url, {...options,
        headers: {...options.headers, 'X-GPTSub-CSRF': token}});
    if (!response) throw new Error(AurvekI18n.t('common.session.expired_message'));
    return response;
}

function textToSpeech(text, userId, conversationId, audioIcon, author) {
    if (isPlaying || isWaiting) {
        stopAllAudio();
        return;
    }
    stopAllAudio();

    const generation = ttsGeneration;
    const selectedConversation = document.querySelector(
        '.list-group-item-action.active-chat'
    );
    const finalConversationId =
        (selectedConversation ? selectedConversation.dataset.conversationId : null)
        || conversationId;

    isWaiting = true;
    Config.currentAudioIcon = audioIcon;
    toggleIcons(audioIcon, 'waiting');

    const controller = new AbortController();
    ttsFetchController = controller;
    const application = audioApplication(finalConversationId);
    const messageId = Number(audioIcon?.closest('.message')?.dataset.messageId);
    if (application && (!application.capabilities.tts || !Number.isSafeInteger(messageId) || messageId <= 0)) {
        isWaiting = false;
        toggleIcons(audioIcon, 'stopped');
        markAudioPlaybackError(audioIcon, AurvekI18n.t('chat_widgets.audio.read_aloud'));
        return;
    }
    const endpoint = application ? `/api/conversations/${finalConversationId}/voice/tts` : '/api/get-tts-audio';
    const payload = application ? {message_id: messageId} : {
        text, conversationId: finalConversationId, author,
    };
    const slideId = Number(audioIcon?.closest('.message')?.querySelector('.multi-ai-slide.active')?.dataset.llmId);
    if (application && Number.isSafeInteger(slideId) && slideId > 0) payload.llm_id = slideId;
    fetchAudio(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
    }, application)
    .then(response => {
        if (generation !== ttsGeneration) return null;
        if (response.ok && response.status !== 204) return response.blob();
        if (response.status === 204) {
            const cacheMiss = new Error('TTS cache miss');
            cacheMiss.name = 'TTSCacheMiss';
            throw cacheMiss;
        }
        throw new Error('TTS cache request failed');
    })
    .then(blob => {
        if (!blob || generation !== ttsGeneration || controller.signal.aborted) return;
        if (ttsFetchController === controller) ttsFetchController = null;

        const objectUrl = URL.createObjectURL(blob);
        currentAudioObjectUrl = objectUrl;
        let audio;
        try {
            audio = new Audio(objectUrl);
        } catch (error) {
            revokeAudioObjectUrl(objectUrl);
            throw error;
        }
        Config.currentAudio = audio;
        Config.currentAudioIcon = audioIcon;
        isWaiting = false;
        isPlaying = true;
        toggleIcons(audioIcon, 'playing');

        audio.onended = () => finishCachedAudio(audio, objectUrl, generation, audioIcon);
        audio.onerror = () => finishCachedAudio(audio, objectUrl, generation, audioIcon);

        const playPromise = audio.play();
        if (playPromise && typeof playPromise.catch === 'function') {
            playPromise.catch(error => {
                if (generation === ttsGeneration) {
                    console.error('Error playing TTS audio:', error);
                }
                finishCachedAudio(audio, objectUrl, generation, audioIcon);
            });
        }
    })
    .catch(error => {
        if (ttsFetchController === controller) ttsFetchController = null;
        if (controller.signal.aborted || generation !== ttsGeneration) return;

        if (error.name === 'TTSCacheMiss' && !application) {
            start_tts(text, audioIcon, author, finalConversationId, generation);
            return;
        }

        console.error('Error fetching audio:', error);
        isWaiting = false;
        isPlaying = false;
        toggleIcons(audioIcon, 'stopped');
        markAudioPlaybackError(audioIcon, AurvekI18n.t('chat_widgets.audio.read_aloud'));
        if (Config.currentAudioIcon === audioIcon) Config.currentAudioIcon = null;
    });
}

function toggleIcons(audioIcon, state) {
    if (!audioIcon) {
        return;
    }
    const baseIcon = audioIcon.dataset.baseIcon || 'fa-volume-up';
    const isOriginal = audioIcon.dataset.audioSource === 'original';
    const playLabel = audioIcon.dataset.playLabel || (isOriginal
        ? AurvekI18n.t('chat_widgets.audio.play_original')
        : AurvekI18n.t('chat_widgets.audio.read_aloud'));
    let actionLabel = playLabel;

    switch (state) {
        case 'waiting':
            audioIcon.classList.remove(baseIcon, 'fa-stop');
            audioIcon.classList.add('fa-hourglass-half');
            actionLabel = isOriginal
                ? AurvekI18n.t('chat_widgets.audio.loading_original')
                : AurvekI18n.t('chat_widgets.audio.preparing_read_aloud');
            break;
        case 'playing':
            audioIcon.classList.remove(baseIcon, 'fa-hourglass-half');
            audioIcon.classList.add('fa-stop');
            actionLabel = isOriginal
                ? AurvekI18n.t('chat_widgets.audio.stop_original')
                : AurvekI18n.t('chat_widgets.audio.stop_reading');
            break;
        case 'stopped':
            audioIcon.classList.remove('fa-stop', 'fa-hourglass-half');
            audioIcon.classList.add(baseIcon);
            audioIcon.classList.remove('audio-playback-error');
            break;
        default:
            break;
    }
    const active = state === 'waiting' || state === 'playing';
    audioIcon.title = actionLabel;
    audioIcon.setAttribute('aria-label', actionLabel);
    audioIcon.setAttribute('aria-pressed', active ? 'true' : 'false');
}

// From here are the functions to record audio with microphone and convert to text
const audioIcon = document.getElementById('audio-button');
const cancelAudioButton = document.getElementById('cancel-audio');
const sendAudioButton = document.getElementById('send-audio');
let recordingGeneration = 0;
let recordingStatus = 'idle';
let recordingSession = null;

function getAudioActivity() {
    if (recordingStatus === 'processing' || isWaiting) return 'generating';
    if (['starting', 'recording', 'stopping'].includes(recordingStatus) || isPlaying) return 'call';
    return 'idle';
}

function releaseMediaStream(stream) {
    if (!stream || typeof stream.getTracks !== 'function') return;
    stream.getTracks().forEach(track => {
        try { track.stop(); } catch (error) { /* already stopped */ }
    });
}

function recordingIsCurrent(session) {
    return recordingSession === session && session.generation === recordingGeneration;
}

async function toggleAudioRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        NotificationModal.error(
            AurvekI18n.t('chat_widgets.audio.browser_unsupported_title'),
            AurvekI18n.t('chat_widgets.audio.browser_unsupported')
        );
        return;
    }

    if (recordingStatus === 'idle') {
        await startAudioRecording();
    } else if (recordingStatus === 'recording') {
        sendAudioRecording();
    }
}

async function startAudioRecording() {
    if (recordingStatus !== 'idle') return;

    const session = {
        generation: ++recordingGeneration,
        recorder: null,
        stream: null,
        chunks: [],
        action: null,
        conversationId: currentConversationId,
        controller: null,
        finalized: false,
    };
    recordingSession = session;
    recordingStatus = 'starting';
    isCanceled = false;

    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (!recordingIsCurrent(session) || recordingStatus !== 'starting') {
            releaseMediaStream(stream);
            return;
        }

        session.stream = stream;
        session.recorder = new MediaRecorder(stream);
        Config.mediaRecorder = session.recorder;
        Config.audioChunks = session.chunks;

        session.recorder.ondataavailable = event => {
            if (recordingIsCurrent(session) && event.data && event.data.size > 0) {
                session.chunks.push(event.data);
            }
        };
        session.recorder.onstart = () => {
            if (!recordingIsCurrent(session)) return;
            recordingStatus = 'recording';
            isRecording = true;
            showAudioRecordingControls();
            startRecording();
            if (window.ChatWarmup && typeof window.ChatWarmup.signal === 'function') {
                window.ChatWarmup.signal('audio_recording', {});
            }
        };
        session.recorder.onstop = () => handleAudioStop(session);
        session.recorder.onerror = event => {
            if (!recordingIsCurrent(session)) return;
            console.error('MediaRecorder error', event.error || event);
            releaseMediaStream(session.stream);
            requestRecordingStop('cancel');
        };

        session.recorder.start();
    } catch (error) {
        releaseMediaStream(session.stream);
        if (!recordingIsCurrent(session)) return;

        recordingSession = null;
        recordingStatus = 'idle';
        Config.mediaRecorder = null;
        Config.audioChunks = [];
        console.error('Error accessing microphone', error);
        NotificationModal.error(
            AurvekI18n.t('chat_widgets.audio.microphone_denied_title'),
            AurvekI18n.t('chat_widgets.audio.microphone_denied')
        );
    }
}

function requestRecordingStop(action) {
    const session = recordingSession;
    if (!session || !recordingIsCurrent(session)) return;

    session.action = action;
    isCanceled = action === 'cancel';
    isRecording = false;

    if (recordingStatus === 'starting' || (action === 'cancel' && recordingStatus === 'processing')) {
        session.controller?.abort();
        if (session.recorder && session.recorder.state !== 'inactive') {
            try { session.recorder.stop(); } catch (error) { /* still starting */ }
        }
        releaseMediaStream(session.stream);
        recordingGeneration += 1;
        recordingSession = null;
        recordingStatus = 'idle';
        Config.mediaRecorder = null;
        Config.audioChunks = [];
        stopRecording();
        hideAudioRecordingControls();
        removeLoadingIndicator();
        return;
    }

    if (recordingStatus !== 'recording') return;
    recordingStatus = 'stopping';
    stopRecording();
    if (action === 'send') addLoadingIndicator();

    if (session.recorder && session.recorder.state !== 'inactive') {
        session.recorder.stop();
    }
}

function stopAudioRecording() {
    requestRecordingStop('send');
}

function showAudioRecordingControls() {
    document.getElementById('form-message')?.classList.add('hidden');
    document.getElementById('audio-recording-controls')?.classList.remove('hidden');
    audioIcon?.classList.remove('fa-microphone');
    audioIcon?.classList.add('fa-stop');
}

function hideAudioRecordingControls() {
    document.getElementById('form-message')?.classList.remove('hidden');
    document.getElementById('audio-recording-controls')?.classList.add('hidden');
    audioIcon?.classList.remove('fa-stop');
    audioIcon?.classList.add('fa-microphone');
}

function cancelAudioRecording() {
    requestRecordingStop('cancel');
}

function sendAudioRecording() {
    requestRecordingStop('send');
}

async function getRecordingDuration(audioBlob) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    const durationContext = new AudioContextClass();
    try {
        const arrayBuffer = await audioBlob.arrayBuffer();
        const audioBuffer = await durationContext.decodeAudioData(arrayBuffer);
        return audioBuffer.duration;
    } finally {
        if (typeof durationContext.close === 'function') {
            await durationContext.close();
        }
    }
}

async function handleAudioStop(session) {
    if (!session || session.finalized) return;
    session.finalized = true;
    releaseMediaStream(session.stream);

    const shouldSend = session.action === 'send';
    if (recordingIsCurrent(session)) {
        recordingStatus = shouldSend ? 'processing' : 'idle';
    }

    try {
        if (!shouldSend) return;

        const mimeType = session.recorder?.mimeType || 'audio/webm;codecs=opus';
        const audioBlob = new Blob(session.chunks, { type: mimeType });
        if (!audioBlob.size) {
            throw new Error(AurvekI18n.t('chat_widgets.audio.empty_recording'));
        }

        const duration = await getRecordingDuration(audioBlob);
        if (!recordingIsCurrent(session) || session.action !== 'send'
            || String(session.conversationId) !== String(currentConversationId)) return;
        const formData = new FormData();
        formData.append('audio', audioBlob);
        formData.append('conversation_id', session.conversationId);
        formData.append('duration', duration);
        session.controller = new AbortController();
        await sendFormData(formData, session);
    } catch (error) {
        removeLoadingIndicator();
        if (error?.name === 'AbortError' || !recordingIsCurrent(session) || session.action === 'cancel') return;
        console.error('Error processing audio recording:', error);
        NotificationModal.error(
            AurvekI18n.t('chat_widgets.audio.error_title'),
            AurvekI18n.t('chat_widgets.audio.processing_failed')
        );
    } finally {
        releaseMediaStream(session.stream);
        session.chunks = [];
        if (session.recorder) {
            session.recorder.ondataavailable = null;
            session.recorder.onstart = null;
            session.recorder.onstop = null;
            session.recorder.onerror = null;
        }

        if (recordingIsCurrent(session)) {
            removeLoadingIndicator();
            recordingSession = null;
            recordingStatus = 'idle';
            Config.mediaRecorder = null;
            Config.audioChunks = [];
            isRecording = false;
            isCanceled = false;
            stopRecording();
            hideAudioRecordingControls();
        }
    }
}

async function sendFormData(formData, session = null) {
    const application = audioApplication(formData.get('conversation_id'));
    const endpoint = application
        ? `/api/conversations/${formData.get('conversation_id')}/voice/transcribe`
        : '/api/transcribe-web';
    const response = await fetchAudio(endpoint, {
        method: 'POST',
        body: formData,
        signal: session?.controller?.signal,
    }, application);
    await handleAudioResponse(response, Boolean(application), () => !session || (
        recordingIsCurrent(session) && !session.controller?.signal.aborted
        && String(session.conversationId) === String(currentConversationId)));
}

async function handleAudioResponse(response, editable = false, isCurrent = () => true) {
    if (!isCurrent()) return;
    removeLoadingIndicator();
    if (response.status === 402) {
        showInsufficientBalancePopup();
        return;
    }
    if (response.status === 204) return;
    const data = await response.json().catch(() => ({}));
    if (!isCurrent()) return;
    if (!response.ok) {
        const message = typeof data.detail === 'string' ? data.detail : data.error;
        NotificationModal.error(
            AurvekI18n.t('chat_widgets.audio.error_title'),
            typeof message === 'string' && message ? message : AurvekI18n.t('chat_errors.provider_unavailable')
        );
        return;
    }
    if (data.prompt) {
        const input = document.getElementById('message-text');
        input.value = data.prompt;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        if (editable) input.focus();
        else document.getElementById('send-button').click();
    }
}

///// Timer for sending audio /////
let recordingStartTime;
let recordingInterval;

// Function to start recording and counter
function startRecording() {
    if (recordingInterval) clearInterval(recordingInterval);
    recordingStartTime = Date.now();
    recordingInterval = setInterval(updateRecordingTime, 1000);
    document.getElementById('audio-recording-controls')?.classList.remove('hidden');
}
function stopRecording() {
    if (recordingInterval) {
        clearInterval(recordingInterval);
        recordingInterval = null;
    }
    const counter = document.getElementById('time-counter');
    if (counter) counter.innerText = '00:00';
    document.getElementById('audio-recording-controls')?.classList.add('hidden');
}

// Function to update time counter
function updateRecordingTime() {
    const elapsedTime = Date.now() - recordingStartTime;
    const seconds = Math.floor(elapsedTime / 1000) % 60;
    const minutes = Math.floor(elapsedTime / 60000);
    const counter = document.getElementById('time-counter');
    if (counter) {
        counter.innerText =
            (minutes < 10 ? '0' : '') + minutes + ':' +
            (seconds < 10 ? '0' : '') + seconds;
    }
}

function discardActiveRecording() {
    const session = recordingSession;
    if (!session) return;
    session.finalized = true;
    session.action = 'cancel';
    session.controller?.abort();
    if (session.recorder && session.recorder.state !== 'inactive') {
        try { session.recorder.stop(); } catch (error) { /* page is unloading */ }
    }
    releaseMediaStream(session.stream);
    recordingGeneration += 1;
    recordingSession = null;
    recordingStatus = 'idle';
    Config.mediaRecorder = null;
    Config.audioChunks = [];
}

audioIcon?.addEventListener('click', toggleAudioRecording);
cancelAudioButton?.addEventListener('click', cancelAudioRecording);
sendAudioButton?.addEventListener('click', sendAudioRecording);
window.addEventListener('pagehide', discardActiveRecording);
