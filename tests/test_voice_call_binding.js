'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = path.resolve(
    __dirname,
    '../data/static/js/chat/voice-call.js'
);
const source = fs.readFileSync(sourcePath, 'utf8');

class FakeClassList {
    constructor() {
        this.values = new Set();
    }

    add(...values) {
        values.forEach(value => this.values.add(value));
    }

    remove(...values) {
        values.forEach(value => this.values.delete(value));
    }

    contains(value) {
        return this.values.has(value);
    }

    toggle(value, force) {
        const enabled = force === undefined ? !this.values.has(value) : force;
        if (enabled) {
            this.values.add(value);
        } else {
            this.values.delete(value);
        }
        return enabled;
    }
}

class FakeElement {
    constructor(id = '') {
        this.id = id;
        this.classList = new FakeClassList();
        this.disabled = false;
        this.innerHTML = '';
        this.style = {};
        this.dataset = {};
        this.attributes = {};
        this.hidden = false;
        this.listeners = new Map();
        this.icon = { className: '' };
        this.textContent = '';
    }

    addEventListener(type, handler) {
        this.listeners.set(type, handler);
    }

    appendChild() {}
    focus() {}
    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    querySelector(selector) {
        return selector === 'i' ? this.icon : null;
    }

    async trigger(type) {
        const handler = this.listeners.get(type);
        if (handler) {
            await handler({ preventDefault() {} });
        }
    }
}

function response(data, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => data
    };
}

function createHarness({ rejectStart = false, availability = null } = {}) {
    const ids = [
        'plus-voice-call',
        'voice-overlay',
        'voice-start-stop',
        'voice-mute-toggle',
        'close-voice-overlay',
        'voice-status-text',
        'voice-status-icon',
        'voice-overlay-prompt',
        'voice-overlay-avatar',
        'voice-overlay-prompt-name',
        'voice-helper-text',
        'voice-overlay-caption',
        'voice-call-availability-reason',
        'message-text',
        'send-button',
        'chat-files',
        'window-chat'
    ];
    const elements = Object.fromEntries(ids.map(id => [id, new FakeElement(id)]));
    const requests = [];
    let sessionOptions = null;
    let refreshCount = 0;
    let loadMessagesCount = 0;
    let endSessionCount = 0;

    const document = {
        readyState: 'complete',
        addEventListener() {},
        createElement: () => new FakeElement(),
        getElementById: id => elements[id] || null
    };

    async function secureFetch(url, options = {}) {
        requests.push({ url: String(url), options });
        const availabilityMatch = String(url).match(
            /^\/api\/conversations\/(\d+)\/elevenlabs\/availability$/
        );
        if (availabilityMatch) {
            return response(availability || {
                available: true,
                error_code: null,
                reason: null,
                provider: 'elevenlabs',
                voice: 'voice-canonical'
            });
        }
        const configMatch = String(url).match(
            /^\/api\/conversations\/(\d+)\/elevenlabs\/config$/
        );
        if (configMatch) {
            const conversationId = Number(configMatch[1]);
            return response({
                conversation_id: conversationId,
                conversation_name: `Chat ${conversationId}`,
                prompt_id: 10,
                prompt_name: 'Coach',
                prompt_text: 'Help the user',
                agent_id: 'agent-main',
                user_id: 7,
                context: ''
            });
        }
        if (String(url).endsWith('/elevenlabs/session')) {
            if (rejectStart) {
                return response({
                    error: 'voice_session_binding_conflict',
                    message: 'Session binding rejected'
                }, 409);
            }
            return response({ status: 'active' });
        }
        if (String(url).endsWith('/elevenlabs/complete')) {
            return response({ status: 'completed', messages_saved: 1 });
        }
        if (String(url).endsWith('/elevenlabs/stop')) {
            return response({ status: 'failed' });
        }
        throw new Error(`Unexpected request: ${url}`);
    }

    const Conversation = {
        async startSession(options) {
            sessionOptions = options;
            await options.onConnect({ conversationId: 'provider-session-a' });
            return {
                async endSession() {
                    endSessionCount += 1;
                    await options.onDisconnect();
                },
                setMicMuted() {}
            };
        }
    };

    const windowObject = {
        resolveConversationGlobal: () => Conversation,
        refreshActiveConversation: async () => {
            refreshCount += 1;
        }
    };
    const context = vm.createContext({
        botProfilePicture: null,
        clearTimeout() {},
        console: { error() {}, log() {}, warn() {} },
        currentConversationId: 101,
        document,
        isCurrentConversationLocked: false,
        loadMessages: async () => {
            loadMessagesCount += 1;
        },
        navigator: {
            mediaDevices: {
                getUserMedia: async () => ({
                    getTracks: () => [{ stop() {} }]
                })
            }
        },
        secureFetch,
        setTimeout(callback) {
            callback();
            return 0;
        },
        window: windowObject
    });
    vm.runInContext(source, context, { filename: sourcePath });

    return {
        context,
        elements,
        requests,
        get sessionOptions() {
            return sessionOptions;
        },
        get refreshCount() {
            return refreshCount;
        },
        get loadMessagesCount() {
            return loadMessagesCount;
        },
        get endSessionCount() {
            return endSessionCount;
        }
    };
}

