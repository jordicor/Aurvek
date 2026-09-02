'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const repoRoot = path.resolve(__dirname, '..', '..');
const sourcePath = path.join(repoRoot, 'data/static/js/chat/phone-call.js');
const cssPath = path.join(repoRoot, 'data/static/css/chat/phone-call.css');
const templatePath = path.join(repoRoot, 'templates/chat/chat.html');
const chatPath = path.join(repoRoot, 'data/static/js/chat/chat.js');
const foldersPath = path.join(repoRoot, 'data/static/js/chat/folders.js');
const source = fs.readFileSync(sourcePath, 'utf8');
const css = fs.readFileSync(cssPath, 'utf8');
const template = fs.readFileSync(templatePath, 'utf8');
const chat = fs.readFileSync(chatPath, 'utf8').replace(/\r\n/g, '\n');
const folders = fs.readFileSync(foldersPath, 'utf8');

test('native telephone controls are separate from browser voice controls', () => {
    assert.match(template, /id="plus-phone-call"/);
    assert.match(template, /id="plus-voice-call"/);
    assert.match(template, /id="phoneCallModal"/);
    assert.match(template, /phone-call\.js/);
    assert.match(template, /phone-call\.css/);
    assert.doesNotMatch(source, /elevenlabs|microphone|getUserMedia/i);
});

