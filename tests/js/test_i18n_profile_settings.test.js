const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const { create: createI18n } = require(path.join(repoRoot, 'data/static/js/common/i18n.js'));

function catalog(language, domain) {
    return JSON.parse(fs.readFileSync(
        path.join(repoRoot, 'locales', language, `${domain}.json`),
        'utf8'
    ));
}

function runtime(language = 'es') {
    return createI18n({
        version: 1,
        language,
        locales: { en: 'en-US', es: 'es-ES' },
        resources: {
            en: { common: catalog('en', 'common'), profile: catalog('en', 'profile') },
            es: { common: catalog('es', 'common'), profile: catalog('es', 'profile') },
        },
    });
}

test('voice sample categories exist in the profile catalog for every UI language', () => {
    const categoryKeys = [
        'children', 'finance', 'relaxation', 'casual', 'drama', 'storytelling',
        'advertising', 'science', 'education', 'corporate', 'mystery', 'sports',
    ];
    for (const language of ['en', 'es', 'ja', 'fr', 'pt', 'it', 'de']) {
        const messages = catalog(language, 'profile');
        for (const key of categoryKeys) {
            assert.equal(typeof messages[`voice.category.${key}`], 'string', `${language}: ${key}`);
            assert.notEqual(messages[`voice.category.${key}`].trim(), '', `${language}: ${key}`);
        }
    }
});

function eventTarget(base = {}) {
    const listeners = new Map();
    return Object.assign(base, {
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(callback);
        },
        removeEventListener(type, callback) {
            listeners.set(type, (listeners.get(type) || []).filter(item => item !== callback));
        },
        dispatchEvent(event) {
            return (listeners.get(event.type) || []).map(callback => callback(event));
        },
    });
}

function guardedForm(input) {
    return eventTarget({
        querySelectorAll() { return [input]; },
    });
}

test('device time zone selects an existing option or adds a missing browser alias once', () => {
    for (const initialOptions of [[], [{ value: 'Europe/Madrid', text: 'Europe / Madrid' }]]) {
        const changes = [];
        const select = eventTarget({
            options: initialOptions,
            add(option) { this.options.push(option); },
            set value(value) { this.selected = this.options.find(option => option.value === value)?.value || ''; },
            get value() { return this.selected; },
        });
        for (const type of ['input', 'change']) select.addEventListener(type, event => changes.push(event.type));
        const button = eventTarget();
        const status = {};
        const context = vm.createContext({
            window: { AurvekI18n: runtime() },
            document: {
                addEventListener() {},
                getElementById(id) {
                    return {
                        timezoneName: select, useDeviceTimezoneButton: button, deviceTimezoneStatus: status,
                        alterEgo: eventTarget(), deleteAccountBtn: eventTarget(),
                    }[id];
                },
            },
            Intl: { DateTimeFormat: () => ({ resolvedOptions: () => ({ timeZone: 'Europe/Madrid' }) }) },
            Option: function(text, value) { return { text, value }; },
            Event: function(type) { return { type }; },
        });
        vm.runInContext(fs.readFileSync(path.join(repoRoot, 'data/static/js/edit_profile.js'), 'utf8'), context);
        context.initTimezonePreference();
        button.dispatchEvent({ type: 'click' });
        button.dispatchEvent({ type: 'click' });
        assert.equal(select.value, 'Europe/Madrid');
        assert.equal(select.options.length, 1);
        assert.equal(select.options[0].text, 'Europe / Madrid');
        assert.deepEqual(changes, ['input', 'change', 'input', 'change']);
        assert.equal(status.textContent, runtime().t('profile.timezone.detected', { timezone: 'Europe/Madrid' }));
    }
});

