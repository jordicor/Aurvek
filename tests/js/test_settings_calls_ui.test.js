'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..', '..');
const template = fs.readFileSync(path.join(root, 'templates/settings.html'), 'utf8');
const settings = fs.readFileSync(path.join(root, 'data/static/js/settings.js'), 'utf8');
const chat = fs.readFileSync(path.join(root, 'data/static/js/chat/chat.js'), 'utf8');
const pageContext = fs.readFileSync(path.join(root, 'chat/services/page_context.py'), 'utf8');
const app = fs.readFileSync(path.join(root, 'app.py'), 'utf8');
const callsSource = settings.slice(
    settings.indexOf('// --- Calls Tab ---'),
    settings.indexOf('// --- Usage Tab')
);

test('Settings exposes a lazy Calls tab with a simple history list', () => {
    assert.match(template, /id="calls-tab"[\s\S]*data-bs-target="#calls"/);
    assert.match(template, /id="calls"[\s\S]*aria-labelledby="calls-tab"/);
    assert.match(template, /id="settings-calls-status"/);
    assert.match(template, /id="settings-calls-list"/);
    assert.match(settings, /'#calls': 'calls-tab'/);
    assert.match(settings, /calls: false/);
    assert.match(settings, /case 'calls':[\s\S]*loadCallsTab\(\)/);
});

test('Calls history uses the account-wide simplified endpoint and text-safe DOM rendering', () => {
    assert.ok(callsSource.includes('/api/telephony/calls?limit=100'));
    assert.match(callsSource, /payload\.calls/);
    assert.match(callsSource, /payload\?\.jobs/);
    assert.match(callsSource, /\.textContent\s*=/);
    assert.match(callsSource, /replaceChildren\(\)/);
    assert.doesNotMatch(callsSource, /\.innerHTML\s*=/);
    assert.doesNotMatch(callsSource, /provider_call_sid|provider_stream_sid|provider_session_id|webhook|provenance/i);
});

test('Calls history shows friendly call facts and opens the owning conversation', () => {
    assert.match(callsSource, /conversation_title \|\| call\.conversation_name/);
    assert.match(callsSource, /phoneCallDirection\(call\.direction\)/);
    assert.match(callsSource, /phoneCallStatus\(call\.status\)/);
    assert.match(callsSource, /phoneCallDuration\(call\.duration_seconds\)/);
    assert.match(callsSource, /phoneCallCost\(call\)/);
    assert.match(callsSource, /\/chat\?conversation_id=\$\{encodeURIComponent\(conversationId\)\}/);
    assert.match(pageContext, /_requested_conversation_id\(request\)/);
    assert.match(
        pageContext,
        /OR c\.id = \?\)[\s\S]{0,120}ORDER BY CASE WHEN c\.id = \? THEN 0 ELSE 1 END/
    );
    assert.match(pageContext, /c\.id = \? AND c\.user_id = \?[\s\S]*hidden_from_history/);
    assert.match(
        pageContext,
        /not any\([\s\S]{0,180}for row in all_init_conversations[\s\S]{0,280}all_init_conversations\.append\(requested_row\)/
    );
    assert.match(pageContext, /"is_incognito": bool\(row\[18\]\)/);
    assert.doesNotMatch(chat, /`Conversation #\$\{requestedConversationId\}`/);
});

test('future scheduled calls appear above history and can be canceled safely', () => {
    assert.match(template, /meta name="aurvek-csrf-token" content="\{\{ telephony_csrf_token \}\}"/);
    assert.match(app, /"telephony_csrf_token": ensure_csrf_token\(request\)/);
    assert.match(callsSource, /job\.status \|\| ''\)\.toLowerCase\(\) === 'scheduled'/);
    assert.match(callsSource, /!job.call_id/);
    assert.match(callsSource, /Scheduled for/);
    assert.ok(callsSource.includes('/api/phone-call-jobs/'));
    assert.match(callsSource, /method: 'POST'/);
    assert.match(callsSource, /'X-GPTSub-CSRF': csrfToken/);
    assert.match(callsSource, /cancel\.textContent = 'Cancel'/);
});

test('Calls history aborts and rejects superseded list responses', () => {
    assert.match(callsSource, /let callsLoadController = null/);
    assert.match(callsSource, /let callsLoadGeneration = 0/);
    assert.match(callsSource, /callsLoadController\.abort\(\)/);
    assert.match(callsSource, /signal/);
    assert.match(callsSource, /generation !== callsLoadGeneration/);
});
