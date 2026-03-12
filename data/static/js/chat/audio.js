let audioContext;
let ws;
let isPlaying = false; // Indicates if any audio is playing
let isBuffering = false;
let isFinished = false;
let isWaiting = false;
let sourceNode;
let audioQueue = [];
let bufferSize = 1;
let stopTimer = null;
let isRecording = false;
let isCanceled = false;

// Ensure currentAudioIcon is initialized
Config.currentAudioIcon = null;

// Function to automatically build WebSocket URL
function getWebSocketURL() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host; // includes port if exists
    return `${protocol}//${host}/ws`;
}

// WebSocket and AudioContext initialization
function connect() {
    const wsURL = getWebSocketURL();

    ws = new WebSocket(wsURL);
    ws.binaryType = "arraybuffer";
    initAudio();

    ws.onmessage = function(event) {
        if (ws.readyState !== WebSocket.OPEN) {
            return;
        }

        if (typeof event.data === 'string') {
            const message = JSON.parse(event.data);

            switch (message.action) {
                case 'insufficient-balance':
                    showInsufficientBalancePopup("this action");
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
            queueAudioChunk(event.data);
        }
    };

    ws.onerror = function(event) {
        console.error('WebSocket connection error', event);
        isPlaying = false;
        isWaiting = false;
        if (Config.currentAudioIcon) {
            toggleIcons(Config.currentAudioIcon, 'stopped');
        }
    };

    ws.onclose = function() {
        if (audioQueue.length > 0 && !isPlaying) {
            playNextInQueue();
        }
    };
}

function handleStoppedMessage() {
    stopAudioAndCloseWebSocket();
}

function handleNoContentMessage() {
    isPlaying = false;
    isWaiting = false;
    toggleIcons(Config.currentAudioIcon, 'stopped'); // Change the icon to 'stopped'
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

function queueAudioChunk(arrayBuffer) {
    // Cancel timer if audio chunk is received
    if (stopTimer) {
        clearTimeout(stopTimer);
        stopTimer = null;
    }
    
    audioContext.decodeAudioData(arrayBuffer, (audioBuffer) => {
        audioQueue.push(audioBuffer);
        if (!isPlaying && !isBuffering) {
            if (audioQueue.length >= bufferSize) {
                isWaiting = false;
                playNextInQueue();
            } else {
                isBuffering = true;
                isWaiting = true;
                toggleIcons(Config.currentAudioIcon, 'waiting');
                setTimeout(checkBuffer, 100);
            }
        }
    }, (error) => {
        console.error('Error decoding audio data', error);
    });
}

function checkBuffer() {
    if (audioQueue.length >= bufferSize || isFinished) {
        isBuffering = false;
        isWaiting = false;
        playNextInQueue();
    } else {
        setTimeout(checkBuffer, 100);
    }
}

function playNextInQueue() {
    if (audioQueue.length === 0) {
        if (isFinished) {
            stopAudioAndCloseWebSocket();
        } else {
            isBuffering = true;
            isWaiting = true;
            toggleIcons(Config.currentAudioIcon, 'waiting');
            setTimeout(checkBuffer, 100);
        }
        return;
    }

    isPlaying = true;
    isWaiting = false;
    const audioBuffer = audioQueue.shift();
    sourceNode = audioContext.createBufferSource();
    sourceNode.buffer = audioBuffer;
    sourceNode.connect(audioContext.destination);
    sourceNode.onended = playNextInQueue;
    sourceNode.start();
    toggleIcons(Config.currentAudioIcon, 'playing');
}

function ensureWebSocketConnection() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        return new Promise((resolve, reject) => {
            const timeout = setTimeout(() => {
                if (ws) ws.close();
                reject(new Error('WebSocket connection timeout'));
            }, 10000);

            connect();

            ws.onopen = () => {
                clearTimeout(timeout);
                resolve();
            };
        });
    }
    return Promise.resolve();
}

function start_tts(text, audioIcon, author, conversationId) {
    ensureWebSocketConnection().then(() => {
        audioQueue = [];
        isFinished = false;
        isWaiting = true;
        toggleIcons(audioIcon, 'waiting');

        ws.send(JSON.stringify({
            action: 'start_tts_ws',
            text: text,
            author: author,
            conversationId: conversationId,
        }));

        Config.currentAudioIcon = audioIcon;
    }).catch((error) => {
        console.error('TTS connection failed:', error);
        toggleIcons(audioIcon, 'stopped');
        isWaiting = false;
    });
}

function stopAudio(audioIcon) {
    if (Config.currentAudio) {
        // If cached audio is playing, stop it
        Config.currentAudio.pause();
        Config.currentAudio.currentTime = 0;
        Config.currentAudio = null;
        isPlaying = false;
        if (Config.currentAudioIcon) {
            toggleIcons(Config.currentAudioIcon, 'stopped');
        }
        return; // Exit function to not stop audio via WebSocket
    }

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'stop' }));
    }
    
    if (sourceNode) {
        sourceNode.stop();
    }
    
    isPlaying = false;
    isWaiting = false;
    isBuffering = false;
    audioQueue = [];
    
    if (audioIcon) {
        toggleIcons(audioIcon, 'stopped');
    }

    if (stopTimer) clearTimeout(stopTimer);
    stopTimer = setTimeout(() => stopAudioAndCloseWebSocket(), 3000);
}

