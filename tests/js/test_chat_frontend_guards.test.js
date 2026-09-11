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
const { createRuntime: createI18n } = require('../../data/static/js/common/i18n.js');
function createChatContext(context) {
    const domains = ['common', 'chat', 'chat_widgets', 'chat_ui'];
    const resources = Object.fromEntries(domains.map(domain => [domain,
        JSON.parse(fs.readFileSync(path.join(repoRoot, 'locales/en', domain + '.json'), 'utf8'))]));
    context.window ||= {};
    context.window.AurvekI18n = createI18n({ version: 1, language: 'en', locales: { en: 'en-US' }, resources: { en: resources } });
    return vm.createContext(context);
}


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
        this.parentElement = null;
        this.innerHTMLWrites = [];
    }

    set innerHTML(value) {
        this.innerHTMLWrites.push(value);
        this.children.forEach(child => { child.parentElement = null; });
        this.children = [];
        this.firstChild = null;
    }

    get innerHTML() { return ''; }

    appendChild(child) {
        child.remove();
        child.parentElement = this;
        this.children.push(child);
        this.firstChild = this.children[0] || null;
        return child;
    }

    insertBefore(child, reference) {
        child.remove();
        child.parentElement = this;
        const index = this.children.indexOf(reference);
        this.children.splice(index < 0 ? this.children.length : index, 0, child);
        this.firstChild = this.children[0] || null;
        return child;
    }

    get firstElementChild() { return this.firstChild; }

    replaceChildren(...children) {
        this.innerHTML = '';
        children.forEach(child => this.appendChild(child));
    }

    querySelector(selector) {
        if (selector === '.prompt-info') {
            return this.children.find(child => child.classList.contains('prompt-info')) || null;
        }
        if (selector === 'img') {
            for (const child of this.children) {
                const image = child.tagName === 'img' ? child : child.querySelector(selector);
                if (image) return image;
            }
        }
        return null;
    }

    remove() {
        if (!this.parentElement) return;
        const siblings = this.parentElement.children;
        siblings.splice(siblings.indexOf(this), 1);
        this.parentElement.firstChild = siblings[0] || null;
        this.parentElement = null;
    }
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
        allMessagesLoaded: true,
        botProfilePicture: '',
        botProfilePicture128: '',
        botProfilePictureFullsize: '',
        imageHandler: { showFullsize() {} },
        String,
    };
    createChatContext(context);
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
        allMessagesLoaded: true,
        botProfilePicture: signed32,
        botProfilePicture128: signed128,
        botProfilePictureFullsize: signedFullsize,
        imageHandler: {
            showFullsize(url) { openedUrls.push(url); },
        },
        String,
    };
    createChatContext(context);
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
    createChatContext(voiceContext);
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
    createChatContext(assignmentContext);
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

