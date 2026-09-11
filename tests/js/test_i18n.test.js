const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { create, createRuntime } = require('../../data/static/js/common/i18n.js');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/i18n_cases.json'), 'utf8'));
const locales = { en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE' };
const translator = language => create({ version: 1, language, locales, resources: fixture.catalogs });

test('display formatting keeps currency and precision while following the active locale', () => {
    const payload = language => ({version: 1, language, locales, resources: fixture.catalogs});
    const runtime = createRuntime(payload('en'));
    const currency = runtime.formatCurrency;
    const amount = 0.00001234;
    assert.equal(currency(amount, 'USD', 6), '$0.000012');
    assert.equal(currency(12.34, 'JPY', 2), '¥12.34');
    assert.equal(runtime.formatNumber(12345.5, {minimumFractionDigits: 2, maximumFractionDigits: 2}), '12,345.50');
    runtime.setPayload(payload('es'));
    assert.match(currency(amount, 'USD', 6), /^0,000012/);
    assert.equal(runtime.formatNumber(12345.5, {minimumFractionDigits: 2, maximumFractionDigits: 2}), '12.345,50');
    runtime.setPayload(payload('ja'));
    assert.equal(currency(12.34, 'USD', 2), '$12.34');
    assert.equal(amount, 0.00001234);
});

for (const item of fixture.cases) {
    test(`${item.language}: ${item.key} ${JSON.stringify(item.params)}`, () => {
        assert.equal(translator(item.language).render(item.key, item.params), item.expected);
    });
}

test('missing and invalid parameters fail explicitly', () => {
    const t = translator('es');
    assert.throws(() => t.render('sample.items', { count: true }));
    assert.throws(() => t.render('sample.items', { count: NaN }));
    assert.throws(() => t.render('sample.hello', { name: {} }));
    assert.throws(() => t.render('sample.hello', { name: 'A', other: 'B' }));
    assert.throws(() => t.render('sample.hello', {}));
    assert.throws(() => t.render('missing.key'));
});

test('instances retain their own locale', () => {
    const spanish = translator('es');
    const japanese = translator('ja');
    assert.equal(spanish.t('sample.hello', { name: 'A' }), 'Hola A');
    assert.equal(japanese.t('sample.hello', { name: 'A' }), 'こんにちは、A');
    assert.equal(spanish.language, 'es');
});

const runtimePayload = language => ({ version: 1, language, locales, resources: fixture.catalogs });
function label() {
    return { textContent: '', isConnected: true, attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }, getAttribute(key) { return this.attributes[key]; } };
}

test('live facade refreshes explicit bindings, retained translators and frame isolation', () => {
    const runtime = createRuntime(runtimePayload('es'));
    const independent = createRuntime(runtimePayload('es'));
    const retainedT = runtime.t;
    const node = label();
    const params = { name: '<b>{secret}</b>' };
    runtime.bindText(node, 'sample.hello', params);
    params.name = 'mutated';
    runtime.bindAttribute(node, 'title', 'sample.hello', { name: 'A' });
    runtime.setPayload(runtimePayload('ja'));
    assert.equal(node.textContent, retainedT('sample.hello', { name: '<b>{secret}</b>' }));
    assert.equal(node.attributes.title, retainedT('sample.hello', { name: 'A' }));
    assert.equal(runtime.language, 'ja');
    assert.equal(independent.language, 'es');
});

test('binding replacement, derived values, unsubscribe and invalid payload handling', () => {
    const runtime = createRuntime(runtimePayload('es'));
    const node = label();
    runtime.bindText(node, 'sample.hello', { name: 'old' });
    runtime.bindValue(node, () => runtime.t('sample.hello', { name: 'current' }));
    let updates = 0;
    const unsubscribe = runtime.onChange(() => { updates++; });
    runtime.setPayload(runtimePayload('ja'));
    assert.equal(node.textContent, runtime.t('sample.hello', { name: 'current' }));
    unsubscribe();
    runtime.setPayload(runtimePayload('es'));
    assert.equal(updates, 1);
    assert.throws(() => runtime.setPayload({ version: 2 }));
    assert.equal(runtime.language, 'es');
});

test('hydration ignores markers inside conversation content', () => {
    const owned = label();
    const content = label();
    content.textContent = 'User and assistant content';
    for (const node of [owned, content]) node.getAttribute = () => 'sample.hello';
    // A no-parameter catalog key keeps this focused on the ownership boundary.
    const payload = { version: 1, language: 'en', locales, resources: { en: { sample: { hello: 'Product label' } } } };
    owned.closest = () => null;
    content.closest = () => ({});
    const runtime = createRuntime(payload);
    runtime.hydrate({ querySelectorAll: selector => selector === '[data-i18n-text]' ? [owned, content] : [] });
    assert.equal(owned.textContent, 'Product label');
    assert.equal(content.textContent, 'User and assistant content');
});

test('repurposed nodes and explicitly unbound targets do not resurrect old labels', () => {
    const runtime = createRuntime(runtimePayload('es'));
    const node = label();
    runtime.bindText(node, 'sample.hello', { name: 'A' });
    runtime.bindAttribute(node, 'title', 'sample.hello', { name: 'A' });
    const oldTitle = node.attributes.title;
    runtime.unbind(node, 'title');
    node.textContent = 'New owner text';
    runtime.setPayload(runtimePayload('ja'));
    assert.equal(node.textContent, 'New owner text');
    assert.equal(node.attributes.title, oldTitle);
});
