const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const {create} = require('../../data/static/js/common/i18n.js');
const root = path.resolve(__dirname, '../..');
const regions = {en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE'};
const escaped = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
class Element {
    constructor() {
        this.value = ''; this.style = {}; this.children = []; this.listeners = {}; this.dataset = {};
        this.classList = {add() {}, remove() {}, toggle() {}};
        this.innerHTML = '';
    }
    set textContent(value) { this.text = String(value); this.innerHTML = escaped(value); this.children = []; }
    get textContent() { return this.text; }
    appendChild(child) { this.children.push(child); this.innerHTML += child.innerHTML || ''; }
    addEventListener(event, callback) { this.listeners[event] = callback; }
    setAttribute(name, value) { this[name] = value; }
    getAttribute(name) { return this[name]; }
    querySelector() { return this.child ||= new Element(); }
    querySelectorAll() { return []; }
    focus() {}
    remove() {}
}
function setup(language) {
    const resources = {};
    for (const code of new Set(['en', language])) resources[code] = {marketplace_admin: JSON.parse(fs.readFileSync(path.join(root, `locales/${code}/marketplace_admin.json`), 'utf8'))};
    const tr = create({version: 1, language, locales: regions, resources});
    const elements = new Map();
    const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
    get('packId').value = '20'; get('packTagsData').value = '[]'; get('packIsPaid').checked = true;
    get('packPrice').value = '1234.50';
    const notices = [];
    const context = vm.createContext({
        AurvekI18n: {...tr, t: tr.render}, COMMISSION_RATE: 0.3, MAX_FREE_BALANCE: 5,
        document: {getElementById: get, querySelector: () => null, querySelectorAll: () => [],
            createElement: () => new Element(), createTextNode: value => ({innerHTML: escaped(value)})},
        NotificationModal: {toast: (...args) => notices.push(args), error: (...args) => notices.push(args),
            success: (...args) => notices.push(args), warning: (...args) => notices.push(args),
            confirm: (...args) => notices.push(args)},
        setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {}, console,
        bootstrap: {Modal: class {show() {} static getInstance() {return null;}}},
    });
    context.window = context;
    return {context, get, tr, notices};
}
const flush = () => new Promise(resolve => setImmediate(resolve));
for (const language of Object.keys(regions)) {
    test(`pack editor ${language}: currency, counts, sale states, confirmation and safe errors`, async () => {
        const {context, get, tr, notices} = setup(language);
        context.fetch = async () => ({ok: true, json: async () => ({total_count: 1234, total_revenue: 1234.5, purchases: [
            {username: 'Buyer <tag>', email: 'test@example.test', amount: 1234.5, payment_method: 'free', status: 'completed', created_at: '2026-09-08T12:30:00'},
        ]})});
        vm.runInContext(fs.readFileSync(path.join(root, 'data/static/js/admin-packs.js'), 'utf8'), context);
        await flush();
        assert.equal(get('lrcBalanceHint').textContent, tr.t('marketplace_admin.ui.maximum_balance', {amount: tr.formatCurrency(864.15)}));
        assert.equal(get('salesCount').textContent, tr.t('marketplace_admin.ui.purchase_count', {count: 1234, purchases: tr.formatNumber(1234)}));
        const sale = get('salesTableBody').children[0].innerHTML;
        assert.ok(sale.includes('Buyer &lt;tag&gt;'));
        assert.ok(sale.includes(tr.formatCurrency(1234.5)));
        assert.ok(sale.includes(tr.t('marketplace_admin.ui.payment_free')));
        assert.ok(sale.includes(tr.t('marketplace_admin.ui.purchase_completed')));
        context.removePackItem(10, 'Creator " <name>');
        const confirmation = notices.pop();
        assert.equal(confirmation[1], tr.t('marketplace_admin.ui.remove_prompt_confirm', {name: 'Creator " <name>'}));
        context.fetch = async () => {throw new Error('Private provider detail');};
        await confirmation[2]();
        assert.equal(notices.pop()[0], tr.t('marketplace_admin.ui.network_error_please_try_again'));
    });
    test(`pack welcome ${language}: categorized files and creator filenames remain text`, async () => {
        const {context, get, tr} = setup(language);
        const filename = 'Creator <img onerror="bad">.html';
        context.fetch = async url => ({ok: true, json: async () => url.endsWith('/files') ? {
            files: {pages: [filename], css: ['style.css'], js: [], images: [], other: [], total_count: 2},
        } : {success: true, has_active_job: false}});
        const template = fs.readFileSync(path.join(root, 'templates/admin_packs_edit.html'), 'utf8');
        const scripts = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)];
        vm.runInContext(scripts.at(-1)[1], context);
        await flush();
        assert.equal(get('welcome-status-badge').textContent, tr.t('marketplace_admin.ui.active'));
        assert.equal(get('welcome-files-ul').children[0].textContent, filename);
        context.welcomeOpenModifyModal();
        assert.equal(get('welcome-modify-file-list').children[0].textContent, filename);
        assert.ok(!get('welcome-modify-file-list').innerHTML.includes('<img'));
    });
}
