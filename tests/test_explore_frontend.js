'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const explorePath = path.resolve(__dirname, '../data/static/js/explore.js');
const exploreSource = fs.readFileSync(explorePath, 'utf8');
const { create: createI18n } = require('../data/static/js/common/i18n.js');
const localeRegions = {en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE'};

function translator(language) {
    const resources = {};
    for (const code of new Set(['en', language])) {
        resources[code] = Object.fromEntries(['marketplace', 'common'].map(domain => [domain,
            JSON.parse(fs.readFileSync(path.resolve(__dirname, `../locales/${code}/${domain}.json`), 'utf8'))
        ]));
    }
    const runtime = createI18n({version: 1, language, locales: localeRegions, resources});
    return {...runtime, t: runtime.render}; // Fail explicitly on missing keys/parameters.
}

function escapeText(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

class FakeClassList {
    constructor() {
        this.values = new Set();
    }

    add(...values) {
        values.forEach(value => this.values.add(value));
    }

    remove(...values) {
        values.forEach(value => this.values.delete(value));
    }

    contains(value) {
        return this.values.has(value);
    }

    toggle(value) {
        if (this.values.has(value)) {
            this.values.delete(value);
            return false;
        }
        this.values.add(value);
        return true;
    }
}

class FakeElement {
    constructor(id = '') {
        this.id = id;
        this.innerHTML = '';
        this.style = {};
        this.dataset = {};
        this.classList = new FakeClassList();
        this.disabled = false;
        this._textContent = '';
    }

    set textContent(value) {
        this._textContent = String(value);
        this.innerHTML = escapeText(value);
    }

    get textContent() {
        return this._textContent;
    }

    addEventListener() {}
    querySelector() { return null; }
    querySelectorAll() { return []; }
    replaceWith() {}
}

function createHarness(language = 'en') {
    const elementIds = [
        'categoryChips',
        'exploreGrid',
        'exploreLoader',
        'exploreModalBackdrop',
        'explorePagination',
        'modalContent',
        'resultsInfo', 'previewItemName', 'previewItemCreator', 'previewCtaBtn',
        'previewCounter', 'previewPrevBtn', 'previewNextBtn', 'packPurchaseBtn', 'packPurchaseError'
    ];
    const elements = Object.fromEntries(
        elementIds.map(id => [id, new FakeElement(id)])
    );
    const pendingFetches = [];

    const document = {
        body: { style: {} },
        addEventListener() {},
        createElement: () => new FakeElement(),
        getElementById: id => elements[id] || null,
        querySelector: () => null,
        querySelectorAll: () => []
    };

    function fetch(url, options = {}) {
        return new Promise((resolve, reject) => {
            pendingFetches.push({
                url: String(url),
                options,
                reject,
                respond(data, status = 200) {
                    resolve({
                        ok: status >= 200 && status < 300,
                        status,
                        json: async () => data
                    });
                }
            });
        });
    }

    const context = vm.createContext({
        AurvekI18n: translator(language),
        AbortController,
        URLSearchParams,
        alert() {},
        clearTimeout,
        console: { error() {}, log() {}, warn() {} },
        document,
        fetch,
        navigator: {},
        setTimeout,
        window: { location: { href: 'https://example.test/explore' } }
    });
    vm.runInContext(exploreSource, context, { filename: explorePath });

    return {
        context,
        elements,
        evaluate: expression => vm.runInContext(expression, context),
        pendingFetches
    };
}

test('dynamic prompt data never becomes an inline event handler', () => {
    const harness = createHarness();
    harness.context.maliciousPrompt = {
        id: 7,
        name: "');globalThis.explorePwned=true;//<img src=x>",
        description: 'description',
        image_url: '/broken-image',
        categories: [],
        is_favorite: false,
        user_has_access: true
    };

    harness.evaluate('ExploreState.prompts = [maliciousPrompt]; renderPrompts();');
    harness.evaluate('openModal(maliciousPrompt);');

    assert.doesNotMatch(exploreSource, /\bon(?:click|error)\s*=/i);
    assert.doesNotMatch(
        harness.elements.exploreGrid.innerHTML,
        /\bon(?:click|error)\s*=/i
    );
    assert.doesNotMatch(
        harness.elements.modalContent.innerHTML,
        /\bon(?:click|error)\s*=/i
    );
    assert.equal(harness.evaluate('globalThis.explorePwned'), undefined);
});

test('a late prompt search response cannot overwrite the latest search', async () => {
    const harness = createHarness();
    harness.evaluate("ExploreState.searchQuery = 'old';");
    const oldRequest = harness.evaluate('loadPrompts();');
    harness.evaluate("ExploreState.searchQuery = 'new';");
    const newRequest = harness.evaluate('loadPrompts();');

    assert.equal(harness.pendingFetches.length, 2);
    assert.equal(harness.pendingFetches[0].options.signal.aborted, true);
    assert.match(harness.pendingFetches[1].url, /search=new/);

    harness.pendingFetches[1].respond({
        prompts: [{ id: 2, name: 'new result', categories: [] }],
        total: 1,
        total_pages: 1,
        page: 1
    });
    await newRequest;

    harness.pendingFetches[0].respond({
        prompts: [{ id: 1, name: 'stale result', categories: [] }],
        total: 1,
        total_pages: 1,
        page: 1
    });
    await oldRequest;

    assert.equal(harness.evaluate('ExploreState.prompts[0].name'), 'new result');
    assert.match(harness.elements.exploreGrid.innerHTML, /new result/);
    assert.doesNotMatch(harness.elements.exploreGrid.innerHTML, /stale result/);
});

test('a late prompt response cannot replace the packs tab', async () => {
    const harness = createHarness();
    const promptRequest = harness.evaluate('loadPrompts();');
    harness.evaluate("ExploreState.activeTab = 'packs';");
    const packRequest = harness.evaluate('loadPacks();');

    assert.equal(harness.pendingFetches[0].options.signal.aborted, true);
    harness.pendingFetches[1].respond({
        packs: [{ id: 20, name: 'current pack', item_count: 0 }],
        total: 1,
        pages: 1,
        page: 1
    });
    await packRequest;

    harness.pendingFetches[0].respond({
        prompts: [{ id: 10, name: 'stale prompt', categories: [] }],
        total: 1,
        total_pages: 1,
        page: 1
    });
    await promptRequest;

    assert.equal(harness.evaluate('ExploreState.activeTab'), 'packs');
    assert.equal(harness.evaluate('ExploreState.packs[0].name'), 'current pack');
    assert.match(harness.elements.exploreGrid.innerHTML, /current pack/);
    assert.doesNotMatch(harness.elements.exploreGrid.innerHTML, /stale prompt/);
});

test('public explore embeds do not use the owner-only preview bypass', () => {
    assert.doesNotMatch(exploreSource, /\?preview=1/);
    assert.match(exploreSource, /\?embed=1/);
});

test('isolated landing purchase requests require a trusted parent confirmation', () => {
    assert.match(exploreSource, /event\.source !== iframe\.contentWindow/);
    assert.match(exploreSource, /event\.data\.type !== 'aurvek-purchase-request'/);
    assert.match(exploreSource, /NotificationModal\.confirm/);
    assert.match(exploreSource, /\/purchase\/prompt\//);
});

for (const language of Object.keys(localeRegions)) {
    test(`marketplace renders cards, modals, plurals and USD prices in ${language}`, () => {
        const harness = createHarness(language);
        const tr = harness.context.AurvekI18n;
        harness.context.pack = {
            name: 'Creator <title>', created_by_username: 'Name <img src=x>',
            item_count: 2, is_paid: true, price: 1234.56, status: 'draft',
        };
        harness.evaluate("ExploreState.activeFilter = 'mine'; ExploreState.packs = [pack]; renderPacks(); openPackModal(pack); updatePreviewBar(pack, 'pack');");
        const html = harness.elements.exploreGrid.innerHTML;
        assert.ok(html.includes(escapeText(tr.t('marketplace.explore.prompt_count', {count: 2, number: tr.formatNumber(2)}))));
        assert.ok(html.includes(escapeText(tr.formatCurrency(1234.56))));
        assert.ok(html.includes('Creator &lt;title&gt;'));
        assert.doesNotMatch(html, /Name <img/);
        assert.ok(harness.elements.modalContent.innerHTML.includes(escapeText(tr.t('marketplace.explore.pack_draft'))));
        assert.equal(harness.elements.previewCtaBtn.textContent, tr.t('marketplace.explore.buy_price', {price: tr.formatCurrency(1234.56)}));

        for (const tab of ['packs', 'prompts']) {
            for (const count of [1, 2, 1234]) {
                harness.evaluate(`ExploreState.activeTab = '${tab}'; ExploreState.total = ${count}; updateResultsBar();`);
                assert.equal(harness.elements.resultsInfo.textContent, tr.t(`marketplace.explore.results_${tab}`, {
                    count, start: tr.formatNumber(1), end: tr.formatNumber(Math.min(count, 24)), total: tr.formatNumber(count)
                }));
            }
        }
        harness.context.prompt = {id: 5, name: 'Creator text', is_paid: true, purchase_price: 19.99};
        harness.evaluate('openModal(prompt);');
        assert.ok(harness.elements.modalContent.innerHTML.includes(escapeText(tr.t('marketplace.explore.one_time', {price: tr.formatCurrency(19.99)}))));
        assert.ok(harness.elements.modalContent.innerHTML.includes(escapeText(tr.t('marketplace.explore.unlock_vip'))));
    });

    test(`marketplace async failure and sharing use ${language} without translating provider data`, async () => {
        const harness = createHarness(language);
        const tr = harness.context.AurvekI18n;
        const request = harness.evaluate('loadPrompts();');
        harness.pendingFetches[0].respond({}, 500);
        await request;
        assert.ok(harness.elements.exploreGrid.innerHTML.includes(escapeText(tr.t('marketplace.explore.load_prompts_error'))));

        let shared;
        harness.context.navigator.share = async data => { shared = data; };
        await harness.evaluate(`sharePrompt('Untouched creator name', 'https://example.test/p/1');`);
        assert.equal(shared.text, tr.t('marketplace.explore.share_text', {name: 'Untouched creator name'}));
        assert.equal(shared.title, 'Untouched creator name');
        assert.equal(shared.url, 'https://example.test/p/1');

        let warning;
        harness.context.NotificationModal = {warning: (...args) => { warning = args; }};
        harness.elements.packPurchaseBtn.innerHTML = 'original button';
        const purchase = harness.evaluate("purchasePack(10, '');");
        assert.equal(harness.elements.packPurchaseBtn.disabled, true);
        assert.ok(harness.elements.packPurchaseBtn.innerHTML.includes(escapeText(tr.t('marketplace.explore.processing'))));
        harness.pendingFetches[1].respond({}, 401);
        await purchase;
        assert.deepEqual(warning, [tr.t('marketplace.explore.login_required'), tr.t('marketplace.explore.login_purchase')]);
        assert.equal(harness.elements.packPurchaseBtn.disabled, false);
        assert.equal(harness.elements.packPurchaseBtn.innerHTML, 'original button');
    });
}
