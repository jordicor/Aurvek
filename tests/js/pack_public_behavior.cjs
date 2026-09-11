// Run the real Jinja-rendered public landing script with local browser/API fakes.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {create} = require('../../data/static/js/common/i18n.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const i18n = create(input.payload);
const nodes = new Map(), requests = [];
function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
        value: '', textContent: '', innerHTML: 'original button <i></i>',
        disabled: false, style: {}, listeners: {},
        addEventListener(name, callback) { this.listeners[name] = callback; },
        reset() { this.resetCalled = true; }
    });
    return nodes.get(id);
}
const form = node('packRegForm');
for (const name of ['email', 'password', 'password_confirm', 'pack_id', 'public_id']) form[name] = node(name);
form.pack_id.value = '42'; form.public_id.value = 'AbCd1234';
let response = {ok: false, status: 400, data: {}}, throwNetwork = false;
const location = {href: '', pathname: '/pack/AbCd1234/fixture/'};
const context = vm.createContext({
    document: {getElementById: node}, window: {location}, console,
    AurvekI18n: {...i18n, t: i18n.render},
    fetch: async (url, options) => {
        if (url === '/api/check-session') return {ok: true, json: async () => ({expired: true})};
        requests.push({url, options});
        if (throwNetwork) throw new Error('local network fixture');
        return {ok: response.ok, status: response.status, json: async () => response.data};
    }
});
vm.runInContext(input.script, context);
async function submit() { await form.listeners.submit({preventDefault() {}}); }
async function run() {
    await submit();
    assert.equal(node('regError').textContent, i18n.render('pack_public.fields_required'));
    form.email.value = 'fixture@example.test'; form.password.value = 'short'; form.password_confirm.value = 'short';
    await submit();
    assert.equal(node('regError').textContent, i18n.render('account.password.error.min_length', {count:8}));
    form.password.value = 'longpassword'; form.password_confirm.value = 'mismatch';
    await submit();
    assert.equal(node('regError').textContent, i18n.render('account.password.error.no_match'));
    assert.equal(requests.length, 0);
    form.password_confirm.value = 'longpassword';
    await submit();
    assert.equal(node('regError').textContent, i18n.render('auth.error.registration_failed'));
    let sent = JSON.parse(requests.at(-1).options.body);
    assert.equal(sent.pack_id, 42); assert.equal(sent.public_id, 'AbCd1234');
    assert.equal(sent.ui_language, input.payload.language);
    response = {ok:true, status:200, data:{message:'Original <server> 日本語'}};
    await submit();
    assert.equal(node('regSuccess').textContent, 'Original <server> 日本語');
    assert.equal(node('regSubmitBtn').textContent, i18n.render(input.paid ? 'pack_public.sign_up' : 'pack_public.sign_up_access'));
    assert.equal(node('regSubmitBtn').disabled, false);
    assert.equal(form.resetCalled, true);
    throwNetwork = true;
    await submit();
    assert.equal(node('regError').textContent, i18n.render('marketplace.explore.connection_error'));
    throwNetwork = false;

    const action = input.paid ? 'purchasePack' : 'claimFreePack';
    const button = node(input.paid ? 'purchaseBtn' : 'claimFreeBtn');
    const error = node(input.paid ? 'purchaseError' : 'claimError');
    response = {ok:false, status:400, data:{}};
    await context[action]();
    assert.equal(error.textContent, i18n.render(input.paid ? 'marketplace.explore.purchase_failed' : 'marketplace.explore.claim_failed'));
    assert.equal(button.innerHTML, 'original button <i></i>');
    assert.equal(button.disabled, false);
    assert.equal(requests.at(-1).url, '/api/packs/42/' + (input.paid ? 'purchase' : 'claim-free'));
    throwNetwork = true;
    await context[action]();
    assert.equal(error.textContent, i18n.render('marketplace.explore.connection_error'));
    assert.equal(button.innerHTML, 'original button <i></i>');
    throwNetwork = false;
    response = {ok:true, status:200, data:{checkout_url:'https://checkout.example.test/fixture', redirect:'/chat?fixture=1'}};
    await context[action]();
    assert.equal(location.href, input.paid ? 'https://checkout.example.test/fixture' : '/chat?fixture=1');
    if (!input.paid) {
        response = {ok:false, status:401, data:{}};
        await context[action]();
        assert.equal(location.href, '/login?next=' + encodeURIComponent(location.pathname));
    }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
