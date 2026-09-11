const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const voicePath = path.join(repoRoot, 'data/static/js/chat/voice-call.js');
const voiceSource = fs.readFileSync(voicePath, 'utf8');
const { create: createI18n, createRuntime } = require('../../data/static/js/common/i18n.js');
const browserVoiceSource = fs.readFileSync(path.join(repoRoot, 'data/static/js/chat/browser-voice.js'), 'utf8');
const AurvekI18n = createI18n({
    version: 1, language: 'en', locales: { en: 'en-US' }, resources: {
        en: Object.fromEntries(['common', 'chat_widgets'].map(domain => [domain,
            JSON.parse(fs.readFileSync(path.join(repoRoot, 'locales/en', `${domain}.json`), 'utf8'))])),
    },
});

function extract(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, `Missing marker: ${startMarker}`);
    assert.notEqual(end, -1, `Missing marker: ${endMarker}`);
    return source.slice(start, end);
}

async function exerciseRetry(firstResult) {
    const completeSessionSource = extract(
        voiceSource,
        'async function completeSession()',
        'async function handleConnected'
    );
    const requests = [];
    const markedStatuses = [];
    const queue = [
        firstResult,
        {
            ok: true,
            status: 200,
            async json() { return { messages_saved: 0, status: 'completed' }; },
        },
    ];
    const context = {
        AurvekI18n,
        tr: (key, params) => AurvekI18n.t(`chat_widgets.voice_call.${key}`, params),
        console: { error() {} },
        async secureFetch(url, options) {
            requests.push({ url, body: JSON.parse(options.body) });
            const result = queue.shift();
            if (result && result.networkError) {
                throw new Error('network unavailable');
            }
            return result;
        },
        async markSessionStatus(status, conversationId, sessionId) {
            markedStatuses.push({ status, conversationId, sessionId });
        },
        setState() {},
        setLocalizedState() {},
        lockChatInputs() {},
        updateMuteUI() {},
    };
    vm.createContext(context);
    vm.runInContext(
        `
        let activeSessionId = 'provider-session';
        let callConversationId = '42';
        let completionRetryPending = false;
        let completing = false;
        let conversationRef = {};
        let sessionStartRejected = false;
        let configData = { conversation_id: '42' };
        let muteState = false;
        const closeButton = { disabled: false };
        function clearCallBinding() {
            activeSessionId = null;
            callConversationId = null;
            completionRetryPending = false;
            configData = null;
        }
        ${extract(voiceSource, 'function localizedVoiceErrorKey(', 'function applyVoiceAvailability(')}
        ${completeSessionSource}
        globalThis.runCompletion = completeSession;
        globalThis.readState = () => JSON.stringify({
            activeSessionId,
            callConversationId,
            completionRetryPending,
            completing,
        });
        `,
        context
    );

    await context.runCompletion();
    const retryState = JSON.parse(context.readState());
    assert.deepEqual(retryState, {
        activeSessionId: 'provider-session',
        callConversationId: '42',
        completionRetryPending: true,
        completing: false,
    });
    assert.deepEqual(markedStatuses, []);

    await context.runCompletion();
    const completedState = JSON.parse(context.readState());
    assert.deepEqual(completedState, {
        activeSessionId: null,
        callConversationId: null,
        completionRetryPending: false,
        completing: false,
    });
    assert.equal(requests.length, 2);
    assert.equal(requests[0].url, requests[1].url);
    assert.deepEqual(requests.map(item => item.body), [
        { session_id: 'provider-session' },
        { session_id: 'provider-session' },
    ]);
}

for (const [label, firstResult] of [
    [
        '425 response',
        {
            ok: false,
            status: 425,
            async json() { return { error: 'Transcript not ready' }; },
        },
    ],
    [
        '502 response',
        {
            ok: false,
            status: 502,
            async json() { return { error: 'Provider unavailable' }; },
        },
    ],
    ['network failure', { networkError: true }],
]) {
    test(`voice completion preserves binding and retries the same session after ${label}`, async () => {
        await exerciseRetry(firstResult);
    });
}

