const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const repoRoot = path.resolve(__dirname, '..', '..');
const template = fs.readFileSync(
    path.join(repoRoot, 'templates/admin_profile.html'),
    'utf8'
);

function inlineScript(branch) {
    const script = template.match(/{% block scripts_inline %}\s*<script>([\s\S]*?)<\/script>/)[1];
    const marker = "{% if auth_provider in ('google', 'google_linked') and not has_password %}";
    const start = script.indexOf(marker);
    const otherwise = script.indexOf('{% else %}', start);
    const end = script.indexOf('{% endif %}', otherwise);
    return script.slice(0, start)
        + (branch === 'set' ? script.slice(start + marker.length, otherwise) : script.slice(otherwise + 10, end))
        + script.slice(end + 11);
}

function classList(initial = []) {
    const classes = new Set(initial);
    return {
        add(...names) { names.forEach(name => classes.add(name)); },
        remove(...names) { names.forEach(name => classes.delete(name)); },
        contains(name) { return classes.has(name); },
    };
}

function element(base = {}) {
    const listeners = new Map();
    return Object.assign({
        textContent: '',
        className: '',
        style: {},
        disabled: false,
        children: [],
        attributes: {},
        classList: classList(),
        addEventListener(type, callback) { listeners.set(type, callback); },
        async dispatch(type) {
            return listeners.get(type)({ preventDefault() {} });
        },
        replaceChildren(...children) {
            this.children = children;
            this.textContent = children.map(child => child.textContent || '').join('');
        },
        setAttribute(name, value) { this.attributes[name] = value; },
    }, base);
}

function fixture(branch) {
    const icon = element({ classList: classList(['fas', 'fa-eye']) });
    const elements = {
        'new-password': element({ type: 'password', value: '' }),
        'confirm-password': element({ type: 'password', value: '' }),
        'old-password': element({ type: 'password', value: '' }),
        'req-length': element(),
        'req-different': element(),
        'password-match-status': element(),
        'error-message': element(),
        'error-text': element(),
        'success-message': element(),
        'success-text': element(),
        'submit-btn': element({ querySelector: () => icon }),
        'set-password-form': element(),
        'change-password-form': element(),
    };
    const translations = {
        'account.password.match': 'Coinciden',
        'account.password.no_match': 'No coinciden',
        'account.password.error.no_match': 'Las contraseñas no coinciden.',
        'account.password.error.min_length': 'Mínimo 8 caracteres.',
        'account.password.error.must_differ': 'Debe ser distinta.',
        'account.password.progress.setting': 'Estableciendo...',
        'account.password.progress.changing': 'Cambiando...',
        'account.password.success.set_redirecting': 'Establecida. Redirigiendo...',
        'account.password.success.change_redirecting': 'Cambiada. Redirigiendo...',
        'account.password.error.set_failed': 'No se pudo establecer.',
        'account.password.error.change_failed': 'No se pudo cambiar.',
        'account.password.set_action': 'Establecer contraseña',
        'account.password.change_action': 'Cambiar contraseña',
        'common.action.show_password': 'Mostrar contraseña',
        'common.action.hide_password': 'Ocultar contraseña',
        'common.error.generic': 'Algo ha salido mal.',
    };
    const clean = [];
    const timeouts = [];
    const context = {
        document: {
            getElementById: id => elements[id],
            createElement: () => element(),
            createTextNode: textContent => ({ textContent }),
        },
        window: { location: { href: '' } },
        AurvekI18n: { t: key => translations[key] },
        FormGuard: { markClean: form => clean.push(form) },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        setTimeout(callback, delay) { timeouts.push({ callback, delay }); },
        URLSearchParams,
        console,
    };
    vm.createContext(context);
    vm.runInContext(inlineScript(branch), context);
    return { context, elements, clean, timeouts, icon };
}

test('localized change-password UI validates, shows progress and restores after API error', async () => {
    const { context, elements } = fixture('change');
    elements['new-password'].value = 'new-password';
    elements['confirm-password'].value = 'different';
    await elements['change-password-form'].dispatch('submit');
    assert.equal(elements['error-text'].textContent, 'Las contraseñas no coinciden.');

    elements['confirm-password'].value = 'new-password';
    elements['old-password'].value = 'old-password';
    let finish;
    context.fetch = () => new Promise(resolve => { finish = resolve; });
    const pending = elements['change-password-form'].dispatch('submit');
    await Promise.resolve();
    assert.equal(elements['submit-btn'].disabled, true);
    assert.equal(elements['submit-btn'].textContent.trim(), 'Cambiando...');

    finish({ ok: false, json: async () => ({ detail: 'Error localizado del servidor' }) });
    await pending;
    assert.equal(elements['error-text'].textContent, 'Error localizado del servidor');
    assert.equal(elements['submit-btn'].disabled, false);
    assert.equal(elements['submit-btn'].textContent.trim(), 'Cambiar contraseña');
});

test('localized set-password UI reports match, success and accessible visibility state', async () => {
    const { context, elements, clean, timeouts, icon } = fixture('set');
    elements['new-password'].value = 'new-password';
    elements['confirm-password'].value = 'new-password';
    context.checkSetPasswordMatch();
    assert.equal(elements['password-match-status'].textContent.trim(), 'Coinciden');

    const toggle = element({ querySelector: () => icon });
    context.togglePassword('new-password', toggle);
    assert.equal(toggle.attributes['aria-label'], 'Ocultar contraseña');
    assert.equal(toggle.attributes.title, 'Ocultar contraseña');

    await elements['set-password-form'].dispatch('submit');
    assert.equal(clean.length, 1);
    assert.equal(elements['success-text'].textContent, 'Establecida. Redirigiendo...');
    assert.equal(elements['success-message'].style.display, 'block');
    assert.equal(timeouts[0].delay, 1500);
});
