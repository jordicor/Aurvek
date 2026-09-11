const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const crypto = require('node:crypto');
const { createRuntime } = require('../../data/static/js/common/i18n.js');

const locales = { en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE' };
function payload(language) {
    const resources = {};
    for (const code of new Set(['en', language])) {
        resources[code] = {};
        for (const domain of ['application', 'embed', 'common']) {
            resources[code][domain] = JSON.parse(fs.readFileSync(path.join(__dirname, `../../locales/${code}/${domain}.json`), 'utf8'));
        }
    }
    return { version: 1, language, locales, resources };
}

const adapter = fs.readFileSync(path.join(__dirname, '../../data/static/js/chat/embed-adapter.js'), 'utf8');
const panel = fs.readFileSync(path.join(__dirname, '../../data/static/js/chat/application-panel.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };

function setup(options = {}) {
    const config = {
        contract_version: 'aurvek_embed.v1', app_id: 'sample', external_project_id: 'project-1',
        conversation_id: String(options.conversationId || 12), frame_instance_id: 'frame_instance_123',
        parent_origin: 'https://product.example', ui_language: 'es',
        capabilities: { text: true }, csrf_token: 'secret-csrf',
        application: { contract_version: 'aurvek_applications.v1', app_id: 'sample', context_id: 'context-1',
            assistant_id: options.assistantId || 'reception', conversation_id: options.conversationId || 12, display_name: 'Reception' }
    };
    const calls = [], emitted = [], reloads = [], handlers = {};
    const storage = options.storage || new Map();
    const formControl = { disabled: false, value: options.draft || '' };
    const nodes = new Map();
    function makeNode() {
        return { textContent: '', value: '', disabled: false, dataset: {}, listeners: {}, children: [], attributes: {},
            setAttribute(name, value) { this.attributes[name] = value; }, getAttribute(name) { return this.attributes[name]; },
            closest() { return null; }, addEventListener(name, callback) { this.listeners[name] = callback; },
            replaceChildren() { this.children = []; }, append(child) { this.children.push(child); } };
    }
    for (const id of ['panel', 'active', 'assistant', 'status', 'resume', 'fresh', 'back', 'entry', 'reset', 'entry-value']) {
        nodes.set(`application-${id}`, makeNode());
    }
    nodes.set('embed-session-status', makeNode());
    const document = {
        getElementById(id) { return id === 'aurvek-embed-config' ? { textContent: JSON.stringify(config) } : nodes.get(id) || null; },
        querySelectorAll(selector) {
            if (selector.startsWith('#form-message')) return [formControl];
            if (selector.startsWith('#application-panel')) return ['assistant', 'resume', 'fresh', 'back', 'entry', 'reset'].map(id => nodes.get(`application-${id}`));
            return [];
        },
        createElement: makeNode, documentElement: {},
        addEventListener(name, callback) { (handlers[`document-${name}`] ||= []).push(callback); }
    };
    const parent = { postMessage(message, origin) { emitted.push({ message, origin }); } };
    const state = {
        application: { ...config.application, private_key: 'hidden' },
        assistants: [{ assistant_id: 'reception', display_name: 'Reception', prompt_id: 99 }, { assistant_id: 'coach', display_name: 'Coach' }],
        entry_preferences: { web: null }, previous_assistant_id: 'coach', previous_conversation_id: 9,
        last_handoff_operation_id: options.lastOperation || null,
        transcript: ['hidden']
    };
    const global = {
        parent, crypto,
        location: { href: 'https://chat.example/embed/sample/frame_instance_123/chat', origin: 'https://chat.example', replace(url) { reloads.push(url); } },
        fetch: async (url, init) => {
            calls.push({ url, init });
            const override = await options.respond?.(url, init, state);
            if (override instanceof Error) throw override;
            if (override) return override;
            if (url.includes('/session?')) return Response.json({ i18n: payload(new URL(url, 'https://chat.example').searchParams.get('ui_language')) });
            return new Response(JSON.stringify(url.endsWith('/application/state') ? state : { activity: 'idle' }), { headers: { 'content-type': 'application/json' } });
        },
        sessionStorage: { getItem(key) { return storage.get(key); }, setItem(key, value) { storage.set(key, value); }, removeItem(key) { storage.delete(key); } },
        addEventListener(name, callback) { handlers[name] = callback; },
        setTimeout(callback) { callback(); }, queueMicrotask
    };
    global.AurvekI18n = createRuntime(payload(config.ui_language), document);
    const context = { window: global, document, URL, Headers, Request, Response, TextEncoder, console };
    vm.runInNewContext(adapter, context);
    vm.runInNewContext(panel, context);
    function command(extra, envelope = {}) {
        handlers.message({ origin: config.parent_origin, source: parent,
            data: { contract_version: config.contract_version, app_id: config.app_id,
                external_project_id: config.external_project_id, frame_instance_id: config.frame_instance_id,
                conversation_id: config.conversation_id, request_id: 'request-1', ...extra }, ...envelope });
    }
    return { global, config, state, calls, emitted, reloads, storage, nodes, formControl, command,
        mountPanel() { handlers['document-DOMContentLoaded'].at(-1)(); } };
}
const handoff = { event: 'request_handoff', operation_id: 'handoff-0001', assistant_id: 'coach', mode: 'resume' };

test('runtime handoff reloads only its completed source frame without stale requests or returned URLs', () => {
    const context = setup();
    const result = { completed: true, source_conversation_id: 12, conversation_id: 9,
        assistant_id: 'coach', operation_id: 'tool-handoff-1', reload_url: 'https://attacker.example' };
    assert.equal(context.global.AurvekApplication.runtimeHandoff({ ...result, source_conversation_id: 77 }), false);
    assert.equal(context.global.AurvekApplication.runtimeHandoff({ ...result, completed: false }), false);
    assert.equal(context.global.AurvekApplication.runtimeHandoff(result), true);
    assert.deepEqual(context.reloads, ['/embed/sample/frame_instance_123/chat']);
    assert.equal(context.calls.length, 0);
    assert.equal(context.formControl.disabled, true);
    assert.equal(JSON.parse([...context.storage.values()][0]).operation_id, 'tool-handoff-1');
});

test('handoff bridge rejects stale scope and malformed or extra fields without side effects', async () => {
    const context = setup();
    for (const extra of [
        { source_conversation_id: 12 }, { transcript: 'private' }, { operation_id: 'short' },
        { assistant_id: '../coach' }, { mode: 'other' }, { note: 'x'.repeat(2001) },
        { destination_conversation_id: '9' }, { mode: 'new', destination_conversation_id: 9 },
        { conversation_id: 'other' }, { frame_instance_id: 'other-frame' }, { app_id: 'other' },
        { external_project_id: 'other' }, { contract_version: 'aurvek_applications.v1' }
    ]) context.command({ ...handoff, ...extra });
    context.command(handoff, { origin: 'https://attacker.example' });
    context.command(handoff, { source: {} });
    await settle();
    assert.equal(context.calls.length, 0);
    assert.equal(context.emitted.length, 0);
});

test('handoff waits for native idle then posts bound source and exact destination before fixed frame reload', async () => {
    let activityReads = 0;
    const context = setup({ respond(url) {
        if (url.endsWith('/activity')) return Response.json({ activity: ++activityReads === 1 ? 'ending' : 'idle' });
        if (url.endsWith('/application/handoff')) return Response.json({ reload_url: 'https://attacker.example' });
    } });
    context.command({ ...handoff, destination_conversation_id: 9, note: 'Brief user-provided context' });
    assert.equal(context.formControl.disabled, true);
    await settle();
    const post = context.calls.find(call => call.url.endsWith('/application/handoff'));
    assert.ok(activityReads >= 3);
    assert.deepEqual(JSON.parse(post.init.body), { operation_id: 'handoff-0001', source_conversation_id: 12,
        assistant_id: 'coach', mode: 'resume', conversation_id: 9, note: 'Brief user-provided context' });
    assert.equal(post.init.headers.get('X-Aurvek-Embed-Conversation'), '12');
    assert.equal(post.init.headers.get('X-GPTSub-CSRF'), 'secret-csrf');
    assert.deepEqual(context.reloads, ['/embed/sample/frame_instance_123/chat']);
    assert.equal(context.emitted.some(item => item.message.event === 'assistant_changed'), false);
});

test('failed activity close never submits a handoff and restores current controls', async () => {
    const context = setup({ respond(url) {
        if (url.endsWith('/activity/stop')) return Response.json({ error: 'failed' }, { status: 503 });
    } });
    context.command(handoff);
    await settle();
    assert.equal(context.calls.some(call => call.url.endsWith('/application/handoff')), false);
    assert.equal(context.formControl.disabled, false);
    assert.equal(context.reloads.length, 0);
    assert.equal(context.emitted.at(-1).message.code, 'activity_close_pending');
});

test('a stale frame rejected during stop re-resolves state and reloads without declaring expiry', async () => {
    const context = setup({ respond(url) {
        if (url.endsWith('/activity/stop') || url.endsWith('/application/state')) return new Response(null, { status: 404 });
    } });
    context.command(handoff);
    await settle();
    assert.equal(context.calls.some(call => call.url.endsWith('/application/handoff')), false);
    assert.deepEqual(context.reloads, ['/embed/sample/frame_instance_123/chat']);
    assert.equal(context.global.AurvekEmbed.isExpired(), false);
    assert.equal(context.formControl.disabled, true);
});

test('rejected destination leaves origin usable and does not leak server errors or note', async () => {
    const context = setup({ respond(url) {
        if (url.endsWith('/application/handoff')) return Response.json({ detail: 'private diagnostics' }, { status: 403 });
    } });
    context.command(handoff);
    await settle();
    assert.equal(context.formControl.disabled, false);
    assert.equal(context.reloads.length, 0);
    assert.equal(context.storage.size, 0);
    assert.equal(context.emitted.at(-1).message.event, 'handoff_failed');
    assert.equal(JSON.stringify(context.emitted).includes('private diagnostics'), false);
});

for (const failure of [404, 409, 503, 'network']) {
    test(`ambiguous ${failure} handoff resolves via fixed frame reload without declaring expiry or retrying`, async () => {
        const context = setup({ respond(url) {
            if (url.endsWith('/application/handoff')) return failure === 'network' ? new Error('network') : new Response(null, { status: failure });
        } });
        context.command(handoff);
        await settle();
        assert.deepEqual(context.reloads, ['/embed/sample/frame_instance_123/chat']);
        assert.equal(context.calls.filter(call => call.url.endsWith('/application/handoff')).length, 1);
        assert.equal(context.global.AurvekEmbed.isExpired(), false);
        assert.equal(context.emitted.some(item => ['session_expired', 'assistant_changed'].includes(item.message.event)), false);
    });
}

test('state and confirmed assistant change publish only whitelisted metadata after destination reload', async () => {
    const source = setup();
    source.command(handoff);
    await settle();
    const destination = setup({ storage: source.storage, conversationId: 20, assistantId: 'coach', lastOperation: 'handoff-0001' });
    await destination.global.AurvekApplication.onReady();
    const event = destination.emitted.find(item => item.message.event === 'assistant_changed');
    assert.equal(event.message.conversation_id, '20');
    assert.equal(event.message.application.assistant_id, 'coach');
    assert.equal(event.message.operation_id, 'handoff-0001');
    assert.equal(event.message.request_id, 'request-1');
    assert.equal(event.origin, destination.config.parent_origin);
    assert.equal(destination.storage.size, 0);
    for (const forbidden of ['private_key', 'hidden', 'secret-csrf', 'prompt_id', 'transcript']) {
        assert.equal(JSON.stringify(destination.emitted).includes(forbidden), false);
    }
});

test('unconfirmed handoff returning to source reports failure without claiming a switch', async () => {
    const source = setup();
    source.command(handoff);
    await settle();
    const destination = setup({ storage: source.storage });
    await destination.global.AurvekApplication.onReady();
    assert.equal(destination.emitted.at(-1).message.event, 'handoff_failed');
    assert.equal(destination.emitted.at(-1).message.code, 'handoff_not_confirmed');
});

test('a concurrent operation reaching the same assistant cannot acknowledge this handoff', async () => {
    const source = setup();
    source.command(handoff);
    await settle();
    const destination = setup({ storage: source.storage, conversationId: 20, assistantId: 'coach', lastOperation: 'different-operation' });
    await destination.global.AurvekApplication.onReady();
    assert.equal(destination.emitted.at(-1).message.event, 'handoff_failed');
    assert.equal(destination.emitted.at(-1).message.code, 'handoff_not_confirmed');
});

test('future entry preference has explicit save/reset and does not navigate or stop the current chat', async () => {
    const context = setup({ respond(url, init, state) {
        if (url.endsWith('/application/entry')) {
            state.entry_preferences.web = JSON.parse(init.body).assistant_id;
            return Response.json({ ok: true });
        }
    } });
    context.command({ event: 'request_entry_preference', channel: 'phone', assistant_id: 'coach' });
    context.command({ event: 'request_entry_preference', channel: 'web', assistant_id: 'coach', unsafe: true });
    await settle();
    assert.equal(context.calls.length, 0);
    for (const assistantId of ['coach', null]) {
        context.command({ event: 'request_entry_preference', channel: 'web', assistant_id: assistantId });
        await settle();
        assert.equal(context.state.entry_preferences.web, assistantId);
    }
    assert.equal(context.reloads.length, 0);
    assert.equal(context.calls.some(call => call.url.includes('/activity')), false);
    assert.equal(context.calls.some(call => call.url.includes('/handoff')), false);
});

test('two frames and pending duplicate commands cannot redirect one another', async () => {
    const first = setup(), second = setup();
    first.command(handoff);
    first.command(handoff);
    await settle();
    assert.equal(first.calls.filter(call => call.url.endsWith('/application/handoff')).length, 1);
    assert.equal(first.emitted.some(item => item.message.code === 'operation_in_progress'), true);
    assert.equal(second.calls.length, 0);
    assert.equal(second.reloads.length, 0);
});

test('ready retains application metadata and panel supports runtime language changes', async () => {
    const context = setup();
    context.global.AurvekEmbed.chatReady();
    await settle();
    const ready = context.emitted.find(item => item.message.event === 'ready');
    assert.equal(ready.message.application.assistant_id, 'reception');
    for (const [language, expected] of [['en', 'Future web entry saved.'], ['es', 'Se ha guardado la entrada para futuras sesiones web.'],
        ['ja', '次回のウェブセッションの開始先を保存しました。'], ['fr', 'Le point d’entrée des prochaines sessions web a été enregistré.'],
        ['pt', 'A entrada para futuras sessões Web foi guardada.'], ['it', 'La voce per le future sessioni web è stata salvata.'],
        ['de', 'Der Einstieg für künftige Websitzungen wurde gespeichert.']]) {
        context.command({ event: 'set_ui_language', ui_language: language });
        await settle();
        await context.global.AurvekApplication.setEntry(null);
        assert.equal(context.nodes.get('application-status').textContent, expected);
    }
});

test('live localization preserves loaded state, selection, controls and draft while rerendering status and complete entry sentence', async () => {
    const context = setup({ draft: 'Do not lose this draft' });
    context.state.entry_preferences.web = 'coach';
    context.mountPanel();
    await context.global.AurvekApplication.readState();
    context.nodes.get('application-assistant').value = 'coach';
    await context.global.AurvekApplication.setEntry('coach');
    assert.equal(context.nodes.get('application-entry-value').textContent, 'Próxima vez: Coach');

    context.command({ event: 'set_ui_language', ui_language: 'ja' });
    await settle();

    assert.equal(context.nodes.get('application-status').textContent, '次回のウェブセッションの開始先を保存しました。');
    assert.equal(context.nodes.get('application-entry-value').textContent, '次回：Coach');
    assert.equal(context.nodes.get('application-assistant').value, 'coach');
    assert.equal(context.nodes.get('application-resume').disabled, false);
    assert.equal(context.formControl.value, 'Do not lose this draft');
    assert.equal(context.calls.filter(call => call.url.endsWith('/application/state')).length, 2);
});

test('application and embed catalogs have matching valid keys in all seven languages', () => {
    for (const domain of ['application', 'embed']) {
        const english = JSON.parse(fs.readFileSync(path.join(__dirname, `../../locales/en/${domain}.json`), 'utf8'));
        for (const language of Object.keys(locales)) {
            const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, `../../locales/${language}/${domain}.json`), 'utf8'));
            assert.deepEqual(Object.keys(catalog).sort(), Object.keys(english).sort());
            for (const key of Object.keys(catalog)) assert.match(key, /^[a-z][a-z0-9_]*$/);
        }
    }
});

