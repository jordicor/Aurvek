const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const { create, createRuntime } = require('../../data/static/js/common/i18n.js');

test('session expiry uses the selected locale and keeps cancel and login actions', () => {
    const root = path.resolve(__dirname, '../..');
    const messages = language => JSON.parse(fs.readFileSync(path.join(root, `locales/${language}/common.json`), 'utf8'));
    const i18n = create({ version: 1, language: 'ja', locales: { en: 'en-US', ja: 'ja-JP' },
        resources: { en: { common: messages('en') }, ja: { common: messages('ja') } } });
    let modal;
    let nativeMessage;
    let redirects = 0;
    const context = vm.createContext({
        window: {}, document: { readyState: 'loading', addEventListener() {} },
        AurvekI18n: i18n, setTimeout() {},
        NotificationModal: { confirm(...args) { modal = args; } },
        confirm(message) { nativeMessage = message; return false; },
    });
    vm.runInContext(fs.readFileSync(path.join(root, 'data/static/js/chat/session-utils.js'), 'utf8'), context);
    const manager = context.window.SessionManager;
    manager.redirectToLogin = () => { redirects++; };
    manager.handleSessionExpiry();
    assert.equal(modal[0], 'セッションの有効期限切れ');
    assert.equal(modal[4].confirmText, 'ログイン画面へ');
    assert.equal(modal[4].cancelText, 'キャンセル');
    modal[3]();
    assert.equal(manager._modalShown, false);
    assert.equal(redirects, 0);
    modal[2]();
    assert.equal(redirects, 1);
    delete context.NotificationModal;
    manager.handleSessionExpiry();
    assert.equal(nativeMessage, messages('ja')['session.expired_confirm']);
    assert.equal(redirects, 1);
});

test('notification text retranslates in place and later plain or HTML content releases old bindings', () => {
    const root = path.resolve(__dirname, '../..');
    const payload = language => ({version: 1, language, locales: {en: 'en-US', ja: 'ja-JP'}, resources: Object.fromEntries(
        Array.from(new Set(['en', language]), code => [code, {common: JSON.parse(
            fs.readFileSync(path.join(root, `locales/${code}/common.json`), 'utf8'))}]))});
    class Node {
        constructor(id = '') { this.id = id; this.textContent = ''; this.innerHTML = ''; this.attributes = {};
            this.style = {}; this.listeners = {}; this.children = []; this.disabled = false; this.isConnected = true;
            this.classList = {add() {}, remove() {}}; }
        setAttribute(name, value) { this.attributes[name] = String(value); }
        getAttribute(name) { return this.attributes[name]; }
        addEventListener(name, callback) { this.listeners[name] = callback; }
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
        append(...children) { children.forEach(child => this.appendChild(child)); }
        querySelector(selector) { return selector === '.btn-close' ? nodes.close : this.children.find(child => selector === '.notification-toast-close' && child.className === 'notification-toast-close'); }
        cloneNode() { const clone = new Node(this.id); Object.assign(clone.style, this.style); clone.disabled = this.disabled; clone.textContent = this.textContent; return clone; }
        remove() { this.isConnected = false; }
    }
    const nodes = Object.fromEntries(['modal', 'label', 'body', 'icon', 'footer', 'confirm', 'cancel', 'close', 'container']
        .map(id => [id, new Node(id)]));
    nodes.modal.querySelector = selector => selector === '.btn-close' ? nodes.close : null;
    nodes.footer.replaceChild = (replacement, current) => {
        replacement.parentNode = nodes.footer;
        nodes[current === nodes.confirm ? 'confirm' : 'cancel'] = replacement;
    };
    nodes.confirm.parentNode = nodes.footer; nodes.cancel.parentNode = nodes.footer;
    const byId = {notificationModal: 'modal', notificationModalLabel: 'label', notificationModalBody: 'body',
        notificationModalIcon: 'icon', notificationModalFooter: 'footer', notificationModalConfirmBtn: 'confirm',
        notificationModalCancelBtn: 'cancel', notificationToastContainer: 'container'};
    const document = {readyState: 'complete', documentElement: {}, body: new Node(), head: {insertAdjacentHTML() {}},
        getElementById(id) { return nodes[byId[id]] || null; }, querySelectorAll() { return []; },
        createElement() { return new Node(); }, createTextNode(text) { const node = new Node(); node.textContent = text; return node; }};
    const runtime = createRuntime(payload('en'), document);
    const bootstrap = {Modal: class { show() {} hide() {}}};
    const context = vm.createContext({document, AurvekI18n: runtime, bootstrap, setTimeout() {}, module: {exports: {}}});
    const source = fs.readFileSync(path.join(root, 'data/static/js/common/notification-modal.js'), 'utf8');
    vm.runInContext(source + '\n;globalThis.modalApi = NotificationModal;', context);
    let confirmed = 0;
    context.modalApi.confirm(
        () => runtime.t('common.session.expired_title'),
        () => runtime.t('common.session.expired_confirm'),
        () => { confirmed++; }
    );
    nodes.confirm.disabled = true;
    const formState = {value: 'draft value', action: 'unchanged'};
    const toast = context.modalApi.toast(() => runtime.t('common.action.ok'), 'success', 0);
    const toastMessage = toast.children[1];

    runtime.setPayload(payload('ja'));

    assert.equal(nodes.label.textContent, runtime.t('common.session.expired_title'));
    assert.equal(nodes.body.textContent, runtime.t('common.session.expired_confirm'));
    assert.equal(nodes.confirm.textContent, runtime.t('common.action.confirm'));
    assert.equal(nodes.cancel.textContent, runtime.t('common.action.cancel'));
    assert.equal(toastMessage.textContent, runtime.t('common.action.ok'));
    assert.equal(nodes.confirm.disabled, true);
    assert.deepEqual(formState, {value: 'draft value', action: 'unchanged'});
    nodes.confirm.listeners.click();
    assert.equal(confirmed, 1);

    context.modalApi.update({title: '<b>plain</b>', message: 'plain message', confirmText: 'Keep'});
    context.modalApi.update({message: '<label>Form <input value="kept"></label>', allowHtml: true});
    runtime.setPayload(payload('en'));
    assert.equal(nodes.label.textContent, '<b>plain</b>');
    assert.equal(nodes.body.innerHTML, '<label>Form <input value="kept"></label>');
    assert.equal(nodes.confirm.textContent, 'Keep');
});

