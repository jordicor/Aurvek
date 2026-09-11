const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const { createRuntime } = require('../../data/static/js/common/i18n.js');
const locales = { en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE' };
function payload(language) {
    const resources = {};
    for (const code of new Set(['en', language])) {
        resources[code] = {};
        for (const domain of ['embed', 'common']) resources[code][domain] = JSON.parse(fs.readFileSync(path.join(__dirname, `../../locales/${code}/${domain}.json`), 'utf8'));
    }
    return { version: 1, language, locales, resources };
}

const source = fs.readFileSync(path.join(__dirname, '../../data/static/js/chat/embed-adapter.js'), 'utf8');

function setup(activityResponses = ['idle'], fetchResponse, configOverrides = {}) {
    const config = {
        contract_version: 'aurvek_embed.v1', app_id: 'sample',
        external_project_id: 'project-1', conversation_id: '12',
        frame_instance_id: 'frame_instance_123', parent_origin: 'https://product.example',
        ui_language: 'es', capabilities: { text: true, voice: false, attachments: false },
        csrf_token: 'csrf-value', ...configOverrides
    };
    const emitted = [];
    const calls = [];
    const handlers = {};
    const status = { textContent: '' };
    const input = { value: 'Private draft', selectionStart: 4, placeholder: '', setAttribute(key, value) { this[key] = value; } };
    const button = { dataset: { streaming: 'true' }, setAttribute(key, value) { this[key] = value; } };
    const parent = { postMessage(message, origin) { emitted.push({ message, origin }); } };
    const document = {
        getElementById(id) { return id === 'aurvek-embed-config' ? { textContent: JSON.stringify(config) } : id === 'embed-session-status' ? status : id === 'message-text' ? input : id === 'send-button' ? button : null; },
        querySelectorAll() { return []; },
        documentElement: { dataset: {} },
        addEventListener() {}
    };
    const global = {
        parent, location: { href: 'https://chat.example/embed/sample/frame_instance_123/chat', origin: 'https://chat.example' },
        fetch: async (url, options) => {
            calls.push({ url, options });
            if (fetchResponse) return fetchResponse(url, options);
            if (url.includes('/session?')) return new Response(JSON.stringify({ i18n: payload(new URL(url, 'https://chat.example').searchParams.get('ui_language')) }));
            return new Response(JSON.stringify({ activity: activityResponses.length > 1 ? activityResponses.shift() : activityResponses[0] }), { headers: { 'content-type': 'application/json' } });
        },
        addEventListener(name, callback) { handlers[name] = callback; },
        setTimeout(callback) { callback(); }, queueMicrotask,
    };
    global.AurvekI18n = createRuntime(payload(config.ui_language), document);
    vm.runInNewContext(source, { window: global, document, URL, Headers, Request, Response, TextEncoder, console });
    const send = (overrides = {}, eventOverrides = {}) => handlers.message({
        data: { ...config, event: 'request_state', request_id: 'request-1', parent_origin: undefined, ui_language: undefined, capabilities: undefined, csrf_token: undefined, ...overrides },
        origin: config.parent_origin, source: parent, ...eventOverrides
    });
    const command = (overrides = {}, eventOverrides = {}) => {
        const data = { contract_version: config.contract_version, app_id: config.app_id, external_project_id: config.external_project_id, conversation_id: config.conversation_id, frame_instance_id: config.frame_instance_id, event: 'request_state', request_id: 'request-1', ...overrides };
        handlers.message({ data, origin: config.parent_origin, source: parent, ...eventOverrides });
    };
    return { global, emitted, calls, command, send, config, status, input, button };
}
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };

test('attachment XHR uses the current frame locale and scope only on its own origin', async () => {
    const context = setup();
    context.command({ event: 'set_ui_language', ui_language: 'de' });
    await settle();
    const requests = [];
    class Xhr {
        constructor() { this.headers = {}; this.upload = {}; requests.push(this); }
        open(method, url) { this.url = url; }
        setRequestHeader(name, value) { this.headers[name.toLowerCase()] = value; }
        send() { this.status = 200; this.responseText = '{}'; this.onload(); }
    }
    const files = fs.readFileSync(path.join(__dirname, '../../data/static/js/chat/fileHandling.js'), 'utf8');
    const scope = { window: context.global, XMLHttpRequest: Xhr, URL };
    vm.runInNewContext(files.slice(files.indexOf('function postAttachmentForm('), files.indexOf('async function discardUploadedAttachmentRefs(')), scope);
    await scope.postAttachmentForm('/api/conversations/12/attachments/chunk', {});
    assert.equal(requests[0].headers['x-aurvek-ui-language'], 'de');
    assert.equal(requests[0].headers['x-aurvek-embed-frame'], context.config.frame_instance_id);
    assert.equal(requests[0].headers['x-aurvek-embed-conversation'], '12');
    assert.equal(requests[0].headers['x-gptsub-csrf'], 'csrf-value');
    await scope.postAttachmentForm('https://other.example/upload', {});
    assert.equal(requests[1].headers['x-aurvek-ui-language'], undefined);
    assert.equal(requests[1].headers['x-gptsub-csrf'], undefined);
});

test('rejects wrong origin, source, version, frame, project and oversized commands', async () => {
    const context = setup();
    context.command({}, { origin: 'https://evil.example' });
    context.command({}, { source: {} });
    for (const changes of [
        { contract_version: 'v2' }, { frame_instance_id: 'old-frame' },
        { external_project_id: 'project-2' }, { conversation_id: 12 },
        { request_id: 'x'.repeat(9000) }, { event: 'send_message', text: 'unauthorized' },
        { event: 'set_ui_language', ui_language: 'es', html: '<script>' }
    ]) context.command(changes);
    await settle();
    assert.equal(context.calls.length, 0);
    assert.equal(context.emitted.length, 0);
});

test('valid state response includes only bound metadata and an exact target origin', async () => {
    const context = setup(['generating']);
    context.command();
    await settle();
    const event = context.emitted[0];
    assert.equal(event.origin, context.config.parent_origin);
    assert.equal(event.message.activity, 'generating');
    assert.equal(event.message.request_id, 'request-1');
    assert.equal(event.message.conversation_id, '12');
    for (const field of ['csrf_token', 'transcript', 'ticket', 'brief', 'audio']) assert.equal(field in event.message, false);
    const headers = context.calls[0].options.headers;
    assert.equal(headers.get('X-Aurvek-Embed-Frame'), context.config.frame_instance_id);
    assert.equal(headers.get('X-GPTSub-CSRF'), context.config.csrf_token);
});

test('idle clears the status banner rather than showing the fallback error', async () => {
    const context = setup(['idle']);
    context.command();
    await settle();
    assert.equal(context.status.textContent, '');
});

test('activity close acknowledgement waits for server idle after ending', async () => {
    const context = setup(['ending', 'ending', 'idle', 'idle']);
    context.command({ event: 'request_activity_close', request_id: 'close-1' });
    await settle();
    assert.equal(context.calls[0].options.method, 'POST');
    assert.ok(context.calls[0].url.endsWith('/activity/stop'));
    const acknowledged = context.emitted.filter(event => event.message.activity_closed);
    assert.equal(acknowledged.length, 1);
    assert.equal(acknowledged[0].message.activity, 'idle');
    assert.equal(acknowledged[0].message.request_id, 'close-1');
    assert.ok(context.emitted.some(event => event.message.activity === 'ending' && !event.message.activity_closed));
});

test('all seven UI languages use the scoped session and unsupported locales are ignored', async () => {
    const context = setup();
    for (const language of Object.keys(locales)) {
        context.command({ event: 'set_ui_language', ui_language: language });
        await settle();
        assert.equal(context.global.AurvekI18n.language, language);
        assert.equal(context.global.AurvekEmbed.t('send'), payload(language).resources[language].embed.send);
    }
    const count = context.calls.length;
    context.command({ event: 'set_ui_language', ui_language: 'ru' });
    assert.equal(context.global.AurvekI18n.language, 'de');
    assert.equal(context.calls.length, count);
    await context.global.fetch('/api/conversations/12');
    assert.equal(context.calls.at(-1).options.headers.get('X-Aurvek-UI-Language'), 'de');
});

