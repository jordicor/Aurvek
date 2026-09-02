'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..', '..');
const modulePath = path.join(
    root,
    'data/static/js/chat/voice-note-retranscription.js'
);
const source = fs.readFileSync(modulePath, 'utf8');
const chat = fs.readFileSync(path.join(root, 'data/static/js/chat/chat.js'), 'utf8');
const css = fs.readFileSync(
    path.join(root, 'data/static/css/chat/voice-note-retranscription.css'),
    'utf8'
);
const template = fs.readFileSync(path.join(root, 'templates/chat/chat.html'), 'utf8');
const helpers = require(modulePath);

test('voice-note action is limited to retained inbound voice notes', () => {
    assert.match(chat, /messageObj\?\.type === 'text'/);
    assert.match(chat, /channelProvenance\?\.direction === 'inbound'/);
    assert.match(chat, /channelProvenance\?\.contentKind === 'voice_note'/);
    assert.match(chat, /channelProvenance\?\.originalAudio/);
    assert.match(chat, /'fa-sync-alt'/);
    assert.match(chat, /AurvekVoiceNoteRetranscription\?\.open\(messageId\)/);
    assert.match(chat, /iconContainer\.appendChild\(retranscriptionIcon\)/);
});

test('dialog uses owner APIs, secureFetch, and explicit CSRF protection', () => {
    assert.match(source, /global\.secureFetch/);
    assert.match(source, /headers\['X-GPTSub-CSRF'\] = csrfToken\(\)/);
    assert.match(source, /may use your Aurvek balance or provider API credits/);
    assert.match(source, /it does not listen to the audio/);
    assert.match(source, /External AI memory, if configured, is not rebuilt/);
    assert.match(source, /\/api\/messaging-voice-notes\/comparison-models/);
    assert.match(source, /\/api\/messaging-voice-notes\/\$\{encodeURIComponent\(messageId\)\}\/retranscribe/);
    assert.match(source, /\/api\/messaging-voice-notes\/revisions\/\$\{encodeURIComponent\(revisionId\)\}/);
    assert.match(source, /\/decision/);
    assert.doesNotMatch(source, /\/api\/admin\//);
    assert.match(template, /voice-note-retranscription\.js/);
    assert.match(template, /voice-note-retranscription\.css/);
});

test('server-provided model and transcript data is rendered as text only', () => {
    assert.match(source, /\.textContent\s*=/);
    assert.match(source, /createElement\('details'/);
    assert.match(source, /transcript\.tabIndex = 0/);
    assert.match(source, /aria-describedby/);
    assert.match(source, /isError \? 'alert' : 'status'/);
    assert.doesNotMatch(source, /\.innerHTML\s*=/);
    assert.doesNotMatch(source, /insertAdjacentHTML|document\.write/);
    assert.match(css, /voice-note-transcript-details/);
    assert.match(css, /white-space:\s*pre-wrap/);
});

test('comparison model payloads are normalized without trusting labels', () => {
    assert.deepEqual(
        helpers.normalizeComparisonModels({
            models: [
                {id: 7, display_name: 'Judge A', machine: 'GPT', model: 'gpt-a'},
                {id: '8', machine: 'Claude', model: 'claude-b'},
                {id: 0, display_name: 'invalid'},
            ],
        }),
        [
            {id: 7, label: 'Judge A · GPT · gpt-a'},
            {id: 8, label: 'claude-b'},
        ]
    );
    const priced = helpers.normalizeComparisonModels({
        models: [{
            id: 9,
            machine: 'GPT',
            model: 'priced-model',
            input_token_cost: 0.125,
            output_token_cost: 1.5,
        }],
    });
    assert.match(priced[0].label, /\$0\.125\/M input/);
    assert.match(priced[0].label, /\$1\.5\/M output/);
});

test('active revision identifiers are recovered from success and conflict shapes', () => {
    assert.equal(helpers.revisionIdFrom({revision_id: 11}), 11);
    assert.equal(helpers.revisionIdFrom({revision: {id: 12}}), 12);
    assert.equal(helpers.revisionIdFrom({detail: {active_revision_id: 13}}), 13);
    assert.equal(helpers.revisionIdFrom({detail: 'already active'}), null);
});

test('polling and review cover active, ready, failure, and optimistic-stale states', () => {
    assert.match(source, /new Set\(\['queued', 'transcribing', 'comparing'\]\)/);
    assert.match(source, /if \(ACTIVE_STATUSES\.has\(status\)\)/);
    assert.match(source, /status === 'ready'/);
    assert.match(source, /status === 'failed'/);
    assert.match(source, /\['rejected', 'stale'\]\.includes\(status\)/);
    assert.match(source, /error\.status === 409/);
    assert.match(source, /refreshActiveConversation/);
    assert.match(source, /const generation = state\.generation/);
    assert.match(source, /state\.currentMessageId !== messageId/);
    assert.match(source, /state\.activeByMessage\.set\(messageId, revisionId\)/);
    assert.match(source, /latest_revision_status/);
    assert.match(source, /resumeLatestRevision/);
    assert.match(source, /Checking saved retranscription state/);
    assert.match(source, /voiceNote\?\.audio_available/);
    assert.match(source, /state\.nodes\.start\.disabled = false/);
    assert.match(source, /Could not check saved retranscription state\. Retrying/);
});