function stopAudioAndCloseWebSocket() {
    if (Config.currentAudio) {
        Config.currentAudio.pause();
        Config.currentAudio.currentTime = 0;
        Config.currentAudio = null;
    }
    isPlaying = false;
    isWaiting = false;
    toggleIcons(Config.currentAudioIcon, 'stopped');

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close();
    }
    
    if (stopTimer) {
        clearTimeout(stopTimer);
        stopTimer = null;
    }
}

function stopAllAudio() {
    // Stop cached Audio element (if playing from cache hit)
    if (Config.currentAudio) {
        Config.currentAudio.pause();
        Config.currentAudio.currentTime = 0;
        Config.currentAudio = null;
    }

    // Stop WebSocket streaming (if playing from generator)
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ action: 'stop' }));
    }
    if (sourceNode) {
        try { sourceNode.stop(); } catch (e) { /* may already be stopped */ }
    }

    // Reset state
    isPlaying = false;
    isWaiting = false;
    isBuffering = false;
    audioQueue = [];

    // Reset icon of whichever button was active
    if (Config.currentAudioIcon) {
        toggleIcons(Config.currentAudioIcon, 'stopped');
    }

    if (stopTimer) {
        clearTimeout(stopTimer);
        stopTimer = null;
    }
}

function textToSpeech(text, userId, conversationId, audioIcon, author) {
    if (isPlaying || isWaiting) {
        stopAllAudio();
        return;
    }
    stopAllAudio();  // Clean any residual state

    const selectedConversation = document.querySelector(
        '.list-group-item-action.active-chat'
    );
    const finalConversationId =
        (selectedConversation ? selectedConversation.dataset.conversationId : null)
        || conversationId;

    toggleIcons(audioIcon, 'waiting');

    // Same cache check as existing button (shared cache)
    fetch('/api/get-tts-audio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            text: text,
            conversationId: finalConversationId,
            author: author,
        }),
    })
    .then(response => {
        if (response.ok && response.status !== 204) return response.blob();
        if (response.status === 204) throw new Error('Audio not found in cache');
        throw new Error('Server error');
    })
    .then(blob => {
        const url = URL.createObjectURL(blob);
        Config.currentAudio = new Audio(url);
        Config.currentAudio.play();
        isPlaying = true;
        toggleIcons(audioIcon, 'playing');
        Config.currentAudioIcon = audioIcon;
        Config.currentAudio.onended = function() {
            toggleIcons(audioIcon, 'stopped');
            Config.currentAudio = null;
            isPlaying = false;
        };
    })
    .catch(error => {
        if (error.message === 'Audio not found in cache') {
            start_tts(text, audioIcon, author, finalConversationId);
        } else {
            console.error('Error fetching audio:', error);
            toggleIcons(audioIcon, 'stopped');
        }
    });
}

function toggleIcons(audioIcon, state) {
    if (!audioIcon) {
        return;
    }
    const baseIcon = audioIcon.dataset.baseIcon || 'fa-volume-up';

    switch (state) {
        case 'waiting':
            audioIcon.classList.remove(baseIcon, 'fa-stop');
            audioIcon.classList.add('fa-hourglass-half');
            break;
        case 'playing':
            audioIcon.classList.remove(baseIcon, 'fa-hourglass-half');
            audioIcon.classList.add('fa-stop');
            break;
        case 'stopped':
            audioIcon.classList.remove('fa-stop', 'fa-hourglass-half');
            audioIcon.classList.add(baseIcon);
            break;
        default:
            break;
    }
}

// From here are the functions to record audio with microphone and convert to text
const audioControl = document.getElementById('audio-control');
const audioIcon = document.getElementById('audio-button');

document.getElementById('audio-button').addEventListener('click', async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        NotificationModal.error('Browser Not Supported', 'Your browser does not support audio recording.');
        return;
    }

    if (Config.mediaRecorder && Config.mediaRecorder.state !== 'inactive') {
        addLoadingIndicator();
        Config.mediaRecorder.stop();
        return;
    }

    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        Config.mediaRecorder = new MediaRecorder(stream);

        Config.mediaRecorder.onstart = () => {
            Config.audioChunks = [];
            audioIcon.classList.remove('fa-microphone');
            audioIcon.classList.add('fa-stop');
        };
		
        Config.mediaRecorder.ondataavailable = event => Config.audioChunks.push(event.data);

        Config.mediaRecorder.onstop = () => {
            handleAudioStop();
            audioIcon.classList.remove('fa-stop');
            audioIcon.classList.add('fa-microphone');
        };

        Config.mediaRecorder.start();

    } catch (err) {
        console.error('Error accessing microphone', err);
    }
});

