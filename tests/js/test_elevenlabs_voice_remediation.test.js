const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const voicePath = path.join(repoRoot, 'data/static/js/chat/voice-call.js');
const voiceSource = fs.readFileSync(voicePath, 'utf8');

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
});