test('paginated history shows the prompt card only at the beginning and preserves the scroll anchor', async () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const messagesContainer = new FakeElement('div');
    const avatar = { disabled: true };
    const chatWindow = { scrollTop: 0 };
    const height = element => element.classList.contains('prompt-info')
        ? 100 : element.children.length * 20;
    Object.defineProperty(chatWindow, 'scrollHeight', {
        get: () => messagesContainer.children.reduce((sum, child) => sum + height(child), 0),
    });
    const pages = [
        { messages: [{ id: 3 }, { id: 4 }], has_more: true },
        { messages: [{ id: 1 }, { id: 2 }], has_more: false },
        { messages: [{ id: 5 }, { id: 6 }], has_more: true },
    ];
    const requests = [];
    const context = {
        document: {
            createElement(tag) {
                const element = new FakeElement(tag);
                element.getBoundingClientRect = () => ({
                    top: messagesContainer.children
                        .slice(0, messagesContainer.children.indexOf(element))
                        .reduce((sum, child) => sum + height(child), 0) - chatWindow.scrollTop,
                });
                return element;
            },
            getElementById: id => ({
                'chat-messages-container': messagesContainer,
                'chat-window': chatWindow,
                'chat-title-avatar': avatar,
            })[id] || null,
            querySelector: () => null,
        },
        window: {},
        currentConversationId: 91,
        conversationViewGeneration: 2,
        isCurrentConversationView: (id, generation) => id === 91 && generation === 2,
        isLoading: false,
        allMessagesLoaded: false,
        oldestLoadedMessageId: null,
        limitMessage: 25,
        AbortController,
        disableInputControls() {},
        setIncognitoUiState() {},
        isConversationIncognitoData: () => false,
        setCurrentProviderHealth() {},
        setCurrentModelAvailability() {},
        processMessage(message, container) {
            const row = new FakeElement('div');
            row.dataset.messageId = message.id;
            container.appendChild(row);
        },
        releaseActiveMessageLoad() { context.isLoading = false; },
        async secureFetch(url) {
            requests.push(url);
            const page = pages.shift();
            return {
                ok: true,
                json: async () => ({
                    ...page,
                    conversation_info: { prompt_name: 'Assistant', prompt_description: 'About me' },
                }),
            };
        },
        console: { error: (...args) => assert.fail(args.join(' ')) },
    };
    createChatContext(context);
    vm.runInContext(extract(source, 'function showPromptInfo()', '// Model Selector functionality'), context);
    vm.runInContext(extract(source, 'async function loadMessages(', 'function refreshActiveConversation()'), context);

    assert.ok(await context.loadMessages(91));
    assert.equal(messagesContainer.querySelector('.prompt-info'), null);
    assert.equal(avatar.disabled, false);
    // A loaded native chat must reach its normal export confirmation, without
    // being mistaken for an application conversation with null capabilities.
    const exportConfirmations = [];
    context.NotificationModal = { confirm: title => exportConfirmations.push(title) };
    context.withSession = callback => callback;
    vm.runInContext(extract(fs.readFileSync(path.join(repoRoot, 'data/static/js/chat/utils.js'), 'utf8'),
        'function downloadPDF(', 'function serveMp3('), context);
    context.downloadPDF(91);
    context.downloadAudio(91);
    assert.equal(exportConfirmations.length, 2);
    const newestPage = messagesContainer.firstElementChild;
    chatWindow.scrollTop = 10;
    const originalAnchorTop = newestPage.getBoundingClientRect().top;

    assert.ok(await context.loadMessages(91, true));
    assert.ok(messagesContainer.firstChild.classList.contains('prompt-info'));
    assert.equal(messagesContainer.children.filter(child => child.classList.contains('prompt-info')).length, 1);
    assert.deepEqual(messagesContainer.children.slice(1)
        .flatMap(page => page.children.map(row => row.dataset.messageId)), [1, 2, 3, 4]);
    assert.equal(newestPage.getBoundingClientRect().top, originalAnchorTop);
    assert.match(requests[1], /before_id=3/);

    context.allMessagesLoaded = false;
    context.oldestLoadedMessageId = null;
    assert.ok(await context.loadMessages(91));
    assert.equal(messagesContainer.querySelector('.prompt-info'), null);
    assert.deepEqual(messagesContainer.firstChild.children.map(row => row.dataset.messageId), [5, 6]);
});

