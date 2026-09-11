const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('data/static/js/application-funding.js', 'utf8');

function setup(configured = true, ok = true) {
    function node(value = '') {
        return {value, checked: false, disabled: false, required: false, handlers: {}, textContent: '',
            addEventListener(name, handler) { this.handlers[name] = handler; },
            classList: {toggle() {}}};
    }
    const forms = [0, 1].map(index => {
        const active = node(), unlimited = node(), limit = node('7.5'), button = node();
        const controls = [active, unlimited, limit, button];
        const named = {active, unlimited, monthly_limit: limit};
        controls.namedItem = name => named[name];
        const feedback = node();
        return {...node(), dataset: {accountIndex: String(index), accountId: 'account-' + index, version: '0'},
            elements: controls, named, feedback, reportValidity: () => true,
            querySelector: () => feedback};
    });
    const config = {csrf: 'csrf-value', configured, base: '/applications/manage/katari/funding/accounts/',
        error: 'Could not save', saved: 'Saved'};
    const selector = node('0'), balance = node(), calls = [], clean = [], resumed = [];
    const nodes = {'funding-page-config': {textContent: JSON.stringify(config)}, 'funding-account': selector,
        'funding-balance': balance};
    const response = {payer: {balance: 12.34}, accounts: [{external_user_id: 'account-0',
        funding: {version: 1, active: true, monthly_limit: 7.5}}]};
    vm.runInNewContext(source, {document: {getElementById: id => nodes[id], querySelectorAll: () => forms},
        fetch: async (url, options) => { calls.push({url, options}); return {ok, json: async () => ok ? response : {message: 'CSRF denied'}}; },
        AurvekI18n: {locale: 'en-US'}, Intl,
        window: {FormGuard: {markClean: form => clean.push(form), resumeAfterSubmit: form => resumed.push(form)}}});
    return {forms, selector, balance, calls, clean, resumed};
}

test('selection keeps drafts and saving uses exact account, version and CSRF with no payer input', async () => {
    const state = setup();
    state.forms[1].named.monthly_limit.value = '19';
    state.selector.value = '1';
    state.selector.handlers.change({target: state.selector});
    state.selector.value = '0';
    state.selector.handlers.change({target: state.selector});
    assert.equal(state.forms[1].named.monthly_limit.value, '19');
    state.forms[0].named.active.checked = true;
    await state.forms[0].handlers.submit({preventDefault() {}});
    assert.equal(state.calls.length, 1);
    assert.equal(state.calls[0].url, '/applications/manage/katari/funding/accounts/account-0');
    assert.equal(state.calls[0].options.headers['X-GPTSub-CSRF'], 'csrf-value');
    assert.deepEqual(JSON.parse(state.calls[0].options.body), {active: true, monthly_limit: 7.5, expected_version: 0});
    assert.deepEqual(state.clean, [state.forms[0]]);
    assert.equal(state.forms[1].named.monthly_limit.value, '19');
    assert.equal(state.forms[0].dataset.version, '1');
});

test('missing wallet sends nothing and rejected update preserves the draft and dirty-state guard', async () => {
    const missing = setup(false);
    await missing.forms[0].handlers.submit({preventDefault() {}});
    assert.equal(missing.calls.length, 0);
    const denied = setup(true, false);
    denied.forms[0].named.monthly_limit.value = '3.5';
    await denied.forms[0].handlers.submit({preventDefault() {}});
    assert.equal(denied.forms[0].named.monthly_limit.value, '3.5');
    assert.equal(denied.forms[0].feedback.textContent, 'CSRF denied');
    assert.equal(denied.clean.length, 0);
    assert.deepEqual(denied.resumed, [denied.forms[0]]);
    assert.equal(denied.forms[0].named.active.disabled, false);
});
