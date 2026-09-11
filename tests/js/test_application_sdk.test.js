const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync('data/static/js/aurvek-applications.js', 'utf8');
const settle = async () => { for (let n = 0; n < 4; n++) await new Promise(resolve => setImmediate(resolve)); };

function setup() {
    const handlers = {}, timers = new Map(), sent = [], events = [], nodes = [], submissions = [];
    let nextTimer = 0;
    function node(tag) {
        const value = { tag, children: [], style: {}, contentWindow: { postMessage: (data, origin) => sent.push({ data, origin }) },
            attributes: {}, setAttribute(key, value) { this.attributes[key] = value; },
            append(child) { this.children.push(child); }, remove() { this.removed = true; },
            submit() { submissions.push({ method: this.method, action: this.action, target: this.target,
                fields: this.children.map(child => ({ name: child.name, value: child.value })) }); } };
        nodes.push(value); return value;
    }
    const global = { crypto, console, addEventListener: (key, handler) => { handlers[key] = handler; },
        removeEventListener: key => { delete handlers[key]; },
        setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; }, clearTimeout: id => timers.delete(id) };
    vm.runInNewContext(source, { window: global, document: { createElement: node }, URL, TextEncoder });
    const bootstrap = { contract_version: 'aurvek_applications.v1', frame_contract_version: 'aurvek_embed.v1',
        embed_url: 'https://chat.example/embed/bootstrap', ticket: 'single-use-ticket', frame_instance_id: 'frame_instance_123' };
    const instance = global.AurvekApplications.mount(node('container'), { bootstrap, appId: 'sample', contextId: 'context-1', onEvent: data => events.push(data) });
    const frame = nodes.find(item => item.tag === 'iframe');
    const metadata = { contract_version: 'aurvek_embed.v1', event: 'ready', app_id: 'sample',
        frame_instance_id: 'frame_instance_123', external_project_id: 'project-1', conversation_id: '12',
        application: { contract_version: 'aurvek_applications.v1', app_id: 'sample', context_id: 'context-1', assistant_id: 'a', conversation_id: 12 } };
    const receive = (data = {}, origin = 'https://chat.example', eventSource = frame.contentWindow) => handlers.message?.({ data: { ...metadata, ...data }, origin, source: eventSource });
    return { instance, frame, nodes, bootstrap, timers, handlers, sent, events, submissions, receive, metadata, global, node };
}

test('profile frame has no conversation, exact origin validation and closing does not send chat stop', async () => {
    const context = setup(); context.instance.destroy();
    const bootstrap = {...context.bootstrap, surface: 'profile', embed_url: 'https://chat.example/embed/profile/bootstrap'};
    const panel = context.global.AurvekApplications.mountProfile(context.node('container'), {bootstrap, appId: 'sample'});
    const frame = context.nodes.filter(item => item.tag === 'iframe').at(-1);
    const metadata = {contract_version: 'aurvek_embed.v1', surface: 'profile', app_id: 'sample',
        frame_instance_id: bootstrap.frame_instance_id, event: 'ready', version: 1};
    let ready = false; panel.ready.then(() => {ready = true;});
    context.handlers.message({origin:'https://wrong.example', source: frame.contentWindow, data:metadata});
    context.handlers.message({origin:'https://chat.example', source: {}, data:metadata});
    await settle(); assert.equal(ready, false);
    context.handlers.message({origin:'https://chat.example', source:frame.contentWindow, data:metadata});
    await panel.ready;
    await panel.close(); assert.equal(frame.removed, true);
    assert.equal(context.sent.length, 0);
    assert.equal(context.submissions.at(-1).action, 'https://chat.example/embed/profile/bootstrap');
});

test('mount posts only the short ticket and accepts ready only from its exact frame, origin and context', async () => {
    const context = setup();
    assert.deepEqual(context.submissions, [{ method: 'POST', action: 'https://chat.example/embed/bootstrap', target: context.frame.name,
        fields: [{ name: 'ticket', value: 'single-use-ticket' }] }]);
    assert.equal(context.frame.allow, 'microphone https://chat.example; autoplay https://chat.example');
    assert.equal(context.frame.referrerPolicy, 'strict-origin');
    assert.equal(context.nodes.find(node => node.tag === 'input').value, '');
    context.receive({}, 'https://wrong.example');
    context.receive({}, 'https://chat.example', {});
    context.receive({ frame_instance_id: 'other-frame' });
    context.receive({ application: { ...context.metadata.application, context_id: 'context-2' } });
    assert.equal(context.events.length, 0);
    context.receive();
    assert.equal((await context.instance.ready).conversation_id, '12');
    await settle();
    assert.equal(context.sent[0].data.event, 'set_presentation');
    assert.equal(context.sent[0].origin, 'https://chat.example');
    context.instance.destroy();
    assert.equal(context.handlers.message, undefined);
    assert.equal(context.frame.removed, true);
    assert.equal(context.timers.size, 0);
});