test('prompt modal preserves the inline card, previews the current fullsize avatar after closing, and blocks unavailable views', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const messagesContainer = new FakeElement('div');
    const body = new FakeElement('div');
    const avatar = { disabled: false };
    const listeners = {};
    const openedImages = [];
    let hideCalls = 0;
    const modal = {
        addEventListener(name, callback, options) {
            listeners[name] = callback;
            if (name === 'hidden.bs.modal') assert.equal(options.once, true);
        },
    };
    const previewAfterHidden = () => {
        const callback = listeners['hidden.bs.modal'];
        delete listeners['hidden.bs.modal'];
        callback();
    };
    const context = {
        document: {
            createElement: tag => new FakeElement(tag),
            getElementById: id => ({
                'chat-messages-container': messagesContainer,
                'chat-title-avatar': avatar,
                promptInfoModal: modal,
                'prompt-info-modal-body': body,
            })[id] || null,
        },
        window: {},
        bootstrap: {
            Modal: {
                getInstance(element) {
                    assert.equal(element, modal);
                    return { hide() { hideCalls += 1; } };
                },
            },
        },
        imageHandler: {
            showFullsize(url, messageId) { openedImages.push({ url, messageId }); },
        },
        currentConversationId: 91,
        allMessagesLoaded: true,
        botname: 'First assistant',
        promptDescription: 'First description',
        botProfilePicture128: '/first_128.webp?token=first-preview',
        botProfilePictureFullsize: '/first_fullsize.webp?token=first-fullsize',
    };
    createChatContext(context);
    vm.runInContext(extract(source, 'function showPromptInfo()', '// Model Selector functionality'), context);
    context.showPromptInfo();
    const inlineCard = messagesContainer.firstChild;
    context.initializePromptInfoModal();
    const open = () => {
        let prevented = false;
        listeners['show.bs.modal']({ preventDefault() { prevented = true; } });
        return !prevented;
    };

    assert.equal(open(), true);
    assert.equal(findByClass(body, 'prompt-name').textContent, 'First assistant');
    assert.equal(messagesContainer.firstChild, inlineCard);
    assert.notEqual(body.firstChild, inlineCard);
    const firstImage = body.querySelector('img');
    assert.equal(firstImage.style.cursor, 'pointer');
    firstImage.onclick();
    assert.equal(firstImage.onclick, null);
    assert.equal(hideCalls, 1);
    assert.deepEqual(openedImages, []);
    previewAfterHidden();
    assert.deepEqual(openedImages, [{
        url: '/first_fullsize.webp?token=first-fullsize', messageId: null,
    }]);

    context.botname = 'Second assistant';
    context.promptDescription = 'Second description';
    context.botProfilePictureFullsize = '/second_fullsize.webp?token=second-fullsize';
    context.allMessagesLoaded = false;
    assert.equal(open(), true);
    assert.equal(body.children.length, 1);
    assert.equal(findByClass(body, 'prompt-name').textContent, 'Second assistant');
    assert.equal(findByClass(body, 'prompt-description').textContent, 'Second description');
    assert.equal(messagesContainer.firstChild, inlineCard);
    body.querySelector('img').onclick();
    assert.equal(hideCalls, 2);
    assert.equal(openedImages.length, 1);
    previewAfterHidden();
    assert.deepEqual(openedImages[1], {
        url: '/second_fullsize.webp?token=second-fullsize', messageId: null,
    });

    avatar.disabled = true;
    assert.equal(open(), false);
    avatar.disabled = false;
    context.currentConversationId = null;
    assert.equal(open(), false);
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
    createChatContext(context);
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
        refreshModelAvailabilityBanner() {},
    };
    createChatContext(context);
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
    createChatContext(credentialContext);
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
            currentConversationApplicationFunding: () => null,
        },
        messageInputContainer: form,
        insufficientBalanceMessage: warning,
        nativeBalanceMessage: 'Insufficient balance',
        admin_view: false,
        currentConversationId: 2193,
        embeddedInitialConversations: [{ id: 2193, is_paid: false }],
        conversationIdsMatch: (left, right) => String(left) === String(right),
        hasOwnKeys: false,
        apiKeyMode: 'own_only',
        userBalance: 0,
    };
    createChatContext(balanceContext);
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
    createChatContext(context);
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
                ? { innerText: '送信', dataset: { streaming: 'false' } }
                : null,
        },
        currentConversationId: 2193,
        conversationViewGeneration: 41,
        conversationIdsMatch: (left, right) => String(left) === String(right),
        isCurrentConversationView: (conversationId, generation) =>
            conversationId === 2193 && generation === 41,
    };
    createChatContext(context);
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

test('adding a saved incognito chat creates a sidebar anchor without reusing a message', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const messages = new FakeElement('div');
    const firstMessage = new FakeElement('div');
    firstMessage.dataset.conversationId = '2193';
    firstMessage.textContent = 'Keep this first message';
    messages.appendChild(firstMessage);
    const sidebar = new FakeElement('div');
    const updated = [];
    const context = {
        document: {
            querySelector(selector) {
                if (selector === '[data-conversation-id="2193"]') return firstMessage;
                if (selector === '#dynamic-chats-container') return sidebar;
                return null;
            },
            createElement: tag => new FakeElement(tag),
        },
        loadedConversationIds: new Set(),
        updateSingleConversation: element => updated.push(element),
        getConversationExternalChannels: () => [],
        conversationHasExternalChannel: () => false,
        createExternalDeviceBadge: () => null,
        renderConversationName: (element, conversation, name) => { element.textContent = name; },
        createChatMenu: () => new FakeElement('div'),
        setupConversationElementListeners() {},
    };
    createChatContext(context);
    vm.runInContext(extract(
        source,
        'function addConversationElement(',
        '// Close all open chat context menus'
    ), context);

    context.addConversationElement({ id: 2193, is_incognito: false }, 'Saved chat', 2193);

    assert.deepEqual(updated, []);
    assert.equal(sidebar.children.length, 1);
    assert.equal(sidebar.firstChild.tagName, 'a');
    assert.equal(sidebar.firstChild.href, '#');
    assert.equal(sidebar.firstChild.dataset.conversationId, 2193);
    assert.notEqual(sidebar.firstChild, firstMessage);
    assert.deepEqual(messages.children, [firstMessage]);
    assert.equal(firstMessage.textContent, 'Keep this first message');
    assert.deepEqual(firstMessage.innerHTMLWrites, []);
});

