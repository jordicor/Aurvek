const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const page = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).html;
const tr = create(JSON.parse(page.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]));
const nodes = new Map();
const get = id => {if (!nodes.has(id)) nodes.set(id, {style: {}, innerHTML: '', textContent: '', value: '', checked: false,
    disabled: false, classList: {add() {}, remove() {}}, children: [],
    appendChild(node) {this.children.push(node);}, insertAdjacentHTML(position, html) {this.innerHTML += html;},
    addEventListener(type, callback) {this[type] = callback;}});return nodes.get(id);};
const make = tag => tag === 'div' ? {set textContent(value) {this.innerHTML = String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}} : {innerHTML: ''};
let countries = [{value: 'ES'}, {value: 'JP'}];
let confirmText;
const c = vm.createContext({AurvekI18n: tr, console: {error() {}}, setTimeout() {}, window: {},
    confirm(message) {confirmText = message;return false;},
    document: {getElementById: get, createElement: make, addEventListener() {},
        querySelector: () => ({value: 'deny'}), querySelectorAll(selector) {return selector.includes('country-item input:checked') ? countries : [];}}});
for (const match of page.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
    if (match[1].includes('const geoText')) vm.runInContext(match[1], c);
}
(async () => {
    c.updateSelectedCount();
    assert.equal(get('selectedCount').textContent, tr.t('admin_geo.selected', {count: 2, number: tr.formatNumber(2)}));
    c.updateModeHelp('allow');
    assert.equal(get('modeHelp').textContent, tr.t('admin_geo.help.allow'));
    assert.equal(c.escapeHtml('x"\'<>'), 'x&quot;&#39;&lt;&gt;');
    c.populateLandingSummary([{id: 7, public_id: 'quoted"id', name: 'Authored <title>', mode: 'allow', countries: ['ES']}]);
    const row = get('landingSummaryBody').children[0].innerHTML;
    assert.ok(row.includes('Authored &lt;title&gt;') && row.includes('quoted&quot;id'));
    assert.ok(row.includes(tr.t('admin_geo.allow')));
    get('responseHtml').value = '<h1>Authored notice</h1>';
    get('saveGlobalBtn').innerHTML = 'original';
    let sent;
    c.secureFetch = async (url, options) => {sent = {url, options};return {json: async () => ({success: false, saved: true, message: tr.t('admin_geo.saved_pending')})};};
    await c.saveGlobalConfig();
    assert.deepEqual(JSON.parse(sent.options.body), {geo_enabled: false, mode: 'deny', countries: ['ES','JP'], continents: [], response_html: '<h1>Authored notice</h1>'});
    assert.ok(get('alertContainer').innerHTML.includes(c.escapeHtml(tr.t('admin_geo.saved_pending'))));
    assert.equal(get('saveGlobalBtn').disabled, false);
    c.secureFetch = async () => null;
    await c.saveGlobalConfig();
    assert.equal(get('saveGlobalBtn').disabled, false);
    c.secureFetch = async () => {throw Error('private network diagnostic');};
    await c.saveGlobalConfig();
    assert.ok(!get('alertContainer').innerHTML.includes('private network diagnostic'));
    await c.forceSync();
    assert.equal(confirmText, tr.t('admin_geo.confirm.sync'));
    await c.deleteLandingPolicy('same-id');
    assert.equal(confirmText, tr.t('admin_geo.confirm.delete', {public_id: 'same-id'}));
})().catch(error => {console.error(error);process.exitCode = 1;});
