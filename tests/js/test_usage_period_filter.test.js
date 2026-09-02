'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');

function readSource(relativePath) {
    return fs.readFileSync(path.join(repoRoot, relativePath), 'utf8');
}

function extractInlineScript(relativePath) {
    const template = readSource(relativePath);
    const blockStart = template.indexOf('{% block scripts_inline %}');
    assert.notEqual(blockStart, -1, `${relativePath} has no scripts_inline block`);

    const scriptMatch = template.slice(blockStart).match(/<script>([\s\S]*?)<\/script>/);
    assert.ok(scriptMatch, `${relativePath} has no inline script`);
    return scriptMatch[1];
}

function fakeDocument(elements) {
    return {
        head: { appendChild() {} },
        addEventListener() {},
        createElement() { return {}; },
        getElementById(id) { return elements[id] || null; },
        querySelectorAll() { return []; },
    };
}

async function settingsUsageUrl(period) {
    let requestedUrl = null;
    const document = fakeDocument({
        usageDateRange: { value: period },
    });
    const context = {
        URLSearchParams,
        bootstrap: { Tab: function Tab() {} },
        console: { error() {} },
        document,
        history: { replaceState() {} },
        secureFetch: async url => {
            requestedUrl = String(url);
            return { ok: false };
        },
        window: { location: { hash: '' } },
    };

    vm.createContext(context);
    vm.runInContext(
        readSource('data/static/js/settings.js'),
        context,
        { filename: 'data/static/js/settings.js' }
    );
    await context.window.loadUsageData();
    return requestedUrl;
}

async function legacyUsageUrl(period) {
    let requestedUrl = null;
    const context = {
        URLSearchParams,
        NotificationModal: { error() {} },
        console: { error() {} },
        document: fakeDocument({ dateRange: { value: period } }),
        fetch: async url => {
            requestedUrl = String(url);
            return { ok: false };
        },
    };

    vm.createContext(context);
    vm.runInContext(
        extractInlineScript('templates/my_usage.html'),
        context,
        { filename: 'templates/my_usage.html#scripts_inline' }
    );
    await context.loadUsageData();
    return requestedUrl;
}

function createAdminHarness(period, type = 'image') {
    let requestedUrl = null;
    const window = { location: { href: '' } };
    const context = {
        URLSearchParams,
        NotificationModal: { error() {} },
        clearTimeout() {},
        console: { error() {} },
        document: fakeDocument({
            dateRange: { value: period },
            typeFilter: { value: type },
            userSearch: { value: '' },
        }),
        fetch: async url => {
            requestedUrl = String(url);
            return { ok: false };
        },
        setTimeout() { return 1; },
        window,
    };

    vm.createContext(context);
    vm.runInContext(
        extractInlineScript('templates/admin_usage.html'),
        context,
        { filename: 'templates/admin_usage.html#scripts_inline' }
    );

    return {
        context,
        requestedUrl: () => requestedUrl,
        window,
    };
}

for (const [period, query] of [['all', 'days=0'], ['90', 'days=90']]) {
    test(`settings usage maps ${period} to ${query}`, async () => {
        assert.equal(
            await settingsUsageUrl(period),
            `/api/my-usage?${query}`
        );
    });

    test(`legacy usage maps ${period} to ${query}`, async () => {
        assert.equal(
            await legacyUsageUrl(period),
            `/api/my-usage?${query}`
        );
    });

    test(`admin usage maps ${period} to ${query}`, async () => {
        const harness = createAdminHarness(period);
        await harness.context.loadUsageData();
        assert.equal(
            harness.requestedUrl(),
            `/api/admin/usage?${query}&type=image`
        );
    });

    test(`admin CSV export maps ${period} to ${query}`, () => {
        const harness = createAdminHarness(period);
        harness.context.exportUsageCSV();
        assert.equal(
            harness.window.location.href,
            `/api/admin/usage/export?${query}&type=image&format=csv`
        );
    });
}
