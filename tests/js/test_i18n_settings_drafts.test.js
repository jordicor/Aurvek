'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const settingsTemplate = fs.readFileSync(path.join(repoRoot, 'templates/settings.html'), 'utf8');

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

function classList(initial = []) {
    const values = new Set(initial);
    return {
        add(...names) { names.forEach(name => values.add(name)); },
        remove(...names) { names.forEach(name => values.delete(name)); },
        contains(name) { return values.has(name); },
        toggle(name, enabled) { if (enabled) values.add(name); else values.delete(name); },
    };
}

function eventTarget(base = {}) {
    const listeners = new Map();
    return Object.assign({
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(callback);
        },
        removeEventListener(type, callback) {
            const callbacks = listeners.get(type) || [];
            listeners.set(type, callbacks.filter(item => item !== callback));
        },
        replaceChildren(...children) {
            this.children = children;
            this.textContent = children.map(child => child.textContent || '').join('');
        },
        append(...children) {
            this.children = (this.children || []).concat(children);
            this.textContent = this.children.map(child => child.textContent || '').join('');
        },
        dispatch(type, init = {}) {
            const event = Object.assign({
                type,
                target: this,
                currentTarget: this,
                defaultPrevented: false,
                preventDefault() { this.defaultPrevented = true; },
            }, init);
            const pending = [];
            let current = this;
            while (current) {
                event.currentTarget = current;
                for (const callback of current._listeners(type)) {
                    const result = callback.call(current, event);
                    if (result && typeof result.then === 'function') pending.push(result);
                }
                current = current.parentElement;
            }
            return Promise.all(pending);
        },
        _listeners(type) { return (listeners.get(type) || []).slice(); },
    }, base);
}

function formElement(controls) {
    const form = eventTarget({ dataset: {} });
    controls.forEach(control => { control.parentElement = form; });
    form.querySelectorAll = selector => {
        if (selector === 'input, textarea, select') return controls;
        const name = selector.match(/name="([^"]+)"/)?.[1];
        return controls.filter(control => !name || control.name === name);
    };
    return form;
}

function passwordScript() {
    const blockStart = settingsTemplate.indexOf('{% block scripts_inline %}');
    const script = settingsTemplate.slice(blockStart).match(/<script>([\s\S]*?)<\/script>/)[1];
    const start = script.indexOf('    // Change password functions');
    const end = script.indexOf('    {% endif %}', start);
    return `
        const profileSettingsText = key => key;
        const accountSettingsText = key => key;
        const commonSettingsText = key => key;
        ${script.slice(start, end)}
    `;
}

function makeFixture() {
    const reminders = eventTarget({
        id: 'wellbeingRemindersEnabled', name: 'wellbeing_reminders_enabled',
        type: 'checkbox', tagName: 'INPUT', checked: false, disabled: false, value: 'on',
    });
    const intense = eventTarget({
        id: 'wellbeingIntenseEnabled', name: 'wellbeing_intense_enabled',
        type: 'checkbox', tagName: 'INPUT', checked: false, disabled: false, value: 'on',
    });
    const minutes = eventTarget({
        id: 'wellbeingPreferredSoftMinutes', name: 'wellbeing_preferred_soft_minutes',
        type: 'number', tagName: 'INPUT', value: '', disabled: false,
    });
    const wellbeingForm = formElement([reminders, intense, minutes]);

    const oldPassword = eventTarget({ id: 'old-password', type: 'password', value: '' });
    const newPassword = eventTarget({ id: 'new-password', type: 'password', value: '' });
    const confirmPassword = eventTarget({ id: 'confirm-password', type: 'password', value: '' });
    const passwordSection = eventTarget();
    for (const input of [oldPassword, newPassword, confirmPassword]) input.parentElement = passwordSection;

    const elements = {
        wellbeingPreferencesForm: wellbeingForm,
        wellbeingRemindersEnabled: reminders,
        wellbeingIntenseEnabled: intense,
        wellbeingPreferredSoftMinutes: minutes,
        wellbeingResetSessionBtn: eventTarget({ dataset: {} }),
        wellbeingActiveMinutes: eventTarget({ textContent: '' }),
        wellbeingUserMessages: eventTarget({ textContent: '' }),
        wellbeingRemindersShown: eventTarget({ textContent: '' }),
        wellbeingSeverity: eventTarget({ textContent: '' }),
        wellbeingStatusText: eventTarget({ textContent: '' }),
        'wellbeing-tab': eventTarget(),
        passwordChangeSection: passwordSection,
        'old-password': oldPassword,
        'new-password': newPassword,
        'confirm-password': confirmPassword,
        'req-length': eventTarget({ className: 'invalid', classList: classList(['invalid']) }),
        'req-different': eventTarget({ className: 'invalid', classList: classList(['invalid']) }),
        'password-match-status': eventTarget({ textContent: '', className: 'password-match' }),
        'pw-error-message': eventTarget({ style: {} }),
        'pw-error-text': eventTarget({ textContent: '' }),
        'pw-success-message': eventTarget({ style: {} }),
        'pw-success-text': eventTarget({ textContent: '' }),
        changePasswordBtn: eventTarget({
            disabled: false,
            children: [],
            replaceChildren(...children) { this.children = children; },
        }),
    };

    const document = eventTarget({
        head: { appendChild() {} },
        createElement: () => eventTarget({ className: '', textContent: '' }),
        createTextNode: textContent => ({ textContent }),
        getElementById: id => elements[id] || null,
        querySelector: selector => selector.startsWith('#') ? elements[selector.slice(1)] || null : null,
        querySelectorAll: () => [],
    });
    const confirmations = [];
    let reloads = 0;
    const initialLoad = deferred();
    let wellbeingFetch = () => initialLoad.promise;
    let passwordFetch = async () => ({ ok: true, json: async () => ({ detail: 'Changed' }) });
    const window = eventTarget({
        AurvekI18n: { locale: 'en-US', t: key => key },
        location: { href: '/settings#wellbeing', hash: '#wellbeing', reload() { reloads += 1; } },
    });
    const context = {
        window,
        document,
        location: window.location,
        history: { replaceState() {} },
        bootstrap: { Tab: class { show() {} } },
        AurvekI18n: window.AurvekI18n,
        NotificationModal: {
            confirm(...args) { confirmations.push(args); },
            error() {},
            success() {},
        },
        secureFetch: (...args) => wellbeingFetch(...args),
        fetch: (...args) => passwordFetch(...args),
        requestAnimationFrame: callback => callback(),
        queueMicrotask,
        setTimeout() {},
        URLSearchParams,
        CSS: { escape: value => value },
        Intl,
        Date,
        Number,
        Set,
        Promise,
        console: { error() {} },
    };
    vm.createContext(context);
    vm.runInContext(
        fs.readFileSync(path.join(repoRoot, 'data/static/js/common/form-guard.js'), 'utf8'),
        context
    );
    context.FormGuard = window.FormGuard;
    vm.runInContext(
        fs.readFileSync(path.join(repoRoot, 'data/static/js/settings.js'), 'utf8'),
        context
    );
    vm.runInContext(passwordScript(), context);
    document.dispatch('DOMContentLoaded');

    return {
        context,
        elements,
        initialLoad,
        confirmations,
        guard: window.FormGuard,
        reloads: () => reloads,
        setWellbeingFetch(handler) { wellbeingFetch = handler; },
        setPasswordFetch(handler) { passwordFetch = handler; },
    };
}

