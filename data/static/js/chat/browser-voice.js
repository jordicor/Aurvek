/* Aurvek's microphone/playback transport; conversation logic remains server-side. */
if (typeof registerProcessor === 'function') {
    class AurvekMicrophone extends AudioWorkletProcessor {
        constructor() { super(); this.frames = new Float32Array(2048); this.offset = 0; }
        process(inputs) {
            const channel = inputs[0]?.[0];
            if (channel) {
                for (const sample of channel) {
                    this.frames[this.offset++] = sample;
                    if (this.offset === this.frames.length) {
                        this.port.postMessage(this.frames, [this.frames.buffer]);
                        this.frames = new Float32Array(2048);
                        this.offset = 0;
                    }
                }
            }
            return true;
        }
    }
    registerProcessor('aurvek-microphone', AurvekMicrophone);
} else (function (global) {
    'use strict';
    const scriptUrl = new URL('/static/js/chat/browser-voice.js', location.origin);
    scriptUrl.search = new URL(document.currentScript.src).search;
    const SAMPLE_RATE = 16000;

    function wavAudio(chunks) {
        const samples = chunks.reduce((total, chunk) => total + chunk.length, 0);
        const buffer = new ArrayBuffer(44 + samples * 2);
        const view = new DataView(buffer);
        const label = (offset, text) => [...text].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
        label(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); label(8, 'WAVE');
        label(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
        view.setUint16(22, 1, true); view.setUint32(24, SAMPLE_RATE, true);
        view.setUint32(28, SAMPLE_RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
        label(36, 'data'); view.setUint32(40, samples * 2, true);
        let offset = 44;
        for (const chunk of chunks) for (const value of chunk) {
            const sample = Math.max(-1, Math.min(1, value));
            view.setInt16(offset, sample * (sample < 0 ? 32768 : 32767), true); offset += 2;
        }
        let binary = '';
        const bytes = new Uint8Array(buffer);
        for (let index = 0; index < bytes.length; index += 16384) {
            binary += String.fromCharCode(...bytes.subarray(index, index + 16384));
        }
        return btoa(binary);
    }

    class BrowserVoice {
        constructor(options) {
            this.options = options;
            this.transport = 'aurvek';
            this.generation = 0;
            this.preRoll = [];
            this.recording = [];
            this.speechMs = 0;
            this.silenceMs = 0;
            this.busy = false;
            this.ready = false;
            this.muted = false;
            this.ending = false;
            this.interruptRequested = false;
        }

        static async startSession(options) {
            const session = new BrowserVoice(options);
            try { await session.start(); return session; }
            catch (error) { await session.cleanup(); session.socket?.close(); throw error; }
        }

        async start() {
            this.stream = await navigator.mediaDevices.getUserMedia({audio: {
                channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true
            }});
            this.context = new AudioContext({sampleRate: SAMPLE_RATE});
            if (this.context.sampleRate !== SAMPLE_RATE || !this.context.audioWorklet) {
                throw new Error(AurvekI18n.t('chat_widgets.browser_voice.unsupported_format'));
            }
            await this.context.resume();
            await this.context.audioWorklet.addModule(scriptUrl);
            this.microphone = this.context.createMediaStreamSource(this.stream);
            this.capture = new AudioWorkletNode(this.context, 'aurvek-microphone');
            this.silentOutput = this.context.createGain();
            this.silentOutput.gain.value = 0;
            this.microphone.connect(this.capture).connect(this.silentOutput).connect(this.context.destination);
            this.capture.port.onmessage = event => this.acceptSamples(event.data);

            const url = new URL(`/ws/conversations/${this.options.conversationId}/voice`, location.href);
            url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const embed = global.AurvekEmbed?.config;
            if (embed) {
                url.searchParams.set('embed_frame', embed.frame_instance_id);
                url.searchParams.set('ui_language', global.AurvekI18n.language);
            }
            const token = embed?.csrf_token || document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
            if (!token) throw new Error(AurvekI18n.t('chat_widgets.browser_voice.sign_in_failed'));
            this.socket = new WebSocket(url);
            await new Promise((resolve, reject) => {
                const timeout = setTimeout(() => reject(new Error(AurvekI18n.t('chat_widgets.browser_voice.connection_timeout'))), 15000);
                this.socket.onopen = () => this.send({event: 'start', csrf_token: token});
                this.socket.onerror = () => { clearTimeout(timeout); reject(new Error(AurvekI18n.t('chat_widgets.browser_voice.connection_failed'))); };
                this.socket.onmessage = async event => {
                    try {
                        const message = JSON.parse(event.data);
                        if (message.event === 'ready' && !this.ready) {
                            this.ready = true; clearTimeout(timeout); resolve();
                        }
                        await this.handleMessage(message);
                    } catch (error) {
                        clearTimeout(timeout); reject(error);
                        this.options.onError?.(error);
                        await this.endSession();
                    }
                };
                this.socket.onclose = async () => {
                    clearTimeout(timeout);
                    if (!this.ready) reject(new Error(AurvekI18n.t('chat_widgets.browser_voice.access_rejected')));
                    await this.cleanup();
                    this.options.onDisconnect?.();
                };
            });
        }

        send(message) {
            if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(message));
        }

        async handleMessage(message) {
            if (message.generation !== undefined) this.generation = message.generation;
            if (message.event === 'ready') {
                this.busy = false;
                this.interruptRequested = false;
                this.options.onState?.('listening', message.message);
            } else if (message.event === 'state') {
                this.options.onState?.(message.state);
            } else if (message.event === 'audio') {
                if (this.interruptRequested || this.ending) return;
                const audio = Uint8Array.from(atob(message.audio), char => char.charCodeAt(0));
                const decoded = await this.context.decodeAudioData(audio.buffer);
                if (this.interruptRequested || this.ending) return;
                const source = this.context.createBufferSource();
                source.buffer = decoded;
                source.connect(this.context.destination);
                this.playback = source;
                this.options.onState?.('speaking');
                source.onended = () => {
                    if (this.playback !== source) return;
                    this.playback = null;
                    this.send({event: 'played', id: message.id, played_ms: Math.round(decoded.duration * 1000)});
                };
                source.start();
            } else if (message.event === 'handoff') {
                this.preRoll = []; this.recording = []; this.speechMs = 0;
                this.options.onHandoff?.(message);
            } else if (message.event === 'error') {
                this.options.onError?.(new Error(message.message));
            } else if (message.event === 'notice') {
                this.options.onState?.('listening', message.message);
            }
        }

        acceptSamples(samples) {
            if (!this.ready || this.muted || this.ending) return;
            const milliseconds = samples.length / SAMPLE_RATE * 1000;
            const rms = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length);
            const speaking = rms > 0.015;
            if (!this.recording.length) {
                this.preRoll.push(samples);
                if (this.preRoll.length > 3) this.preRoll.shift();
                this.speechMs = speaking ? this.speechMs + milliseconds : 0;
                if (this.speechMs < 180) return;
                this.recording = this.preRoll;
                this.preRoll = [];
                this.silenceMs = 0;
                if (this.busy && !this.interruptRequested) {
                    this.interruptRequested = true;
                    this.stopPlayback();
                    this.send({event: 'interrupt'});
                }
            } else this.recording.push(samples);
            this.silenceMs = speaking ? 0 : this.silenceMs + milliseconds;
            // Keep 256 ms below the backend's one-minute recording bound.
            if (this.silenceMs >= 650 || this.recording.length >= 466) {
                this.send({event: 'utterance', generation: this.generation, audio: wavAudio(this.recording)});
                this.busy = true;
                this.recording = []; this.preRoll = []; this.speechMs = 0; this.silenceMs = 0;
                this.options.onState?.('thinking');
            }
        }

        stopPlayback() {
            const source = this.playback;
            this.playback = null;
            if (source) { source.onended = null; source.stop(); source.disconnect(); }
        }

        setMicMuted(muted) {
            this.muted = Boolean(muted);
            this.recording = []; this.preRoll = []; this.speechMs = 0;
        }

        async cleanup() {
            this.ending = true;
            this.stopPlayback();
            this.stream?.getTracks().forEach(track => track.stop());
            this.capture?.disconnect(); this.microphone?.disconnect();
            if (this.context && this.context.state !== 'closed') await this.context.close();
        }

        async endSession() {
            this.send({event: 'stop'});
            await this.cleanup();
            if (this.socket && this.socket.readyState < WebSocket.CLOSING) this.socket.close();
        }
    }
    global.AurvekBrowserVoice = BrowserVoice;
})(window);