async function toggleAudioRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(track => track.stop());

        if (!isRecording) {
            startAudioRecording();
        } else {
            stopAudioRecording();
        }
    } catch (err) {
        console.error('Error accessing microphone', err);
        NotificationModal.error('Microphone Access Denied', 'Could not access microphone. Make sure you have granted permissions and have a microphone connected.');
    }
}

function startAudioRecording() {
    navigator.mediaDevices.getUserMedia({ audio: true })
        .then(stream => {
            Config.mediaRecorder = new MediaRecorder(stream);
            Config.mediaRecorder.ondataavailable = event => Config.audioChunks.push(event.data);
            Config.mediaRecorder.onstart = () => {
                Config.audioChunks = [];
                showAudioRecordingControls();
                startRecording();
                isCanceled = false;
            };
            Config.mediaRecorder.onstop = handleAudioStop;

            Config.mediaRecorder.start();
            isRecording = true;
        })
        .catch(err => {
            console.error('Error accessing microphone', err);
            NotificationModal.error('Microphone Error', 'Error accessing microphone. Make sure you have a microphone connected and have granted permissions to use it.');
        });
}

function stopAudioRecording() {
    if (Config.mediaRecorder && Config.mediaRecorder.state !== 'inactive') {
        Config.mediaRecorder.stop();
    }
    isRecording = false;
    stopRecording();
}

function showAudioRecordingControls() {
    document.getElementById('form-message').classList.add('hidden');
    document.getElementById('audio-recording-controls').classList.remove('hidden');
    document.getElementById('audio-button').classList.remove('fa-microphone');
    document.getElementById('audio-button').classList.add('fa-stop');
}

function hideAudioRecordingControls() {
    document.getElementById('form-message').classList.remove('hidden');
    document.getElementById('audio-recording-controls').classList.add('hidden');
    document.getElementById('audio-button').classList.remove('fa-stop');
    document.getElementById('audio-button').classList.add('fa-microphone');
}

function cancelAudioRecording() {
    if (Config.mediaRecorder && Config.mediaRecorder.state !== 'inactive') {
        Config.mediaRecorder.stop();
    }
    isRecording = false;
    isCanceled = true;
    stopRecording();
    Config.audioChunks = [];
    hideAudioRecordingControls();
}

function sendAudioRecording() {
    stopAudioRecording();
    handleAudioStop();
}

async function handleAudioStop() {
    if (isCanceled) {
        return;
    }

    const audioBlob = new Blob(Config.audioChunks, { type: 'audio/webm;codecs=opus' });
    Config.audioChunks = [];

    const audioUrl = URL.createObjectURL(audioBlob);
    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const response = await fetch(audioUrl);
    const arrayBuffer = await response.arrayBuffer();
    const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);
    const duration = audioBuffer.duration;

    const formData = new FormData();
    formData.append("audio", audioBlob);
    formData.append("conversation_id", currentConversationId);
    formData.append("duration", duration);

    sendFormData(formData);
    hideAudioRecordingControls();
}

async function sendFormData(formData) {
    try {
        const response = await fetch("/api/transcribe-web", {
            method: "POST",
            body: formData
        });

        handleResponse(response);
    } catch (error) {
        console.error("Error sending audio:", error);
    }
}

async function handleResponse(response) {
    removeLoadingIndicator();
    switch (response.status) {
        case 402:
            showInsufficientBalancePopup("transcribe audio");
            break;
        case 204:
            break;
        case 500:
            const data = await response.json();
            NotificationModal.error('Server Error', data.error);
            break;
        default:
            if (response.ok) {
                const data = await response.json();
                if (data["prompt"]) {
                    document.getElementById('message-text').value = data["prompt"];
                    document.getElementById('send-button').click();
                }
            }
            break;
    }
}

///// Timer for sending audio /////
let recordingStartTime;
let recordingInterval;

// Function to start recording and counter
function startRecording() {
    recordingStartTime = Date.now();
    recordingInterval = setInterval(updateRecordingTime, 1000);
    document.getElementById('audio-recording-controls').classList.remove('hidden');
}
function stopRecording() {
    clearInterval(recordingInterval);
    document.getElementById('time-counter').innerText = '00:00';
    document.getElementById('audio-recording-controls').classList.add('hidden');
}
// Function to stop recording and counter
function stopAudioRecording() {
    if (Config.mediaRecorder && Config.mediaRecorder.state !== 'inactive') {
        Config.mediaRecorder.stop();
    }
    isRecording = false;
    stopRecording(); // Stop the counter
}

// Function to update time counter
function updateRecordingTime() {
    const elapsedTime = Date.now() - recordingStartTime;
    const seconds = Math.floor(elapsedTime / 1000) % 60;
    const minutes = Math.floor(elapsedTime / 60000);
    document.getElementById('time-counter').innerText = 
        (minutes < 10 ? '0' : '') + minutes + ':' + 
        (seconds < 10 ? '0' : '') + seconds;
}
document.getElementById('audio-button').addEventListener('click', toggleAudioRecording);
document.getElementById('cancel-audio').addEventListener('click', cancelAudioRecording);
document.getElementById('send-audio').addEventListener('click', sendAudioRecording);