const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const settle = async () => { for (let i = 0; i < 3; i++) await new Promise(resolve => setImmediate(resolve)); };

test('profile locale relabels languages and channels while preserving drafts and phone editor', async () => {
    const resources = Object.fromEntries(['es', 'en'].map(locale => [locale,
        Object.fromEntries(['profile', 'application'].map(domain => [domain,
            JSON.parse(fs.readFileSync(`locales/${locale}/${domain}.json`, 'utf8'))]))]));
    const metadata = {app_id: 'katari', frame_instance_id: 'profile_test_frame',
        parent_origin: 'https://katari.example', ui_language: 'es', csrf_token: 'test', messages: {resources}};
    function element() {
        return {value: '', textContent: '', dataset: {}, options: [], children: [],
            append(...children) {this.children.push(...children);},
            replaceChildren(...children) {this.children = children;}};
    }
    const fields = Object.fromEntries(['profile-metadata', 'preferred_name', 'timezone_name', 'about_me',
        'primary_language', 'additional_languages', 'contact', 'channels', 'status', 'device-zone',
        'request-phone', 'phone-form', 'phone_number', 'phone_code', 'verify-phone', 'profile-form',
        'save', 'phone-editor', 'phone-action'].map(id => [id, element()]));
    fields['profile-metadata'].textContent = JSON.stringify(metadata);
    for (const id of ['primary_language', 'additional_languages']) {
        fields[id].options = [{value: 'es', textContent: 'español', selected: false},
            {value: 'en', textContent: 'inglés', selected: false}];
    }
    const state = {version: 1, profile: {preferred_name: 'Saved name', timezone_name: 'UTC', about_me: 'Saved', preferred_languages: ['es']},
        contact: {phone_number: '+15550009537', verified: true, version: 'contact-version'},
        channels: ['phone', 'whatsapp', 'telegram'].map(channel => ({channel, status: 'connected', receivers: []}))};
    const requests = [], handlers = {}, messages = [];
    const parent = {postMessage: data => messages.push(data)};
    const document = {getElementById: id => fields[id], createElement: element,
        documentElement: {lang: 'es', scrollHeight: 800}, querySelectorAll: () => []};
    const context = {document, parent, location: {pathname: '/embed/katari/profile_test_frame/profile'},
        addEventListener: (event, callback) => {handlers[event] = callback;},
        fetch: async (url, options) => {
            requests.push({url, body: options.body && JSON.parse(options.body)});
            return {ok: true, json: async () => url.endsWith('/state') ? state : {
                ui_language: 'en', messages: {resources}, languages: [{code: 'es', label: 'Spanish'}, {code: 'en', label: 'English'}]}};
        }};
    vm.runInNewContext(fs.readFileSync('data/static/js/application-profile.js', 'utf8'), context);
    await settle();
    assert.equal(fields['phone-editor'].open, false);
    assert.equal(fields['phone-action'].textContent, 'Cambiar móvil');
    assert.equal(fields.channels.children[0].textContent, 'Llamada telefónica: Conectado');
    fields.preferred_name.value = 'Unsaved name'; fields.about_me.value = 'Unsaved details';
    fields.timezone_name.value = 'Europe/Madrid'; fields.primary_language.value = 'en';
    fields.additional_languages.options[0].selected = true;
    fields['phone-editor'].open = true; fields.phone_number.value = '+15550001234'; fields.phone_code.value = '123456';
    await handlers.message({source: parent, origin: metadata.parent_origin, data: {
        contract_version: 'aurvek_embed.v1', surface: 'profile', app_id: metadata.app_id,
        frame_instance_id: metadata.frame_instance_id, event: 'set_ui_language', request_id: 'locale-1', ui_language: 'en'}});
    assert.equal(fields.primary_language.options[0].textContent, 'Spanish');
    assert.equal(fields.additional_languages.options[1].textContent, 'English');
    assert.equal(fields.primary_language.value, 'en');
    assert.equal(fields.additional_languages.options[0].selected, true);
    assert.equal(fields.preferred_name.value, 'Unsaved name'); assert.equal(fields.about_me.value, 'Unsaved details');
    assert.equal(fields.timezone_name.value, 'Europe/Madrid');
    assert.equal(fields['phone-editor'].open, true); assert.equal(fields.phone_number.value, '+15550001234');
    assert.equal(fields.phone_code.value, '123456'); assert.equal(fields.contact.textContent, '+15550009537');
    assert.equal(fields['phone-action'].textContent, 'Change mobile number');
    assert.deepEqual(fields.channels.children.map(item => item.textContent), ['Phone call: Connected', 'WhatsApp: Connected', 'Telegram: Connected']);
    assert.deepEqual(requests.map(item => item.url.split('/').pop()), ['state', 'language']);
    assert.equal(messages.at(-1).event, 'language_changed');
});