test('external platform updates replace sidebar cards without deleting or reusing messages', async () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    for (const includeUpdatedChat of [true, false]) {
        const messages = new FakeElement('div');
        const dynamic = new FakeElement('div');
        const external = new FakeElement('div');
        const originalMessages = [2193, 2194].map(id => {
            const message = new FakeElement('div');
            message.dataset.conversationId = String(id);
            message.textContent = `Message ${id}`;
            messages.appendChild(message);
            return message;
        });
        const oldCard = new FakeElement('a');
        oldCard.dataset.conversationId = '2193';
        oldCard.classList.add('list-group-item');
        dynamic.appendChild(oldCard);
        const updatedCards = [];
        const errors = [];
        const document = {
            createElement: tag => new FakeElement(tag),
            querySelectorAll(selector) {
                const conversationId = selector.match(/data-conversation-id="(\d+)"/)[1];
                return [...dynamic.children, ...external.children, ...messages.children].filter(element =>
                    element.dataset.conversationId === conversationId &&
                    (!selector.includes('#sidebar') || element.parentElement !== messages) &&
                    (!selector.includes('.list-group-item') || element.classList.contains('list-group-item'))
                );
            },
            querySelector(selector) {
                if (selector === '#dynamic-chats-container') return dynamic;
                if (selector === '#external-chats-container') return external;
                if (selector === '.external-section') return { style: {} };
                return this.querySelectorAll(selector)[0] || null;
            },
        };
        const context = {
            document,
            console: { error: error => errors.push(error) },
            withSession: callback => callback,
            messagingChannelMutationGenerations: { whatsapp: 0 },
            getVisibleConversationsCount: () => 1,
            mergeVisibleConversationChannel: conversation => conversation,
            secureFetch: async () => ({ json: async () => ({
                success: true,
                updatedConversations: includeUpdatedChat ? [{ id: 2193 }, { id: 2194 }] : [{ id: 2194 }],
            }) }),
            updateSingleConversation(element, conversation) {
                updatedCards.push(element);
                element.dataset.conversationId = String(conversation.id);
                element.classList.add('list-group-item');
                dynamic.appendChild(element);
                return true;
            },
            conversationHasExternalChannel: () => false,
            sortDynamicChats() {},
            updateExternalSection() {},
        };
        createChatContext(context);
        vm.runInContext(extract(
            source,
            'const toggleExternalPlatform = withSession(',
            'function getVisibleConversationsCount()'
        ), context);
        vm.runInContext("toggleExternalPlatform(2193, 'whatsapp', false)", context);
        await new Promise(resolve => setImmediate(resolve));

        assert.deepEqual(errors, []);
        assert.deepEqual(messages.children, originalMessages);
        assert.deepEqual(originalMessages.map(message => message.textContent), ['Message 2193', 'Message 2194']);
        assert.equal(oldCard.parentElement, null);
        assert.equal(updatedCards.length, includeUpdatedChat ? 2 : 1);
        assert.ok(updatedCards.every(element => element.tagName === 'a'));
        assert.ok(updatedCards.every(element => !originalMessages.includes(element)));
    }
});

