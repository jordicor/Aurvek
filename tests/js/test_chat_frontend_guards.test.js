const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const chatPath = path.join(repoRoot, 'data/static/js/chat/chat.js');
const mainPath = path.join(repoRoot, 'data/static/js/chat/main.js');
const fileHandlingPath = path.join(repoRoot, 'data/static/js/chat/fileHandling.js');
const foldersPath = path.join(repoRoot, 'data/static/js/chat/folders.js');
const chatTemplatePath = path.join(repoRoot, 'templates/chat/chat.html');

function extract(source, startMarker, endMarker) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start);
    assert.notEqual(start, -1, `Missing marker: ${startMarker}`);
    assert.notEqual(end, -1, `Missing marker: ${endMarker}`);
    return source.slice(start, end);
}

function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach(name => values.add(name)); },
        contains(name) { return values.has(name); },
    };
}

class FakeElement {
    constructor(tagName) {
        this.tagName = tagName;
        this.children = [];
        this.classList = classList();
        this.className = '';
        this.style = {};
        this.dataset = {};
        this.textContent = '';
        this.title = '';
        this.firstChild = null;
        this.innerHTMLWrites = [];
    }

    set innerHTML(value) {
        this.innerHTMLWrites.push(value);
        this.children = [];
        this.firstChild = null;
    }

    get innerHTML() { return ''; }

    appendChild(child) {
        this.children.push(child);
        this.firstChild = this.children[0] || null;
        return child;
    }

    insertBefore(child) {
        this.children.unshift(child);
        this.firstChild = this.children[0] || null;
        return child;
    }

    querySelector(selector) {
        if (selector === '.prompt-info') {
            return this.children.find(child => child.classList.contains('prompt-info')) || null;
        }
        return null;
    }

    remove() {}
}

function findByClass(root, name) {
    if (root.classList?.contains(name) || root.className.split(/\s+/).includes(name)) {
        return root;
    }
    for (const child of root.children || []) {
        const match = findByClass(child, name);
        if (match) return match;
    }
    return null;
}

test('prompt extension names are rendered as text instead of HTML', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const showPromptInfoSource = extract(
        source,
        'function showPromptInfo()',
        '// Model Selector functionality'
    );
    const chatMessagesContainer = new FakeElement('div');
    const maliciousName = '<img src=x onerror="globalThis.pwned=true">';
    const document = {
        createElement(tagName) { return new FakeElement(tagName); },
        getElementById(id) {
            return id === 'chat-messages-container' ? chatMessagesContainer : null;
        },
    };
    const context = {
        document,
        window: {
            extensionSelector: {
                extensions: [{ id: 1, name: maliciousName }],
                currentExtensionId: 1,
            },
        },
        botname: 'Assistant',
        promptDescription: 'Safe description',
        botProfilePicture: '',
        botProfilePicture128: '',
        botProfilePictureFullsize: '',
        imageHandler: { showFullsize() {} },
        String,
    };
    vm.createContext(context);
    vm.runInContext(showPromptInfoSource, context);
    vm.runInContext('showPromptInfo()', context);

    const pill = findByClass(chatMessagesContainer, 'extension-pill');
    assert.ok(pill);
    assert.equal(pill.textContent, maliciousName);
    assert.equal(context.pwned, undefined);
    assert.deepEqual(pill.innerHTMLWrites, []);
});

