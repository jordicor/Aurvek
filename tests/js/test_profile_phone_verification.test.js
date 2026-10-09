'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function control(values = {}) {
    const listeners = new Map();
    return {
        style: {}, dataset: {}, ...values,
        addEventListener(name, callback) { listeners.set(name, callback); },
        dispatch(name) { return listeners.get(name)({preventDefault() {}}); },
        appendChild(child) { this.child = child; },
    };
}

test('an approved phone code saves the number before reporting success', async () => {
    const phone = control({value: '2025550123', dataset: {phoneVerified: 'false'}});
    const send = control();
    const codeBox = control();
    const code = control({value: '123456'});
    const challengeId = control({value: ''});
    const form = control();
    const username = control({value: 'test-user'});
    const elements = {
        phone, sendCodeButton: send, verificationCodeContainer: codeBox,
        verificationCode: code, phoneVerificationId: challengeId,
        editProfileForm: form, username, deleteAccountBtn: control(),
    };
    const calls = [];
    const successes = [];
    const errors = [];
    let completeSave;
    const context = vm.createContext({
        document: {
            getElementById(id) { return elements[id]; },
            createElement() { return control(); },
            addEventListener() {},
        },
        window: {AurvekI18n: {t: key => key}},
        intlTelInputUtils: {numberFormat: {E164: 1}},
        testPhoneInput: {getNumber() { return '+12025550123'; }},
        originalPhoneNumber: '',
        FormData: class {
            constructor() { this.values = new Map(); }
            set(key, value) { this.values.set(key, value); }
            get(key) { return this.values.get(key); }
        },
        FormGuard: {
            markFieldsClean() { assert.equal(phone.disabled, false); },
            navigate() {},
        },
        NotificationModal: {
            success(...args) { successes.push(args); },
            error(...args) { errors.push(args); },
        },
        secureFetch(url, options) {
            calls.push({url, options});
            if (url === '/api/verify-code') {
                return Promise.resolve({ok: true, json: async () => ({status: 'approved'})});
            }
            return new Promise(resolve => { completeSave = resolve; });
        },
        setTimeout() {},
        console: {error() {}},
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../data/static/js/edit_profile.js'), 'utf8'), context);
    vm.runInContext("phoneInputJS = testPhoneInput; phoneChallengeId = 'challenge-1'; challengePhoneNumber = '+12025550123';", context);
    context.setupEventListeners();
    const save = codeBox.child.dispatch('click');
    await new Promise(resolve => setImmediate(resolve));

    assert.deepEqual(calls.map(call => call.url), ['/api/verify-code', '/api/edit-profile']);
    assert.equal(calls[1].options.body.get('phone_number'), '+12025550123');
    assert.equal(calls[1].options.body.get('phone_verification_id'), 'challenge-1');
    assert.equal(successes.length, 0);

    completeSave({ok: true, json: async () => ({success: true, reauthenticate: true})});
    await save;
    assert.equal(errors.length, 0);
    assert.equal(successes.length, 1);
    assert.equal(phone.dataset.phoneVerified, 'true');
    assert.equal(codeBox.style.display, 'none');
});
