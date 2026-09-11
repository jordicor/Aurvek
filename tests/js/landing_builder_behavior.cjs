// Executed by pytest with real Jinja-rendered scripts and their locale payload.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { create } = require('../../data/static/js/common/i18n.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const i18n = create(input.payload);
const nodes = new Map(), notices = [], requests = [], ready = [];
function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
        id, value: '', checked: false, textContent: '', innerHTML: '', disabled: false,
        offsetWidth: 800, style: { setProperty() {} }, dataset: {}, selectedOptions: [], options: [],
        listeners: {}, classList: { add() {}, remove() {} },
        addEventListener(name, callback) { this.listeners[name] = callback; },
        querySelector(selector) { return node(selector); }, querySelectorAll() { return []; },
        setAttribute() {}, append() {}, appendChild() {}, focus() {}, closest() { return this; },
        reset() { this.resetCalled = true; }
    });
    return nodes.get(id);
}
const document = {
    getElementById: node, querySelector: node, querySelectorAll() { return []; },
    createElement: node, createTextNode: text => ({ textContent: text }),
    addEventListener(name, callback) { if (name === 'DOMContentLoaded') ready.push(callback); },
    documentElement: node('root')
};
let response = { ok: true, body: { success: true } }, ajax;
function $(selector) {
    const el = node(selector);
    return {
        submit(callback) { el.submit = callback; },
        text(value) { el.textContent = value; return this; },
        attr(name, value) { el[name] = value; return this; },
        css() { return this; }, addClass() { return this; }, is() { return true; }
    };
}
$.ajax = value => { ajax = value; };
const context = vm.createContext({
    document, window: { innerHeight: 1000, addEventListener() {}, location: {hostname:'fixture.test'}, open() {} },
    AurvekI18n: { ...i18n, t: i18n.render }, console,
    navigator: { clipboard: { writeText: async () => {} } },
    NotificationModal: Object.fromEntries(['error','success','warning','info','confirm'].map(type => [type, (...args) => notices.push({ type, args })])),
    FormGuard: { watchWithListeners() {}, markClean() {}, markDirty() {}, reloadIfClean() {}, watchCodeMirror() {}, createGroup() { return this; } },
    FullsizeViewer: { init() {}, setImages() {} },
    bootstrap: { Modal: class { show() {} static getInstance() { return { hide() {} }; } } },
    fetch: async (url, options) => { requests.push({ url, options }); return { ok: response.ok, json: async () => response.body }; },
    secureFetch: async (url, options) => { requests.push({ url, options }); return { ok: response.ok, json: async () => response.body }; },
    FormData: class {}, FileReader: class {}, IntersectionObserver: class { observe() {} },
    CodeMirror: { fromTextArea() { return { getValue: () => '<p>Créateur & untouched</p>', setSize() {} }; } },
    $, setTimeout() {}, setInterval() {}, clearInterval() {},
    btoa: text => Buffer.from(text, 'binary').toString('base64'), unescape, encodeURIComponent, Date
});
vm.runInContext(input.script, context);
async function run() {
    if (input.page === 'landing_config.html' || input.page === 'pack_landing_config.html') {
        vm.runInContext("currentFiles = {pages: ['home.html'], css: ['app.css'], js: [], images: [], other: [], total_count:2}", context);
        context.showModeSelection();
        assert(node('modeFileSummaryText').textContent.includes(i18n.render('landing_builder.ui.pages_count', {count: i18n.formatNumber(1)})));
        assert.equal(node('modifyOption').style.pointerEvents, 'auto');
        node('wizardDescription').value = '';
        await context.generateLanding();
        assert.equal(notices.at(-1).type, 'warning');
        assert.equal(notices.at(-1).args[0], i18n.render('landing_builder.ui.description_required'));
        assert.equal(requests.length, 0);
        node('wizardDescription').value = 'Description du créateur unchanged';
        node('input[name="wizardStyle"]:checked').value = 'modern';
        node('wizardLanguage').value = 'es';
        node('wizardTimeout').value = '60';
        node('wizardPrimaryColor').value = '#123456';
        node('wizardSecondaryColor').value = '#654321';
        response.body = {success:false, message:i18n.render('landing_builder.ui.failed_to_start_job')};
        await context.generateLanding();
        const body = JSON.parse(requests.at(-1).options.body);
        assert.equal(body.description, 'Description du créateur unchanged');
        assert.equal(body.language, 'es');
        assert.equal(body.style, 'modern');
        assert.equal(body.timeout_minutes, 60);
        assert.equal(node('wizardError').textContent, i18n.render('landing_builder.ui.failed_to_start_job'));
        assert.equal(node('wizardGenerateBtn').disabled, false);
        if (input.page === 'landing_config.html') {
            node('publicPromptsAccess').checked = true;
            node('categoryAccess').selectedOptions = [{value:'1'}, {value:'2'}];
            node('billingUserPays').checked = true;
            node('billingLimit').value = '1234.50';
            context.updateRegSummary();
            assert(node('regSummaryText').textContent.includes(i18n.formatCurrency(1234.50, 'USD', 2)));
            assert.equal(node('billingLimit').value, '1234.50');
        }
    } else if (input.page === 'user_branding.html') {
        node('companyName').value = 'Créateur <name>';
        node('footerText').value = 'Footer authored unchanged';
        node('emailSignature').value = 'Signature authored unchanged';
        node('colorPrimaryText').value = '#123456';
        node('colorSecondaryText').value = '#654321';
        context.updatePreview();
        assert.equal(node('companyNamePreview').textContent, 'Créateur <name>');
        assert.equal(node('emailTitlePreview').textContent, i18n.render('landing_builder.ui.welcome_to_companyname', {companyName:'Créateur <name>'}));
        assert.equal(node('footerPreview').textContent, 'Footer authored unchanged');
        await context.saveBranding();
        assert(node('saveBtn').innerHTML.includes(i18n.render('landing_builder.ui.saved')));
        assert.equal(JSON.parse(requests.at(-1).options.body).company_name, 'Créateur <name>');
        response = {ok:false, body:{error:i18n.render('landing_builder.response.invalid_primary_color_format_use_hex_format_rrggbb')}};
        await context.saveBranding();
        assert.equal(notices.at(-1).type, 'error');
        assert(notices.at(-1).args[1].includes(response.body.error));
    } else if (input.page === 'web/components_list.html') {
        response = {ok:false, body:{message:i18n.render('landing_builder.response.invalid_image_file_value1', {value1:'broken.png'}), images:0}};
        node('imageUploadForm').listeners.submit.call(node('imageUploadForm'), {preventDefault(){}});
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(notices.at(-1).type, 'error');
        assert.equal(notices.at(-1).args[1], response.body.message);
        assert.equal(node('imageUploadForm').resetCalled, undefined);
    } else {
        for (const callback of ready) callback();
        node('#editForm').submit({preventDefault(){}});
        assert.equal(Buffer.from(ajax.data.encodedContent,'base64').toString('utf8'), '<p>Créateur & untouched</p>');
        ajax.success({success:true, message:'Saved <not markup>'});
        assert.equal(node('#message').textContent, 'Saved <not markup>');
        if (input.page === 'web/web_edit.html') {
            ajax.success({success:true, published:false, message:'Saved', publication_errors:['Localized issue <quoted HTML>']});
            assert.equal(node('#message').textContent, 'Saved\nLocalized issue <quoted HTML>');
            assert.equal(node('#message').class, 'alert alert-warning');
        }
        ajax.error({responseJSON:{message:i18n.render('landing_builder.response.error_saving_changes')}}, 'error', 'Bad Request');
        assert.equal(node('#message').textContent, i18n.render('landing_builder.response.error_saving_changes'));
    }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
