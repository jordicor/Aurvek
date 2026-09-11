const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const source = fs.readFileSync(
    path.join(repoRoot, 'data/static/js/api-credentials.js'),
    'utf8'
);
const { create: createI18n } = require(path.join(repoRoot, 'data/static/js/common/i18n.js'));

function englishI18n() {
    const read = domain => JSON.parse(fs.readFileSync(
        path.join(repoRoot, 'locales/en', `${domain}.json`),
        'utf8'
    ));
    return createI18n({
        version: 1,
        language: 'en',
        locales: { en: 'en-US' },
        resources: { en: { common: read('common'), profile: read('profile') } },
    });
}

class FakeStorage {
    constructor(initial = {}) {
        this.data = new Map(Object.entries(initial).map(([key, value]) => [key, String(value)]));
    }

    getItem(key) {
        return this.data.has(key) ? this.data.get(key) : null;
    }

    setItem(key, value) {
        this.data.set(key, String(value));
    }

    removeItem(key) {
        this.data.delete(key);
    }
}

function eventTarget(base = {}) {
    const listeners = new Map();
    return Object.assign(base, {
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(callback);
        },
        async dispatch(type, event = {}) {
            if (!event.target) event.target = this;
            for (const callback of listeners.get(type) || []) {
                await callback(event);
            }
        },
    });
}

function makeElement(extra = {}) {
    return eventTarget(Object.assign({
        value: '',
        dataset: {},
        checked: false,
        disabled: false,
        innerHTML: '',
        textContent: '',
        title: '',
        type: 'password',
        className: '',
        classList: { add() {}, remove() {} },
        querySelector() { return makeElement(); },
        replaceChildren(...children) {
            this.children = children;
            this.textContent = children.map(child => child.textContent || '').join('');
        },
        append(...children) {
            this.children = [...(this.children || []), ...children];
            this.textContent = this.children.map(child => child.textContent || '').join('');
        },
    }, extra));
}

function response(status, payload) {
    return {
        status,
        ok: status >= 200 && status < 300,
        async json() { return payload; },
    };
}

async function loadManager(options = {}) {
    const localStorage = new FakeStorage(options.localStorage);
    const sessionStorage = new FakeStorage(options.sessionStorage);
    const elements = options.elements || {};
    const radios = options.radios || [];
    const testButtons = options.testButtons || [];
    const clearButtons = options.clearButtons || [];
    const toggleButtons = options.toggleButtons || [];
    const document = eventTarget({
        getElementById(id) { return elements[id] || null; },
        createElement() { return makeElement(); },
        createTextNode(textContent) { return { textContent }; },
        querySelector() { return null; },
        querySelectorAll(selector) {
            if (selector === 'input[name="storageMode"]') return radios;
            if (selector === '.test-btn') return testButtons;
            if (selector === '.clear-btn') return clearButtons;
            if (selector === '.toggle-visibility') return toggleButtons;
            return [];
        },
    });
    const fetchCalls = [];
    let fetchHandler = options.fetchHandler || (async () => response(500, { success: false }));
    const toasts = [];
    const markedClean = [];
    const context = {
        window: { AurvekI18n: englishI18n() },
        document,
        localStorage,
        sessionStorage,
        fetch: async (...args) => {
            fetchCalls.push(args);
            return fetchHandler(...args);
        },
        NotificationModal: {
            toast(message, kind) { toasts.push({ message, kind }); },
            info() {},
            confirm() {},
        },
        FormGuard: {
            markClean(target) { markedClean.push(target); },
            watchWithListeners() {},
        },
        bootstrap: { Tooltip: function Tooltip() {} },
        btoa(value) { return Buffer.from(value).toString('base64'); },
        setTimeout() { return 1; },
        console,
    };
    vm.createContext(context);
    vm.runInContext(source, context);
    await new Promise(resolve => setImmediate(resolve));

    return {
        context,
        manager: context.window.userCredentials,
        document,
        localStorage,
        sessionStorage,
        fetchCalls,
        toasts,
        markedClean,
        setFetchHandler(handler) { fetchHandler = handler; },
    };
}

