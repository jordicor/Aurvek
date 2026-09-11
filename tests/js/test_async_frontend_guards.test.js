const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { create: createI18n } = require('../../data/static/js/common/i18n.js');

const repoRoot = path.resolve(__dirname, '..', '..');
const llmAdminI18n = createI18n({
    version: 1,
    language: 'en',
    locales: { en: 'en-US' },
    resources: { en: {
        common: JSON.parse(fs.readFileSync(path.join(repoRoot, 'locales/en/common.json'), 'utf8')),
        admin_models: JSON.parse(fs.readFileSync(path.join(repoRoot, 'locales/en/admin_models.json'), 'utf8')),
    } },
});

function eventTarget(base = {}) {
    const listeners = new Map();
    return Object.assign(base, {
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(callback);
        },
        removeEventListener(type, callback) {
            const callbacks = listeners.get(type) || [];
            listeners.set(type, callbacks.filter(item => item !== callback));
        },
        dispatch(type, event = {}) {
            const results = [];
            for (const callback of listeners.get(type) || []) results.push(callback(event));
            return Promise.all(results);
        },
    });
}

function makeClassList() {
    const classes = new Set();
    return {
        add(...names) { names.forEach(name => classes.add(name)); },
        remove(...names) { names.forEach(name => classes.delete(name)); },
        contains(name) { return classes.has(name); },
        replace(previous, next) { if (classes.delete(previous)) classes.add(next); },
    };
}

function formGuardFixture(inputs) {
    const form = eventTarget({
        querySelectorAll(selector) {
            const checkboxes = selector.match(/^input\[type="checkbox"\]\[name="(.*)"\]$/);
            return checkboxes
                ? inputs.filter(input => input.type === 'checkbox' && input.name === checkboxes[1])
                : inputs;
        },
    });
    const document = eventTarget({
        querySelector() { return form; },
        querySelectorAll() { return []; },
        getElementById(id) { return inputs.find(input => input.id === id) || eventTarget(); },
    });
    const window = eventTarget({ location: { href: '', reload() {} } });
    const context = {
        window,
        document,
        CSS: { escape: value => value },
        NotificationModal: { confirm() {} },
        requestAnimationFrame: callback => callback(),
        queueMicrotask,
        Promise,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(repoRoot, 'data/static/js/common/form-guard.js'), 'utf8'),
        context
    );
    context.FormGuard = window.FormGuard;
    return { form, context, guard: window.FormGuard };
}

test('FormGuard keeps AJAX failures dirty regardless of listener order', async () => {
    const input = {
        name: 'title',
        value: 'before',
        type: 'text',
        disabled: false,
        tagName: 'INPUT',
    };
    const form = eventTarget({
        querySelectorAll() { return [input]; },
    });
    const document = eventTarget({
        querySelector() { return form; },
        querySelectorAll() { return []; },
    });
    const window = eventTarget({ location: { href: '', reload() {} } });
    const context = {
        window,
        document,
        CSS: { escape: value => value },
        NotificationModal: { confirm() {} },
        requestAnimationFrame: callback => callback(),
        queueMicrotask,
        Promise,
        Set,
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(repoRoot, 'data/static/js/common/form-guard.js'),
            'utf8'
        ),
        context
    );

    window.FormGuard.watch(form);
    input.value = 'after';

    // Register after FormGuard to exercise the ordering that used to leave
    // _fgSubmitting stuck at true.
    form.addEventListener('submit', event => event.preventDefault());
    const ajaxEvent = {
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
    };
    form.dispatch('submit', ajaxEvent);
    await Promise.resolve();

    assert.equal(window.FormGuard.anyDirty(), true);

    window.FormGuard.markClean(form);
    const nativeEvent = {
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
    };
    // Remove the AJAX listener by using a second watched form.
    const nativeInput = { ...input, value: 'before-native' };
    const nativeForm = eventTarget({ querySelectorAll() { return [nativeInput]; } });
    window.FormGuard.watch(nativeForm);
    nativeInput.value = 'after-native';
    nativeForm.dispatch('submit', nativeEvent);
    await Promise.resolve();
    assert.equal(window.FormGuard.isDirty(nativeForm), true);
    assert.equal(window.FormGuard.anyDirty(), false);

    window.FormGuard.resumeAfterSubmit(nativeForm);
    assert.equal(window.FormGuard.anyDirty(), true);
});