test('late locale responses cannot override the latest valid host choice or another frame', async () => {
    const pending = [];
    const context = setup(['idle'], () => new Promise(resolve => pending.push(resolve)));
    const other = setup();
    context.command({ event: 'set_ui_language', ui_language: 'ja', request_id: 'first' });
    context.command({ event: 'set_ui_language', ui_language: 'fr', request_id: 'last' });
    context.command({ event: 'set_ui_language', ui_language: 'ru' });
    pending[1](new Response(JSON.stringify({ i18n: payload('fr') })));
    await settle();
    pending[0](new Response(JSON.stringify({ i18n: payload('ja') })));
    await settle();
    assert.equal(context.global.AurvekI18n.language, 'fr');
    assert.equal(other.global.AurvekI18n.language, 'es');
    assert.deepEqual(context.emitted.map(item => item.message.request_id), ['last']);
});

test('reselecting current language cancels a pending change without another fetch', async () => {
    let resolve;
    const context = setup(['idle'], () => new Promise(done => { resolve = done; }));
    context.command({ event: 'set_ui_language', ui_language: 'ja' });
    context.command({ event: 'set_ui_language', ui_language: 'es', request_id: 'current' });
    resolve(new Response(JSON.stringify({ i18n: payload('ja') })));
    await settle();
    assert.equal(context.calls.length, 1);
    assert.equal(context.global.AurvekI18n.language, 'es');
    assert.deepEqual(context.emitted.map(item => item.message.request_id), ['current']);
});

test('failed or mismatched locale payload retains valid UI and reports only a safe code', async () => {
    for (const response of [new Response('provider private error', { status: 503 }), new Response(JSON.stringify({ i18n: payload('de') }))]) {
        const context = setup(['idle'], async () => response);
        context.command({ event: 'set_ui_language', ui_language: 'ja' });
        await settle();
        assert.equal(context.global.AurvekI18n.language, 'es');
        assert.equal(context.emitted.at(-1).message.code, 'service_unavailable');
        assert.equal(JSON.stringify(context.emitted).includes('private'), false);
    }
});

test('frame credentials are never attached to cross-origin fetches or legacy media', async () => {
    const context = setup();
    await context.global.fetch('https://cdn.example/public.js');
    assert.equal(context.calls[0].options, undefined);
    assert.equal(context.global.AurvekEmbed.resourceUrl('https://cdn.example/users/secret.png'), '');
    assert.equal(context.global.AurvekEmbed.resourceUrl('/users/secret.png'), '');
});

test('switching during activity preserves composer and streaming controls without stop or reload', async () => {
    const context = setup(['generating']);
    context.command();
    await settle();
    context.command({ event: 'set_ui_language', ui_language: 'ja' });
    await settle();
    assert.equal(context.input.value, 'Private draft');
    assert.equal(context.input.selectionStart, 4);
    assert.equal(context.button.dataset.streaming, 'true');
    assert.equal(context.button['aria-label'], payload('ja').resources.ja.embed.stop);
    assert.equal(context.status.textContent, payload('ja').resources.ja.embed.generating);
    assert.equal(context.calls.length, 2);
    assert.ok(context.calls.every(call => call.options.method !== 'POST'));
});

test('a superseded failed locale request cannot expire or overwrite the current frame', async () => {
    let resolve;
    const context = setup(['idle'], () => new Promise(done => { resolve = done; }));
    context.command({ event: 'set_ui_language', ui_language: 'ja' });
    context.command({ event: 'set_ui_language', ui_language: 'es', request_id: 'current' });
    resolve(new Response('{}', { status: 401 }));
    await settle();
    assert.equal(context.global.AurvekEmbed.isExpired(), false);
    assert.deepEqual(context.emitted.map(item => item.message.request_id), ['current']);
});

