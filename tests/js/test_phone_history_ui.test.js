'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..', '..');
const chat = fs.readFileSync(path.join(root, 'data/static/js/chat/chat.js'), 'utf8');
const audio = fs.readFileSync(path.join(root, 'data/static/js/chat/audio.js'), 'utf8');
const provenanceCss = fs.readFileSync(path.join(root, 'data/static/css/chat/phone-history.css'), 'utf8');
const history = fs.readFileSync(path.join(root, 'data/static/js/chat/phone-history.js'), 'utf8');
const messagesRoute = fs.readFileSync(path.join(root, 'chat/routes/messages.py'), 'utf8');
const template = fs.readFileSync(path.join(root, 'templates/chat/chat.html'), 'utf8');

test('normal message contract is enriched only when a telephone link exists', () => {
    assert.match(messagesRoute, /phone_metadata = phone_history\.message_metadata\.get/);
    assert.match(messagesRoute, /if phone_metadata is not None:\s*msg_data\.update\(phone_metadata\)/);
    assert.match(messagesRoute, /if has_phone_history:\s*response_content\["phone_history"\]/);
    assert.match(chat, /message\.phone_call_id \? \{/);
    assert.match(chat, /phone_call_id: String\(message\.phone_call_id\)/);
    assert.match(chat, /delivery_state: message\.delivery_state/);
    assert.match(chat, /played_ms: message\.played_ms \?\? null/);
    assert.match(chat, /turn_id: message\.provenance\?\.turn_id \|\| null/);
});

test('telephone markers are idempotent across refresh and paginated prepends', () => {
    const loadMessagesSource = chat.slice(
        chat.indexOf('async function loadMessages('),
        chat.indexOf('function refreshActiveConversation()')
    );
    const modelCommitSource = chat.slice(
        chat.indexOf('applyCommittedModel('),
        chat.indexOf('invalidateCachedModel(')
    );
    assert.match(loadMessagesSource, /if \(!prepend\) window\.AurvekPhoneHistory\.reset\(\)/);
    assert.match(loadMessagesSource, /renderPage\(data\.phone_history \|\| \{\}, tempDiv\)/);
    assert.doesNotMatch(modelCommitSource, /AurvekPhoneHistory/);
    assert.match(history, /const renderedMarkerIds = new Set\(\)/);
    assert.match(history, /markerAlreadyExists\(markerId\)/);
    assert.match(history, /renderedMarkerIds\.add\(markerId\)/);
    assert.match(history, /anchor_message_id/);
    assert.match(history, /marker\.placement === 'before'/);
    assert.match(history, /const afterCursor = new Map\(\)/);
});

test('phone provenance and call detail rendering are text-safe', () => {
    assert.doesNotMatch(history, /\.innerHTML\s*=/);
    assert.match(history, /detailBody\.replaceChildren\(\)/);
    assert.match(history, /textContent =/);
    assert.match(history, /url\.origin !== window\.location\.origin/);
    assert.match(history, /url\.pathname\.startsWith\('\/api\/phone-calls\/'\)/);
    assert.doesNotMatch(history, /provider_call_sid|provider_session_id|provider_stream_sid/);
    assert.doesNotMatch(history, /participant_path|assistant_path|mixed_path/);
    assert.match(history, /function renderProvenance\(call\)/);
    assert.match(history, /summary\.interrupted_messages/);
    assert.match(history, /summary\.played_ms/);
    assert.match(history, /summary\.delivery_states/);
    assert.match(history, /summary\.turn_ids/);
    assert.match(history, /function isAdminView\(\)/);
    assert.match(history, /window\.isAdmin === true \|\| window\.admin_view === true/);
    assert.match(history, /detailTitle\.textContent = direction === 'Phone' \? 'Phone call'/);
    assert.match(history, /appendField\(summary, 'Date'/);
    assert.match(history, /appendField\(summary, 'Duration', formatDuration/);
    assert.match(history, /appendField\(summary, 'Cost', formatCost/);
    assert.match(
        history,
        /if \(isAdminView\(\)\) \{[\s\S]*renderTimeline\(call\),[\s\S]*renderProvenance\(call\),[\s\S]*renderCosts\(call\)/
    );
    assert.match(chat, /const phoneDescription = 'Spoken during a phone call'/);
    assert.doesNotMatch(chat, /phoneDetailParts|visiblePhoneState/);
});

test('chat shows a discrete telephone indicator and loads the isolated renderer', () => {
    assert.match(chat, /message-phone-provenance/);
    assert.match(chat, /phoneLabel\.textContent = 'Phone'/);
    assert.match(chat, /AurvekPhoneHistory\?\.showCall/);
    assert.match(template, /\/static\/css\/chat\/phone-history\.css/);
    assert.match(template, /\/static\/js\/chat\/phone-history\.js/);
});

test('chat renders durable WhatsApp and Telegram provenance beside phone provenance', () => {
    assert.match(chat, /messageObj\?\.channel_provenance/);
    assert.match(chat, /channel !== 'whatsapp' && channel !== 'telegram'/);
    assert.match(chat, /getExternalPlatformIcon\(provenance\.channel\)/);
    assert.match(chat, /message-channel-provenance/);
    assert.match(chat, /contentKind === 'voice_note'/);
    assert.match(chat, /contentKind === 'voice_reply'/);
    assert.match(chat, /'Voice reply'/);
    assert.match(chat, /'fas fa-microphone'/);
    assert.match(chat, /messageContent\.appendChild\(channelBadge\)/);
    assert.match(provenanceCss, /message-channel-provenance-whatsapp/);
    assert.match(provenanceCss, /message-channel-provenance-telegram/);
});

test('saved voice notes play their original same-origin audio and otherwise keep TTS', () => {
    assert.match(chat, /audio\?\.available === true/);
    assert.match(chat, /playOriginalAudio\(originalAudio\.url, audioIcon\)/);
    assert.match(chat, /textToSpeech\(resolveMessageText\(\), user_id/);
    assert.match(chat, /audioIcon\.dataset\.audioSource = originalAudio \? 'original' : 'tts'/);
    assert.match(chat, /audioIcon\.dataset\.playLabel = audioActionLabel/);
    assert.match(audio, /function playOriginalAudio\(url, audioIcon\)/);
    assert.match(audio, /resolved\.origin !== window\.location\.origin/);
    assert.match(audio, /\/api\\\/attachments\\\/\[\^\/\]\+\\\/content/);
    assert.match(audio, /audio\.preload = 'metadata'/);
    assert.match(audio, /audioIcon\.setAttribute\('aria-pressed', active \? 'true' : 'false'\)/);
    assert.match(audio, /Stop original voice note/);
    assert.match(audio, /markAudioPlaybackError/);

    const originalPlayback = audio.slice(
        audio.indexOf('function playOriginalAudio('),
        audio.indexOf('function textToSpeech(')
    );
    assert.doesNotMatch(originalPlayback, /fetch\(/);
});

test('active telephone calls never render a false end state', () => {
    assert.match(history, /const TERMINAL_CALL_STATUSES = new Set/);
    assert.match(history, /callState\.textContent = statusLabel\(call\.status\)/);
    assert.match(history, /TERMINAL_CALL_STATUSES\.has\(String\(call\.status\)\) && call\.ended_at/);
});

test('history deletion actions are explicit, fenced and CSRF protected', () => {
    assert.match(history, /const pendingDeletes = new Set\(\)/);
    assert.match(history, /pendingDeletes\.has\(requestKey\)/);
    assert.match(history, /window\.confirm\(`/);
    assert.match(history, /method: 'DELETE'/);
    assert.match(history, /'X-GPTSub-CSRF': csrfToken/);
    assert.match(history, /window\.secureFetch/);
    assert.match(history, /encodeURIComponent\(callId\)/);
    assert.match(history, /window\.refreshActiveConversation/);
    assert.match(history, /recording\.present && TERMINAL_CALL_STATUSES\.has\(String\(call\.status\)\)/);
    assert.match(history, /TERMINAL_CALL_STATUSES\.has\(String\(call\.status\)\)/);
    assert.match(history, /\['scheduled', 'running', 'needs_attention'\]/);
    assert.match(history, /Deletion is pending review\./);
    assert.doesNotMatch(history, /retry.*purge|purge.*retry/i);
    assert.match(history, /heading\.textContent = 'Saved audio and call'/);
    assert.match(history, /'Delete audio'/);
    assert.match(history, /'Audio deletion scheduled\.'/);
    assert.match(history, /scope === 'recording' \? 'saved audio'/);
    assert.match(history, /detail && isAdminView\(\)/);
});

test('delete refresh remains fenced to its originating conversation generation', () => {
    assert.match(history, /let conversationGeneration = 0/);
    assert.match(history, /const originConversationId = activeConversationId\(\)/);
    assert.match(history, /const originGeneration = conversationGeneration/);
    assert.match(history, /isOriginConversationCurrent\(originConversationId, originGeneration\)/);
    assert.match(history, /activeConversationId\(\) === originConversationId/);
    assert.match(history, /'aurvek:conversation-changed'/);
    assert.match(history, /conversationGeneration \+= 1/);
});