test('return control resumes the precise source conversation and new control explicitly creates a destination', async () => {
    for (const [button, expected] of [
        ['back', { assistant_id: 'coach', mode: 'resume', conversation_id: 9 }],
        ['fresh', { assistant_id: 'reception', mode: 'new' }]
    ]) {
        const context = setup();
        context.mountPanel();
        await context.global.AurvekApplication.readState();
        context.nodes.get(`application-${button}`).listeners.click();
        await settle();
        const posted = JSON.parse(context.calls.find(call => call.url.endsWith('/application/handoff')).init.body);
        assert.equal(posted.source_conversation_id, 12);
        assert.equal(posted.assistant_id, expected.assistant_id);
        assert.equal(posted.mode, expected.mode);
        assert.equal(posted.conversation_id, expected.conversation_id);
        assert.match(posted.operation_id, /^[a-f0-9-]{36}$/);
    }
});

test('unavailable saved entry does not disable selection and explains the future fallback', async () => {
    const context = setup();
    context.state.entry_preferences.web = 'retired-assistant';
    const result = await context.global.AurvekApplication.readState();
    assert.ok(result);
    assert.equal(context.nodes.get('application-resume').disabled, false);
    assert.match(context.nodes.get('application-entry-value').textContent, /no est\u00e1 disponible/);
});