test('a call keeps its original conversation after navigating to another chat', async () => {
    const harness = createHarness();

    await harness.elements['plus-voice-call'].trigger('click');
    await harness.elements['voice-start-stop'].trigger('click');

    assert.equal(
        harness.sessionOptions.dynamicVariables.aurvek_conversation_id,
        '101'
    );
    assert.equal(harness.sessionOptions.dynamicVariables.aurvek_user_id, '7');

    harness.context.currentConversationId = 202;
    await harness.sessionOptions.onDisconnect();

    const operationRequests = harness.requests.filter(
        request => !request.url.endsWith('/elevenlabs/config')
            && !request.url.endsWith('/elevenlabs/availability')
    );
    assert.deepEqual(
        operationRequests.map(request => request.url),
        [
            '/api/conversations/101/elevenlabs/session',
            '/api/conversations/101/elevenlabs/complete'
        ]
    );
    assert.equal(
        JSON.parse(operationRequests[1].options.body).session_id,
        'provider-session-a'
    );
    assert.equal(harness.refreshCount, 0);
    assert.equal(harness.loadMessagesCount, 0);
});

test('cached configuration is not reused after selecting another chat', async () => {
    const harness = createHarness();

    await harness.elements['plus-voice-call'].trigger('click');
    await harness.elements['plus-voice-call'].trigger('click');
    harness.context.currentConversationId = 202;
    await harness.elements['plus-voice-call'].trigger('click');

    const configUrls = harness.requests
        .map(request => request.url)
        .filter(url => url.endsWith('/elevenlabs/config'));
    assert.deepEqual(configUrls, [
        '/api/conversations/101/elevenlabs/config',
        '/api/conversations/202/elevenlabs/config'
    ]);
});

test('a provider handle is closed when binding fails before startSession resolves', async () => {
    const harness = createHarness({ rejectStart: true });

    await harness.elements['plus-voice-call'].trigger('click');
    await harness.elements['voice-start-stop'].trigger('click');

    assert.equal(harness.endSessionCount, 1);
    assert.equal(
        harness.requests.filter(request => request.url.endsWith('/elevenlabs/session')).length,
        1
    );
});

test('canonical availability disables the voice button and exposes its exact reason', async () => {
    const reason = 'ElevenLabs cannot reproduce the canonical openai voice exactly.';
    const harness = createHarness({
        availability: {
            available: false,
            error_code: 'elevenlabs_webrtc_voice_incompatible',
            reason,
            provider: 'openai',
            voice: 'alloy'
        }
    });

    await harness.context.window.syncElevenLabsVoiceAvailability();

    const button = harness.elements['plus-voice-call'];
    const visibleReason = harness.elements['voice-call-availability-reason'];
    assert.equal(button.disabled, true);
    assert.equal(button.hidden, false);
    assert.equal(button.title, reason);
    assert.equal(button.dataset.availabilityCode, 'elevenlabs_webrtc_voice_incompatible');
    assert.equal(visibleReason.hidden, false);
    assert.equal(visibleReason.textContent, reason);

    await button.trigger('click');
    assert.equal(
        harness.requests.filter(request => request.url.endsWith('/elevenlabs/config')).length,
        0
    );
});

test('availability synchronizes when the selected conversation changes', async () => {
    const harness = createHarness();
    await harness.context.window.syncElevenLabsVoiceAvailability();

    harness.context.currentConversationId = 202;
    await harness.context.window.syncElevenLabsVoiceAvailability();

    const availabilityUrls = harness.requests
        .map(request => request.url)
        .filter(url => url.endsWith('/elevenlabs/availability'));
    assert.equal(availabilityUrls.at(-1), '/api/conversations/202/elevenlabs/availability');
    assert.equal(harness.elements['plus-voice-call'].disabled, false);
});

test('incognito availability hides the WebRTC control', async () => {
    const harness = createHarness({
        availability: {
            available: false,
            error_code: 'conversation_incognito',
            reason: 'Browser voice calls are unavailable in incognito chats.',
            provider: null,
            voice: null
        }
    });

    await harness.context.window.syncElevenLabsVoiceAvailability();

    assert.equal(harness.elements['plus-voice-call'].disabled, true);
    assert.equal(harness.elements['plus-voice-call'].hidden, true);
    assert.equal(harness.elements['voice-call-availability-reason'].hidden, true);
});
