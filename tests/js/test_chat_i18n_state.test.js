const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { createRuntime } = require('../../data/static/js/common/i18n.js');

const source = name => fs.readFileSync(path.join(__dirname, '../../data/static/js/chat', name), 'utf8');
function extract(text, start, end) {
    return text.slice(text.indexOf(start), text.indexOf(end, text.indexOf(start)));
}

function payload(language) {
    const resources = {};
    for (const locale of new Set(['en', language])) {
        resources[locale] = {};
        for (const domain of ['common', 'chat', 'chat_widgets', 'chat_errors', 'embed']) {
            resources[locale][domain] = JSON.parse(fs.readFileSync(path.join(__dirname, '../../locales', locale, domain + '.json'), 'utf8'));
        }
    }
    return { version: 1, language, locales: { en: 'en-US', es: 'es-ES', ja: 'ja-JP', de: 'de-DE' }, resources };
}
function translator(language) {
    return createRuntime(payload(language));
}

test('stream controls keep their action when their visible label changes', () => {
    const button = { dataset: {}, innerText: '送信' };
    const synced = [];
    const context = {
        document: { getElementById: () => button },
        window: { AurvekI18n: translator('ja'), AurvekEmbed: { syncSendControl: value => synced.push(value) } },
        stopReceivingStream() {}, handleSendButtonClick() {},
    };
    vm.createContext(context);
    vm.runInContext(extract(source('utils.js'), 'function toggleSendButton(', 'function handleSendButtonClick('), context);
    context.toggleSendButton(true);
    assert.equal(button.innerText, '停止');
    button.innerText = 'Parar'; // The embed adapter can change presentation while streaming.
    assert.equal(button.dataset.streaming, 'true');
    assert.equal(button.onclick, context.stopReceivingStream);
    context.toggleSendButton(false);
    assert.equal(button.innerText, '送信');
    assert.equal(button.dataset.streaming, 'false');
    assert.equal(button.onclick, context.handleSendButtonClick);
    assert.deepEqual(synced, [true, false]);
});