function timezoneSearchHarness() {
    const element = (base = {}) => eventTarget(Object.assign({
        children: [], textContent: '', disabled: false, value: '',
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        setAttribute(name, value) { this[name] = value; },
        focus() { this.focused = true; },
    }, base));
    const elements = Object.fromEntries([
        'timezoneName', 'useDeviceTimezoneButton', 'deviceTimezoneStatus', 'timezoneCitySearch',
        'timezoneCityInput', 'timezoneCitySearchButton', 'timezoneCityStatus', 'timezoneCityResults',
        'alterEgo', 'deleteAccountBtn',
    ].map(id => [id, element()]));
    Object.assign(elements.timezoneName, {
        name: 'timezone_name', tagName: 'SELECT', value: '', options: [],
        add(option) { this.options.push(option); },
    });
    const inputMarkup = fs.readFileSync(path.join(repoRoot, 'templates/profile/_timezone_preference.html'), 'utf8')
        .match(/<input[^>]+id="timezoneCityInput"[^>]*>/)[0];
    elements.timezoneCityInput.name = inputMarkup.match(/\sname="([^"]*)"/)?.[1] || '';
    const form = eventTarget({ querySelectorAll() { return [elements.timezoneName, elements.timezoneCityInput]; } });
    const requests = [];
    const window = eventTarget({ AurvekI18n: runtime() });
    const context = vm.createContext({
        window,
        document: eventTarget({
            getElementById(id) { return elements[id]; },
            createElement() { return element(); },
            querySelectorAll() { return []; },
        }),
        Option: function(text, value) { return { text, value }; },
        Event: function(type) { return { type }; },
        secureFetch(url) {
            return new Promise(resolve => requests.push({
                url,
                respond(results = [], ok = true) { resolve({ ok, json: async () => ({ results }) }); },
            }));
        },
        queueMicrotask, Promise, Set,
    });
    for (const relative of ['common/form-guard.js', 'edit_profile.js']) {
        vm.runInContext(fs.readFileSync(path.join(repoRoot, 'data/static/js', relative), 'utf8'), context);
    }
    window.FormGuard.watch(form);
    context.initTimezonePreference();
    return {
        elements, requests, isDirty: () => window.FormGuard.isDirty(form),
        type(value) {
            elements.timezoneCityInput.value = value;
            elements.timezoneCityInput.dispatchEvent({ type: 'input' });
        },
        search() { return elements.timezoneCitySearchButton.dispatchEvent({ type: 'click' })[0]; },
    };
}

const cordobaCities = [
    { id: 1, city: 'Córdoba', region: 'Andalucía', country: 'España', timezone: 'Europe/Madrid' },
    { id: 2, city: 'Córdoba', region: 'Córdoba', country: 'Argentina', timezone: 'America/Argentina/Cordoba' },
];

test('city lookup stays clean until a result is explicitly selected and manual selection clears its message', async () => {
    const h = timezoneSearchHarness();
    const e = h.elements;
    e.timezoneCitySearch.open = true;
    h.type('Córdoba');
    assert.equal(h.requests.length, 0);
    const search = h.search();
    assert.equal(h.requests[0].url, '/api/profile/timezone-cities?q=C%C3%B3rdoba');
    h.requests[0].respond(cordobaCities);
    await search;
    assert.equal(e.timezoneCityResults.children.length, 2);
    assert.equal(e.timezoneName.value, '');
    assert.equal(h.isDirty(), false);
    const spanishRow = e.timezoneCityResults.children[0];
    assert.equal(spanishRow.children[0].children[0].textContent, 'Córdoba · Andalucía · España');
    spanishRow.children[1].dispatchEvent({ type: 'click' });
    assert.equal(e.timezoneName.value, 'Europe/Madrid');
    assert.equal(h.isDirty(), true);
    assert.equal(e.timezoneCitySearch.open, false);
    assert.equal(e.timezoneName.focused, true);
    assert.equal(e.deviceTimezoneStatus.textContent, runtime().t('profile.timezone.city_search_selected', {
        city: 'Córdoba · Andalucía · España', timezone: 'Europe/Madrid',
    }));
    e.timezoneName.value = 'America/New_York';
    e.timezoneName.dispatchEvent({ type: 'change' });
    assert.equal(e.deviceTimezoneStatus.textContent, '');
});