test('voice retry button and incognito UI use the existing call and privacy state', () => {
    assert.match(
        voiceSource,
        /completionRetryPending\s*&&\s*activeSessionId[\s\S]*await completeSession\(\)/
    );
    assert.match(
        voiceSource,
        /typeof currentConversationIncognito !== 'undefined'/
    );
    assert.match(voiceSource, /voiceButton\.hidden = incognito/);
    assert.match(voiceSource, /new MutationObserver\(syncVoiceAvailability\)/);
    assert.match(voiceSource, /bindText\(savedText, 'chat_widgets\.voice_call\.transcript_saved', \{count: saved\}\)/);
});

for (const errorCode of ['wellbeing_pause_active', 'wellbeing_pause_required']) {
    test(`session start preserves ${errorCode} and refreshes wellbeing state`, async () => {
        const refreshes = [];
        const states = [];
        const context = {
            AurvekI18n,
            tr: (key, params) => AurvekI18n.t(`chat_widgets.voice_call.${key}`, params),
            console: { warn() {}, error() {} },
            window: {
                WellbeingReminders: {
                    async refresh() { refreshes.push(errorCode); },
                },
            },
            async secureFetch() {
                return {
                    ok: false,
                    status: 429,
                    async json() { return { error: errorCode }; },
                };
            },
            setState(state, message, options) { states.push({ state, message, options }); },
            setLocalizedState(state, messageKey, options = {}) { states.push({state,
                message: AurvekI18n.t(`chat_widgets.voice_call.${messageKey}`),
                options: {helper: options.helperKey ? AurvekI18n.t(`chat_widgets.voice_call.${options.helperKey}`) : undefined}}); },
            lockChatInputs() {},
        };
        vm.createContext(context);
        vm.runInContext(
            `
            let activeSessionId = null;
            let callConversationId = '42';
            let configData = {};
            let conversationRef = null;
            let sessionStartRejected = false;
            const closeButton = { disabled: true };
            function clearCallBinding() {
                activeSessionId = null;
                callConversationId = null;
            }
            function extractSessionId() { return 'provider-session'; }
            ${extract(voiceSource, 'function localizedVoiceErrorKey(', 'function applyVoiceAvailability(')}
            ${extract(voiceSource, 'async function markSessionStarted(', 'async function markSessionStatus(')}
            ${extract(voiceSource, 'async function handleConnected(', 'async function handleDisconnected(')}
            globalThis.runConnected = handleConnected;
            globalThis.readResult = () => ({ sessionStartRejected, closeDisabled: closeButton.disabled });
            `,
            context
        );

        await context.runConnected({});

        assert.deepEqual(refreshes, [errorCode]);
        assert.equal(states.length, 1);
        assert.equal(states[0].state, 'error');
        assert.equal(states[0].message, AurvekI18n.t('chat_widgets.voice_call.pause_required'));
        assert.equal(states[0].options.helper, AurvekI18n.t('chat_widgets.voice_call.use_break_reminder'));
        assert.deepEqual(JSON.parse(JSON.stringify(context.readResult())), {
            sessionStartRejected: true,
            closeDisabled: false,
        });
    });
}

test('unknown session errors remain localized to the generic message', async () => {
    const context = {
        AurvekI18n,
        tr: (key, params) => AurvekI18n.t(`chat_widgets.voice_call.${key}`, params),
        console: { warn() {}, error() {} },
        async secureFetch() {
            return {
                ok: false,
                status: 409,
                async json() { return { error: 'raw backend detail' }; },
            };
        },
    };
    vm.createContext(context);
    vm.runInContext(
        `
        let callConversationId = '42';
        let configData = {};
        ${extract(voiceSource, 'function localizedVoiceErrorKey(', 'function applyVoiceAvailability(')}
        ${extract(voiceSource, 'async function markSessionStarted(', 'async function markSessionStatus(')}
        globalThis.runStart = markSessionStarted;
        `,
        context
    );

    const result = await context.runStart('provider-session');
    assert.equal(result.error, 'raw backend detail');
    assert.equal(result.message, AurvekI18n.t('chat_widgets.voice_call.request_failed'));
    assert.notEqual(result.message, 'raw backend detail');
});