function languageSelectorFixture(fetchResponse) {
    const root = path.resolve(__dirname, '../..');
    const navigation = JSON.parse(fs.readFileSync(path.join(root, 'locales/en/navigation.json'), 'utf8'));
    const i18n = create({version: 1, language: 'en', locales: {en: 'en-US'},
        resources: {en: {navigation}}});
    const makeOption = language => {
        const classes = new Set();
        const check = {hidden: true};
        const listeners = {};
        return {
            dataset: {uiLanguage: language}, attributes: {}, classList: {
                toggle(name, active) { if (active) classes.add(name); else classes.delete(name); },
                contains(name) { return classes.has(name); },
            },
            setAttribute(name, value) { this.attributes[name] = String(value); },
            querySelector(selector) { return selector === '.ui-language-check' ? check : null; },
            addEventListener(type, callback) { listeners[type] = callback; },
            click() { listeners.click({preventDefault() {}, stopPropagation() {}}); },
            check,
        };
    };
    const options = ['en', 'es', 'ja'].map(makeOption);
    const settings = {value: 'en'};
    const elements = {uiLanguage: settings, 'message-text': {value: ''}};
    const document = {
        readyState: 'complete', documentElement: {lang: 'en'},
        querySelectorAll: selector => selector === '.ui-language-option' ? options : [],
        querySelector: () => null,
        getElementById: id => elements[id] || null,
    };
    const requests = [];
    const cleaned = [];
    let reloads = 0;
    const messages = [];
    const storage = new Map();
    const window = {
        AurvekI18n: i18n,
        __userInitPromise: Promise.resolve({session: {csrf_token: 'init-csrf'}}),
        currentConversationId: 42,
        FormGuard: {
            markFieldsClean(...args) { cleaned.push(args); },
            reloadIfClean() { reloads += 1; },
        },
        location: {reload() { reloads += 1; }},
        fetch(url, init) { requests.push({url, init}); return fetchResponse(); },
    };
    const context = vm.createContext({
        window, document, localStorage: {setItem(key, value) { storage.set(key, value); }},
        NotificationModal: {
            warning(title, message) { messages.push({type: 'warning', title, message}); },
            error(title, message) { messages.push({type: 'error', title, message}); },
        },
        Array, Boolean, Error, JSON, Promise, Set,
    });
    vm.runInContext(fs.readFileSync(path.join(root, 'data/static/js/common/ui-language.js'), 'utf8'), context);
    return {options, settings, elements, requests, cleaned, messages, storage, reloads: () => reloads};
}

test('native language selector updates only the saved Settings field', async () => {
    const fixture = languageSelectorFixture(async () => ({
        ok: true, json: async () => ({success: true, ui_language: 'es'}),
    }));
    fixture.options[1].click();
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(fixture.settings.value, 'es');
    assert.equal(fixture.cleaned.length, 1);
    assert.equal(fixture.cleaned[0][0], '#editProfileForm');
    assert.deepEqual(Array.from(fixture.cleaned[0][1]), ['ui_language']);
    assert.equal(fixture.reloads(), 1);
});

test('native language selector persists once and preserves a newer Settings language draft', async () => {
    let resolveFetch;
    const response = new Promise(resolve => { resolveFetch = resolve; });
    const fixture = languageSelectorFixture(() => response);
    assert.equal(fixture.options[0].classList.contains('active'), true);

    fixture.options[1].click();
    fixture.options[2].click();
    fixture.settings.value = 'ja';
    resolveFetch({ok: true, json: async () => ({success: true, ui_language: 'es'})});
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(fixture.requests.length, 1);
    assert.equal(fixture.requests[0].url, '/api/ui-language');
    assert.equal(fixture.requests[0].init.headers['X-GPTSub-CSRF'], 'init-csrf');
    assert.equal(fixture.requests[0].init.body, JSON.stringify({ui_language: 'es'}));
    assert.equal(fixture.options[1].classList.contains('active'), true);
    assert.equal(fixture.settings.value, 'ja');
    assert.deepEqual(fixture.cleaned, []);
    assert.equal(fixture.storage.get('restoreConversationId'), '42');
    assert.equal(fixture.reloads(), 1);
});

test('native language selector blocks chat drafts and leaves selection unchanged on failure', async () => {
    const fixture = languageSelectorFixture(async () => ({ok: false, json: async () => ({})}));
    fixture.elements['message-text'].value = 'draft';
    fixture.options[1].click();
    assert.equal(fixture.requests.length, 0);
    assert.equal(fixture.messages[0].type, 'warning');

    fixture.elements['message-text'].value = '';
    fixture.options[1].click();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(fixture.options[0].classList.contains('active'), true);
    assert.equal(fixture.settings.value, 'en');
    assert.deepEqual(fixture.cleaned, []);
    assert.equal(fixture.reloads(), 0);
    assert.equal(fixture.messages.at(-1).type, 'error');
});