test('bot avatars keep their exact signed URLs for prompt info and voice calls', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const showPromptInfoSource = extract(
        source,
        'function showPromptInfo()',
        '// Model Selector functionality'
    );
    const chatMessagesContainer = new FakeElement('div');
    const openedUrls = [];
    const signed32 = '/avatar_32.webp?token=signed-for-32';
    const signed128 = '/avatar_128.webp?token=signed-for-128';
    const signedFullsize = '/avatar_fullsize.webp?token=signed-for-fullsize';
    const context = {
        document: {
            createElement(tagName) { return new FakeElement(tagName); },
            getElementById(id) {
                return id === 'chat-messages-container' ? chatMessagesContainer : null;
            },
        },
        window: {},
        botname: 'Assistant',
        promptDescription: 'Description',
        botProfilePicture: signed32,
        botProfilePicture128: signed128,
        botProfilePictureFullsize: signedFullsize,
        imageHandler: {
            showFullsize(url) { openedUrls.push(url); },
        },
        String,
    };
    vm.createContext(context);
    vm.runInContext(showPromptInfoSource, context);
    vm.runInContext('showPromptInfo()', context);

    const imageSection = findByClass(chatMessagesContainer, 'prompt-image-section');
    const avatar = imageSection.children.find(child => child.tagName === 'img');
    assert.ok(avatar);
    assert.equal(avatar.src, signed128);
    assert.equal(avatar.dataset.fullsize, signedFullsize);
    avatar.onclick();
    assert.deepEqual(openedUrls, [signedFullsize]);

    chatMessagesContainer.children = [];
    chatMessagesContainer.firstChild = null;
    context.botProfilePicture128 = '';
    context.botProfilePictureFullsize = '';
    vm.runInContext('showPromptInfo()', context);

    const fallbackSection = findByClass(chatMessagesContainer, 'prompt-image-section');
    const fallbackAvatar = fallbackSection.children.find(child => child.tagName === 'img');
    assert.equal(fallbackAvatar.src, signed32);
    assert.equal(fallbackAvatar.dataset.fullsize, signed32);

    const voiceSource = fs.readFileSync(
        path.join(repoRoot, 'data/static/js/chat/voice-call.js'),
        'utf8'
    );
    const voiceAvatarSource = extract(
        voiceSource,
        'const voiceAvatarUrl = (',
        'promptName.textContent = data.prompt_name;'
    );
    const voicePromptAvatar = new FakeElement('div');
    const voiceContext = {
        document: {
            createElement(tagName) { return new FakeElement(tagName); },
        },
        promptAvatar: voicePromptAvatar,
        data: { prompt_name: 'Assistant' },
        botProfilePicture: signed32,
        botProfilePicture128: signed128,
        botProfilePictureFullsize: signedFullsize,
    };
    vm.createContext(voiceContext);
    vm.runInContext(voiceAvatarSource, voiceContext);

    const voiceAvatar = voicePromptAvatar.children.find(child => child.tagName === 'img');
    assert.ok(voiceAvatar);
    assert.equal(voiceAvatar.src, signedFullsize);
    assert.doesNotMatch(voiceAvatarSource, /\.replace\(/);

    const avatarAssignmentSource = extract(
        source,
        'botProfilePicture = conversationInfo.bot_profile_picture',
        'const sidebarEl = document.querySelector'
    );
    const assignmentContext = {
        conversationInfo: {
            bot_profile_picture: signed32,
            bot_profile_picture_128: signed128,
            bot_profile_picture_fullsize: signedFullsize,
        },
        botProfilePicture: '',
        botProfilePicture128: '',
        botProfilePictureFullsize: '',
    };
    vm.createContext(assignmentContext);
    vm.runInContext(avatarAssignmentSource, assignmentContext);
    assert.equal(assignmentContext.botProfilePicture, signed32);
    assert.equal(assignmentContext.botProfilePicture128, signed128);
    assert.equal(assignmentContext.botProfilePictureFullsize, signedFullsize);

    assignmentContext.conversationInfo = {};
    vm.runInContext(avatarAssignmentSource, assignmentContext);
    assert.equal(assignmentContext.botProfilePicture, '');
    assert.equal(assignmentContext.botProfilePicture128, '');
    assert.equal(assignmentContext.botProfilePictureFullsize, '');
});