test('every phone UI element referenced by the module exists in the chat template', () => {
    const referenced = new Set(
        [...source.matchAll(/byId\('([^']+)'\)/g)].map(match => match[1])
    );
    const intentionallyShared = new Set([
        'incognito-chat-badge',
        'locked-conversation-banner'
    ]);
    for (const id of referenced) {
        if (intentionallyShared.has(id)) continue;
        assert.match(template, new RegExp(`id="${id}"`), `missing #${id}`);
    }
});

test('telephone UI uses authenticated mutation transport and renders server data as text', () => {
    assert.match(source, /window\.secureFetch/);
    assert.match(source, /credentials/);
    assert.match(source, /headers\['X-GPTSub-CSRF'\] = csrfToken/);
    assert.match(template, /meta name="aurvek-csrf-token"/);
    assert.match(source, /\.textContent\s*=/);
    assert.doesNotMatch(source, /\.innerHTML\s*=/);
    assert.doesNotMatch(source, /insertAdjacentHTML|document\.write/);
    assert.match(source, /\[400, 403, 404, 409, 422, 503\]/);
    assert.match(source, /response\.status >= 500/);
});

test('Call me UI uses the account phone and minimal one-shot call contracts', () => {
    const requiredContracts = [
        '/api/telephony/me',
        '/phone-bindings',
        '/phone-bindings/self',
        '/phone-calls',
        '/phone-call-jobs/',
        '/cancel',
        '/hangup'
    ];
    requiredContracts.forEach(contract => assert.ok(source.includes(contract), contract));
    assert.match(template, />Call me</);
    assert.match(template, /id="phone-call-target"/);
    assert.match(template, /href="\/settings#profile"/);
    assert.doesNotMatch(template, /phone-contact-e164|phone-preferred-line|phone-allow-inbound|phone-allow-outbound/);
    assert.doesNotMatch(source, /\/api\/telephony\/contacts|\/api\/telephony\/numbers/);
});

test('disabled outgoing calls are distinguished from a missing phone assignment', () => {
    assert.match(source, /Calls from Aurvek to your phone are disabled for this conversation\./);
    assert.match(source, /Choose a conversation for phone calls before starting a call\./);
    assert.doesNotMatch(source, /not linked/i);
});

test('scheduling uses browser-local time without exposing engineering overrides', () => {
    assert.match(template, /id="phone-schedule-at"/);
    assert.doesNotMatch(template, /phone-schedule-timezone|phone-schedule-fold/);
    assert.doesNotMatch(template, /phone-recording-override|phone-amd-override/);
    assert.match(source, /scheduled_at: byId\('phone-schedule-at'\)\.value/);
    assert.match(source, /timezone_name: browserTimeZone\(\)/);
    assert.match(source, /fold: 0/);
    assert.match(source, /idempotency_key: newIdempotencyKey\('schedule'\)/);
    assert.doesNotMatch(source, /recording_override|amd_override|\/reschedule/);
});

test('one click has one durable idempotency key and call-now is disabled in flight', () => {
    assert.match(source, /button\.disabled = true;[\s\S]*idempotency_key: newIdempotencyKey\('now'\)/);
    assert.doesNotMatch(source, /setInterval\(/);
});

test('opening Call me is non-mutating while explicit assignment and call actions bind the conversation', () => {
    const openSource = source.slice(
        source.indexOf('async function openModal()'),
        source.indexOf('async function openForAssignment()')
    );
    assert.doesNotMatch(openSource, /ensureSelfBinding|phone-bindings\/self/);
    assert.match(source, /async function openForAssignment\(\)[\s\S]*await assignConversation\(\)/);
    assert.match(source, /async function assignConversation\(\)[\s\S]*await ensureSelfBinding\(\)/);
    assert.match(source, /async function callNow\(event\)[\s\S]*await ensureSelfBinding\(\)/);
    assert.match(source, /async function scheduleCall\(event\)[\s\S]*await ensureSelfBinding\(\)/);
    assert.match(source, /'aurvek:phone-binding-changed'/);
    assert.match(source, /openForAssignment,/);
});

test('conversation assignment remains visible and switches between clear assign and stop actions', () => {
    assert.match(template, /id="phone-conversation-assignment-control"/);
    assert.match(template, /id="phone-conversation-assignment"/);
    assert.match(template, />Use this conversation for phone calls</);
    assert.match(source, /assignmentControl\.hidden = !\(assigned \|\| \(eligible && !flags\.locked\)\)/);
    assert.match(source, /assignmentLabel\.textContent = assigned[\s\S]*Stop using this conversation for phone calls[\s\S]*Use this conversation for phone calls/);
    assert.match(source, /is-assigned/);
    assert.match(source, /is-unassigned/);
    assert.match(source, /async function toggleConversationAssignment\(\)[\s\S]*unassignConversation\(\)[\s\S]*assignConversation\(\)/);
    assert.match(css, /\.phone-call-assignment-action\.is-assigned[\s\S]*--bs-danger/);
    assert.match(css, /\.phone-call-assignment-action\.is-unassigned[\s\S]*--bs-primary/);
    assert.doesNotMatch(template, /phone-unassign-contact/);
});

test('assignment busy state belongs only to assign and unassign mutations', () => {
    const assignSource = source.slice(
        source.indexOf('async function assignConversation()'),
        source.indexOf('async function callNow(event)')
    );
    const callNowSource = source.slice(
        source.indexOf('async function callNow(event)'),
        source.indexOf('async function scheduleCall(event)')
    );
    const unassignSource = source.slice(
        source.indexOf('async function unassignConversation()'),
        source.indexOf('async function toggleConversationAssignment()')
    );
    for (const mutationSource of [assignSource, unassignSource]) {
        assert.match(mutationSource, /state\.assignmentPending = true/);
        assert.match(mutationSource, /finally\s*\{[\s\S]*state\.assignmentPending = false/);
    }
    assert.doesNotMatch(callNowSource, /assignmentPending/);
});

test('call history lives with the saved phone and scheduling aligns the button with the input', () => {
    const targetCard = template.slice(
        template.indexOf('<section class="phone-call-target-card"'),
        template.indexOf('</section>', template.indexOf('<section class="phone-call-target-card"'))
    );
    assert.match(targetCard, /Your phone/);
    assert.match(targetCard, /href="\/settings#calls"[\s\S]*View call history/);
    assert.match(template, /phone-schedule-row[\s\S]*phone-schedule-at[\s\S]*phone-schedule-submit[\s\S]*<\/div>[\s\S]*Uses your current time zone/);
    assert.match(css, /\.phone-schedule-row\s*\{[\s\S]*align-items:\s*stretch/);
    assert.doesNotMatch(css, /\.phone-schedule-form\s*\{[^}]*align-items:\s*end/);
});

test('immediate scheduled jobs are treated as preparing while future jobs get one cancel card', () => {
    assert.match(source, /dueAt <= Date\.now\(\) \+ 30000/);
    assert.match(source, /dueAt > Date\.now\(\) \+ 30000/);
    assert.match(template, /id="phone-scheduled-card"/);
    assert.match(template, /id="phone-cancel-scheduled"/);
    assert.match(source, /scheduleForm\.hidden = flags\.locked \|\| Boolean\(scheduled\)/);
    assert.match(source, /state\.modalOpen && conversationId === state\.conversationId[\s\S]{0,120}renderCallState\(\)/);
    assert.match(source, /callNow\.disabled = flags\.locked \|\| !eligible \|\| Boolean\(call \|\| pendingJob\)/);
});

test('the owner-wide future job is visible and cancelable from every conversation', () => {
    assert.match(source, /requestJson\('\/api\/telephony\/calls\?limit=100'/);
    assert.match(source, /state\.jobs = Array\.isArray\(globalPayload\.jobs\) \? globalPayload\.jobs : \[\]/);
    assert.match(source, /state\.jobs = Array\.isArray\(globalPayload\.jobs\)[\s\S]{0,100}renderTarget\(\)/);
    assert.match(source, /jobConversationTitle\(scheduled\)/);
    assert.match(source, /jobConversationTitle\(pendingJob\)/);
    assert.match(source, /Calling now from this conversation will cancel the scheduled call for/);
    assert.match(source, /The scheduled call for \$\{displacedTitle\} was canceled\./);
    assert.match(source, /async function cancelScheduled\(\)[\s\S]*phone-call-jobs/);
});

test('polling is bounded and stale or superseded history work is aborted', () => {
    assert.match(source, /function schedulePoll\(\)/);
    assert.match(source, /if \(!state\.modalOpen\) return/);
    assert.match(source, /if \(activeCall\(\) \|\| preparingJob\(\)\)/);
    assert.match(source, /state\.controller\.abort\(\)/);
    assert.match(source, /state\.historyController\.abort\(\)/);
    assert.match(source, /state\.historyRequestId \+= 1/);
    assert.match(source, /historyRequest\.id !== state\.historyRequestId/);
    assert.match(source, /Number\(conversationId\) === Number\(currentConversationId\(\)\)/);
    assert.match(source, /hidden\.bs\.modal/);
    assert.match(source, /shown\.bs\.modal', syncLauncher/);
    assert.match(
        source,
        /if \(invalidModalContext[\s\S]{0,180}invalidateModalSession\(\);[\s\S]{0,80}state\.modal\?\.hide\(\)/
    );
    assert.match(chat, /aurvek:conversation-changed/);
    assert.match(chat, /notifyConversationChannelControls\(\)/);
    assert.match(
        chat,
        /isCurrentConversationLocked = true;[\s\S]{0,240}lockedBanner\.style\.display = 'flex';[\s\S]{0,80}notifyConversationChannelControls\(\)/
    );
});

test('incognito is unavailable while locked conversations retain only reductive controls', () => {
    assert.match(source, /button\.hidden = Boolean\(window\.admin_view\) \|\| flags\.incognito/);
    assert.match(source, /button\.disabled = !conversationId/);
    assert.match(source, /const invalidModalContext = flags\.incognito \|\| conversationId !== state\.conversationId/);
    assert.match(source, /callNow\.hidden = flags\.locked/);
    assert.match(source, /scheduleForm\.hidden = flags\.locked \|\| Boolean\(scheduled\)/);
    assert.match(source, /This conversation is locked\. You can only end or remove existing phone activity\./);
    assert.match(source, /Phone calls are unavailable in incognito conversations\./);
    assert.match(source, /async function openForAssignment\(\)[\s\S]*currentConversationFlags\(\)\.locked/);
});

test('normal phone errors do not expose implementation terminology', () => {
    assert.match(source, /Your saved phone number cannot be used for this call\. Check it in Settings\./);
    assert.match(source, /That time cannot be scheduled\. Choose a different time and try again\./);
    assert.doesNotMatch(source, /if \(detail\) return detail\.slice/);
});

test('conversation menus expose phone assignment and management in normal and folder rows', () => {
    assert.match(chat, /'Use for phone calls'/);
    assert.match(chat, /'Manage phone calls'/);
    assert.match(chat, /AurvekPhoneCall\.openForAssignment\(\)/);
    assert.match(chat, /AurvekPhoneCall\.open\(\)/);
    assert.match(chat, /createPhoneCallsMenuLink\(conversation\)/);
    assert.match(folders, /createPhoneCallsMenuLink\(conversation\)/);
    assert.match(chat, /!isConversationIncognitoData\(conversation\)/);
    assert.match(folders, /!isConversationIncognitoData\(conversation\)/);
    assert.match(chat, /!conversation\.locked \|\| conversationUsesPlatform\(conversation, 'phone'\)/);
    assert.match(folders, /!conversation\.locked \|\| conversationUsesPlatform\(conversation, 'phone'\)/);
});

test('conversation rows support combined WhatsApp Telegram and phone badges', () => {
    assert.match(chat, /EXTERNAL_CHANNEL_ORDER = \['whatsapp', 'telegram', 'phone'\]/);
    assert.match(chat, /conversation-channel-badges/);
    assert.match(chat, /conversationHasExternalChannel\(conversation\)/);
    assert.match(chat, /external_channels/);
    assert.match(chat, /phone_binding/);
    assert.match(folders, /renderConversationName\(chatNameElement, conversation, chatName\)/);
});

test('renaming re-renders complete channel and lock decorations', () => {
    assert.match(
        chat,
        /function updateActiveChatName\(newName\)[\s\S]*renderConversationName\(chatNameSpan, conversation, newName\)/
    );
    assert.match(
        chat,
        /conversationElement\._conversationData = conversation;[\s\S]{0,120}renderConversationName\(nameSpan, conversation, newName\)/
    );
});

test('channel mutations sequence each messaging platform and merge only that channel', () => {
    assert.match(chat, /messagingChannelMutationGenerations = \{[\s\S]*whatsapp: 0,[\s\S]*telegram: 0/);
    assert.match(chat, /mutationGeneration !== messagingChannelMutationGenerations\[platform\]/);
    assert.match(chat, /mergeVisibleConversationChannel\(conversation, platform\)/);
    assert.match(chat, /responseChannels\.has\(mutatedPlatform\)/);
    assert.match(chat, /visibleChannels\.add\(mutatedPlatform\)/);
    assert.match(chat, /visibleChannels\.delete\(mutatedPlatform\)/);
    assert.match(chat, /phone_binding: visible\.phone_binding \|\| null/);
});

test('messaging response merge preserves the other messaging channel and phone binding', () => {
    const functionStart = chat.indexOf('function mergeVisibleConversationChannel(');
    const functionEnd = chat.indexOf('\n}\n\nconst toggleExternalPlatform', functionStart) + 2;
    const functionSource = chat.slice(functionStart, functionEnd);
    let visibleConversation = null;
    const merge = new Function(
        'visibleConversationCardData',
        'getConversationExternalChannels',
        'EXTERNAL_CHANNEL_ORDER',
        `${functionSource}; return mergeVisibleConversationChannel;`
    )(
        () => visibleConversation,
        conversation => conversation.external_channels || [],
        ['whatsapp', 'telegram', 'phone']
    );

    visibleConversation = {
        id: 42,
        external_channels: ['telegram', 'phone'],
        phone_binding: {id: 7, display_name: 'My phone'}
    };
    const whatsappAdded = merge(
        {id: 42, external_channels: ['whatsapp']},
        'whatsapp'
    );
    assert.deepEqual(
        whatsappAdded.external_channels,
        ['whatsapp', 'telegram', 'phone']
    );
    assert.deepEqual(whatsappAdded.phone_binding, visibleConversation.phone_binding);

    visibleConversation = whatsappAdded;
    const telegramRemoved = merge(
        {id: 42, external_channels: ['whatsapp']},
        'telegram'
    );
    assert.deepEqual(telegramRemoved.external_channels, ['whatsapp', 'phone']);
    assert.deepEqual(telegramRemoved.phone_binding, visibleConversation.phone_binding);
});
