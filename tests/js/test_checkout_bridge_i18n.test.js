const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const root = path.resolve(__dirname, '../..');
const template = fs.readFileSync(path.join(root, 'templates/prompt_purchase_bridge.html'), 'utf8');
const script = template.match(/<script>([\s\S]*?)<\/script>/)[1].replace('{{ prompt_id }}', '10');
const locales = {en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE'};

for (const language of Object.keys(locales)) {
    test(`checkout bridge ${language} handles retry, network errors and trusted localized responses`, async () => {
        const resources = {};
        for (const code of new Set(['en', language])) {
            resources[code] = {marketplace: JSON.parse(fs.readFileSync(path.join(root, `locales/${code}/marketplace.json`), 'utf8'))};
        }
        const tr = create({version: 1, language, locales, resources});
        const elements = {
            checkoutStatus: {textContent: '', className: ''},
            checkoutRetry: {
                hidden: true,
                classList: {add() { elements.checkoutRetry.hidden = true; }, remove() { elements.checkoutRetry.hidden = false; }},
                addEventListener(name, callback) { this.retry = callback; },
            },
        };
        let failure = true;
        let redirect;
        let response;
        const context = {
            window: {AurvekI18n: {...tr, t: tr.render}, location: {assign(value) { redirect = value; }}},
            document: {getElementById: id => elements[id]},
            fetch: async (url, options) => {
                assert.equal(url, '/api/prompts/10/purchase');
                assert.equal(options.method, 'POST');
                assert.deepEqual(JSON.parse(options.body), {});
                if (failure) throw new TypeError('English browser error with private details');
                return response;
            },
        };
        vm.runInNewContext(script, context);
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(elements.checkoutStatus.textContent, tr.t('marketplace.purchase.failed'));
        assert.equal(elements.checkoutRetry.hidden, false);

        failure = false;
        const message = tr.t('marketplace.checkout.prompt_self_purchase');
        response = {ok: false, json: async () => ({detail: message})};
        await elements.checkoutRetry.retry();
        assert.equal(elements.checkoutStatus.textContent, message);
        response = {ok: false, json: async () => ({detail: {private: 'details'}})};
        await elements.checkoutRetry.retry();
        assert.equal(elements.checkoutStatus.textContent, tr.t('marketplace.purchase.failed'));
        response = {ok: true, json: async () => ({checkout_url: 'https://checkout.example.test/session'})};
        await elements.checkoutRetry.retry();
        assert.equal(redirect, 'https://checkout.example.test/session');
    });
}