test('chat navigation and selectors guard stale asynchronous responses', () => {
    const source = fs.readFileSync(chatPath, 'utf8');

    assert.match(source, /let conversationViewGeneration = 0/);
    assert.match(source, /activeMessageLoad !== loadState/);
    assert.match(source, /!isCurrentConversationView\(conversationId, viewGeneration\)/);
    assert.match(source, /releaseActiveMessageLoad\(loadState, true\)/);
    assert.match(source, /signal: detailsController\.signal/);
    assert.match(source, /class ModelSelector[\s\S]*signal: state\.controller\.signal/);
    assert.match(source, /class ExtensionSelector[\s\S]*signal: state\.controller\.signal/);
    assert.match(source, /isCurrentConversationView\(state\.conversationId, state\.viewGeneration\)/);
    assert.match(source, /stopAudioAndCloseWebSocket\(\)/);
    assert.doesNotMatch(source, /stopAudioAndWebSocket\(\)/);
    assert.match(source, /const detailsIdentityRevision = window\.modelSelector\?\.identityRevision/);
    assert.match(source, /const detailsConversationRevision = window\.modelSelector/);
    assert.match(source, /modelIdentityStillCurrent/);
    assert.match(source, /this\.identityRevision = \(Number\.isInteger\(this\.identityRevision\)/);
    assert.match(source, /getConversationIdentityRevision\(conversationId\)/);
    assert.match(source, /bumpConversationIdentityRevision\(conversationId\)/);
});

test('bookmarks has one click handler and response-local deduplication', () => {
    const source = fs.readFileSync(chatPath, 'utf8');

    assert.equal(
        (source.match(/myBookmarksButton\.addEventListener\('click'/g) || []).length,
        1
    );
    assert.equal(
        (source.match(/else if \(e\.target.*my-bookmarks-btn/g) || []).length,
        0
    );
    assert.match(source, /const localProcessedMessageIds = new Set\(\)/);
    assert.match(source, /const bookmarksController = new AbortController\(\)/);
    assert.match(source, /activeBookmarksLoad === loadState/);
});

test('removing sent attachment A preserves attachment B and its preview', () => {
    const source = fs.readFileSync(fileHandlingPath, 'utf8');
    const fileA = { name: 'a.pdf' };
    const fileB = { name: 'b.pdf' };
    const children = [];
    const makePreview = file => {
        const preview = {
            _aurvekAttachedFile: file,
            remove() {
                const index = children.indexOf(preview);
                if (index >= 0) children.splice(index, 1);
            },
        };
        children.push(preview);
        return preview;
    };
    makePreview(fileA);
    makePreview(fileB);
    const previews = {
        children,
        classList: { toggle() {} },
    };
    const fileInput = { value: 'selected' };
    const context = {
        window: {},
        attachedFiles: [fileA, fileB],
        document: {
            getElementById(id) {
                return id === 'image-previews' ? previews : fileInput;
            },
        },
        console,
        Array,
        Set,
        WeakMap,
        Promise,
        Object,
        Math,
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    context.window.removeAttachedFileBatch([fileA]);

    assert.deepEqual(context.attachedFiles, [fileB]);
    assert.equal(children.length, 1);
    assert.equal(children[0]._aurvekAttachedFile, fileB);

    const chatSource = fs.readFileSync(chatPath, 'utf8');
    assert.match(chatSource, /const outgoingFiles = Object\.freeze\(Array\.from\(/);
    assert.match(source, /const uploadBatch = Object\.freeze\(Array\.from\(files \|\| \[\]\)\)/);
    assert.doesNotMatch(chatSource, /attachedFiles\s*=\s*\[\]/);
});

test('model selector keeps provider identity when model names collide', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const classSource = extract(source, 'class ModelSelector {', 'class ExtensionSelector {');
    const context = {
        window: {
            availableModels: [
                { id: 605, machine: 'GPT', model: 'gpt-5.6-luna' },
                { id: 817, machine: 'GPTSub', model: 'gpt-5.6-luna' },
            ],
        },
    };
    vm.createContext(context);
    vm.runInContext(`${classSource}; globalThis.ModelSelector = ModelSelector;`, context);

    const items = [605, 817].map(id => {
        const values = new Set();
        return {
            dataset: { llmId: String(id), model: 'gpt-5.6-luna' },
            classList: {
                add(name) { values.add(name); },
                remove(name) { values.delete(name); },
                contains(name) { return values.has(name); },
            },
        };
    });
    const selector = Object.create(context.ModelSelector.prototype);
    selector.dropdownContent = { querySelectorAll: () => items };

    selector.updateCurrentModel('gpt-5.6-luna', 817);
    assert.equal(selector.currentLlmId, 817);
    assert.equal(context.window.conversationModelIdentityUnknown, false);
    assert.equal(items[0].classList.contains('current'), false);
    assert.equal(items[1].classList.contains('current'), true);

    context.window.availableModels = [
        { id: 605, machine: 'GPT', model: 'gpt-5.6-luna' },
    ];
    selector.updateCurrentModel('gpt-5.6-luna', 817);
    assert.equal(selector.currentLlmId, 817);

    selector.updateCurrentModel('gpt-5.6-luna');
    assert.equal(selector.currentLlmId, null);
    assert.equal(context.window.conversationModelIdentityUnknown, true);
    assert.equal(items[0].classList.contains('current'), false);
    assert.equal(items[1].classList.contains('current'), false);
});

test('GPTSub is an exact owned credential for API-key and balance UI', () => {
    const chatSource = fs.readFileSync(chatPath, 'utf8');
    const mainSource = fs.readFileSync(mainPath, 'utf8');
    const credentialSource = extract(
        chatSource,
        'function getCurrentConversationLlmIdForUi()',
        'const COLLAPSIBLE_LINE_THRESHOLD'
    );
    const banner = { style: {} };
    const inputContainer = {
        attributes: new Set(['data-disabled']),
        removeAttribute(name) { this.attributes.delete(name); },
        setAttribute(name) { this.attributes.add(name); },
    };
    const credentialContext = {
        window: { modelSelector: { currentLlmId: 817 } },
        currentConversationId: 2193,
        embeddedInitialConversations: [{ id: 2193, llm_id: 605 }],
        availableModels: [
            { id: 605, machine: 'GPT', model: 'gpt-5.6-luna' },
            { id: 817, machine: 'GPTSub', model: 'gpt-5.6-luna' },
        ],
        conversationIdsMatch: (left, right) => String(left) === String(right),
        document: {
            getElementById(id) {
                if (id === 'api-keys-required-banner') return banner;
                if (id === 'message-input-container') return inputContainer;
                return null;
            },
        },
        console,
    };
    vm.createContext(credentialContext);
    vm.runInContext(
        `${credentialSource}; globalThis.ApiKeyManager = ApiKeyManager;`,
        credentialContext
    );
    // The production template defines these globals after chat.js loads.
    credentialContext.apiKeyMode = 'own_only';
    credentialContext.canSendMessages = false;
    credentialContext.requiresOwnKeys = true;
    credentialContext.hasOwnKeys = false;

    assert.equal(credentialContext.currentConversationUsesChatGptSubscription(), true);
    assert.equal(credentialContext.ApiKeyManager.canSendMessages(), true);
    credentialContext.ApiKeyManager.updateUI();
    assert.equal(banner.style.display, 'none');
    assert.equal(inputContainer.attributes.has('data-disabled'), false);
    credentialContext.window.modelSelector.currentLlmId = 605;
    assert.equal(credentialContext.currentConversationUsesChatGptSubscription(), false);
    assert.equal(credentialContext.ApiKeyManager.canSendMessages(), false);
    credentialContext.ApiKeyManager.updateUI();
    assert.equal(banner.style.display, 'block');
    assert.equal(inputContainer.attributes.has('data-disabled'), true);

    const balanceSource = extract(
        mainSource,
        'window.updateConversationBalanceAvailability = function',
        'function checkBalanceAndHideInput()'
    );
    const form = { style: {} };
    const warning = { style: {} };
    const balanceContext = {
        window: {
            selectedChat: null,
            currentConversationUsesChatGptSubscription: () => true,
        },
        messageInputContainer: form,
        insufficientBalanceMessage: warning,
        admin_view: false,
        currentConversationId: 2193,
        embeddedInitialConversations: [{ id: 2193, is_paid: false }],
        conversationIdsMatch: (left, right) => String(left) === String(right),
        hasOwnKeys: false,
        apiKeyMode: 'own_only',
        userBalance: 0,
    };
    vm.createContext(balanceContext);
    vm.runInContext(balanceSource, balanceContext);

    balanceContext.window.updateConversationBalanceAvailability(false);
    assert.equal(form.style.display, 'flex');
    assert.equal(warning.style.display, 'none');

    balanceContext.window.currentConversationUsesChatGptSubscription = () => false;
    balanceContext.window.updateConversationBalanceAvailability(false);
    assert.equal(form.style.display, 'none');
    assert.equal(warning.style.display, 'block');

    balanceContext.window.currentConversationUsesChatGptSubscription = () => true;
    balanceContext.window.updateConversationBalanceAvailability(true);
    assert.equal(form.style.display, 'none');
    assert.equal(warning.style.display, 'block');

    const selectorSource = extract(
        chatSource,
        'class ModelSelector {',
        'class ExtensionSelector {'
    );
    assert.equal(
        (selectorSource.match(/updateConversationBalanceAvailability\(\)/g) || []).length,
        2
    );

    const template = fs.readFileSync(chatTemplatePath, 'utf8');
    const bannerRegion = extract(
        template,
        '<!-- API Keys Required Banner -->',
        '<!-- Locked Conversation Banner'
    );
    assert.match(bannerRegion, /id="api-keys-required-banner"/);
    assert.doesNotMatch(bannerRegion, /\{% if not can_send_messages %\}/);
});

test('shared model mutation queue serializes writes and holds the send barrier', async () => {
    const source = fs.readFileSync(mainPath, 'utf8');
    const queueSource = extract(
        source,
        "if (typeof window.enqueueConversationModelMutation !== 'function') {",
        "document.addEventListener('DOMContentLoaded'"
    );
    const context = { window: {} };
    vm.createContext(context);
    vm.runInContext(queueSource, context);

    const events = [];
    let releaseFirst;
    const firstGate = new Promise(resolve => { releaseFirst = resolve; });
    const first = context.window.enqueueConversationModelMutation(async () => {
        events.push('first:start');
        await firstGate;
        events.push('first:end');
        return 'first';
    });
    const second = context.window.enqueueConversationModelMutation(async () => {
        events.push('second:start');
        events.push('second:end');
        return 'second';
    });

    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(events, ['first:start']);
    assert.equal(context.window.conversationModelMutationPending, true);

    releaseFirst();
    const [firstResult, secondResult] = await Promise.all([first, second]);
    assert.deepEqual(events, [
        'first:start',
        'first:end',
        'second:start',
        'second:end',
    ]);
    assert.equal(firstResult.isLatest, false);
    assert.equal(secondResult.isLatest, true);
    assert.equal(context.window.conversationModelMutationPending, false);
});

test('model selector serializes a slow 817 to 605 switch and keeps 605 final', async () => {
    const mainSource = fs.readFileSync(mainPath, 'utf8');
    const chatSource = fs.readFileSync(chatPath, 'utf8');
    const queueSource = extract(
        mainSource,
        "if (typeof window.enqueueConversationModelMutation !== 'function') {",
        "document.addEventListener('DOMContentLoaded'"
    );
    const classSource = extract(
        chatSource,
        'class ModelSelector {',
        'class ExtensionSelector {'
    );
    const context = {
        window: {},
        document: {
            querySelectorAll: () => [],
            getElementById: id => id === 'send-button'
                ? { innerText: 'Send' }
                : null,
        },
        currentConversationId: 2193,
        conversationViewGeneration: 41,
        conversationIdsMatch: (left, right) => String(left) === String(right),
        isCurrentConversationView: (conversationId, generation) =>
            conversationId === 2193 && generation === 41,
    };
    vm.createContext(context);
    vm.runInContext(queueSource, context);
    vm.runInContext(`${classSource}; globalThis.ModelSelector = ModelSelector;`, context);

    let release817;
    const gate817 = new Promise(resolve => { release817 = resolve; });
    const fetchOrder = [];
    const applied = [];
    const selector = Object.create(context.ModelSelector.prototype);
    selector.requestGeneration = 0;
    selector.currentModel = 'original';
    selector.currentLlmId = 1;
    selector.chatModel = { textContent: '' };
    selector.closeDropdown = () => {};
    selector.showSuccess = () => {};
    selector.showError = () => {};
    selector.requestModelUpdate = async state => {
        fetchOrder.push(`${state.llmId}:start`);
        if (state.llmId === 817) await gate817;
        fetchOrder.push(`${state.llmId}:end`);
        return {
            success: true,
            updated: true,
            llm_id: state.llmId,
            model: 'gpt-5.6-luna',
        };
    };
    selector.applyCommittedModel = (llmId, modelName) => {
        selector.currentLlmId = Number(llmId);
        selector.currentModel = modelName;
        applied.push(Number(llmId));
        return true;
    };

    const slow817 = selector.selectModel(817, 'gpt-5.6-luna');
    await Promise.resolve();
    await Promise.resolve();
    const final605 = selector.selectModel(605, 'gpt-5.6-luna');
    assert.deepEqual(fetchOrder, ['817:start']);

    release817();
    assert.equal(await slow817, false);
    assert.equal(await final605, true);
    assert.deepEqual(fetchOrder, [
        '817:start',
        '817:end',
        '605:start',
        '605:end',
    ]);
    assert.equal(applied[0], 817);
    assert.equal(applied.at(-1), 605);
    assert.equal(selector.currentLlmId, 605);
    assert.equal(context.window.conversationModelMutationPending, false);
});

test('message send is rejected while a model mutation is pending', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const sendMessageSource = extract(
        source,
        'function sendMessage(',
        'function updateMessageId('
    );
    const warnings = [];
    const context = {
        window: { conversationModelMutationPending: true },
        NotificationModal: {
            warning(...args) { warnings.push(args); },
        },
        ApiKeyManager: {
            canSendMessages() {
                throw new Error('send guard was evaluated too late');
            },
        },
    };
    vm.createContext(context);
    vm.runInContext(
        `${sendMessageSource}; globalThis.sendMessage = sendMessage;`,
        context
    );

    assert.equal(context.sendMessage('must not leave the browser'), false);
    assert.deepEqual(warnings, [[
        'Model update in progress',
        'Wait for the selected AI model to finish updating before sending.',
    ]]);
});

test('message send binds the exact model id and reconciles server conflicts', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const sendMessageSource = extract(
        source,
        'function sendMessage(',
        'function updateMessageId('
    );

    assert.match(
        sendMessageSource,
        /expectedLlmId = Number\.parseInt\(window\.modelSelector\?\.currentLlmId, 10\)/
    );
    assert.match(
        sendMessageSource,
        /formData\.append\('expected_llm_id', String\(expectedLlmId\)\)/
    );
    assert.match(sendMessageSource, /conversation_model_changed/);
    assert.match(sendMessageSource, /expected_llm_id_required/);
    assert.match(sendMessageSource, /conversationModelIdentityUnknown = true/);
    assert.match(sendMessageSource, /reconcileConversationModelIdentity/);
    assert.match(sendMessageSource, /conversation_model_changed[\s\S]*removeRetryEcho\(\)/);
    assert.match(sendMessageSource, /Re-attach the files before sending again/);
    assert.doesNotMatch(
        sendMessageSource,
        /conversation_model_changed[\s\S]{0,500}sendMessage\(/
    );
});

test('model selectors reject changes while a message is in progress', async () => {
    const chatSource = fs.readFileSync(chatPath, 'utf8');
    const mainSource = fs.readFileSync(mainPath, 'utf8');
    const classSource = extract(
        chatSource,
        'class ModelSelector {',
        'class ExtensionSelector {'
    );
    const warnings = [];
    const context = {
        window: {},
        document: {
            getElementById(id) {
                return id === 'send-button' ? { innerText: 'Stop' } : null;
            },
        },
        currentConversationId: 2193,
        conversationViewGeneration: 4,
        NotificationModal: {
            warning(...args) { warnings.push(args); },
        },
    };
    vm.createContext(context);
    vm.runInContext(`${classSource}; globalThis.ModelSelector = ModelSelector;`, context);
    const selector = Object.create(context.ModelSelector.prototype);
    selector.closeDropdown = () => {};
    selector.requestModelUpdate = () => {
        throw new Error('model write must not start during a message');
    };

    assert.equal(await selector.selectModel(817, 'gpt-5.6-luna'), false);
    assert.equal(warnings.length, 1);
    assert.match(mainSource, /send-button'\)\?\.innerText === 'Stop'/);
    assert.match(mainSource, /e\.target\.value = committedLlmDropdownValue/);
});

test('reusing an empty chat reconciles the exact default model identity', async () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const classSource = extract(source, 'class ModelSelector {', 'class ExtensionSelector {');
    const newChatSource = extract(
        source,
        'function startNewConversation(',
        'function stopReceivingStream('
    );
    const foldersSource = fs.readFileSync(foldersPath, 'utf8');
    const selections = [];
    const context = {
        window: {
            availableModels: [
                { id: 605, machine: 'GPT', model: 'gpt-5.6-luna' },
                { id: 817, machine: 'GPTSub', model: 'gpt-5.6-luna' },
            ],
        },
        document: {
            getElementById(id) {
                return id === 'llmDropdown' ? { value: '817' } : null;
            },
        },
        currentConversationId: 2193,
        conversationViewGeneration: 41,
        isCurrentConversationEmpty: true,
        isCurrentConversationView: (conversationId, generation) =>
            conversationId === 2193 && generation === 41,
    };
    vm.createContext(context);
    vm.runInContext(`${classSource}; globalThis.ModelSelector = ModelSelector;`, context);

    const selector = Object.create(context.ModelSelector.prototype);
    selector.currentLlmId = 605;
    selector.forcedLlmId = null;
    selector.allowedLlms = [605, 817];
    selector.selectModel = (...args) => {
        selections.push(args);
        return Promise.resolve(true);
    };

    assert.equal(
        await selector.reconcileDefaultForEmptyConversation(2193, 41),
        true
    );
    assert.equal(selections.length, 1);
    assert.equal(selections[0][0], 817);
    assert.equal(selections[0][1], 'gpt-5.6-luna');
    assert.equal(selections[0][2].onlyIfEmpty, true);

    assert.match(
        newChatSource,
        /isCurrentConversationEmpty[\s\S]*reconcileDefaultForEmptyConversation/
    );
    assert.match(
        foldersSource,
        /isCurrentConversationEmpty[\s\S]*reconcileDefaultForEmptyConversation/
    );
    assert.match(newChatSource, /__forceCreate/);
    assert.match(newChatSource, /if \(reused\) return true/);
    assert.match(newChatSource, /reuseLocationMatches/);
    assert.match(newChatSource, /reuseContextMatches/);
    assert.match(newChatSource, /promptId === null/);
    assert.match(newChatSource, /!currentConversationIncognito/);
    assert.match(newChatSource, /activeConversationHasExternalAccess/);
    assert.match(newChatSource, /getActiveConversationPromptId/);
    assert.match(newChatSource, /selectedPromptId === getActiveConversationPromptId\(\)/);
    assert.match(foldersSource, /__forceCreate/);
    assert.match(foldersSource, /getActiveConversationFolderId/);
    assert.match(foldersSource, /reuseLocationMatches/);
    assert.match(foldersSource, /reuseContextMatches/);
    assert.match(foldersSource, /getActiveConversationPromptId/);
    assert.doesNotMatch(
        classSource,
        /defaultLlmId === Number\(this\.currentLlmId\)/
    );
    const continueSource = extract(
        source,
        'function continueConversation(',
        'function isMyBookmarksView('
    );
    assert.doesNotMatch(
        continueSource,
        /reconcileDefaultForEmptyConversation/
    );
});

test('default model failures revert the dropdown and malformed empty updates fail closed', () => {
    const source = fs.readFileSync(mainPath, 'utf8');

    assert.match(source, /throw new Error\('Failed to update the default AI model'\)/);
    assert.match(source, /e\.target\.value = committedLlmDropdownValue/);
    assert.match(source, /data\.success !== true \|\| typeof data\.updated !== 'boolean'/);
    assert.match(source, /Empty conversation model update returned an invalid response/);
    assert.match(source, /llmDropdown\.addEventListener\('change', function\(e\)/);
    assert.doesNotMatch(
        source,
        /llmDropdown\.addEventListener\('change', withSession/
    );
});

test('folder chat keeps exact model identity and passes its payload on load', () => {
    const source = fs.readFileSync(foldersPath, 'utf8');
    const functionSource = extract(
        source,
        'function createFolderChatElement(conversation, folderId = null)',
        '// Create chat menu for folder chats'
    );
    const continueCalls = [];
    const created = [];
    const document = {
        createElement(tagName) {
            const element = new FakeElement(tagName);
            element.listeners = {};
            element.attributes = {};
            element.setAttribute = (name, value) => {
                element.attributes[name] = value;
            };
            element.addEventListener = (name, listener) => {
                element.listeners[name] = listener;
            };
            created.push(element);
            return element;
        },
        querySelectorAll() { return []; },
    };
    const conversation = {
        id: 2193,
        chat_name: 'Exact identity',
        start_date: '2026-08-15T00:00:00Z',
        last_activity: '2026-08-15T00:00:00Z',
        machine: 'GPTSub',
        llm_model: 'gpt-5.6-luna',
        llm_id: 817,
        prompt_id: 19,
    };
    const context = {
        document,
        window: {},
        escapeHTML: value => value,
        createChatMenuForFolder: () => new FakeElement('menu'),
        handleDragStart() {},
        handleDragEnd() {},
        continueConversation: (...args) => continueCalls.push(args),
        Date,
    };
    vm.createContext(context);
    vm.runInContext(
        `${functionSource}; globalThis.createFolderChatElement = createFolderChatElement;`,
        context
    );

    const element = context.createFolderChatElement(conversation, 42);
    assert.equal(element.dataset.llmId, '817');
    assert.equal(element.dataset.llmModel, 'gpt-5.6-luna');
    assert.equal(element.dataset.machine, 'GPTSub');
    assert.equal(element.dataset.folderId, '42');
    assert.equal(element.dataset.promptId, '19');

    element.listeners.click({ target: { closest: () => null } });
    assert.equal(continueCalls.length, 1);
    assert.equal(continueCalls[0][0], 2193);
    assert.equal(continueCalls[0][2], 'GPTSub');
    assert.equal(continueCalls[0][5], conversation);
});

test('chat menus use one delegated outside-click listener', () => {
    const chatSource = fs.readFileSync(chatPath, 'utf8');
    const foldersSource = fs.readFileSync(foldersPath, 'utf8');
    const mainMenuSource = extract(
        chatSource,
        'function closeAllChatMenus()',
        'const renameConversation ='
    );
    const folderMenuSource = extract(
        foldersSource,
        'function createChatMenuForFolder',
        '// Delete folder handler'
    );

    assert.equal(
        (mainMenuSource.match(/document\.addEventListener\('click'/g) || []).length,
        1
    );
    assert.equal(
        (folderMenuSource.match(/document\.addEventListener\('click'/g) || []).length,
        0
    );
    assert.match(mainMenuSource, /closest\?\.\('\.chat-menu, \.chat-menu-content'\)/);
    assert.match(chatSource, /delete element\.dataset\.folderMenuEnhanced/);
});

test('loaded folder batches receive idempotent drop handlers', () => {
    const source = fs.readFileSync(foldersPath, 'utf8');

    assert.match(source, /function loadMoreFolders\(\)[\s\S]*setupDropZones\(container\)/);
    assert.match(source, /folderItem\.hasAttribute\('data-drop-zone-ready'\)/);
    assert.match(source, /folderItem\.setAttribute\('data-drop-zone-ready', 'true'\)/);
    assert.match(source, /container\.hasAttribute\('data-drop-zone-ready'\)/);
    assert.match(source, /container\.setAttribute\('data-drop-zone-ready', 'true'\)/);
});

test('persistence SSE errors preserve streamed response content', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const sendMessageSource = extract(
        source,
        'function sendMessage(',
        'function updateMessageId('
    );
    const persistenceBranch = extract(
        source,
        '} else if (parsedData.persistence_error === true)',
        '} else if (parsedData.error && !parsedData.multi_ai_error)'
    );

    assert.match(sendMessageSource, /let persistenceErrorOccurred = false/);
    assert.match(persistenceBranch, /persistenceErrorOccurred = true/);
    assert.match(persistenceBranch, /copyIcon\.style\.display = 'inline'/);
    assert.match(persistenceBranch, /message-persistence-warning/);
    assert.match(persistenceBranch, /botMessageParagraph\.appendChild\(warningEl\)/);
    assert.match(persistenceBranch, /NotificationModal\.warning\('Response not saved'/);
    assert.doesNotMatch(persistenceBranch, /innerHTML\s*=/);
    assert.doesNotMatch(persistenceBranch, /textContent\s*=\s*''/);
    assert.doesNotMatch(persistenceBranch, /streamSucceeded\s*=\s*true/);

    const persistenceGuards = [
        ...sendMessageSource.matchAll(/if \(persistenceErrorOccurred\) \{/g),
    ];
    const persistedSuccessAssignments = [
        ...sendMessageSource.matchAll(/streamSucceeded = true;/g),
    ];
    assert.equal(persistenceGuards.length, 2);
    assert.equal(persistedSuccessAssignments.length, 2);
    for (const guard of persistenceGuards) {
        const guardedCompletion = sendMessageSource.slice(guard.index, guard.index + 220);
        assert.match(guardedCompletion, /return;/);
        assert.doesNotMatch(guardedCompletion, /streamSucceeded\s*=\s*true/);
    }
    assert.ok(persistenceGuards[0].index < persistedSuccessAssignments[0].index);
    assert.ok(persistedSuccessAssignments[0].index < persistenceGuards[1].index);
    assert.ok(persistenceGuards[1].index < persistedSuccessAssignments[1].index);
    assert.match(sendMessageSource, /if \(streamSucceeded\) \{/);
});

test('chat template drops jQuery and defers ordered head dependencies', () => {
    const source = fs.readFileSync(chatTemplatePath, 'utf8');

    assert.doesNotMatch(source, /jquery/i);
    for (const dependency of [
        'bootstrap.bundle.min.js',
        'highlight.min.js',
        'languages/python.min.js',
        'pako.min.js',
        'purify.min.js',
    ]) {
        const escaped = dependency.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        assert.match(source, new RegExp(`<script defer src="[^"]*${escaped}"`));
    }
});