test('a delayed forbidden response body cannot expire a newer successful locale selection', async () => {
    let finishBody;
    let bodyStarted = false;
    const context = setup(['idle'], async url => {
        if (url.endsWith('ui_language=ja')) {
            return {
                status: 403, ok: false,
                clone: () => ({ json: () => {
                    bodyStarted = true;
                    return new Promise(resolve => { finishBody = resolve; });
                } }),
            };
        }
        return new Response(JSON.stringify({ i18n: payload('fr') }));
    });
    context.command({ event: 'set_ui_language', ui_language: 'ja', request_id: 'old' });
    await settle();
    assert.equal(bodyStarted, true);
    context.command({ event: 'set_ui_language', ui_language: 'fr', request_id: 'current' });
    await settle();
    finishBody({ error: 'membership_inactive' });
    await settle();
    assert.equal(context.global.AurvekI18n.language, 'fr');
    assert.equal(context.global.AurvekEmbed.isExpired(), false);
    assert.equal(context.status.textContent, '');
    assert.deepEqual(context.emitted.map(item => item.message.request_id), ['current']);
});

test('application media URLs carry frame scope only for this conversation', () => {
    const {global} = setup(['idle'], null, {application: {context_id: 'context'}, capabilities: {text: true, attachments: true, voice: true}});
    for (const path of ['/api/conversations/12/media/content?media_id=9', '/api/conversations/12/voice/audio?attachment_ref=ref']) {
        const url = new URL(global.AurvekEmbed.resourceUrl(path));
        assert.equal(url.searchParams.get('embed_frame'), 'frame_instance_123');
        assert.equal(url.searchParams.get('embed_conversation'), '12');
        assert.equal(global.AurvekEmbed.resourceUrl(path.replace('/12/', '/13/')), '');
    }
    assert.equal(global.AurvekEmbed.resourceUrl('https://foreign.example/api/conversations/12/media/content?media_id=9'), '');
});

test('closing frame stops local recording and playback before acknowledging idle', async () => {
    const context = setup();
    let recording = true, playing = true;
    context.global.getAudioActivity = () => recording || playing ? 'call' : 'idle';
    context.global.cancelAudioRecording = () => { recording = false; };
    context.global.stopAllAudio = () => { playing = false; };
    context.command();
    await settle();
    assert.equal(context.emitted.at(-1).message.activity, 'call');
    context.command({event: 'request_activity_close'});
    await settle();
    assert.equal(recording || playing, false);
    assert.equal(context.emitted.at(-1).message.activity_closed, true);
});

test('host actions retain exact scope and dispatch the same native helpers without reading draft content', async () => {
    const context = setup(['idle'], null, { application: { context_id: 'context' } });
    const actions = [];
    context.global.AurvekChatActions = { run: async (name, options) => { actions.push({ name, options }); return { status: 'opened' }; } };
    context.input.focus = () => { context.input.focused = true; };
    context.command({ event: 'request_action', action: 'dictation' }, { origin: 'https://foreign.example' });
    context.command({ event: 'request_action', action: 'dictation', transcript: 'unexpected' });
    context.command({ event: 'request_action', action: 'dictation' });
    context.command({ event: 'set_presentation', presentation: 'compact' });
    context.command({ event: 'request_focus' });
    await settle();
    assert.equal(actions.length, 1);
    assert.equal(actions[0].name, 'dictation');
    assert.equal(actions[0].options.host, true);
    assert.equal(context.input.focused, true);
    assert.equal(context.input.value, 'Private draft');
    assert.ok(context.emitted.some(item => item.message.presentation === 'compact'));
    assert.ok(context.emitted.every(item => !('transcript' in item.message) && !('text' in item.message)));
});

test('export download URLs require capability and match the current conversation and exact job route', () => {
    const path = `/api/conversations/12/exports/pdf/${'a'.repeat(32)}/content`;
    const denied = setup(['idle'], null, { application: { context_id: 'context' } });
    assert.equal(denied.global.AurvekEmbed.resourceUrl(path), '');
    const context = setup(['idle'], null, { application: { context_id: 'context' }, capabilities: { export_pdf: true } });
    assert.equal(new URL(context.global.AurvekEmbed.resourceUrl(path)).searchParams.get('embed_conversation'), '12');
    assert.equal(context.global.AurvekEmbed.resourceUrl(path.replace('/12/', '/13/')), '');
    assert.equal(context.global.AurvekEmbed.resourceUrl(path.replace('/content', '/stop')), '');
});