test('Enter searches without submitting or duplicating and stale city responses cannot replace current results', async () => {
    const h = timezoneSearchHarness();
    const e = h.elements;
    h.type('Córdoba');
    e.timezoneCityInput.dispatchEvent({
        type: 'keydown', key: 'Enter', isComposing: true,
        preventDefault() { assert.fail('IME confirmation must remain available'); },
    });
    assert.equal(h.requests.length, 0);
    const first = h.search();
    let prevented = false;
    e.timezoneCityInput.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    assert.equal(h.requests.length, 1);
    h.type('Miami');
    e.timezoneCityInput.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
    h.requests[1].respond([{ city: '<Miami>', country: 'Estados Unidos', timezone: 'America/New_York' }]);
    await flushPromises();
    h.requests[0].respond(cordobaCities);
    await first;
    assert.equal(e.timezoneCityResults.children.length, 1);
    assert.equal(e.timezoneCityResults.children[0].children[0].children[0].textContent, '<Miami> · Estados Unidos');
    h.type('Miami, USA');
    assert.equal(e.timezoneCityResults.children.length, 0);
    assert.equal(e.timezoneCityResults.hidden, true);
    assert.equal(h.isDirty(), false);
});

test('city lookup explains short queries, no matches and service failures without changing the zone', async () => {
    const h = timezoneSearchHarness();
    h.type('C');
    await h.search();
    assert.equal(h.requests.length, 0);
    assert.equal(h.elements.timezoneCityStatus.textContent, runtime().t('profile.timezone.city_search_short'));
    for (const [ok, message] of [[true, 'city_search_empty'], [false, 'city_search_error']]) {
        h.type('Unknown city');
        const search = h.search();
        h.requests.at(-1).respond([], ok);
        await search;
        assert.equal(h.elements.timezoneCityStatus.textContent, runtime().t(`profile.timezone.${message}`));
        assert.equal(h.elements.timezoneCitySearchButton.disabled, false);
    }
    assert.equal(h.elements.timezoneName.value, '');
    assert.equal(h.isDirty(), false);
});

test('Spanish profile messages render through the real browser runtime', () => {
    const i18n = runtime();
    assert.equal(i18n.locale, 'es-ES');
    assert.equal(i18n.t('profile.language.interface_label'), 'Idioma de la interfaz');
    assert.equal(
        i18n.t('profile.api.saved_count', { count: 2 }),
        'Se han guardado 2 claves API.'
    );
    assert.equal(
        i18n.t('profile.calls.scheduled_for', { date: '7/9/2026, 10:00' }),
        'Programada para 7/9/2026, 10:00'
    );
    assert.equal(
        i18n.t('profile.api.test_summary', { valid: 2, tested: 3 }),
        'Claves válidas: 2/3.'
    );
    assert.equal(
        i18n.t('profile.api.masked_skipped', { valid: 2, tested: 3, count: 1 }),
        'Claves válidas: 2/3. Se ha omitido 1 clave enmascarada.'
    );
});