test('localized menu labels are escaped and do not become markup', () => {
    const link = { classList: { add() {} }, addEventListener() {} };
    const escapeHTML = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    const context = { document: { createElement: () => link }, escapeHTML };
    vm.createContext(context);
    vm.runInContext(extract(source('chat.js'), 'function createMenuLink(', 'const EXTERNAL_CHANNEL_ORDER'), context);
    const text = translator('ja').render('chat.use_platform', { platform: '<img src=x onerror=alert(1)>' });
    context.createMenuLink('fa-plug', text, () => {});
    assert.match(link.innerHTML, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.doesNotMatch(link.innerHTML, /<img/);
});

test('timestamp formatting follows UI locale while preserving its UTC source', () => {
    const original = '2026-09-08T13:24:00Z';
    for (const language of ['ja', 'de']) {
        const i18n = translator(language);
        const context = { window: { AurvekI18n: i18n }, Intl, Date };
        vm.createContext(context);
        vm.runInContext(extract(source('chat.js'), 'function convertToLocalTime(', 'function processMessage('), context);
        const result = context.convertToLocalTime(original);
        assert.equal(result.originalUtc, original);
        assert.equal(result.localTime, new Date(original).toLocaleString(i18n.locale, {
            year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
            hour12: false, timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        }));
    }
});

test('a conversation named My Bookmarks is not the bookmarks view', () => {
    const context = { currentChatView: 'conversation' };
    vm.createContext(context);
    vm.runInContext(extract(source('chat.js'), 'function isMyBookmarksView()', 'function updateChatHeader('), context);
    assert.equal(context.isMyBookmarksView(), false);
    context.currentChatView = 'bookmarks';
    assert.equal(context.isMyBookmarksView(), true);
});

test('audio errors present localized HTTP details and never submit the draft on failure', async () => {
    const i18n = translator('ja');
    const shown = [];
    let balanceShown = 0;
    const composer = { value: 'Unsent draft', dispatchEvent() {} };
    let sends = 0;
    const context = {
        AurvekI18n: i18n, Event, removeLoadingIndicator() {},
        showInsufficientBalancePopup() { balanceShown++; },
        NotificationModal: { error: (...args) => shown.push(args) },
        document: { getElementById: id => id === 'message-text' ? composer : { click() { sends++; } } },
    };
    vm.createContext(context);
    vm.runInContext(extract(source('audio.js'), 'async function handleAudioResponse(', '///// Timer for sending audio'), context);
    const detail = i18n.render('chat_errors.provider_unavailable');
    for (const status of [400, 500, 503]) {
        await context.handleAudioResponse({ status, ok: false, json: async () => ({ detail }) });
        assert.deepEqual(shown.at(-1), [i18n.render('chat_widgets.audio.error_title'), detail]);
    }
    await context.handleAudioResponse({ status: 502, ok: false, json: async () => { throw new Error('HTML proxy error'); } });
    assert.deepEqual(shown.at(-1), [i18n.render('chat_widgets.audio.error_title'), detail]);
    await context.handleAudioResponse({ status: 402, ok: false });
    await context.handleAudioResponse({ status: 204, ok: true });
    assert.equal(balanceShown, 1);
    assert.equal(composer.value, 'Unsent draft');
    assert.equal(sends, 0);
    await context.handleAudioResponse({ status: 200, ok: true, json: async () => ({ prompt: 'Original transcript 日本語' }) });
    assert.equal(composer.value, 'Original transcript 日本語');
    assert.equal(sends, 1);
});

function labelNode() {
    const attributes = new Map();
    return {
        textContent: '', children: [], isConnected: true,
        classList: { add() {} },
        setAttribute(name, value) { attributes.set(name, String(value)); },
        getAttribute(name) { return attributes.get(name) ?? null; },
        append(...children) { this.children.push(...children); },
        appendChild(child) { this.children.push(child); },
    };
}

test('scoped dictation and playback send native CSRF while ordinary audio keeps its endpoints', async () => {
    const requests = [];
    let sends = 0;
    const composer = {value: '', focus() {}, dispatchEvent() {}};
    const response = {ok: true, status: 200, blob: async () => null, json: async () => ({prompt: 'Editable dictation'})};
    const context = {
        window: {secureFetch: async (url, options) => { requests.push({secure: true, url, options}); return response; }},
        fetch: async (url, options) => { requests.push({secure: false, url, options}); return response; },
        document: {querySelector: selector => selector.startsWith('meta') ? {content: 'native-csrf-token'} : null,
            getElementById: id => id === 'message-text' ? composer : {click() { sends++; }}},
        Config: {}, AbortController, Event, console,
        isPlaying: false, isWaiting: false, ttsGeneration: 0,
        stopAllAudio() {}, toggleIcons() {}, removeLoadingIndicator() {},
    };
    vm.createContext(context);
    vm.runInContext(extract(source('audio.js'), 'function audioApplication(', 'function toggleIcons('), context);
    vm.runInContext(extract(source('audio.js'), 'async function sendFormData(', '///// Timer for sending audio'), context);
    // The native page loads chat.js after audio.js; its legacy handler must not
    // overwrite scoped dictation and turn an editable transcript into an AI turn.
    vm.runInContext(extract(source('chat.js'), 'async function handleResponse(', 'function showPromptInfo('), context);
    const icon = {closest: () => ({dataset: {messageId: '90'}, querySelector: () => null})};
    for (const application of [true, false]) {
        context.window.applicationConversation = application ? {id: 2231, application: {capabilities: {tts: true}}} : null;
        await context.sendFormData(new Map([['conversation_id', '2231']]));
        assert.equal(composer.value, 'Editable dictation');
        assert.equal(sends, application ? 0 : 1);
        context.isWaiting = false;
        context.textToSpeech('Saved reply', 136, 2231, icon, 'bot');
        await new Promise(resolve => setImmediate(resolve));
    }
    assert.deepEqual(requests.map(item => [item.secure, item.url]), [
        [true, '/api/conversations/2231/voice/transcribe'], [true, '/api/conversations/2231/voice/tts'],
        [false, '/api/transcribe-web'], [false, '/api/get-tts-audio'],
    ]);
    assert.equal(requests[0].options.headers['X-GPTSub-CSRF'], 'native-csrf-token');
    assert.equal(requests[1].options.headers['X-GPTSub-CSRF'], 'native-csrf-token');
    assert.equal(requests[1].options.headers['Content-Type'], 'application/json');
    assert.equal(requests[2].options.headers, undefined);
    assert.equal(requests[3].options.headers['X-GPTSub-CSRF'], undefined);
});

test('late dictation cannot replace the draft after cancel or a conversation change', async () => {
    for (const action of ['cancel', 'conversation_change']) {
        let completeJson;
        const composer = {value: 'Unsent draft'};
        const session = {generation: 7, conversationId: 2231, action: 'send', controller: new AbortController()};
        const context = {
            recordingGeneration: 7, recordingSession: session, recordingStatus: 'processing',
            currentConversationId: 2231, isWaiting: false, isPlaying: false, Config: {},
            audioApplication: () => ({capabilities: {stt: true}}),
            fetchAudio: async () => ({ok: true, status: 200, json: () => new Promise(resolve => { completeJson = resolve; })}),
            removeLoadingIndicator() {}, stopRecording() {}, hideAudioRecordingControls() {}, releaseMediaStream() {},
            document: {getElementById: () => composer},
            NotificationModal: {error() { throw new Error('Cancelled dictation showed an error'); }},
        };
        vm.createContext(context);
        const audio = source('audio.js');
        vm.runInContext(extract(audio, 'function getAudioActivity()', 'function releaseMediaStream(')
            + extract(audio, 'function recordingIsCurrent(', 'async function toggleAudioRecording(')
            + extract(audio, 'function requestRecordingStop(', 'function stopAudioRecording(')
            + extract(audio, 'function cancelAudioRecording()', 'function sendAudioRecording(')
            + extract(audio, 'async function sendFormData(', '///// Timer for sending audio'), context);
        assert.equal(context.getAudioActivity(), 'generating');
        const pending = context.sendFormData(new Map([['conversation_id', '2231']]), session);
        await new Promise(resolve => setImmediate(resolve));
        if (action === 'cancel') {
            context.cancelAudioRecording();
            assert.equal(session.controller.signal.aborted, true);
            assert.equal(context.getAudioActivity(), 'idle');
        } else context.currentConversationId = 2229;
        completeJson({prompt: 'Late transcription'});
        await pending;
        assert.equal(composer.value, 'Unsent draft');
    }
});

test('channel labels and descriptions switch while retaining normalized message metadata', () => {
    const i18n = translator('en');
    const context = { window: { AurvekI18n: i18n }, document: { createElement: labelNode },
        getExternalPlatformIcon: () => 'fa-brands fa-whatsapp' };
    vm.createContext(context);
    vm.runInContext(extract(source('chat.js'), 'function normalizeMessageChannelProvenance(', 'function getVoiceInterruptionPresentation('), context);
    const message = { channel_provenance: { channel: 'whatsapp', direction: 'inbound', content_kind: 'voice_note',
        voice_note: { audio: { available: true, url: '/api/attachments/a/content' } } } };
    const before = JSON.stringify(message);
    const provenance = context.normalizeMessageChannelProvenance(message);
    const badge = context.createMessageChannelBadge(provenance);
    const english = badge.getAttribute('title');
    const kindLabel = badge.children[2].children[1];
    i18n.setPayload(payload('ja'));
    assert.notEqual(badge.getAttribute('title'), english);
    assert.equal(kindLabel.textContent, i18n.t('chat.voice_note'));
    assert.equal(badge.getAttribute('title'), badge.getAttribute('aria-label'));
    assert.equal(provenance.originalAudio.url, '/api/attachments/a/content');
    assert.equal(JSON.stringify(message), before);
});

test('final pipeline summary updates nested status and numeric formats without reopening the panel', () => {
    const i18n = translator('en');
    const title = labelNode();
    const classes = new Set();
    const panel = { classList: { add: name => classes.add(name) }, querySelector: () => title, querySelectorAll: () => [] };
    const message = { querySelector: () => panel };
    const context = { window: { AurvekI18n: i18n }, Intl };
    vm.createContext(context);
    vm.runInContext(source('chat.js').slice(source('chat.js').indexOf('function finalizeGranSabioStatus(')), context);
    const summary = { approved: true, final_score: 8.5, iterations_used: 2, total_cost: 0.0123 };
    context.finalizeGranSabioStatus(message, summary);
    const initial = title.textContent;
    i18n.setPayload(payload('de'));
    assert.notEqual(title.textContent, initial);
    assert.ok(title.textContent.includes(i18n.t('chat.gransabio.approved')));
    assert.ok(title.textContent.includes('8,5'));
    assert.ok(classes.has('collapsed'));
    assert.equal(summary.total_cost, 0.0123);
});

test('embed errors keep host guidance and hide unknown provider details', () => {
    const i18n = translator('en');
    const context = { window: { AurvekI18n: i18n } };
    vm.createContext(context);
    vm.runInContext(extract(source('chat.js'), 'function embeddedErrorKey(', 'function accessibleCopyControl('), context);
    assert.equal(context.embeddedErrorKey({ error_code: 'api_keys_required' }), 'embed.keys');
    assert.equal(context.embeddedErrorKey({ error_code: 'insufficient_balance' }), 'embed.balance');
    assert.equal(context.embeddedErrorKey({ error_code: 'expected_llm_id_required' }), 'embed.error');
    assert.equal(context.embeddedErrorKey({ error_code: 'conversation_model_changed' }), 'embed.error');
    assert.equal(context.embeddedErrorText({ error_code: 'provider_private', error: 'private raw detail' }), i18n.t('embed.error'));
    i18n.setPayload(payload('ja'));
    assert.equal(context.embeddedErrorText({ error_code: 'api_keys_required' }), i18n.t('embed.keys'));
});