test('FormGuard accepts async settings in either order and protects later selections', () => {
    for (const order of [['voice', 'engine'], ['engine', 'voice']]) {
        const voice = { name: 'sample_voice_id', tagName: 'SELECT', value: '' };
        const native = { name: 'wsEngine', type: 'radio', value: 'native', checked: false };
        const perplexity = { name: 'wsEngine', type: 'radio', value: 'perplexity', checked: false };
        const { form, guard } = formGuardFixture([voice, native, perplexity]);
        guard.watch(form);

        for (const field of order) {
            if (field === 'voice') {
                voice.value = 'saved-voice';
                guard.markFieldsClean(form, ['sample_voice_id']);
            } else {
                native.checked = true;
                guard.markFieldsClean(form, ['wsEngine']);
            }
            assert.equal(guard.anyDirty(), false);
        }

        voice.value = 'another-voice';
        assert.equal(guard.anyDirty(), true);
        voice.value = 'saved-voice';
        assert.equal(guard.anyDirty(), false);
        native.checked = false;
        perplexity.checked = true;
        assert.equal(guard.anyDirty(), true);
        native.checked = true;
        perplexity.checked = false;
        assert.equal(guard.anyDirty(), false);
    }
});

test('FormGuard field hydration preserves other edits, exclusions and manual dirty state', () => {
    const timezone = { name: 'timezone_name', type: 'text', value: 'Europe/Madrid' };
    const first = { name: 'label', type: 'text', value: '' };
    const literalSuffix = { name: 'label__2', type: 'text', value: 'saved suffix' };
    const duplicate = { name: 'label', type: 'text', value: '' };
    const excluded = { name: 'ignored', type: 'text', value: 'before' };
    const { form, guard } = formGuardFixture([timezone, first, literalSuffix, duplicate, excluded]);
    guard.watch(form, { exclude: ['ignored'] });

    timezone.value = 'America/New_York';
    literalSuffix.value = 'user edit';
    first.value = 'first loaded value';
    duplicate.value = 'second loaded value';
    excluded.value = 'after';
    guard.markFieldsClean(form, ['label', 'ignored']);
    assert.equal(guard.anyDirty(), true);
    timezone.value = 'Europe/Madrid';
    assert.equal(guard.anyDirty(), true, 'a real field named label__2 must keep its own baseline');
    literalSuffix.value = 'saved suffix';
    assert.equal(guard.anyDirty(), false);

    duplicate.disabled = true;
    guard.markFieldsClean(form, ['label']);
    assert.equal(guard.anyDirty(), false, 'a removed duplicate value must leave the baseline');
    guard.markDirty(form);
    first.value = 'new loaded value';
    guard.markFieldsClean(form, ['label']);
    assert.equal(guard.anyDirty(), true);
    guard.markClean(form);
    assert.equal(guard.anyDirty(), false);
});

test('profile phone initialization accepts formatting but preserves an early user edit', async () => {
    for (const editedDuringLoad of [false, true]) {
        const phone = eventTarget({
            id: 'phone', name: 'phone_number', type: 'tel', value: '+34931234567',
        });
        const { form, context, guard } = formGuardFixture([phone]);
        let finishLoading;
        context.window.AurvekI18n = { locale: 'ja-JP' };
        context.window.intlTelInputGlobals = { getCountryData: () => [{ iso2: 'de', name: 'Germany' }] };
        context.window.intlTelInput = (_input, options) => {
            assert.equal(options.localizedCountries.de, 'ドイツ');
            return { promise: new Promise(resolve => { finishLoading = resolve; }) };
        };
        vm.runInContext(
            fs.readFileSync(path.join(repoRoot, 'data/static/js/edit_profile.js'), 'utf8'),
            context
        );
        context.initPhoneInput();
        guard.watch(form);

        if (editedDuringLoad) {
            phone.value = '+34937654321';
            await phone.dispatch('input');
        }
        phone.value = editedDuringLoad ? '937 65 43 21' : '931 23 45 67';
        finishLoading();
        await Promise.resolve();
        assert.equal(guard.anyDirty(), editedDuringLoad);
    }
});

