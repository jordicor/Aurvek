'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

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
    assert.match(chat, /audio: message\.provenance\?\.audio &&/);
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
    assert.match(history, /detailTitle\.textContent = tr\('direction_call'/);
    assert.match(history, /appendField\(summary, tr\('date'\)/);
    assert.match(history, /appendField\(summary, tr\('duration_label'\), formatDuration/);
    assert.match(history, /appendField\(summary, tr\('cost'\), formatCost/);
    assert.match(
        history,
        /if \(isAdminView\(\)\) \{[\s\S]*renderTimeline\(call\),[\s\S]*renderProvenance\(call\),[\s\S]*renderCosts\(call\)/
    );
    assert.match(chat, /AurvekI18n\.bindText\(phoneLabel, 'chat\.phone'\)/);
    assert.doesNotMatch(chat, /phoneDetailParts|visiblePhoneState/);
});

test('chat shows a discrete telephone indicator and loads the isolated renderer', () => {
    assert.match(chat, /message-phone-provenance/);
    assert.match(chat, /AurvekI18n\.bindText\(phoneLabel, 'chat\.phone'\)/);
    assert.match(chat, /AurvekPhoneHistory\?\.showCall/);
    assert.match(template, /\/static\/css\/chat\/phone-history\.css/);
    assert.match(template, /\/static\/js\/chat\/phone-history\.js/);
});

test('interrupted phone and voice turns have an explicit, participant-aware presentation', () => {
    const helperSource = chat.slice(
        chat.indexOf('function getVoiceInterruptionPresentation('),
        chat.indexOf('function addMessage(')
    );
    const context = {
        window: {
            AurvekI18n: {
                t(key) {
                    return {
                        'chat.interrupted': 'Interrupted',
                        'chat.response_interrupted': 'Response interrupted'
                    }[key];
                }
            }
        },
        document: {
            createElement(tagName) {
                return {
                    attributes: {},
                    className: '',
                    tagName,
                    textContent: '',
                    setAttribute(name, value) {
                        this.attributes[name] = value;
                    }
                };
            }
        }
    };
    vm.createContext(context);
    vm.runInContext(
        `${helperSource}; globalThis.presentation = getVoiceInterruptionPresentation; ` +
        'globalThis.needsEllipsis = shouldAppendInterruptionEllipsis; ' +
        'globalThis.appendEllipsis = appendInterruptionEllipsis;',
        context
    );

    assert.deepEqual(
        {...context.presentation('bot', {
            phone_provenance: {participant: 'assistant', interrupted: true}
        })},
        {appendEllipsis: true, label: 'Interrupted', source: 'phone'}
    );
    assert.deepEqual(
        {...context.presentation('user', {
            phone_provenance: {participant: 'caller', interrupted: true}
        })},
        {appendEllipsis: false, label: 'Response interrupted', source: 'phone'}
    );
    assert.deepEqual(
        {...context.presentation('bot', {
            elevenlabs_voice: {interrupted: true}
        })},
        {appendEllipsis: true, label: 'Interrupted', source: 'voice'}
    );
    assert.equal(context.presentation('bot', {
        phone_provenance: {participant: 'assistant', interrupted: false}
    }), null);
    assert.equal(context.needsEllipsis('Audible prefix.'), true);
    assert.equal(context.needsEllipsis('Already interrupted\u2026'), false);
    assert.equal(context.needsEllipsis('Already interrupted...'), false);

    const appended = [];
    const textContainer = {
        lastElementChild: null,
        appendChild(node) {
            appended.push(node);
        }
    };
    context.appendEllipsis(textContainer, 'Audible prefix.');
    context.appendEllipsis(textContainer, 'Already interrupted\u2026');
    assert.equal(appended.length, 1);
    assert.equal(appended[0].className, 'message-interruption-ellipsis');
    assert.equal(appended[0].textContent, ' \u2026');
    assert.equal(appended[0].attributes['aria-hidden'], 'true');

    const afterCode = [];
    context.appendEllipsis({
        lastElementChild: {tagName: 'PRE'},
        appendChild(node) {
            afterCode.push(node);
        }
    }, 'Code output');
    assert.equal(afterCode.length, 0);

    assert.match(chat, /divMessage\.dataset\.messageInterrupted = 'true'/);
    assert.match(chat, /message-interruption-state/);
    assert.match(chat, /appendInterruptionEllipsis\(divText, messageText\)/);
    assert.match(chat, /message\.elevenlabs_voice/);
    assert.match(provenanceCss, /\.message-interruption-state/);
    assert.match(provenanceCss, /\.message-interruption-ellipsis/);
});

test('chat renders durable WhatsApp and Telegram provenance beside phone provenance', () => {
    assert.match(chat, /messageObj\?\.channel_provenance/);
    assert.match(chat, /channel !== 'whatsapp' && channel !== 'telegram'/);
    assert.match(chat, /getExternalPlatformIcon\(provenance\.channel\)/);
    assert.match(chat, /message-channel-provenance/);
    assert.match(chat, /contentKind === 'voice_note'/);
    assert.match(chat, /contentKind === 'voice_reply'/);
    assert.match(chat, /contentKind === 'voice_reply' \? 'chat\.voice_reply'/);
    assert.match(chat, /'fas fa-microphone'/);
    assert.match(chat, /messageContent\.appendChild\(channelBadge\)/);
    assert.match(provenanceCss, /message-channel-provenance-whatsapp/);
    assert.match(provenanceCss, /message-channel-provenance-telegram/);
});

test('saved voice notes and phone turns prefer original audio and otherwise keep TTS', () => {
    assert.match(chat, /audio\?\.available === true/);
    assert.match(chat, /const retainedPhoneAudio = phoneProvenance\?\.audio &&/);
    assert.match(chat, /phoneProvenance\.audio\.available === true/);
    const selectOriginal = chat.match(/const originalAudio = [\s\S]+?;/)[0];
    const phoneAudio = {url: '/api/phone-messages/42/audio'};
    const selected = vm.runInNewContext(`${selectOriginal} originalAudio`, {
        messageObj: {}, retainedPhoneAudio: phoneAudio, channelProvenance: null,
    });
    assert.equal(selected, phoneAudio);
    assert.equal(vm.runInNewContext(`${selectOriginal} originalAudio`, {
        messageObj: {}, retainedPhoneAudio: null, channelProvenance: null,
    }), null);
    assert.match(
        chat,
        /if \(originalAudio\) \{\s*playOriginalAudio\(originalAudio\.url, audioIcon, originalAudio\.label\);\s*return;\s*\}\s*textToSpeech\(resolveMessageText\(\), user_id/
    );
    assert.match(chat, /textToSpeech\(resolveMessageText\(\), user_id/);
    assert.match(chat, /audioIcon\.dataset\.audioSource = originalAudio \? 'original' : 'tts'/);
    assert.match(chat, /audioIcon\.dataset\.playLabel = audioActionLabel/);
    assert.match(audio, /function playOriginalAudio\(url, audioIcon, label = AurvekI18n\.t\('chat_widgets\.audio\.original'\)\)/);
    assert.match(audio, /resolved\.origin !== window\.location\.origin/);
    assert.match(audio, /\/api\\\/attachments\\\/\[\^\/\]\+\\\/content/);
    assert.match(audio, /\/api\\\/phone-messages\\\/\\d\+\\\/audio/);
    assert.match(audio, /if \(!isAttachment && !isPhoneMessage && !isWebRecording\)/);
    assert.match(audio, /audio\.preload = 'metadata'/);
    assert.match(audio, /audioIcon\.setAttribute\('aria-pressed', active \? 'true' : 'false'\)/);
    assert.match(audio, /actionLabel = isOriginal[\s\S]*tr|chat_widgets\.audio\.stop_original/);
    assert.match(audio, /markAudioPlaybackError/);

    const originalPlayback = audio.match(/function playOriginalAudio\([\s\S]+?\r?\n}\r?\n/)[0];
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
    assert.match(history, /window\.confirm\(tr\(/);
    assert.match(history, /method: 'DELETE'/);
    assert.match(history, /'X-GPTSub-CSRF': csrfToken/);
    assert.match(history, /window\.secureFetch/);
    assert.match(history, /encodeURIComponent\(callId\)/);
    assert.match(history, /window\.refreshActiveConversation/);
    assert.match(history, /recording\.present && TERMINAL_CALL_STATUSES\.has\(String\(call\.status\)\)/);
    assert.match(history, /TERMINAL_CALL_STATUSES\.has\(String\(call\.status\)\)/);
    assert.match(history, /\['scheduled', 'running', 'needs_attention'\]/);
    assert.match(history, /tr\('deletion_pending_review'\)/);
    assert.doesNotMatch(history, /retry.*purge|purge.*retry/i);
    assert.match(history, /heading\.textContent = tr\('saved_audio_and_call'\)/);
    assert.match(history, /tr\('delete_audio'\)/);
    assert.match(history, /tr\('audio_deletion_scheduled'\)/);
    assert.match(history, /scope === 'recording'[\s\S]*delete_audio_confirm/);
    assert.doesNotMatch(history, /payload\?\.(?:message|error)\b/);
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
