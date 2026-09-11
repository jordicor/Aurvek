const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const pages = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
function runtime(html) {
    const payload = JSON.parse(html.match(/<script[^>]+id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]);
    const tr = create(payload);
    const nodes = new Map();
    const get = id => {
        if (!nodes.has(id)) nodes.set(id, {value: '', textContent: '', innerHTML: '', style: {}, disabled: false,
            classList: {add() {}, remove() {}}, addEventListener(type, handler) {this[type] = handler;}});
        return nodes.get(id);
    };
    get('markup').value = '0.012345';
    const c = vm.createContext({AurvekI18n: {...tr, t: tr.render}, console,
        document: {getElementById: get, addEventListener() {}, querySelectorAll: () => []},
        FormGuard: {markClean() {}}, setTimeout() {}, window: {},
        NotificationModal: {error(title, message) {c.errorMessage = message;}},
    });
    return {c, get, tr};
}
(async () => {
    const {c, get, tr} = runtime(pages.curation_settings);
    for (const script of pages.curation_settings.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
        new vm.Script(script[1]);
        if (script[1].includes('function updatePreview')) vm.runInContext(script[1], c);
    }
    assert.equal(get('previewEarning').textContent, tr.render('creator.curation.per_mtokens', {amount: tr.formatCurrency(0.012345 * 0.7)}));
    assert.equal(get('previewExample').textContent, tr.render('creator.curation.example', {amount: tr.formatCurrency(0.012345 * 0.7)}));
    let sent;
    c.fetch = async (url, options) => {sent = {url, body: JSON.parse(options.body)};return {json: async () => ({success: true})};};
    await get('markupForm').submit({preventDefault() {}});
    assert.equal(sent.url, '/api/user/curation-settings');
    assert.equal(sent.body.referral_markup_per_mtokens, 0.012345);
    c.fetch = async () => {throw Error('internal network diagnostics');};
    await get('markupForm').submit({preventDefault() {}});
    assert.equal(c.errorMessage, tr.render('creator.curation.save_error'));

    const list = runtime(pages['prompts/prompt_list']);
    vm.runInContext(fs.readFileSync('data/static/js/prompt-list.js', 'utf8'), list.c);
    vm.runInContext("PromptListState.prompts = Array.from({length: 1234}, (_, i) => ({public: i < 1233 ? 'public' : 'private'}));", list.c);
    list.c.updateStats(1233);
    assert.equal(list.get('statTotal').textContent, list.tr.render('creator.stats.total_count', {count: list.tr.formatNumber(1234)}));
    assert.equal(list.get('statPrivate').textContent, list.tr.render('creator.stats.private_count', {count: list.tr.formatNumber(1)}));
    assert.equal(list.get('statFiltered').textContent, list.tr.render('creator.stats.shown_count', {count: list.tr.formatNumber(1233)}));
    list.c.updateSelectAllState = () => {};
    list.c.document.querySelectorAll = () => [{value: '41'}, {value: '73'}];
    list.c.updateSelectedCount();
    assert.equal(list.get('selectedCount').textContent, list.tr.render('creator.action.delete_count', {count: list.tr.formatNumber(2)}));
    assert.equal(list.get('deleteBtn').disabled, false);
})().catch(error => {console.error(error);process.exitCode = 1;});