test('settings web search initialization accepts its saved engine without clearing profile edits', async () => {
    const timezone = { name: 'timezone_name', type: 'text', value: 'Europe/Madrid' };
    const native = eventTarget({ name: 'wsEngine', type: 'radio', value: 'native', checked: false });
    const perplexity = eventTarget({ name: 'wsEngine', type: 'radio', value: 'perplexity', checked: false });
    const { form, context, guard } = formGuardFixture([timezone, native, perplexity]);
    const elements = {
        wsEngineNative: eventTarget({ querySelector: () => native, classList: makeClassList() }),
        wsEnginePerplexity: eventTarget({ querySelector: () => perplexity, classList: makeClassList() }),
    };
    context.document.getElementById = id => elements[id];
    let finishLoading;
    context.fetch = () => new Promise(resolve => { finishLoading = resolve; });
    const template = fs.readFileSync(path.join(repoRoot, 'templates/settings.html'), 'utf8');
    const initializer = template.match(/\(async function initWebSearchSettings\(\) \{[\s\S]*?\n    \}\)\(\);/);
    assert.ok(initializer);
    const initializing = vm.runInContext(initializer[0], context);
    assert.equal(native.disabled, true);
    assert.equal(perplexity.disabled, true);
    guard.watch(form);
    timezone.value = 'America/New_York';

    finishLoading({ ok: true, json: async () => ({ web_search_mode: 'native', perplexity_available: true }) });
    await initializing;
    assert.equal(native.disabled, false);
    assert.equal(perplexity.disabled, false);
    assert.equal(native.checked, true);
    assert.equal(guard.anyDirty(), true);
    timezone.value = 'Europe/Madrid';
    assert.equal(guard.anyDirty(), false);
});

test('FullsizeViewer ignores stale image loads and deletes the displayed resource', () => {
    const translatedLabel = eventTarget({ textContent: '' });
    const elements = {
        '.visually-hidden': eventTarget({ textContent: '' }),
        '.fullsize-viewer-backdrop': eventTarget({ style: {} }),
        '.fullsize-viewer-close': eventTarget({ style: {} }),
        '.fullsize-viewer-prev': eventTarget({ style: {} }),
        '.fullsize-viewer-next': eventTarget({ style: {} }),
        '.fullsize-viewer-download': eventTarget({ style: {}, querySelector: () => translatedLabel }),
        '.fullsize-viewer-delete': eventTarget({ style: {}, disabled: false, querySelector: () => translatedLabel }),
        '.fullsize-viewer-controls': eventTarget({ style: {} }),
        '.fullsize-viewer-spinner': eventTarget({ style: {} }),
        '.fullsize-viewer-image': eventTarget({ style: {}, src: '' }),
    };
    const container = eventTarget({
        classList: makeClassList(),
        querySelector(selector) { return elements[selector]; },
    });

    let injected = false;
    const body = {
        insertAdjacentHTML() { injected = true; },
        appendChild() {},
        removeChild() {},
    };
    const document = eventTarget({
        body,
        getElementById(id) {
            return id === 'fullsizeViewer' && injected ? container : null;
        },
        createElement() {
            return { click() {}, href: '', download: '' };
        },
    });

    const imageInstances = [];
    class FakeImage {
        constructor() {
            this.onload = null;
            this.onerror = null;
            this._src = '';
            this.currentSrc = '';
            imageInstances.push(this);
        }
        set src(value) {
            this._src = value;
            this.currentSrc = value;
        }
        get src() { return this._src; }
        load() { if (this.onload) this.onload.call(this); }
    }

    const window = { AurvekI18n: { t: key => key } };
    const context = { window, document, Image: FakeImage, Date, Object };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(
            path.join(repoRoot, 'data/static/js/fullsize-viewer.js'),
            'utf8'
        ),
        context
    );

    const deleted = [];
    const images = [
        { id: 'first', url: '/first.webp' },
        { id: 'second', url: '/second.webp' },
    ];
    window.FullsizeViewer.init({
        showNav: true,
        showDelete: true,
        transformUrl: false,
        images,
        onDelete(url, index, imageData) {
            deleted.push({ url, index, id: imageData.id });
        },
    });

    window.FullsizeViewer.show('/first.webp', 0);
    const staleLoader = imageInstances.at(-1);
    window.FullsizeViewer.next();
    const currentLoader = imageInstances.at(-1);
    currentLoader.load();
    staleLoader.load();

    assert.equal(window.FullsizeViewer.getDisplayedResource().imageData.id, 'second');
    elements['.fullsize-viewer-delete'].dispatch('click');
    assert.deepEqual(deleted, [{ url: '/second.webp', index: 1, id: 'second' }]);
});

