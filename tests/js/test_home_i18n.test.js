const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'data/static/js/home.js'), 'utf8');
const regions = {en: 'en-US', es: 'es-ES', ja: 'ja-JP', fr: 'fr-FR', pt: 'pt-PT', it: 'it-IT', de: 'de-DE'};
const escape = value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

class Element {
    constructor() {
        this.style = {};
        this.children = [];
        this.options = [{}];
        this.listeners = {};
        this.classList = {add() {}, remove() {}, toggle() {}};
    }
    set textContent(value) { this.text = String(value); this.innerHTML = escape(value); }
    get textContent() { return this.text; }
    appendChild(child) { this.children.push(child); }
    addEventListener(name, handler) { this.listeners[name] = handler; }
    querySelector() { return this.child ||= new Element(); }
    querySelectorAll() { return []; }
}

for (const [language, locale] of Object.entries(regions)) {
    test(`home initializes ${language} greetings, library, welcome messages and dock with creator text preserved`, async () => {
        const resources = {};
        for (const code of new Set(['en', language])) {
            resources[code] = {marketplace: JSON.parse(fs.readFileSync(path.join(root, `locales/${code}/marketplace.json`), 'utf8'))};
        }
        const tr = create({version: 1, language, locales: regions, resources});
        const elements = new Map();
        const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
        let initialize;
        const document = {
            documentElement: {style: {setProperty() {}}}, body: {style: {}},
            getElementById: get, querySelector: get, querySelectorAll: () => [],
            createElement: () => new Element(),
            addEventListener(name, handler) { if (name === 'DOMContentLoaded') initialize = handler; },
        };
        const creatorName = 'Author " <tag>';
        const ownedName = 'Creator prompt " <b>';
        const content = '<p>Creator HTML <strong>unchanged</strong></p>';
        const context = vm.createContext({
            AurvekI18n: {...tr, t: tr.render}, document,
            window: {_homeConfig: {marketplaceEnabled: true, marketplaceDiscoveryEnabled: true}},
            Date: class extends Date { getHours() { return 10; } },
            clearTimeout() {}, setTimeout() {}, console,
            fetch: async url => ({ok: true, json: async () => url === '/api/home' ? {
                user: {username: 'User <name>'}, branding: {},
                prompts: [{id: 1, name: ownedName, is_mine: true, has_welcome: true}],
                packs: [], favorites: [1], latest_prompts: [{id: 2, name: 'Latest creator', has_welcome: true, created_at: new Date().toISOString().replace('Z', '')}],
                home_preferences: {minimized_windows: ['welcome', 'latest', 'library']},
            } : {messages: [{id: 1, name: ownedName, creator_name: creatorName, entity_type: 'prompt', entity_id: 1, has_welcome_page: true, content, is_read: true}]}}),
        });
        vm.runInContext(source, context);
        await initialize();
        assert.equal(get('home-greeting-title').textContent, tr.t('marketplace.home.morning', {name: 'User <name>'}));
        assert.equal(get('home-greeting-subtitle').textContent, tr.t('marketplace.home.ready'));
        assert.equal(get('home-prompt-select').children[0].label, '\u2605 ' + tr.t('marketplace.explore.favorites'));
        assert.equal(get('home-prompt-select').children[0].children[0].textContent, ownedName);
        assert.ok(get('welcome-display').innerHTML.includes(content));
        assert.ok(get('welcome-display').innerHTML.includes(escape(tr.t('marketplace.explore.by_creator', {name: creatorName})).replace(/"/g, '&quot;').replace(/'/g, '&#39;')));
        assert.ok(get('home-favorites').innerHTML.includes('Creator prompt &quot; &lt;b&gt;'));
        assert.ok(get('dock').innerHTML.includes(tr.t('marketplace.home.library_short')));
        const packButton = {getAttribute: () => 'packs', classList: {add() {}}};
        get('home-tab-toggle').listeners.click({target: {closest: () => packButton}});
        assert.equal(get('home-library-empty').querySelector('p').textContent, tr.t('marketplace.home.no_packs'));
    });
}