test('persistent to server migration reads the explicit old store and persists mode last', async () => {
    const env = await loadManager();
    const realKeys = { openai: 'sk-real-persistent-secret' };
    env.manager.storageMode = 'persistent';
    env.localStorage.setItem(env.manager.STORAGE_MODE_KEY, 'persistent');
    env.localStorage.setItem(
        env.manager.STORAGE_KEY,
        JSON.stringify({ storageMode: 'persistent', keys: realKeys })
    );
    env.sessionStorage.setItem(
        env.manager.STORAGE_KEY,
        JSON.stringify({ storageMode: 'session', keys: { openai: 'wrong-session-secret' } })
    );
    env.setFetchHandler(async () => response(200, { success: true }));

    const result = await env.manager.setStorageMode('server');

    assert.equal(result.success, true);
    assert.equal(env.manager.storageMode, 'server');
    assert.equal(env.localStorage.getItem(env.manager.STORAGE_MODE_KEY), 'server');
    assert.equal(env.localStorage.getItem(env.manager.STORAGE_KEY), null);
    assert.equal(env.fetchCalls.length, 1);
    const requestBody = JSON.parse(env.fetchCalls[0][1].body);
    assert.equal(requestBody.keys.openai, realKeys.openai);
    assert.notEqual(requestBody.keys.openai, 'wrong-session-secret');
});

test('failed server upload preserves the previous mode and browser keys', async () => {
    const env = await loadManager();
    const stored = JSON.stringify({
        storageMode: 'persistent',
        keys: { anthropic: 'sk-ant-real-secret' },
    });
    env.manager.storageMode = 'persistent';
    env.localStorage.setItem(env.manager.STORAGE_MODE_KEY, 'persistent');
    env.localStorage.setItem(env.manager.STORAGE_KEY, stored);
    env.setFetchHandler(async () => response(503, {
        success: false,
        message: 'temporary failure',
    }));

    const result = await env.manager.setStorageMode('server');

    assert.equal(result.success, false);
    assert.match(result.message, /temporary failure/);
    assert.equal(env.manager.storageMode, 'persistent');
    assert.equal(env.localStorage.getItem(env.manager.STORAGE_MODE_KEY), 'persistent');
    assert.equal(env.localStorage.getItem(env.manager.STORAGE_KEY), stored);
});

test('legacy masks are never uploaded during a local to server migration', async () => {
    const env = await loadManager();
    env.manager.storageMode = 'persistent';
    env.localStorage.setItem(env.manager.STORAGE_MODE_KEY, 'persistent');
    env.localStorage.setItem(env.manager.STORAGE_KEY, JSON.stringify({
        storageMode: 'persistent',
        keys: { openai: 'sk-proj-...abcd' },
    }));

    const result = await env.manager.setStorageMode('server');

    assert.equal(result.success, false);
    assert.deepEqual(Array.from(result.maskedProviders), ['openai']);
    assert.equal(env.fetchCalls.length, 0);
    assert.equal(env.manager.storageMode, 'persistent');
});

test('a legacy local mask can only migrate after the full key is re-entered', async () => {
    const openaiInput = makeElement();
    const env = await loadManager({ elements: { 'key-openai': openaiInput } });
    env.manager.storageMode = 'persistent';
    env.localStorage.setItem(env.manager.STORAGE_MODE_KEY, 'persistent');
    env.localStorage.setItem(env.manager.STORAGE_KEY, JSON.stringify({
        storageMode: 'persistent',
        keys: { openai: 'sk-proj-...abcd' },
    }));
    openaiInput.value = 'sk-full-reentered-secret';
    env.setFetchHandler(async () => response(200, { success: true }));

    const result = await env.manager.setStorageMode('server');

    assert.equal(result.success, true);
    const requestBody = JSON.parse(env.fetchCalls[0][1].body);
    assert.equal(requestBody.keys.openai, 'sk-full-reentered-secret');
    assert.notEqual(requestBody.keys.openai, 'sk-proj-...abcd');
});

test('server to browser migration requires full key re-entry and never stores masks', async () => {
    const openaiInput = makeElement({
        value: 'sk-proj-...abcd',
        dataset: { hasServerKey: 'true' },
    });
    const env = await loadManager({ elements: { 'key-openai': openaiInput } });
    env.manager.storageMode = 'server';
    env.localStorage.setItem(env.manager.STORAGE_MODE_KEY, 'server');
    env.setFetchHandler(async (url, requestOptions = {}) => {
        assert.equal(url, '/api/user-credentials');
        assert.equal(requestOptions.method, undefined);
        return response(200, {
            success: true,
            keys: { openai: 'sk-proj-...abcd' },
        });
    });

    const blocked = await env.manager.setStorageMode('persistent');
    assert.equal(blocked.success, false);
    assert.equal(blocked.reentryRequired, true);
    assert.equal(env.manager.storageMode, 'server');
    assert.equal(env.localStorage.getItem(env.manager.STORAGE_KEY), null);

    openaiInput.value = 'sk-full-reentered-openai-secret';
    delete openaiInput.dataset.hasServerKey;
    const migrated = await env.manager.setStorageMode('persistent');

    assert.equal(migrated.success, true);
    assert.equal(env.manager.storageMode, 'persistent');
    const localData = JSON.parse(env.localStorage.getItem(env.manager.STORAGE_KEY));
    assert.equal(localData.keys.openai, 'sk-full-reentered-openai-secret');
    assert.notEqual(localData.keys.openai, 'sk-proj-...abcd');
});