test('audio recorder and folder chat code keep single-operation invariants', () => {
    const audioSource = fs.readFileSync(
        path.join(repoRoot, 'data/static/js/chat/audio.js'),
        'utf8'
    );
    const foldersSource = fs.readFileSync(
        path.join(repoRoot, 'data/static/js/chat/folders.js'),
        'utf8'
    );

    assert.equal(
        (audioSource.match(/mediaDevices\.getUserMedia\(\{ audio: true \}\)/g) || []).length,
        1
    );
    assert.equal(
        (audioSource.match(/addEventListener\('click', toggleAudioRecording\)/g) || []).length,
        1
    );
    assert.match(audioSource, /new AbortController\(\)/);
    assert.match(audioSource, /URL\.revokeObjectURL\(url\)/);
    assert.match(audioSource, /session\.recorder\.onstop = \(\) => handleAudioStop\(session\)/);

    assert.match(foldersSource, /const targetFolderId = incognito \? null : currentSelectedFolderId/);
    assert.match(foldersSource, /await loadFolderChats\(targetFolderId/);
    assert.match(foldersSource, /await updateFolderConversationCount\(targetFolderId\)/);
    assert.equal(
        (foldersSource.match(/originalStartNewConversation\(promptId, options\)/g) || []).length,
        1
    );
});

function llmAdminFixture() {
    const template = fs.readFileSync(path.join(repoRoot, 'templates/llms/llm_list.html'), 'utf8');
    const elements = Object.fromEntries([
        'providerFilter', 'searchFilter', 'visionFilter', 'enabledFilter', 'sortBy',
        'selectedCount', 'bulkSelectionCount', 'statsSelected', 'enableSelectedButton',
        'disableSelectedButton', 'clearSelectionButton', 'deleteSelectedButton',
        'selectAllCheckbox', 'statsFiltered', 'filteredCount', 'totalCount', 'visionCount',
        'enabledCount', 'modelUpdateStatus', 'llmCatalogControls',
    ].map(id => [id, { value: '', style: {}, textContent: '', disabled: false, checked: false }]));
    elements.providerFilter.value = 'GPT';
    elements.sortBy.value = 'model';
    const rows = [
        [1, 'GPT', 'alpha', 'yes', 'no', 'yes'],
        [2, 'GPT', 'beta', 'yes', 'no', 'no'],
        [3, 'Claude', 'gamma', 'yes', 'no', 'yes'],
        [4, 'GPTSub', 'delta', 'yes', 'yes', 'no'],
        [5, 'GPT', 'epsilon', 'no', 'no', 'yes'],
    ].map(([id, provider, model, enabled, managed, deletable]) => {
        const row = { dataset: { id: String(id), provider, model, modelId: model,
            enabled, managed, deletable, vision: 'no' }, style: {} };
        row.checkbox = { value: String(id), checked: false, disabled: managed === 'yes', closest: () => row };
        const icon = { className: '' };
        row.toggle = { disabled: false, attributes: {}, classList: makeClassList(),
            setAttribute(name, value) { this.attributes[name] = value; },
            querySelector: () => icon };
        row.toggle.classList.toggle = function (name, enabled) {
            if (enabled) this.add(name); else this.remove(name);
        };
        row.querySelector = selector => ({
            '.llm-checkbox': row.checkbox,
            '.llm-toggle': managed === 'yes' ? null : row.toggle,
            'td strong': { textContent: model },
        })[selector];
        return row;
    });
    elements.llmTableBody = { appendChild(row) { rows.splice(rows.indexOf(row), 1); rows.push(row); } };
    const storage = new Map();
    const requests = [];
    const scrolls = [];
    const document = eventTarget({
        getElementById: id => elements[id],
        querySelector: selector => rows.find(row => selector.endsWith(`[data-id="${row.dataset.id}"]`)),
        querySelectorAll: selector => selector === '.llm-row' ? rows : rows.map(row => row.checkbox)
            .filter(cb => selector === '.llm-checkbox:checked' ? cb.checked : !cb.disabled),
    });
    const context = {
        AurvekI18n: llmAdminI18n,
        document,
        autoUpdateLlmCatalog: async () => null,
        window: { AurvekI18n: llmAdminI18n,
            scrollX: 12, scrollY: 360, scrollTo: (...args) => scrolls.push(args),
            location: { reload() { assert.fail('Model updates must not reload the page'); } } },
        sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
        NotificationModal: { confirm() { assert.fail('Mixed or synced selection cannot be deleted'); } },
        async fetch(url, options) {
            const body = JSON.parse(options.body);
            requests.push({ url, method: options.method, ...body });
            return { ok: !context.rejectWrite, json: async () => context.rejectWrite
                ? { error: 'Update refused' }
                : { models: body.llm_ids.map(id => ({ id, enabled: body.enabled })) } };
        },
    };
    vm.createContext(context);
    vm.runInContext(template.match(/<script>([\s\S]*?)<\/script>/)[1], context);
    const initialized = document.dispatch('DOMContentLoaded');
    return { context, elements, rows, requests, scrolls, storage, initialized };
}

test('LLM admin updates individual and bulk availability in place and preserves state after a refusal', async () => {
    const { context, elements, rows, requests, scrolls, storage, initialized } = llmAdminFixture();
    await initialized;
    const manual = rows.find(row => row.dataset.id === '1');
    const rowOrder = rows.map(row => row.dataset.id);
    const filters = storage.get('aurvek.llmFilters');
    const updating = context.toggleLlmEnabled(1);
    assert.equal(manual.checkbox.disabled, true);
    await updating;
    assert.equal(manual.dataset.enabled, 'no');
    assert.equal(manual.toggle.attributes['aria-pressed'], 'false');
    assert.equal(manual.toggle.classList.contains('btn-outline-secondary'), true);
    assert.equal(manual.checkbox.disabled, false);

    elements.selectAllCheckbox.checked = true;
    context.toggleSelectAll();
    await context.setSelectedEnabled(true);
    assert.deepEqual(requests, [
        { url: '/api/admin/llms/enabled', method: 'PATCH', llm_ids: [1], enabled: false },
        { url: '/api/admin/llms/enabled', method: 'PATCH', llm_ids: [1, 5], enabled: true },
    ]);
    assert.equal(manual.dataset.enabled, 'yes');
    assert.equal(manual.toggle.attributes['aria-pressed'], 'true');
    assert.equal(manual.checkbox.checked, true);
    assert.equal(elements.selectedCount.textContent, '3');
    assert.equal(elements.enabledCount.textContent, '5');
    assert.equal(elements.modelUpdateStatus.textContent, '2 models enabled.');
    assert.deepEqual(rows.map(row => row.dataset.id), rowOrder);
    assert.equal(storage.get('aurvek.llmFilters'), filters);
    assert.deepEqual(scrolls, [[12, 360], [12, 360]]);

    context.rejectWrite = true;
    await context.toggleLlmEnabled(1);
    assert.equal(manual.dataset.enabled, 'yes');
    assert.equal(manual.toggle.attributes['aria-pressed'], 'true');
    assert.equal(manual.checkbox.checked, true);
    assert.equal(manual.checkbox.disabled, false);
    assert.equal(elements.enabledCount.textContent, '5');
    assert.match(elements.modelUpdateStatus.textContent, /Could not save changes: Update refused/);
    assert.equal(scrolls.length, 2);
});

test('LLM admin selects only visible editable models and prevents deleting mixed synced selections', async () => {
    const { context, elements, rows, requests, initialized } = llmAdminFixture();
    await initialized;
    elements.selectAllCheckbox.checked = true;
    context.toggleSelectAll();
    assert.deepEqual(rows.filter(row => row.checkbox.checked).map(row => row.dataset.id), ['1', '2', '5']);
    assert.equal(elements.deleteSelectedButton.disabled, true);
    assert.match(elements.deleteSelectedButton.title, /Only manual models/);
    context.deleteSelectedLlms();
    assert.equal(requests.length, 0);

    elements.providerFilter.value = '';
    context.applyFilters();
    assert.equal(elements.selectAllCheckbox.indeterminate, true);
    elements.selectAllCheckbox.checked = true;
    context.toggleSelectAll();
    assert.equal(rows.find(row => row.dataset.id === '3').checkbox.checked, true);
    assert.equal(rows.find(row => row.dataset.id === '4').checkbox.checked, false);

    elements.searchFilter.value = 'alpha';
    context.applyFilters();
    assert.deepEqual(rows.filter(row => row.checkbox.checked).map(row => row.dataset.id), ['1']);
    assert.equal(elements.deleteSelectedButton.disabled, false);
    assert.equal(elements.selectedCount.textContent, '1');
    assert.equal(elements.selectAllCheckbox.checked, true);
    assert.equal(elements.selectAllCheckbox.indeterminate, false);
});

function loadCatalogAutoUpdate({ context, elements }) {
    elements.catalogAutoUpdate = { classList: makeClassList() };
    elements.catalogAutoUpdate.classList.add('alert-info');
    elements.catalogAutoUpdateText = { textContent: '' };
    elements.catalogAutoUpdateSpinner = { hidden: false };
    const template = fs.readFileSync(path.join(repoRoot, 'templates/llms/catalog_auto_update.html'), 'utf8');
    vm.runInContext(template.match(/<script>([\s\S]*?)<\/script>/)[1], context);
}

test('catalog entry checks skip fresh catalogs and fetch one rendered page after a refresh', async () => {
    for (const refreshed of [false, true]) {
        const fixture = llmAdminFixture();
        await fixture.initialized;
        loadCatalogAutoUpdate(fixture);
        const { context, elements } = fixture;
        const requests = [];
        const freshPage = { getElementById: id => id === 'catalogAutoUpdate' ? {} : null };
        context.window.location.href = 'https://aurvek.example.test/models';
        context.DOMParser = class {
            parseFromString(html, type) {
                assert.equal(html, 'Fresh server-rendered catalog');
                assert.equal(type, 'text/html');
                return freshPage;
            }
        };
        context.fetch = async (url, options) => {
            requests.push({ url, ...options });
            return {
                ok: true,
                json: async () => ({ success: true, refreshed, results: {} }),
                text: async () => 'Fresh server-rendered catalog',
            };
        };
        const result = await context.autoUpdateLlmCatalog();
        assert.deepEqual(requests, [
            { url: '/api/admin/llms/sync-if-stale', method: 'POST' },
            ...(refreshed ? [{ url: context.window.location.href, cache: 'no-store' }] : []),
        ]);
        assert.equal(result.freshPage, refreshed ? freshPage : undefined);
        assert.equal(elements.catalogAutoUpdateSpinner.hidden, true);
        assert.equal(elements.catalogAutoUpdate.classList.contains('alert-success'), true);
    }
});

test('catalog check failure unlocks model management and leaves saved models usable', async () => {
    const fixture = llmAdminFixture();
    await fixture.initialized;
    loadCatalogAutoUpdate(fixture);
    const { context, elements, rows } = fixture;
    const originalRows = rows.slice();
    let rejectCheck;
    context.fetch = () => new Promise((_, reject) => { rejectCheck = reject; });
    elements.llmCatalogControls.disabled = true;
    const initializing = context.document.dispatch('DOMContentLoaded');
    assert.equal(elements.llmCatalogControls.disabled, true);
    rejectCheck(new Error('Network unavailable'));
    await initializing;

    assert.equal(elements.llmCatalogControls.disabled, false);
    assert.equal(elements.catalogAutoUpdateSpinner.hidden, true);
    assert.equal(elements.catalogAutoUpdate.classList.contains('alert-warning'), true);
    assert.match(elements.catalogAutoUpdateText.textContent, /still manage saved models/);
    assert.deepEqual(rows, originalRows);
    assert.equal(elements.providerFilter.value, 'GPT');
    elements.selectAllCheckbox.checked = true;
    context.toggleSelectAll();
    assert.equal(elements.selectedCount.textContent, '3');
    assert.equal(elements.disableSelectedButton.disabled, false);
});