function incognitoContext(secureFetch) {
    const source = fs.readFileSync(chatPath, 'utf8');
    const messages = { innerHTML: 'Existing messages', scrollTop: 125 };
    const card = { dataset: { conversationId: '2193' } };
    const storage = new Map();
    const added = [];
    const removed = [];
    const notifications = [];
    const context = {
        window: {},
        currentConversationId: 2193,
        currentConversationIncognito: true,
        incognitoSavePromise: null,
        incognitoClosePromise: null,
        loadedConversationIds: new Set([2193]),
        conversationIdsMatch: (left, right) => String(left) === String(right),
        secureFetch,
        stopReceivingStream() {},
        document: {
            getElementById: id => id === 'chat-messages-container' ? messages : null,
            querySelector: () => card,
        },
        localStorage: {
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: key => storage.delete(key),
        },
        setIncognitoUiState(value) { context.currentConversationIncognito = value; },
        addConversationElement: (...args) => added.push(args),
        removeConversationElement: id => removed.push(id),
        sortDynamicChats() {},
        notifyConversationChannelControls() {},
        updateIncognitoChatControls() {},
        NotificationModal: {
            toast: (...args) => notifications.push(args),
            error: (...args) => notifications.push(args),
        },
        console: { error() {} },
    };
    createChatContext(context);
    vm.runInContext(extract(
        source,
        'function saveCurrentIncognitoConversation()',
        'function applyUpdatedConversationCards('
    ), context);
    vm.runInContext(extract(
        source,
        'function closeCurrentIncognitoConversation()',
        'function initIncognitoChatControls()'
    ), context);
    return { context, messages, card, storage, added, removed, notifications };
}

test('saving an incognito chat preserves the open conversation and pending navigation waits for it', async () => {
    let finishSave;
    const requests = [];
    const fixture = incognitoContext((url, options) => {
        requests.push({ url, method: options.method });
        return new Promise(resolve => { finishSave = resolve; });
    });
    const { context, messages, card, storage, added, removed } = fixture;
    const savedConversation = { id: 2193, chat_name: 'Saved conversation', is_incognito: false };

    const saving = context.saveCurrentIncognitoConversation();
    assert.equal(context.saveCurrentIncognitoConversation(), saving);
    assert.equal(context.closeCurrentIncognitoConversation(), saving);
    const leaving = context.maybeCloseCurrentIncognitoBeforeLeaving();
    assert.equal(leaving, saving);
    assert.equal(context.currentConversationIncognito, true);
    assert.equal(storage.has('activeConversationId'), false);
    assert.deepEqual(added, []);

    finishSave({ ok: true, json: async () => ({ success: true, conversation: savedConversation }) });
    assert.equal(await saving, true);
    assert.equal(await leaving, true);
    assert.deepEqual(requests, [{ url: '/api/conversations/2193/incognito/save', method: 'POST' }]);
    assert.equal(context.currentConversationId, 2193);
    assert.equal(context.currentConversationIncognito, false);
    assert.deepEqual(messages, { innerHTML: 'Existing messages', scrollTop: 125 });
    assert.equal(storage.get('activeConversationId'), '2193');
    assert.equal(context.window.selectedChat, card);
    assert.deepEqual(added, [[savedConversation, 'Saved conversation', 2193]]);
    assert.deepEqual(removed, []);
    assert.equal(context.incognitoSavePromise, null);
});

test('failed incognito save or close preserves the chat and allows retry', async () => {
    for (const operation of ['saveCurrentIncognitoConversation', 'closeCurrentIncognitoConversation']) {
        const fixture = incognitoContext(async () => ({
            ok: false,
            json: async () => ({ success: false, error: 'Temporary failure' }),
        }));
        const { context, messages, added, removed, notifications } = fixture;
        assert.equal(await context[operation](), false);
        assert.equal(context.currentConversationId, 2193);
        assert.equal(context.currentConversationIncognito, true);
        assert.deepEqual(messages, { innerHTML: 'Existing messages', scrollTop: 125 });
        assert.equal(context.loadedConversationIds.has(2193), true);
        assert.deepEqual(added, []);
        assert.deepEqual(removed, []);
        assert.equal(notifications.length, 1);
        assert.equal(context.incognitoSavePromise, null);
        assert.equal(context.incognitoClosePromise, null);

        context.secureFetch = async () => ({
            ok: true,
            json: async () => ({
                success: true,
                conversation: { id: 2193, chat_name: 'Retried' },
            }),
        });
        assert.equal(await context[operation](), true);
    }
});

