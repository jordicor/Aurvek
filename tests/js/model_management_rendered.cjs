const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {create} = require('../../data/static/js/common/i18n.js');
const pages = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
for (const [name, html] of Object.entries(pages)) {
    const payload = JSON.parse(html.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]);
    const scripts = [...html.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)].filter(m => !m[1].includes('application/json')).map(m => m[2]);
    scripts.forEach(s => new vm.Script(s));
    if (!['admin_models', 'llms/llm_list'].includes(name)) continue;
    const i18n = create(payload);
    const notices = [];
    const context = vm.createContext({console, Intl, Set, Map, URL, AurvekI18n: i18n,
        NotificationModal: {warning: (...args) => notices.push(args), confirm: (...args) => notices.push(args)},
        document: {addEventListener() {}, querySelectorAll() {return [];}}, window: {}});
    scripts.filter(s => s.includes('function autoUpdateLlmCatalog') || s.includes('const modelText')).forEach(s => vm.runInContext(s, context));
    if (name === 'admin_models') {
        const rendered = vm.runInContext(`renderModelRow(normalizeModel({id: 'vendor/<model>', display_name: 'Fixture <model>', input_price: 0.1234, enabled: true, vision: true, local_id: 77, metadata_source: 'provider_api'}, 'openrouter'), {enabled: new Set(['vendor/<model>']), originalEnabled: new Set(['vendor/<model>'])})`, context);
        assert.ok(rendered.includes('Fixture &lt;model&gt;'));
        assert.ok(!rendered.includes('Fixture <model>'));
        assert.ok(rendered.includes(i18n.formatCurrency(0.1234, 'USD', 4)));
        assert.ok(rendered.includes(i18n.t('admin_models.label.enabled')));
        assert.ok(rendered.includes(i18n.t('admin_models.source.provider_api')));
        assert.equal(vm.runInContext('formatTokens(123456)', context), i18n.formatNumber(123456, {notation: 'compact', maximumFractionDigits: 1}));
    } else {
        vm.runInContext('deleteSelectedLlms()', context);
        assert.equal(notices[0][1], i18n.t('admin_models.catalog.please_select_at_least_one_model_to_delete'));
        vm.runInContext('deleteLlm(77)', context);
        assert.equal(notices[1][1], i18n.t('admin_models.catalog.are_you_sure_you_want_to_delete_this_model_this_action_cannot_be_undone'));
        for (const count of [1, 2]) assert.equal(vm.runInContext(`modelText('delete.success', {count:${count}, number: AurvekI18n.formatNumber(${count})})`,context), i18n.t('admin_models.delete.success', {count,number:i18n.formatNumber(count)}));
        const nodes = new Map();
        context.document.getElementById = id => {
            if (!nodes.has(id)) nodes.set(id, {style: {}, textContent: '', disabled: false});
            return nodes.get(id);
        };
        for (const count of [0, 1, 2]) {
            const checked = Array.from({length: count}, () => ({closest: () => ({style: {}, dataset: {enabled: 'yes', deletable: 'yes'}})}));
            context.document.querySelectorAll = () => checked;
            vm.runInContext('updateSelectedCount()', context);
            assert.equal(nodes.get('bulkSelectionCount').textContent, i18n.t('admin_models.selection.count', {number: i18n.formatNumber(count)}));
            assert.equal(nodes.get('selectedCount').textContent, i18n.formatNumber(count));
        }
    }
}