test('handoff resolves after authorized destination ready and uses its scope for later commands', async () => {
    const context = setup(); context.receive(); await context.instance.ready; await settle();
    const result = context.instance.handoff('b'); await settle();
    const request = context.sent.at(-1).data;
    assert.equal(request.event, 'request_handoff');
    const destination = { ...context.metadata.application, assistant_id: 'b', conversation_id: 24 };
    context.receive({ conversation_id: '24', application: destination });
    context.receive({ event: 'assistant_changed', request_id: request.request_id, conversation_id: '24', application: destination });
    assert.equal((await result).application.assistant_id, 'b');
    const state = context.instance.state(); await settle();
    const stateRequest = context.sent.at(-1).data;
    assert.equal(stateRequest.conversation_id, '24');
    context.receive({ event: 'state', request_id: stateRequest.request_id, conversation_id: '24', application: destination });
    await state; context.instance.destroy();
});

test('stop requires settled acknowledgement and failed close keeps the frame available for retry', async () => {
    const context = setup(); context.receive(); await context.instance.ready; await settle();
    let finished = false;
    const stopping = context.instance.stop().then(() => { finished = true; }); await settle();
    const request = context.sent.at(-1).data;
    context.receive({ event: 'activity_changed', request_id: request.request_id, activity: 'ending' }); await settle();
    assert.equal(finished, false);
    context.receive({ event: 'activity_changed', request_id: request.request_id, activity: 'idle', activity_closed: true });
    await stopping;
    const close = context.instance.close(); await settle();
    context.receive({ event: 'error', request_id: context.sent.at(-1).data.request_id, code: 'activity_close_pending' });
    await assert.rejects(close, /activity_close_pending/);
    assert.equal(context.frame.removed, undefined);
    context.instance.destroy();
    assert.equal(context.frame.removed, true);
    assert.equal(context.timers.size, 0);
});

test('ready timeout cleans the frame and invalid action never sends a command', async () => {
    const context = setup();
    await assert.rejects(context.instance.action('send_message'), /unsupported_action/);
    const rejection = assert.rejects(context.instance.ready, /ready_timeout/);
    context.timers.values().next().value();
    await rejection;
    assert.equal(context.frame.removed, true);
    assert.equal(context.sent.length, 0);
});

function popup(context) {
    return { closed: false, opener: context.global, location: { href: 'about:blank' },
        document: { createElement: context.node, head: context.node('head'), body: context.node('body') },
        close() { this.closed = true; } };
}

test('top-level opens during the gesture, cuts opener and posts a fresh ticket into the same new window', async () => {
    const context = setup(); context.instance.destroy();
    const opened = popup(context);
    let fetched = false, openCalls = 0;
    context.global.open = (url, target) => { assert.equal(url, 'about:blank'); assert.equal(target, '_blank'); openCalls++; return opened; };
    const submitted = context.global.AurvekApplications.openWindow(async () => {
        fetched = true;
        assert.equal(opened.opener, null);
        return { bootstrap: { ...context.bootstrap, ticket: 'new-single-use-ticket' }, appId: 'sample', contextId: 'context-1' };
    }, { title: 'Conversation', loadingText: 'Opening conversation' });
    assert.equal(openCalls, 1); assert.equal(fetched, false);
    assert.equal(opened.document.body.children[0].attributes.role, 'status');
    assert.equal((await submitted).status, 'submitted');
    assert.deepEqual(context.submissions.at(-1), { method: 'POST', action: 'https://chat.example/embed/bootstrap', target: '_self',
        fields: [{ name: 'ticket', value: 'new-single-use-ticket' }] });
    assert.equal(opened.document.head.children[0].content, 'strict-origin');
    assert.equal(context.nodes.filter(node => node.tag === 'input').at(-1).value, '');
    assert.equal(context.timers.size, 0);
});

test('blocked popups do not fetch a ticket or stop the existing panel; failed close keeps that panel', async () => {
    const context = setup();
    let requests = 0;
    context.global.open = () => null;
    await assert.rejects(context.global.AurvekApplications.openWindow(() => { requests++; }), /popup_blocked/);
    assert.equal(requests, 0); assert.equal(context.frame.removed, undefined);
    const opened = popup(context);
    context.global.open = () => opened;
    await assert.rejects(context.global.AurvekApplications.openWindow(async () => { throw new Error('activity_close_pending'); }), /activity_close_pending/);
    assert.equal(opened.closed, true);
    assert.equal(context.submissions.length, 1);
    assert.equal(context.frame.removed, undefined);
    context.instance.destroy();
});

test('a user closing the blank window or a bootstrap timeout cannot cause a later navigation', async () => {
    const context = setup(); context.instance.destroy();
    const opened = popup(context);
    context.global.open = () => opened;
    let resolveBootstrap;
    const response = new Promise(resolve => { resolveBootstrap = resolve; });
    const result = context.global.AurvekApplications.openWindow(() => response);
    const rejection = assert.rejects(result, /bootstrap_timeout/);
    context.timers.values().next().value();
    await rejection;
    resolveBootstrap({ bootstrap: context.bootstrap, appId: 'sample', contextId: 'context-1' });
    await settle();
    assert.equal(opened.closed, true);
    assert.equal(context.submissions.length, 1);
    assert.equal(context.timers.size, 0);
});