test('setKey and testKey reject masks and honor failed HTTP results', async () => {
    const env = await loadManager();
    env.manager.storageMode = 'server';

    const maskedSave = await env.manager.setKey('openai', 'sk-proj-...abcd');
    const maskedTest = await env.manager.testKey('openai', 'sk-proj-...abcd');
    assert.equal(maskedSave.success, false);
    assert.equal(maskedTest.success, false);
    assert.equal(maskedTest.masked, true);
    assert.equal(env.fetchCalls.length, 0);

    env.setFetchHandler(async () => response(500, {
        success: false,
        message: 'provider unavailable',
    }));
    const failedSave = await env.manager.setKey('openai', 'sk-full-real-secret');
    const failedTest = await env.manager.testKey('openai', 'sk-full-real-secret');
    assert.equal(failedSave.success, false);
    assert.equal(failedTest.success, false);
    assert.match(failedSave.message, /provider unavailable/);
    assert.match(failedTest.message, /provider unavailable/);
});

test('storage mode UI rolls back and Save All does not mark failed keys as saved', async () => {
    const persistentRadio = makeElement({ value: 'persistent' });
    const serverRadio = makeElement({ value: 'server' });
    const openaiInput = makeElement({ value: 'sk-full-real-secret' });
    const openaiStatus = makeElement();
    const saveButton = makeElement();
    const infoText = makeElement();
    const form = makeElement();
    const env = await loadManager({
        radios: [persistentRadio, serverRadio],
        elements: {
            'key-openai': openaiInput,
            'status-openai': openaiStatus,
            saveAllCredentials: saveButton,
            storageInfoText: infoText,
            apiKeysForm: form,
        },
    });
    await env.document.dispatch('DOMContentLoaded');

    env.manager.storageMode = 'persistent';
    env.localStorage.setItem(env.manager.STORAGE_MODE_KEY, 'persistent');
    env.localStorage.setItem(env.manager.STORAGE_KEY, JSON.stringify({
        storageMode: 'persistent',
        keys: { openai: 'sk-full-real-secret' },
    }));
    env.manager.syncStorageModeUI();
    env.setFetchHandler(async () => response(500, {
        success: false,
        message: 'save failed',
    }));

    serverRadio.checked = true;
    persistentRadio.checked = false;
    await serverRadio.dispatch('change', { target: serverRadio });

    assert.equal(env.manager.storageMode, 'persistent');
    assert.equal(persistentRadio.checked, true);
    assert.equal(serverRadio.checked, false);
    assert.equal(env.toasts.at(-1).kind, 'error');

    openaiInput.value = 'sk-full-real-secret';
    env.manager.setKey = async () => ({ success: false, message: 'still failed' });
    await saveButton.dispatch('click');
    assert.match(openaiStatus.innerHTML, /times-circle/);
    assert.equal(env.toasts.at(-1).kind, 'error');
    assert.equal(env.markedClean.length, 0);
});

test('an early credential draft remains guarded after initialization timers run', async () => {
    const input = makeElement();
    const form = makeElement();
    const env = await loadManager({ elements: { 'key-openai': input, apiKeysForm: form } });
    const timers = [];
    const confirmations = [];
    let reloads = 0;
    env.context.window = eventTarget(env.context.window);
    env.context.window.location = { href: '/settings', reload() { reloads += 1; } };
    env.context.location = env.context.window.location;
    env.context.AurvekI18n = env.context.window.AurvekI18n;
    env.context.requestAnimationFrame = callback => callback();
    env.context.setTimeout = callback => { timers.push(callback); return timers.length; };
    env.context.NotificationModal.confirm = (...args) => confirmations.push(args);
    vm.runInContext(fs.readFileSync(path.join(repoRoot, 'data/static/js/common/form-guard.js'), 'utf8'), env.context);
    env.context.FormGuard = env.context.window.FormGuard;
    await env.document.dispatch('DOMContentLoaded');
    assert.equal(env.context.FormGuard.anyDirty(), false);
    input.value = 'unsaved-fixture-key';
    await form.dispatch('input', { target: input });
    for (const callback of timers) callback();
    env.context.FormGuard.reloadIfClean();
    assert.equal(reloads, 0);
    assert.equal(confirmations.length, 1);
    assert.equal(env.context.FormGuard.anyDirty(), true);
    assert.equal(input.value, 'unsaved-fixture-key');
});
