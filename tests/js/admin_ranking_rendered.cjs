const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const tr = create(JSON.parse(fixture.html.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]));
const nodes = new Map();
const get = id => {if (!nodes.has(id)) nodes.set(id, {style: {}, innerHTML: '', textContent: '', value: '', disabled: false,
    classList: {add() {}, remove() {}}, addEventListener(type, handler) {this[type] = handler;}});return nodes.get(id);};
for (const [key, value] of Object.entries(fixture.weights)) get('w_' + key).value = String(value);
get('interval_hours').value = '6';
const mode = {value: 'scheduled'};
const c = vm.createContext({AurvekI18n: tr, console,
    document: {getElementById: get, querySelectorAll: () => [], querySelector: () => mode}, setTimeout() {},
    FormGuard: {markClean() {}}, NotificationModal: {toast(message) {c.message = message;}, error(title, message) {c.errorMessage = message;}}});
for (const match of fixture.html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
    if (match[1].includes('const rankingText')) vm.runInContext(match[1], c);
}
(async () => {
    c.updateFormulaPreview();
    assert.equal(get('fw_access').textContent, tr.formatNumber(3.125, {maximumFractionDigits: 20}));
    assert.equal(get('w_users_with_access').value, '3.125');
    let sent;
    c.fetch = async (url, options) => {sent = {url, options};return {ok: true, json: async () => ({success: true})};};
    await get('rankingForm').submit({preventDefault() {}});
    assert.equal(sent.url, '/api/admin/ranking-config');
    assert.deepEqual(JSON.parse(sent.options.body), {mode: 'scheduled', interval_hours: 6, weights: fixture.weights});
    assert.equal(get('currentModeBadge').textContent, tr.t('admin_ranking.mode.scheduled'));
    const previousTime = get('lastUpdatedDisplay').textContent;
    await get('recalcBtn').click();
    assert.equal(sent.url, '/api/admin/ranking-recalculate');
    assert.equal(c.message, tr.t('admin_ranking.recalculation_started'));
    assert.equal(get('lastUpdatedDisplay').textContent, previousTime);
    c.fetch = async () => {throw Error('raw network diagnostic');};
    await get('recalcBtn').click();
    assert.equal(c.errorMessage, tr.t('admin_ranking.error.recalculate'));
    assert.equal(get('recalcBtn').disabled, false);
})().catch(error => {console.error(error);process.exitCode = 1;});
