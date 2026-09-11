const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('data/static/js/chat/application-actions.js', 'utf8');
const settle = async () => { for (let n = 0; n < 8; n++) await new Promise(resolve => setImmediate(resolve)); };

function setup(capabilities, responses = []) {
    const calls = [], emitted = [], nodes = new Map();
    function element(tag) {
        return { tag, children: [], handlers: {}, disabled: false, clicks: 0, textContent: '',
            setAttribute() {}, addEventListener: function (key, fn) { this.handlers[key] = fn; },
            replaceChildren(...children) { this.children = children; }, click() { this.clicks++; }, focus() { this.focused = true; } };
    }
    nodes.set('application-action-controls', element('div'));
    nodes.set('chat-files', element('input'));
    let dictation = 0, voice = 0;
    const global = { location: { origin: 'https://chat.example' }, currentConversationId: 12,
        applicationConversation: { id: 12, application: { capabilities } },
        AurvekI18n: { t: value => value },
        toggleAudioRecording: async () => { dictation++; }, AurvekVoice: { open: async () => { voice++; } },
        secureFetch: async (url, options) => { calls.push({ url, options }); return new Response(JSON.stringify(responses.shift())); },
        setTimeout, clearTimeout };
    const document = { getElementById: id => nodes.get(id), createElement: element,
        querySelector: () => ({ content: 'native-csrf' }), querySelectorAll: () => [], addEventListener() {} };
    vm.runInNewContext(source, { window: global, document, URL, Headers });
    return { global, nodes, calls, emitted, dictation: () => dictation, voice: () => voice };
}

test('host attachment opens a real picker control inside the frame without faking activation', async () => {
    const context = setup({ attachments: true });
    assert.equal((await context.global.AurvekChatActions.run('attachment', { host: true })).status, 'interaction_required');
    const file = context.nodes.get('chat-files');
    assert.equal(file.clicks, 0);
    const choose = context.nodes.get('application-action-controls').children[0];
    assert.equal(choose.focused, true);
    choose.handlers.click();
    assert.equal(file.clicks, 1);
});

test('dictation and voice use native functions and disabled capabilities never invoke them', async () => {
    const context = setup({ stt: true, voice: true });
    await context.global.AurvekChatActions.run('dictation');
    await context.global.AurvekChatActions.run('voice');
    assert.equal(context.dictation(), 1); assert.equal(context.voice(), 1);
    context.global.applicationConversation.application.capabilities = {};
    await assert.rejects(context.global.AurvekChatActions.run('dictation'), /capability_disabled/);
    await assert.rejects(context.global.AurvekChatActions.run('export_pdf'), /capability_disabled/);
    assert.equal(context.dictation(), 1); assert.equal(context.calls.length, 0);
});

test('export uses scoped native CSRF and publishes a private download only after completion', async () => {
    const id = 'a'.repeat(32), status = `/api/conversations/12/exports/pdf/${id}`;
    const context = setup({ export_pdf: true }, [{ job_id: id, status_url: status, status: 'queued' },
        { status: 'completed', download_url: status + '/content' }]);
    assert.equal((await context.global.AurvekChatActions.run('export_pdf')).status, 'started');
    await settle();
    assert.equal(context.calls[0].url, '/api/conversations/12/exports/pdf');
    assert.equal(context.calls[0].options.headers.get('X-GPTSub-CSRF'), 'native-csrf');
    assert.equal(context.nodes.get('application-action-controls').children[0].href, status + '/content');
    assert.equal(context.global.AurvekChatActions.activity(), 'idle');
});

test('export response cannot redirect status polling or content to another conversation', async () => {
    const id = 'a'.repeat(32);
    const context = setup({ export_pdf: true }, [{ job_id: id, status_url: `/api/conversations/13/exports/pdf/${id}` }]);
    await assert.rejects(context.global.AurvekChatActions.run('export_pdf'), /invalid_export_response/);
    assert.equal(context.calls.length, 1);
    assert.equal(context.global.AurvekChatActions.activity(), 'idle');
    const mp3 = setup({ export_mp3: true, tts: false });
    await assert.rejects(mp3.global.AurvekChatActions.run('export_mp3'), /capability_disabled/);
    assert.equal(mp3.calls.length, 0);
});

test('export stop waits for terminal status instead of treating its cancellation receipt as completion', async () => {
    const id = 'b'.repeat(32), status = `/api/conversations/12/exports/mp3/${id}`;
    const context = setup({ export_mp3: true, tts: true }, [{ job_id: id, status_url: status, status: 'queued' },
        { status: 'running' }, { status: 'ending' }, { status: 'cancelled' }]);
    await context.global.AurvekChatActions.run('export_mp3');
    assert.equal(context.global.AurvekChatActions.activity(), 'generating');
    let stopped = false;
    const stopping = context.global.AurvekChatActions.stopExport().then(() => { stopped = true; });
    await settle();
    assert.equal(stopped, false);
    assert.equal(context.calls[2].url, status + '/stop');
    await stopping;
    assert.equal(context.global.AurvekChatActions.activity(), 'idle');
});
