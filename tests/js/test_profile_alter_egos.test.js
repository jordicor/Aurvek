const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function element() {
    const listeners = new Map();
    const classes = new Set();
    return {
        value: '', src: '', innerHTML: '',
        classList: {
            add(name) { classes.add(name); },
            remove(name) { classes.delete(name); },
            contains(name) { return classes.has(name); },
        },
        removeAttribute(name) { this[name] = ''; },
        addEventListener(type, callback) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(callback);
        },
        dispatch(type) {
            for (const callback of listeners.get(type) || []) callback.call(this, { type, target: this });
        },
    };
}

function harness() {
    const elements = Object.fromEntries([
        'createAlterEgoButton', 'alterEgo', 'saveAlterEgo', 'alterEgoPictureContainer',
        'alterEgoProfilePicture', 'previewAlterContainer', 'previewAlterEgoImage',
        'alterEgoId', 'alterEgoName', 'alterEgoDescription', 'alterEgoForm',
        'alterEgoDetails', 'deleteAccountBtn',
    ].map(id => [id, element()]));
    elements.alterEgo.options = [];
    elements.alterEgo.replaceChildren = function() {
        this.options = [];
        this.value = '';
    };
    elements.alterEgo.appendChild = function(option) {
        this.options.push(option);
        if (this.options.length === 1) this.value = option.value;
    };
    const fileInput = elements.alterEgoProfilePicture;
    fileInput.files = [];
    Object.defineProperty(fileInput, 'value', {
        get() { return this.files.length ? 'C:\\fakepath\\avatar.png' : ''; },
        set(value) {
            assert.equal(value, '');
            this.files = [];
        },
    });
    elements.alterEgoForm.reportValidity = () => elements.alterEgoName.value !== '';
    const requests = [];
    const context = vm.createContext({
        document: {
            addEventListener() {},
            getElementById(id) { return elements[id] || null; },
            createElement() { return element(); },
        },
        window: { AurvekI18n: { t: key => key } },
        escapeHtml: value => String(value),
        NotificationModal: {
            confirm(title, message, onConfirm) { onConfirm(); },
            success() {},
            error() { assert.fail('Unexpected error notification'); },
        },
        FormData: class extends Map {
            constructor(form) {
                assert.equal(form, elements.alterEgoForm);
                super([
                    ['id', elements.alterEgoId.value],
                    ['name', elements.alterEgoName.value],
                    ['description', elements.alterEgoDescription.value],
                    ['profile_picture', fileInput.files[0] || { name: '', size: 0 }],
                ]);
            }
        },
        secureFetch(url, options) {
            return new Promise(resolve => requests.push({
                url, options,
                respond(data) { resolve({ ok: true, json: async () => data }); },
            }));
        },
        console,
    });
    vm.runInContext(fs.readFileSync(
        path.resolve(__dirname, '../../data/static/js/edit_profile.js'), 'utf8'
    ), context);
    vm.runInContext('alterEgoModal = { show() {}, hide() {} };', context);
    context.initializeAlterEgoHandlers();
    return { elements, requests, context };
}

const flushPromises = () => new Promise(resolve => setImmediate(resolve));

test('editing another alter ego does not upload an image left in a cancelled edit', async () => {
    const h = harness();
    h.context.editAlterEgo('10');
    h.requests[0].respond({ success: true, alterEgo: { name: 'First', description: '', profilePicture: null } });
    await flushPromises();

    // Choose an image, then close this editor without saving and open another alter ego.
    h.elements.alterEgoProfilePicture.files = [{ name: 'first-avatar.png', size: 123 }];
    h.elements.previewAlterEgoImage.src = 'data:image/png;base64,test';
    h.elements.previewAlterContainer.classList.remove('hidden');
    vm.runInContext('alterEgoModal.hide();', h.context);
    h.context.editAlterEgo('20');
    h.requests[1].respond({ success: true, alterEgo: { name: 'Second', description: '', profilePicture: null } });
    await flushPromises();

    h.elements.alterEgoDescription.value = 'Updated text';
    h.elements.saveAlterEgo.dispatch('click');
    const save = h.requests[2];
    assert.equal(save.url, '/api/update-alter-ego/20');
    assert.equal(save.options.method, 'PUT');
    assert.equal(save.options.body.get('description'), 'Updated text');
    assert.equal(save.options.body.has('profile_picture'), false);
    assert.equal(h.elements.previewAlterEgoImage.src, '');
    assert.equal(h.elements.previewAlterContainer.classList.contains('hidden'), true);
});

test('saving an invalid alter ego makes no request and sends after a name is supplied', () => {
    const h = harness();
    h.elements.createAlterEgoButton.dispatch('click');
    h.elements.saveAlterEgo.dispatch('click');
    assert.equal(h.requests.length, 0);

    h.elements.alterEgoName.value = 'New alter ego';
    h.elements.saveAlterEgo.dispatch('click');
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].url, '/api/create-alter-ego');
    assert.equal(h.requests[0].options.method, 'POST');
    assert.equal(h.requests[0].options.body.get('name'), 'New alter ego');
});

test('changing the alter ego selector requests its details once', () => {
    const h = harness();
    h.elements.alterEgo.value = '20';
    h.elements.alterEgo.dispatch('change');
    assert.equal(h.requests.length, 1);
    assert.equal(h.requests[0].url, '/api/get-alter-ego-details/20');
    h.elements.alterEgo.value = '0';
    h.elements.alterEgo.dispatch('change');
    assert.equal(h.requests.length, 1);
    assert.equal(h.elements.alterEgoDetails.innerHTML, '');
});

test('deleting the selected alter ego restores the placeholder without requesting deleted details', async () => {
    const h = harness();
    h.elements.alterEgo.value = '20';
    h.elements.alterEgoDetails.innerHTML = 'Selected alter ego details';
    h.context.deleteAlterEgo('20');
    assert.equal(h.requests[0].options.method, 'DELETE');
    h.requests[0].respond({ success: true });
    await flushPromises();
    h.requests[1].respond({ success: true, alterEgos: [{ id: '10', name: 'Remaining alter ego' }] });
    await flushPromises();

    assert.deepEqual(h.requests.map(request => request.url), [
        '/api/delete-alter-ego/20', '/api/get-alter-egos',
    ]);
    assert.equal(h.elements.alterEgo.value, '0');
    assert.deepEqual(h.elements.alterEgo.options.map(option => option.value), ['0', '10']);
    assert.equal(h.elements.alterEgo.options[0].textContent, 'profile.alter_ego.select_placeholder');
    assert.equal(h.elements.alterEgoDetails.innerHTML, '');
});