test('embedded 401 voice errors retain the host-directed message identity across locale changes', () => {
    const locales = {en: 'en-US', ja: 'ja-JP'};
    const payload = language => ({version: 1, language, locales, resources: Object.fromEntries(
        Array.from(new Set(['en', language]), code => [code, Object.fromEntries(
            ['common', 'chat_widgets', 'embed'].map(domain => [domain, JSON.parse(
                fs.readFileSync(path.join(repoRoot, `locales/${code}/${domain}.json`), 'utf8'))]))]))});
    const runtime = createRuntime(payload('en'));
    const context = vm.createContext({AurvekI18n: runtime, window: {AurvekEmbed: {}},
        tr: (key, params) => runtime.t(`chat_widgets.voice_call.${key}`, params)});
    vm.runInContext(
        extract(voiceSource, 'function localizedVoiceErrorKey(', 'function applyVoiceAvailability(')
        + extract(voiceSource, 'function message(presentation)', 'function repaintLocalizedState()'), context);
    const key = context.localizedVoiceErrorKey('', 401);
    assert.equal(key, 'embed.expired');
    assert.equal(context.message({key}), runtime.t('embed.expired'));
    assert.doesNotMatch(context.message({key}), /Go to Login/i);
    runtime.setPayload(payload('ja'));
    assert.equal(context.message({key}), runtime.t('embed.expired'));
});

test('embedded browser voice binds the websocket to the current validated UI language', () => {
    assert.match(browserVoiceSource, /url\.searchParams\.set\('embed_frame', embed\.frame_instance_id\)/);
    assert.match(browserVoiceSource, /url\.searchParams\.set\('ui_language', global\.AurvekI18n\.language\)/);
});

test('active embedded voice presentation retranslates without changing call state', () => {
    const locales = {en: 'en-US', ja: 'ja-JP'};
    const payload = language => ({version: 1, language, locales, resources: Object.fromEntries(
        Array.from(new Set(['en', language]), code => [code, {
            common: JSON.parse(fs.readFileSync(path.join(repoRoot, `locales/${code}/common.json`), 'utf8')),
            chat_widgets: JSON.parse(fs.readFileSync(path.join(repoRoot, `locales/${code}/chat_widgets.json`), 'utf8')),
        }])
    )});
    const runtime = createRuntime(payload('en'));
    const statusText = {textContent: ''};
    const helperText = {textContent: ''};
    const caption = {textContent: ''};
    const muteButton = {setAttribute() {}, querySelector() { return null; }, classList: {toggle() {}}};
    const states = [];
    const context = {
        AurvekI18n: runtime, tr: (key, params) => runtime.t(`chat_widgets.voice_call.${key}`, params),
        statusText, helperText, caption, muteButton, muteState: true,
        currentState: 'active', startStopButton: {textContent: ''},
        voiceAvailability: null, captionPresentation: {key: 'conversation_caption', params: {name: 'Named conversation'}}, localizedState: null,
        updateMuteUI() {}, applyVoiceAvailability() {}, localizedVoiceError() {},
        setState(state, rendered, options) { states.push(state); context.currentState = state; statusText.textContent = rendered; helperText.textContent = options.helper; },
    };
    vm.createContext(context);
    vm.runInContext(extract(voiceSource, 'function message(presentation)', 'function setState(state, message, options = {})'), context);
    runtime.onChange(context.repaintLocalizedState);
    context.setLocalizedState('active', 'listening');
    const session = {id: 'same-session', muted: true, disabled: false, draft: 'Keep this draft'};

    runtime.setPayload(payload('ja'));

    assert.deepEqual(states, ['active']);
    assert.equal(statusText.textContent, runtime.t('chat_widgets.voice_call.listening'));
    assert.equal(helperText.textContent, runtime.t('chat_widgets.voice_call.speak_normally'));
    assert.equal(caption.textContent, runtime.t('chat_widgets.voice_call.conversation_caption', {name: 'Named conversation'}));
    assert.equal(context.startStopButton.textContent, runtime.t('chat_widgets.voice_call.end_call'));
    context.setLocalizedState('ready', 'ready');
    runtime.setPayload(payload('en'));
    assert.equal(statusText.textContent, runtime.t('chat_widgets.voice_call.ready'));
    assert.equal(helperText.textContent, runtime.t('chat_widgets.voice_call.press_start'));
    assert.equal(context.startStopButton.textContent, runtime.t('chat_widgets.voice_call.start_call'));
    assert.deepEqual(session, {id: 'same-session', muted: true, disabled: false, draft: 'Keep this draft'});
});