function wellbeingResponse(minutes = 20) {
    return {
        ok: true,
        json: async () => ({
            preferences: {
                reminders_enabled: true,
                intense_reminders_enabled: false,
                preferred_soft_minutes: minutes,
            },
            status: { session: {} },
        }),
    };
}

async function settle() {
    await new Promise(resolve => setImmediate(resolve));
}

test('wellbeing hydration is clean and preserves a draft made while loading', async () => {
    const clean = makeFixture();
    clean.initialLoad.resolve(wellbeingResponse());
    await settle();
    assert.equal(clean.elements.wellbeingPreferredSoftMinutes.value, 20);
    assert.equal(clean.guard.isDirty(clean.elements.wellbeingPreferencesForm), false);

    const edited = makeFixture();
    edited.elements.wellbeingPreferredSoftMinutes.value = '35';
    edited.initialLoad.resolve(wellbeingResponse());
    await settle();
    assert.equal(edited.elements.wellbeingPreferredSoftMinutes.value, '35');
    assert.equal(edited.guard.isDirty(edited.elements.wellbeingPreferencesForm), true);

    edited.guard.reloadIfClean();
    assert.equal(edited.reloads(), 0);
    assert.equal(edited.confirmations.length, 1);
    assert.equal(edited.elements.wellbeingPreferredSoftMinutes.value, '35');
});

test('wellbeing save accepts only its saved draft and failures stay dirty', async () => {
    const fixture = makeFixture();
    fixture.initialLoad.resolve(wellbeingResponse());
    await settle();

    fixture.elements['new-password'].value = 'password-draft';
    await fixture.elements['new-password'].dispatch('input');
    fixture.elements.wellbeingPreferredSoftMinutes.value = '45';
    let savedPayload;
    fixture.setWellbeingFetch(async (_url, options) => {
        savedPayload = JSON.parse(options.body);
        return wellbeingResponse(45);
    });
    await fixture.elements.wellbeingPreferencesForm.dispatch('submit');
    assert.deepEqual(savedPayload, {
        reminders_enabled: true,
        intense_reminders_enabled: false,
        preferred_soft_minutes: 45,
    });
    assert.equal(fixture.guard.isDirty(fixture.elements.wellbeingPreferencesForm), false);
    assert.equal(fixture.guard.isDirty(fixture.elements.passwordChangeSection), true);

    fixture.elements.wellbeingPreferredSoftMinutes.value = '60';
    fixture.setWellbeingFetch(async () => ({ ok: false }));
    await fixture.elements.wellbeingPreferencesForm.dispatch('submit');
    assert.equal(fixture.elements.wellbeingPreferredSoftMinutes.value, '60');
    assert.equal(fixture.guard.isDirty(fixture.elements.wellbeingPreferencesForm), true);

    const pendingSave = deferred();
    fixture.setWellbeingFetch(() => pendingSave.promise);
    const saving = fixture.elements.wellbeingPreferencesForm.dispatch('submit');
    fixture.elements.wellbeingPreferredSoftMinutes.value = '75';
    pendingSave.resolve(wellbeingResponse(60));
    await saving;
    assert.equal(fixture.elements.wellbeingPreferredSoftMinutes.value, '75');
    assert.equal(fixture.guard.isDirty(fixture.elements.wellbeingPreferencesForm), true);
});

