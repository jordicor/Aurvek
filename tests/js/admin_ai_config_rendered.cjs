const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const pages = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function load(html) {
    const tr = create(JSON.parse(html.match(/<script[^>]*id="aurvek-i18n"[^>]*>([\s\S]*?)<\/script>/)[1]));
    const nodes = new Map();
    const get = id => {
        if (!nodes.has(id)) nodes.set(id, {style: {}, innerHTML: '', textContent: '', value: '', checked: false,
            classList: {add() {}, remove() {}, toggle() {}}, setAttribute() {}, getAttribute() {}, focus() {}});
        return nodes.get(id);
    };
    const context = vm.createContext({AurvekI18n: tr, setTimeout() {}, console, confirm: () => true,
        document: {getElementById: get, querySelectorAll: () => [], querySelector: () => ({value: 'post_prompt'}), addEventListener() {},
            createElement() { return {set textContent(value) {this.innerHTML = escapeHtml(value);}};}},
        NotificationModal: {error(title,message){context.notice={title,message};}, warning(title,message){context.notice={title,message};}, info(title,message){context.notice={title,message};}}
    });
    context.window = context;
    for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
        new vm.Script(match[1]);
        if (match[1].includes('const aiText')) vm.runInContext(match[1], context);
    }
    return {context, get, tr};
}

(async () => {
    {
        const {context:c,get,tr} = load(pages.admin_atagia);
        c.updateSecretUi('service');
        assert.equal(get('atagia_service_api_key').placeholder, tr.t('ai_config.current_key', {value:'masked<key>'}));
        c.markSecretForClear('service');
        assert.equal(c.payload().clear_service_api_key, true);
        assert.equal(get('serviceApiKeyNote').textContent, tr.t('ai_config.clear_notice'));
        assert.equal(c.statusLabel('completed_with_errors'),tr.t('ai_config.completed_with_errors'));
        c.showAlert('danger','<img onerror=attack>');
        assert(!get('alertContainer').innerHTML.includes('<img'));
        c.fetch = async () => {throw Error('Raw network internals');};
        await c.testConnection();
        assert.equal(get('connStatusText').textContent,tr.t('ai_config.connection_failed'));
        await assert.rejects(c.readJsonResponse({text:async()=>'<private server response>'}), e=>e.message===tr.t('ai_config.request_failed'));
    }
    {
        const {context:c,get,tr} = load(pages.admin_gransabio);
        const warning=tr.t('ai_config.validation_warning',{error:'Localized validation'});
        c.fetch=async()=>({ok:true,json:async()=>({success:true,message:warning})});
        await c.saveConfig();
        assert(get('alertContainer').innerHTML.includes(escapeHtml(warning)));
        c.fetch=async()=>({ok:false,json:async()=>({success:false,message:tr.t('ai_config.invalid_ips')})});
        await c.saveConfig();
        assert.equal(c.notice.message,tr.t('ai_config.save_detail',{error:tr.t('ai_config.invalid_ips')}));
        c.fetch=async()=>{throw Error('Raw network internals');};
        await c.saveConfig();
        assert.equal(c.notice.message,tr.t('ai_config.save_detail',{error:tr.t('ai_config.save_failed')}));
    }
    {
        const {context:c,get,tr} = load(pages.admin_system_prompts);
        await c.createBlock();
        assert.equal(c.notice.message,tr.t('ai_config.name_required'));
        get('createName').value='Authored name'; get('createContent').value='Authored {user_level}';
        let sent;
        c.secureFetch=async(url,options)=>{sent=JSON.parse(options.body);return {ok:false,json:async()=>({error:tr.t('ai_config.reserved_key')})};};
        await c.createBlock();
        assert.equal(sent.content,'Authored {user_level}'); assert.equal(sent.position,'post_prompt');
        assert.equal(c.notice.message,tr.t('ai_config.reserved_key'));
        c.secureFetch=async()=>null;
        await c.createBlock();
        assert.equal(c.notice.message,tr.t('ai_config.request_failed'));
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
