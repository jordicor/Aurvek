const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const tr = create(JSON.parse(fixture.html.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]));
const nodes = new Map();
function make() {
    return {children: [], dataset: {}, style: {}, value: '', textContent: '',
        classList: {add() {}, remove() {}}, addEventListener(type, handler) {this[type] = handler;},
        appendChild(child) {this.children.push(child);}, replaceChildren(...children) {this.children = children;},
        setAttribute(key, value) {this[key] = value;}, scrollIntoView() {}};
}
const get = id => {if (!nodes.has(id)) nodes.set(id, make());return nodes.get(id);};
const c = vm.createContext({AurvekI18n: tr, console,
    document: {getElementById: get, createElement: make, querySelectorAll: () => []},
    NotificationModal: {confirm(title, message, callback) {c.confirmation = {message, callback};}},
    FormGuard: {markClean() {}}, setTimeout() {}, location: {reload() {}},
});
for (const match of fixture.html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
    if (match[1].includes('const categoryText')) vm.runInContext(match[1], c);
}
(async () => {
    const category = JSON.parse(fixture.data);
    c.editCategoryData({dataset: {category: fixture.data}});
    assert.equal(get('categoryName').value, category.name);
    assert.equal(get('displayOrder').value, 1234);
    assert.equal(get('isAgeRestricted').checked, true);
    let sent;
    c.fetch = async (url, options) => {sent = {url, method: options.method, body: options.body && JSON.parse(options.body)};return {ok: true, json: async () => ({success: true})};};
    await get('categoryForm').submit({preventDefault() {}});
    assert.equal(sent.url, '/api/categories/41');
    assert.equal(sent.method, 'PUT');
    assert.equal(sent.body.name, category.name);
    assert.equal(sent.body.display_order, 1234);
    c.deleteCategory(41, category.name);
    assert.equal(c.confirmation.message, tr.t('admin_categories.dialog.delete_message', {name: category.name}));
    await c.confirmation.callback();
    assert.equal(sent.method, 'DELETE');assert.equal(sent.url, '/api/categories/41');
    c.fetch = async () => {throw Error('<img> raw network diagnostics');};
    await c.confirmation.callback();
    const alert = get('alertContainer').children.at(-1);
    assert.equal(alert.textContent, tr.t('common.error.generic'));
    assert.equal(alert.children[0]['aria-label'], tr.t('admin_categories.action.close'));
})().catch(error => {console.error(error);process.exitCode = 1;});
