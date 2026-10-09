/* Native PCM transport. Aurvek owns credentials, identity, tools, and billing. */
if (typeof registerProcessor === 'function') {
    class NativeMicrophone extends AudioWorkletProcessor {
        constructor() { super(); this.samples = new Int16Array(320); this.offset = 0; }
        process(inputs) {
            for (const sample of inputs[0]?.[0] || []) {
                this.samples[this.offset++] = Math.round(Math.max(-1, Math.min(1, sample)) * 32767);
                if (this.offset === 320) {
                    this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
                    this.samples = new Int16Array(320); this.offset = 0;
                }
            }
            return true;
        }
    }
    registerProcessor('aurvek-native-microphone', NativeMicrophone);
} else (function () {
    'use strict';
    const scriptUrl = document.currentScript.src;
    const dialog = document.getElementById('native-live-dialog');
    if (!dialog) return;
    const camera = document.getElementById('native-live-camera');
    const cameraOption = document.getElementById('native-live-camera-option');
    const video = document.getElementById('native-live-preview');
    const status = document.getElementById('native-live-status');
    const captions = document.getElementById('native-live-captions');
    const start = document.getElementById('native-live-start');
    const mute = document.getElementById('native-live-mute');
    const close = document.getElementById('native-live-close');
    let session = null;
    let conversationId = null;
    let configuration = null;
    let configurationRequest = null;

    function endpoint(path, websocket = false) {
        const url = new URL(path, location.href);
        if (websocket) url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const embed = window.AurvekEmbed?.config;
        if (embed) {
            url.searchParams.set('embed_frame', embed.frame_instance_id);
            url.searchParams.set('ui_language', window.AurvekI18n.language);
        }
        return url;
    }

    function setStatus(message, error = false) {
        status.textContent = message;
        status.hidden = !message;
        status.dataset.error = String(error);
    }

    function idleControls() {
        start.textContent = 'Start call'; start.disabled = !configuration;
        start.dataset.active = 'false'; mute.hidden = true; mute.disabled = true;
        cameraOption.hidden = !configuration?.camera;
        camera.disabled = !configuration?.camera;
        if (camera.disabled) camera.checked = false;
    }

    class NativeSession {
        constructor() {
            this.sources = new Set(); this.generation = 0; this.nextStart = 0;
            this.ready = false; this.ending = false; this.frameBusy = false;
        }

        async start() {
            if (typeof stopAllAudio === 'function') stopAllAudio();
            const token = window.AurvekEmbed?.config?.csrf_token || document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
            if (!token) throw new Error('Sign in again to start a call.');
            if (!window.isSecureContext) throw new Error('Open this page over HTTPS to start a call.');
            if (!navigator.mediaDevices?.getUserMedia) throw new Error('This browser does not support voice calls. Try another browser.');
            this.stream = await navigator.mediaDevices.getUserMedia({audio: {
                channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
            }, video: camera.checked ? {width: {ideal: 640}, height: {ideal: 480}, frameRate: {ideal: 10}} : false});
            if (this.ending) {
                this.stream.getTracks().forEach(track => track.stop());
                throw new Error('Call canceled.');
            }
            video.srcObject = this.stream;
            video.hidden = !camera.checked;
            if (camera.checked) await video.play();
            if (this.ending) throw new Error('Call canceled.');
            this.context = new AudioContext({sampleRate: 16000});
            await this.context.resume();
            if (this.context.sampleRate !== 16000 || !this.context.audioWorklet) throw new Error('This browser does not support voice calls. Try another browser.');
            const workletUrl = endpoint('/sdk/native-live-worklet.js');
            const version = new URL(scriptUrl).searchParams.get('v');
            if (version) workletUrl.searchParams.set('v', version);
            try {
                await this.context.audioWorklet.addModule(workletUrl.href);
            } catch (error) {
                console.error('Call audio module could not load.', error);
                throw new Error('Could not prepare call audio. Reload the page and try again.');
            }
            if (this.ending) throw new Error('Call canceled.');
            this.source = this.context.createMediaStreamSource(this.stream);
            this.worklet = new AudioWorkletNode(this.context, 'aurvek-native-microphone');
            this.silence = this.context.createGain(); this.silence.gain.value = 0;
            this.source.connect(this.worklet).connect(this.silence).connect(this.context.destination);
            this.worklet.port.onmessage = event => {
                if (this.ready && !this.ending) {
                    if (this.socket.bufferedAmount > 64000) { void fail(new Error('The connection is too slow. Please try again.')); return; }
                    this.socket.send(event.data);
                }
            };
            this.socket = new WebSocket(endpoint(`/ws/conversations/${conversationId}/live`, true));
            await new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('The call took too long to connect. Please try again.')), 30000);
                this.socket.onopen = () => {
                    if (this.ending) this.socket.close();
                    else this.send({event: 'start', csrf_token: token, model_id: configuration.model_id, camera: camera.checked});
                };
                this.socket.onerror = () => { clearTimeout(timer); reject(new Error('Could not connect. Check your connection and try again.')); };
                this.socket.onclose = () => {
                    clearTimeout(timer);
                    if (!this.started) reject(new Error('The call could not connect. Please try again.'));
                    else if (session === this && !this.ending) void stopSession('Call ended.');
                };
                this.socket.onmessage = event => {
                    try {
                        const message = JSON.parse(event.data);
                        if (message.event === 'error') throw new Error(message.message);
                        if (message.event === 'ready') {
                            this.ready = this.started = true; clearTimeout(timer); resolve();
                            setStatus('Listening. You can speak now.');
                            if (camera.checked) this.frameTimer = setInterval(() => void this.frame(), 1100);
                        } else if (message.event === 'audio') this.play(message);
                        else if (message.event === 'ending') {
                            this.serverEnding = true;
                            void this.releaseMedia();
                            start.disabled = true; mute.disabled = true;
                            setStatus('Ending call...');
                        } else if (message.event === 'ended') {
                            this.serverEnding = true;
                            if (session === this) void stopSession('Call ended.');
                        } else if (message.event === 'backend') {
                            this.backendActive = message.active;
                            if (!this.ending && !this.serverEnding) setStatus(this.backendActive
                                ? 'Checking...' : (this.sources.size ? 'Speaking. You can interrupt.' : 'Listening.'));
                        } else if (message.event === 'notice' && message.message === 'voice_recording_unavailable') {
                            this.recordingFailed = true;
                            setStatus(window.AurvekI18n.t('chat_widgets.voice_call.recording_unavailable'), true);
                        }
                        else if (message.event === 'interrupted') {
                            this.generation = message.generation; this.clearAudio();
                            setStatus(this.backendActive ? 'Checking...' : 'Listening.');
                        } else if (message.event === 'transcript') {
                            const label = message.role === 'user' ? 'You: ' : 'Assistant: ';
                            captions.textContent = (captions.textContent + '\n' + label + message.text).slice(-3000);
                            captions.hidden = false;
                            captions.scrollTop = captions.scrollHeight;
                        }
                    } catch (error) { clearTimeout(timer); reject(error); void fail(error); }
                };
            });
        }

        send(value) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(value)); }

        play(message) {
            if (this.ending || this.serverEnding || message.generation !== this.generation) return;
            const bytes = Uint8Array.from(atob(message.audio), ch => ch.charCodeAt(0));
            const view = new DataView(bytes.buffer);
            const buffer = this.context.createBuffer(1, bytes.length / 2, 24000);
            const samples = buffer.getChannelData(0);
            for (let index = 0; index < samples.length; index++) samples[index] = view.getInt16(index * 2, true) / 32768;
            const node = this.context.createBufferSource(); node.buffer = buffer; node.connect(this.context.destination);
            const generation = this.generation;
            this.sources.add(node);
            this.nextStart = Math.max(this.nextStart, this.context.currentTime + .025);
            if (this.nextStart - this.context.currentTime > 30) throw new Error('Audio could not keep up. Please try again.');
            node.onended = () => {
                this.sources.delete(node);
                node.disconnect();
                if (!this.ending && !this.serverEnding && generation === this.generation && !node.canceled) {
                    this.send({event: 'played', id: message.id, generation});
                    if (!this.sources.size) setStatus(this.backendActive ? 'Checking...' : 'Listening.');
                }
            };
            node.start(this.nextStart); this.nextStart += buffer.duration;
            setStatus(this.backendActive ? 'Checking...' : 'Speaking. You can interrupt.');
        }

        async frame() {
            if (this.frameBusy || this.ending || !this.ready || !video.videoWidth) return;
            this.frameBusy = true;
            try {
                const canvas = document.createElement('canvas');
                canvas.width = Math.min(video.videoWidth, 640);
                canvas.height = Math.round(video.videoHeight * canvas.width / video.videoWidth);
                canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
                const jpeg = canvas.toDataURL('image/jpeg', .65).split(',')[1];
                this.send({event: 'frame', jpeg});
            } finally { this.frameBusy = false; }
        }

        clearAudio() {
            for (const node of this.sources) { node.canceled = true; node.stop(); node.disconnect(); }
            this.sources.clear(); this.nextStart = 0;
        }

        releaseMedia() {
            if (this.mediaReleased) return this.mediaReleased;
            this.ready = false;
            clearInterval(this.frameTimer);
            this.stream?.getTracks().forEach(track => track.stop());
            this.clearAudio();
            this.worklet?.disconnect(); this.source?.disconnect();
            this.silence?.disconnect();
            this.mediaReleased = this.context && this.context.state !== 'closed' ? this.context.close() : Promise.resolve();
            video.srcObject = null; video.hidden = true;
            return this.mediaReleased;
        }

        async stop() {
            if (this.ending) return;
            this.ending = true;
            await this.releaseMedia();
            if (!this.serverEnding) this.send({event: 'stop'});
            if (this.socket && this.socket.readyState !== WebSocket.CLOSED) {
                await new Promise(resolve => {
                    const timer = setTimeout(resolve, 5000);
                    this.socket.addEventListener('close', () => { clearTimeout(timer); resolve(); }, {once: true});
                });
                this.socket.close();
            }
        }
    }

    async function stopSession(message = 'Call ended.') {
        const current = session;
        if (!current) return;
        session = null;
        start.disabled = true; mute.disabled = true;
        setStatus('Ending call...');
        await current.stop();
        idleControls();
        setStatus(current.recordingFailed
            ? `${message} ${window.AurvekI18n.t('chat_widgets.voice_call.recording_unavailable')}` : message,
            Boolean(current.recordingFailed));
        if (typeof window.refreshActiveConversation === 'function') await window.refreshActiveConversation();
    }

    async function fail(error) {
        const devices = camera.checked ? 'microphone and camera' : 'microphone';
        const message = {
            NotAllowedError: `Allow access to your ${devices} in your browser, then try again.`,
            NotFoundError: `No ${devices} found. Check your devices and try again.`,
            NotReadableError: `Your ${devices} could not be opened. Close other apps using them and try again.`
        }[error.name] || error.message || 'The call could not start. Please try again.';
        await stopSession(message);
        setStatus(message, true);
    }

    async function openPanel() {
        conversationId = typeof currentConversationId === 'undefined' ? null : currentConversationId;
        if (!conversationId || dialog.open) return;
        configuration = null; camera.checked = false; idleControls();
        dialog.showModal(); setStatus('Preparing call...'); captions.textContent = ''; captions.hidden = true;
        const request = new AbortController(); configurationRequest = request;
        try {
            const response = await fetch(endpoint(`/api/conversations/${conversationId}/live/config`), {credentials: 'same-origin', signal: request.signal});
            const result = await response.json();
            if (request.signal.aborted) return;
            if (!response.ok) {
                setStatus(typeof result.detail === 'string' ? result.detail : 'Voice calls are unavailable right now.', true);
                return;
            }
            configuration = result; idleControls(); setStatus('');
        } catch (error) {
            if (!request.signal.aborted) setStatus('Could not prepare the call. Close this window and try again.', true);
        }
    }

    document.getElementById('plus-native-live')?.addEventListener('click', () => void openPanel());
    document.getElementById('voice-native-open')?.addEventListener('click', () => void openPanel());
    start.addEventListener('click', async () => {
        if (session) { await stopSession(); return; }
        if (!configuration) return;
        start.disabled = true; start.textContent = 'Connecting...'; camera.disabled = true; cameraOption.hidden = true;
        captions.textContent = ''; captions.hidden = true;
        setStatus(camera.checked ? 'Allow microphone and camera access to start the call.' : 'Allow microphone access to start the call.');
        const active = new NativeSession(); session = active;
        try {
            await active.start();
            if (session !== active) return;
            start.textContent = 'End call'; start.dataset.active = 'true'; start.disabled = false;
            mute.hidden = false; mute.disabled = false; mute.textContent = 'Mute'; mute.setAttribute('aria-pressed', 'false');
        } catch (error) { if (session === active) await fail(error); }
    });
    mute.addEventListener('click', () => {
        const track = session?.stream?.getAudioTracks()[0];
        if (track) {
            track.enabled = !track.enabled; mute.textContent = track.enabled ? 'Mute' : 'Unmute';
            mute.setAttribute('aria-pressed', String(!track.enabled));
        }
    });
    close.addEventListener('click', async () => { configurationRequest?.abort(); await stopSession(); dialog.close(); });
    dialog.addEventListener('cancel', event => { event.preventDefault(); close.click(); });
    window.addEventListener('pagehide', () => { session?.stream?.getTracks().forEach(track => track.stop()); session?.socket?.close(); });
})();