test('current-password autofill alone stays clean while new password drafts warn', async () => {
    const fixture = makeFixture();
    fixture.initialLoad.resolve(wellbeingResponse());
    await settle();

    fixture.elements['old-password'].value = 'autofilled-current-password';
    await fixture.elements['old-password'].dispatch('input');
    await fixture.elements['old-password'].dispatch('change');
    assert.equal(fixture.guard.anyDirty(), false);
    fixture.guard.reloadIfClean();
    assert.equal(fixture.reloads(), 1);
    assert.equal(fixture.confirmations.length, 0);

    fixture.elements['new-password'].value = 'new-password';
    await fixture.elements['new-password'].dispatch('input');
    assert.equal(fixture.guard.isDirty(fixture.elements.passwordChangeSection), true);
    fixture.elements['new-password'].value = '';
    await fixture.elements['new-password'].dispatch('change');
    assert.equal(fixture.guard.anyDirty(), false);
});

test('password drafts warn, survive cancellation and failure, and clean on revert', async () => {
    const fixture = makeFixture();
    fixture.initialLoad.resolve(wellbeingResponse());
    await settle();

    fixture.elements['old-password'].value = 'current-password';
    fixture.elements['new-password'].value = 'new-password';
    fixture.elements['confirm-password'].value = 'new-password';
    await fixture.elements['old-password'].dispatch('input');
    assert.equal(fixture.guard.isDirty(fixture.elements.passwordChangeSection), true);

    fixture.guard.reloadIfClean();
    assert.equal(fixture.reloads(), 0);
    assert.equal(fixture.confirmations.length, 1);
    assert.equal(fixture.elements['new-password'].value, 'new-password');

    fixture.setPasswordFetch(async () => ({
        ok: false,
        json: async () => ({ detail: 'Rejected' }),
    }));
    await fixture.elements.changePasswordBtn.dispatch('click');
    assert.equal(fixture.elements['old-password'].value, 'current-password');
    assert.equal(fixture.guard.isDirty(fixture.elements.passwordChangeSection), true);

    fixture.elements['old-password'].value = '';
    fixture.elements['new-password'].value = '';
    fixture.elements['confirm-password'].value = '';
    await fixture.elements['confirm-password'].dispatch('input');
    assert.equal(fixture.guard.isDirty(fixture.elements.passwordChangeSection), false);
});

test('password success clears only its draft and profile payload has no passwords', async () => {
    const fixture = makeFixture();
    fixture.initialLoad.resolve(wellbeingResponse());
    await settle();

    fixture.elements.wellbeingPreferredSoftMinutes.value = '50';
    fixture.elements['old-password'].value = 'current-password';
    fixture.elements['new-password'].value = 'new-password';
    fixture.elements['confirm-password'].value = 'new-password';
    await fixture.elements['old-password'].dispatch('input');
    let passwordBody;
    fixture.setPasswordFetch(async (_url, options) => {
        passwordBody = options.body;
        return { ok: true, json: async () => ({ detail: 'Changed' }) };
    });
    await fixture.elements.changePasswordBtn.dispatch('click');
    assert.equal(passwordBody.get('old_password'), 'current-password');
    assert.equal(passwordBody.get('new_password'), 'new-password');
    assert.equal(fixture.elements['old-password'].value, '');
    assert.equal(fixture.guard.isDirty(fixture.elements.passwordChangeSection), false);
    assert.equal(fixture.guard.isDirty(fixture.elements.wellbeingPreferencesForm), true);

    const formStart = settingsTemplate.indexOf('<form method="post" action="/api/edit-profile"');
    const formMarkup = settingsTemplate.slice(formStart, settingsTemplate.indexOf('</form>', formStart));
    const passwordControls = [...formMarkup.matchAll(/<input\b[^>]*\bid="(old-password|new-password|confirm-password)"[^>]*>/g)]
        .map(match => ({ id: match[1], name: match[0].match(/\bname="([^"]+)"/)?.[1] }));
    const profilePayload = new Map(passwordControls.filter(control => control.name)
        .map(control => [control.name, 'secret']));
    assert.deepEqual(passwordControls.map(control => control.id).sort(), [
        'confirm-password', 'new-password', 'old-password',
    ]);
    assert.equal(profilePayload.has('old_password'), false);
    assert.equal(profilePayload.has('new_password'), false);
    assert.equal(profilePayload.has('confirm_password'), false);
});