test('closing a chat saved in another tab keeps its messages and active identity', async () => {
    const { context, messages, removed, storage } = incognitoContext(async () => ({
        ok: true,
        json: async () => ({ success: true, already_saved: true }),
    }));
    assert.equal(await context.closeCurrentIncognitoConversation(), true);
    assert.equal(context.currentConversationId, 2193);
    assert.equal(context.currentConversationIncognito, false);
    assert.equal(storage.get('activeConversationId'), '2193');
    assert.deepEqual(messages, { innerHTML: 'Existing messages', scrollTop: 125 });
    assert.equal(context.loadedConversationIds.has(2193), true);
    assert.deepEqual(removed, []);
});

test('disabled model guidance respects assistant restrictions and clears after a replacement is committed', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const banner = { hidden: true };
    const bannerText = { textContent: '' };
    const changeButton = { hidden: true };
    const newChatOption = { disabled: false };
    const draft = Object.freeze({ value: 'Keep this draft while I choose a model' });
    const context = {
        window: {
            availableModels: [
                { id: 41, model: 'previous-model', enabled: true },
                { id: 42, model: 'replacement-model', enabled: true },
            ],
        },
        currentConversationId: 91,
        conversationViewGeneration: 2,
        currentModelAvailability: null,
        isCurrentConversationView: (id, generation) => id === 91 && generation === 2,
        document: {
            querySelector: selector => selector === '#llmDropdown option[value="41"]' ? newChatOption : null,
            getElementById: id => ({
                'model-unavailable-banner': banner,
                'model-unavailable-text': bannerText,
                'change-unavailable-model-btn': changeButton,
                'message-text': draft,
            })[id] || null,
        },
    };
    createChatContext(context);
    vm.runInContext(extract(source, 'function isCurrentModelUnavailable()', 'function showNoChatTemplate()'), context);
    vm.runInContext(`${extract(source, 'class ModelSelector {', 'class ExtensionSelector {')}; globalThis.ModelSelector = ModelSelector;`, context);
    const selector = Object.create(context.ModelSelector.prototype);
    Object.assign(selector, {
        currentLlmId: 41,
        forcedLlmId: null,
        allowedLlms: null,
        chatModel: { textContent: '' },
        dropdownContent: { querySelectorAll: () => [] },
        cacheCommittedModel() {},
        populateModels() {},
    });
    context.window.modelSelector = selector;

    context.setCurrentModelAvailability(false, 41);
    assert.equal(context.isCurrentModelUnavailable(), true);
    assert.equal(banner.hidden, false);
    assert.match(bannerText.textContent, /choose|select/i);
    assert.equal(changeButton.hidden, false);
    assert.equal(context.window.availableModels[0].enabled, false);
    assert.equal(newChatOption.disabled, true);

    selector.forcedLlmId = 41;
    context.refreshModelAvailabilityBanner();
    assert.equal(changeButton.hidden, true);
    assert.match(bannerText.textContent, /creator|admin|owner/i);

    selector.forcedLlmId = null;
    selector.allowedLlms = [41];
    context.refreshModelAvailabilityBanner();
    assert.equal(changeButton.hidden, true);
    assert.match(bannerText.textContent, /creator|admin|owner/i);

    selector.allowedLlms = null;
    context.window.AurvekEmbed = {};
    context.refreshModelAvailabilityBanner();
    assert.equal(changeButton.hidden, true);
    delete context.window.AurvekEmbed;

    assert.equal(selector.applyCommittedModel(42, 'replacement-model'), true);
    assert.equal(context.isCurrentModelUnavailable(), false);
    assert.equal(banner.hidden, true);
    assert.equal(selector.currentLlmId, 42);
    assert.equal(context.window.availableModels[0].enabled, false);
    assert.equal(draft.value, 'Keep this draft while I choose a model');

    context.setCurrentModelAvailability(false, 41);
    assert.equal(context.isCurrentModelUnavailable(), false);
    assert.equal(banner.hidden, true);
});