function profileSaveHarness({ originalPhone = '', fullPhone = '', phoneVerified = false } = {}) {
    const field = (name, value, type = 'text') => eventTarget({
        name, value, type, tagName: 'INPUT', dataset: {}, checked: true,
    });
    const fields = {
        ui_language: field('ui_language', 'en'),
        user_info: field('user_info', 'original'),
        afterLogin: field('afterLogin', '/home', 'radio'),
        wsEngine: field('wsEngine', 'native', 'radio'),
        old_password: field('old_password', '', 'password'),
    };
    const form = eventTarget({ querySelectorAll() { return Object.values(fields); } });
    const apiForm = guardedForm(field('openai', '', 'password'));
    const elements = {
        editProfileForm: form,
        phone: field('phone_number', originalPhone),
        useAlterEgo: { checked: false },
        alterEgo: field('alter_ego_id', '0'),
        deleteAccountBtn: eventTarget(),
    };
    elements.phone.dataset.phoneVerified = phoneVerified ? 'true' : 'false';
    const requests = [];
    const confirmations = [];
    const errors = [];
    let reloads = 0;
    const i18n = runtime('en');
    const document = eventTarget({
        getElementById(id) { return elements[id]; },
        querySelector(selector) {
            if (selector === '#editProfileForm') return form;
            if (selector === 'input[name="afterLogin"]:checked') return fields.afterLogin;
            if (selector === 'input[name="wsEngine"]:checked') return fields.wsEngine;
            return null;
        },
        querySelectorAll() { return []; },
    });
    const window = eventTarget({
        AurvekI18n: i18n,
        location: { href: '/settings', reload() { reloads += 1; } },
    });
    const context = vm.createContext({
        window, document, location: window.location, AurvekI18n: i18n,
        originalPhoneNumber: originalPhone, currentUserVoiceId: '', currentAlterEgoId: '0',
        NotificationModal: {
            confirm(...args) { confirmations.push(args); },
            success() {},
            error(...args) { errors.push(args); },
        },
        FormData: class {
            constructor() { this.values = new Map(Object.values(fields).map(input => [input.name, input.value])); }
            get(name) { return this.values.get(name); }
            set(name, value) { this.values.set(name, value); }
        },
        secureFetch(url, options) {
            return new Promise((resolve, reject) => {
                requests.push({
                    url, options, reject,
                    respond(data = { success: true }, ok = true) {
                        resolve({ ok, json: async () => data });
                    },
                });
            });
        },
        CSS: { escape: value => value },
        requestAnimationFrame: callback => callback(),
        queueMicrotask, Promise, Set, setTimeout,
        console: { error() {} },
    });
    for (const relative of ['common/form-guard.js', 'edit_profile.js']) {
        vm.runInContext(fs.readFileSync(path.join(repoRoot, 'data/static/js', relative), 'utf8'), context);
        context.FormGuard = window.FormGuard;
    }
    context.testFullPhoneNumber = fullPhone;
    vm.runInContext('getFullPhoneNumber = () => testFullPhoneNumber;', context);
    const guard = window.FormGuard;
    guard.watch(form, { exclude: ['old_password'] });
    guard.watch(apiForm);
    form.addEventListener('submit', context.handleFormSubmit);
    return {
        fields, form, apiForm, guard, requests, confirmations, errors,
        get reloads() { return reloads; },
        edit(name, value) {
            fields[name].value = value;
            form.dispatchEvent({ type: 'input', target: fields[name] });
        },
        submit() {
            const event = {
                type: 'submit', target: form, defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; },
            };
            return form.dispatchEvent(event).find(result => result && typeof result.then === 'function');
        },
    };
}

const flushPromises = () => new Promise(resolve => setImmediate(resolve));
const changedLanguage = { success: true, ui_language: 'es', ui_language_changed: true };

test('saving other profile fields keeps an unchanged unverified phone number', async () => {
    const phone = '+34931234567';
    const h = profileSaveHarness({ originalPhone: phone, fullPhone: phone, phoneVerified: false });
    h.edit('ui_language', 'es');
    const save = h.submit();

    assert.equal(h.requests[0].url, '/api/edit-profile');
    assert.equal(h.requests[0].options.body.get('phone_number'), phone);
    h.requests[0].respond(changedLanguage);
    await flushPromises();
    h.requests[1].respond();
    h.requests[2].respond();
    await save;
    assert.equal(h.errors.length, 0);
});

