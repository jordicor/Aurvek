/* Shared catalog renderer. Catalogs are data; render text with textContent. */
(function (root) {
    'use strict';

    const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
    const token = /\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}|[{}]/g;

    function create(payload) {
        if (!payload || payload.version !== 1 || !payload.resources?.en ||
            !payload.resources[payload.language] || !payload.locales?.[payload.language]) {
            throw new Error('Invalid i18n payload');
        }
        const { language, resources, locales } = payload;
        const pluralRules = new Map();

        function message(locale, domain, key) {
            const messages = resources[locale] && resources[locale][domain];
            return messages && own(messages, key) ? messages[key] : undefined;
        }

        function render(key, params = {}) {
            const separator = key.indexOf('.');
            if (separator < 1) throw new Error('A message key must include its domain');
            const domain = key.slice(0, separator);
            const name = key.slice(separator + 1);
            let selectedLanguage = language;
            let pattern = message(language, domain, name);
            if (pattern === undefined) {
                selectedLanguage = 'en';
                pattern = message('en', domain, name);
            }
            if (pattern === undefined) throw new Error('Unknown message key');
            const required = new Set();
            if (typeof pattern !== 'string') {
                if (typeof params.count !== 'number' || !Number.isFinite(params.count)) {
                    throw new Error('Plural messages require a finite numeric count');
                }
                if (!pluralRules.has(selectedLanguage)) {
                    pluralRules.set(selectedLanguage, new Intl.PluralRules(locales[selectedLanguage], {
                        maximumFractionDigits: 20,
                    }));
                }
                const category = pluralRules.get(selectedLanguage).select(params.count);
                pattern = own(pattern, category) ? pattern[category] : pattern.other;
                required.add('count');
            }
            if (typeof pattern !== 'string') throw new Error('Invalid message variant');
            for (const match of pattern.matchAll(token)) {
                if (match[1]) required.add(match[1]);
                else if (match[0] !== '{{' && match[0] !== '}}') throw new Error('Invalid message placeholder');
            }
            const names = Object.keys(params);
            if (names.length !== required.size || names.some(name => !required.has(name))) {
                throw new Error('Message parameters do not match the catalog');
            }
            for (const value of Object.values(params)) {
                if (typeof value !== 'string' && !(typeof value === 'number' && Number.isFinite(value))) {
                    throw new Error('Message parameters must be text or finite numbers');
                }
            }
            return pattern.replace(token, (match, name) => name ? String(params[name]) : match[0]);
        }

        function t(key, params) {
            try {
                return render(key, params);
            } catch (_) {
                // Values can contain private text: log only message identity.
                console.error('Invalid UI message', key, language);
                return message(language, 'common', 'error.generic') || message('en', 'common', 'error.generic');
            }
        }

        function formatNumber(value, options = {}) {
            return new Intl.NumberFormat(locales[language], options).format(value);
        }

        function formatCurrency(value, currency = 'USD', fractionDigits = 2) {
            return formatNumber(value, {
                style: 'currency', currency,
                minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits,
            });
        }

        return Object.freeze({ language, locale: locales[language], t, render, formatNumber, formatCurrency });
    }

    // Existing consumers may retain t: the facade always delegates to the
    // active immutable translator. Bind only product-owned label nodes.
    function createRuntime(payload, document) {
        let translator = create(payload);
        const bindings = new WeakMap();
        const elements = new Set();
        let pruning = elements.values();
        const listeners = new Set();
        function bind(element, attribute, key, params = {}, renderValue) {
            if (!element) return;
            let binding = bindings.get(element);
            if (!binding) {
                // Amortize stale weak-reference cleanup without scanning the
                // complete message history whenever another label is created.
                let candidate = pruning.next();
                if (candidate.done) { pruning = elements.values(); candidate = pruning.next(); }
                if (!candidate.done && !candidate.value.deref()) elements.delete(candidate.value);
                binding = { ref: new WeakRef(element), targets: new Map() };
                bindings.set(element, binding);
                elements.add(binding.ref);
            }
            const { targets } = binding;
            const text = renderValue ? renderValue() : translator.t(key, params);
            if (attribute === null) element.textContent = text;
            else element.setAttribute(attribute, text);
            targets.set(attribute, { key, params: { ...params }, renderValue, lastRendered: String(text) });
        }
        function hydrate(scope = document) {
            if (!scope) return;
            for (const attribute of ['text', 'title', 'aria-label', 'placeholder', 'alt']) {
                for (const element of scope.querySelectorAll(`[data-i18n-${attribute}]`)) {
                    // Message HTML is untrusted content, even when it carries
                    // attributes which look like product template markers.
                    if (element.closest('#chat-messages-container')) continue;
                    bind(element, attribute === 'text' ? null : attribute,
                        element.getAttribute(`data-i18n-${attribute}`));
                }
            }
        }
        return Object.freeze({
            get language() { return translator.language; },
            get locale() { return translator.locale; },
            t: (key, params) => translator.t(key, params),
            render: (key, params) => translator.render(key, params),
            formatNumber: (value, options) => translator.formatNumber(value, options),
            formatCurrency: (value, currency, fractionDigits) => translator.formatCurrency(value, currency, fractionDigits),
            bindText: (element, key, params) => bind(element, null, key, params),
            bindAttribute: (element, attribute, key, params) => bind(element, attribute, key, params),
            bindValue: (element, renderValue, attribute = null) => bind(element, attribute, null, {}, renderValue),
            unbind(element, attribute = null) {
                const binding = bindings.get(element);
                if (!binding) return;
                const { targets, ref } = binding;
                targets.delete(attribute);
                if (!targets.size) { bindings.delete(element); elements.delete(ref); }
            },
            hydrate,
            onChange(callback) {
                listeners.add(callback);
                return () => listeners.delete(callback);
            },
            setPayload(nextPayload) {
                const next = create(nextPayload); // Validate before changing any UI.
                translator = next;
                if (document) document.documentElement.lang = next.language;
                for (const ref of elements) {
                    const element = ref.deref();
                    if (!element || element.isConnected === false) {
                        elements.delete(ref);
                        if (element) bindings.delete(element);
                        continue;
                    }
                    const { targets } = bindings.get(element);
                    for (const [attribute, { key, params, renderValue, lastRendered }] of targets) {
                        const current = attribute === null ? element.textContent : element.getAttribute(attribute);
                        if (current !== lastRendered) { targets.delete(attribute); continue; }
                        bind(element, attribute, key, params, renderValue);
                    }
                    if (!targets.size) { bindings.delete(element); elements.delete(ref); }
                }
                for (const callback of listeners) callback();
            },
        });
    }

    if (typeof module !== 'undefined' && module.exports) module.exports = { create, createRuntime };
    const data = root.document && root.document.getElementById('aurvek-i18n');
    if (data) {
        root.AurvekI18n = createRuntime(JSON.parse(data.textContent), root.document);
        if (root.document.readyState === 'loading') {
            root.document.addEventListener('DOMContentLoaded', () => root.AurvekI18n.hydrate(), { once: true });
        } else root.AurvekI18n.hydrate();
    }
})(typeof window === 'undefined' ? globalThis : window);