test('disabled-model refusals restore the draft without overwriting another chat', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const warnings = [];
    const context = {
        window: {
            modelSelector: { currentLlmId: 41, forcedLlmId: null, allowedLlms: null },
            availableModels: [{ id: 42, model: 'replacement-model', enabled: true }],
        },
        currentConversationId: 91,
        currentModelAvailability: null,
        NotificationModal: { warning: (...args) => warnings.push(args.map(value => typeof value === 'function' ? value() : value)) },
    };
    createChatContext(context);
    vm.runInContext(extract(source, 'function isCurrentModelUnavailable()', 'function showNoChatTemplate()'), context);

    const refusalBranch = extract(source,
        "if (body?.error_code === 'model_unavailable') {",
        "if (body && ("
    );
    for (const activeId of [91, 92]) {
        const restoredDraft = { value: activeId === 91 ? '' : 'Draft in another chat' };
        const cleanup = [];
        Object.assign(context, {
            currentConversationId: activeId,
            currentModelAvailability: null,
            sendConversationId: 91,
            expectedLlmId: 41,
            body: { error_code: 'model_unavailable', llm_id: 41 },
            messageText_raw: 'Unsent message',
            hadOutgoingAttachments: true,
            discardUploadedRefs: () => cleanup.push('uploaded refs'),
            removeRetryEcho: () => cleanup.push('temporary messages'),
            conversationIdsMatch: (left, right) => String(left) === String(right),
            document: {
                getElementById: id => id === 'message-text' ? restoredDraft : null,
                querySelector: () => null,
            },
        });
        assert.equal(vm.runInContext(`(function () { ${refusalBranch} })()`, context), null);
        assert.deepEqual(cleanup, ['uploaded refs', 'temporary messages']);
        assert.equal(restoredDraft.value, activeId === 91 ? 'Unsent message' : 'Draft in another chat');
        assert.equal(context.isCurrentModelUnavailable(), activeId === 91);
        if (activeId === 91) assert.match(warnings.at(-1)[1], /Re-attach/);
    }
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
        incognitoSavePromise: null,
        incognitoClosePromise: null,
        NotificationModal: {
            warning(...args) { warnings.push(args.map(value => typeof value === 'function' ? value() : value)); },
        },
        ApiKeyManager: {
            canSendMessages() {
                throw new Error('send guard was evaluated too late');
            },
        },
    };
    createChatContext(context);
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
    assert.match(sendMessageSource, /chat\.reattach/);
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
                return id === 'send-button' ? { innerText: '停止', dataset: { streaming: 'true' } } : null;
            },
        },
        currentConversationId: 2193,
        conversationViewGeneration: 4,
        NotificationModal: {
            warning(...args) { warnings.push(args); },
        },
    };
    createChatContext(context);
    vm.runInContext(`${classSource}; globalThis.ModelSelector = ModelSelector;`, context);
    const selector = Object.create(context.ModelSelector.prototype);
    selector.closeDropdown = () => {};
    selector.requestModelUpdate = () => {
        throw new Error('model write must not start during a message');
    };

    assert.equal(await selector.selectModel(817, 'gpt-5.6-luna'), false);
    assert.equal(warnings.length, 1);
    assert.match(mainSource, /send-button'\)\?\.dataset.streaming === 'true'/);
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
    createChatContext(context);
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
    createChatContext(context);
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

test('sidebar updates make only conversation links draggable and leave messages untouched', () => {
    const source = fs.readFileSync(foldersPath, 'utf8');
    function item(name, inSidebar = false, listItem = false, folderItem = false) {
        const attributes = new Map();
        const listeners = [];
        return {
            name, inSidebar, listItem, folderItem, attributes, listeners,
            dataset: { conversationId: '2193' },
            style: {},
            hasAttribute: key => attributes.has(key),
            setAttribute: (key, value) => attributes.set(key, value),
            addEventListener: (type, callback) => listeners.push({ type, callback }),
        };
    }

    const existingChat = item('existing chat', true, true);
    const folderChat = item('folder chat', true, true, true);
    folderChat.setAttribute('draggable', 'true');
    folderChat.style.cursor = 'grab';
    const userMessage = item('user message');
    const assistantMessage = item('assistant message');
    const searchResult = item('search result', false, true);
    const elements = [existingChat, folderChat, userMessage, assistantMessage, searchResult];
    const container = {};
    const enhanced = [];
    let onMutation;
    const context = {
        document: {
            getElementById: id => id === 'dynamic-chats-container' ? container : null,
            querySelectorAll: selector => elements.filter(element =>
                (!selector.includes('#sidebar') || element.inSidebar) &&
                (!selector.includes('.list-group-item') || element.listItem) &&
                (!selector.includes(':not(.folder-chat-item)') || !element.folderItem)
            ),
        },
        MutationObserver: class {
            constructor(callback) { onMutation = callback; }
            observe(target) { assert.equal(target, container); }
        },
        setTimeout: callback => callback(),
        handleDragStart() {},
        handleDragEnd() {},
        enhanceExistingChatMenu: element => enhanced.push(element),
    };
    createChatContext(context);
    vm.runInContext(extract(
        source,
        'function makeChatItemsDraggable()',
        'function setupDropZones('
    ), context);
    context.makeChatItemsDraggable();

    const newChat = item('new chat', true, true);
    elements.push(newChat);
    onMutation([{ type: 'childList', addedNodes: [newChat] }]);
    onMutation([{ type: 'childList', addedNodes: [{}] }]);

    for (const chat of [existingChat, newChat]) {
        assert.equal(chat.attributes.get('draggable'), 'true');
        assert.equal(chat.style.cursor, 'grab');
        assert.deepEqual(chat.listeners.map(listener => listener.type), ['dragstart', 'dragend']);
    }
    assert.deepEqual(enhanced, [existingChat, newChat]);
    for (const element of [userMessage, assistantMessage, searchResult]) {
        assert.equal(element.hasAttribute('draggable'), false, element.name);
        assert.equal(element.hasAttribute('data-folder-menu-enhanced'), false, element.name);
        assert.deepEqual(element.style, {}, element.name);
        assert.deepEqual(element.listeners, [], element.name);
        assert.equal(element.dataset.conversationId, '2193');
    }
    assert.deepEqual(folderChat.listeners, []);
    assert.equal(folderChat.style.cursor, 'grab');
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
    assert.match(persistenceBranch, /NotificationModal\.warning\(\(\) => window\.AurvekI18n\.t\('chat\.unsaved_response'/);
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

test('reasoning control resolves capabilities by exact llm id', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const template = fs.readFileSync(chatTemplatePath, 'utf8');
    const controlSource = extract(
        source,
        'function initializeReasoningControl()',
        '// Web Search Toggle (Plus Menu Item)'
    );
    const sendMessageSource = extract(
        source,
        'function sendMessage(',
        'function updateMessageId('
    );

    assert.match(controlSource, /model\?\.capabilities\?\.reasoning/);
    assert.match(controlSource, /\$\{currentConversationId\}:\$\{llmId\}/);
    assert.match(controlSource, /\$\{currentConversationId\}:multi:\$\{llmIds\.join\(','\)\}/);
    assert.match(controlSource, /filter\(mode => mode !== 'custom'\)/);
    assert.match(controlSource, /budget\.step \?\? 1/);
    assert.doesNotMatch(controlSource, /chat-model|Claude|textContent\s*\.includes/i);
    assert.match(controlSource, /mode !== 'custom' \|\| hasValidCustomBudget/);
    assert.match(sendMessageSource, /formData\.append\('reasoning_mode', reasoningSelection\.mode\)/);
    assert.match(sendMessageSource, /reasoningSelection\?\.mode === 'custom'/);
    assert.match(sendMessageSource, /formData\.append\('reasoning_budget_tokens'/);
    assert.doesNotMatch(sendMessageSource, /thinking_budget_tokens/);
    assert.match(template, /id="plus-reasoning"/);
    assert.match(template, /id="reasoning-budget-input"/);
});

test('Multi-AI excludes GPTSub and disabled models from the picker and visibility count', () => {
    const source = fs.readFileSync(chatPath, 'utf8');
    const managerSource = extract(
        source,
        'class MultiAiManager {',
        '// Initialize model selector, extension selector, and multi-ai manager'
    );
    const populateSource = extract(
        managerSource,
        '    populateModels() {',
        '    onCheckboxChange(event) {'
    );
    const visibilitySource = extract(
        managerSource,
        '    updateVisibility() {',
        '    getModelIds() {'
    );

    for (const modelSource of [populateSource, visibilitySource]) {
        assert.match(modelSource, /m\.machine !== 'GPTSub'/);
        assert.match(modelSource, /m\.enabled !== false && m\.enabled !== 0/);
    }
    assert.match(visibilitySource, /multiAiCandidates\.filter\(m => allowedLlms\.includes\(m\.id\)\)/);
});