test('changing a phone number still requires an approved verification challenge', async () => {
    const h = profileSaveHarness({
        originalPhone: '+34931234567',
        fullPhone: '+34937654321',
        phoneVerified: false,
    });
    h.edit('ui_language', 'es');
    const save = h.submit();

    assert.equal(h.requests[0].url, '/api/check-phone-number');
    h.requests[0].respond({ exists: false });
    await save;
    assert.equal(h.requests.length, 1);
    assert.equal(h.errors.length, 1);
    assert.equal(h.errors[0][1], 'Verify the new phone number before saving.');
});

for (const stage of ['profile', 'auxiliary']) {
    test(`profile edits during ${stage} save survive language reload and explicit retry`, async () => {
        const h = profileSaveHarness();
        h.edit('ui_language', 'es');
        h.edit('user_info', 'submitted');
        let completed = false;
        const save = h.submit().then(() => { completed = true; });
        assert.equal(h.requests[0].options.body.get('user_info'), 'submitted');
        if (stage === 'auxiliary') {
            h.requests[0].respond(changedLanguage);
            await flushPromises();
        }
        // Returning to the old baseline is still a new draft relative to the saved payload.
        h.edit('user_info', 'original');
        h.edit('afterLogin', '/chat');
        if (stage === 'profile') {
            h.requests[0].respond(changedLanguage);
            await flushPromises();
        }
        assert.equal(completed, false);
        assert.equal(JSON.parse(h.requests[1].options.body).after_login, '/home');
        assert.equal(h.guard.anyDirty(), true);
        h.requests[1].respond();
        await flushPromises();
        assert.equal(completed, false);
        h.requests[2].respond();
        await save;
        assert.equal(h.reloads, 0);
        assert.equal(h.confirmations.length, 1);
        assert.equal(h.guard.isDirty(h.form), true);
        assert.equal(h.fields.user_info.value, 'original');
        assert.equal(h.requests.length, 3);

        const retry = h.submit();
        assert.equal(h.requests[3].options.body.get('user_info'), 'original');
        h.requests[3].respond({ ...changedLanguage, ui_language_changed: false });
        await flushPromises();
        assert.equal(JSON.parse(h.requests[4].options.body).after_login, '/chat');
        h.requests[4].respond();
        h.requests[5].respond();
        await retry;
        assert.equal(h.guard.isDirty(h.form), false);
        assert.equal(h.reloads, 1);
    });
}

test('partial auxiliary failure waits for remaining saves and retry reloads the stale runtime', async () => {
    const h = profileSaveHarness();
    h.edit('ui_language', 'es');
    let completed = false;
    const save = h.submit().then(() => { completed = true; });
    h.requests[0].respond(changedLanguage);
    await flushPromises();
    h.requests[1].respond({}, false);
    await flushPromises();
    assert.equal(completed, false);
    assert.equal(h.guard.anyDirty(), true);
    h.requests[2].respond();
    await save;
    assert.equal(h.errors.length, 1);
    assert.equal(h.reloads, 0);
    assert.equal(h.guard.anyDirty(), true);

    const retry = h.submit();
    h.requests[3].respond({ ...changedLanguage, ui_language_changed: false });
    await flushPromises();
    h.requests[4].respond();
    h.requests[5].respond();
    await retry;
    assert.equal(h.reloads, 1);
    assert.equal(h.guard.anyDirty(), false);
});

test('successful profile save preserves other drafts and ignores excluded password autofill', async () => {
    const h = profileSaveHarness();
    // Settings verification inputs intentionally have no name in the profile form.
    h.fields.old_password.id = 'old-password';
    h.fields.old_password.name = '';
    h.edit('ui_language', 'es');
    h.guard.markDirty(h.apiForm);
    const save = h.submit();
    h.edit('old_password', 'test-only-value');
    h.requests[0].respond(changedLanguage);
    await flushPromises();
    h.requests[1].respond();
    h.requests[2].respond();
    await save;
    assert.equal(h.guard.isDirty(h.form), false);
    assert.equal(h.guard.isDirty(h.apiForm), true);
    assert.equal(h.reloads, 0);
    assert.equal(h.confirmations.length, 1);
});
