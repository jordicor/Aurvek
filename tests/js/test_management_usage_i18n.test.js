const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const root = path.resolve(__dirname, '../..');
const regions = {en:'en-US', es:'es-ES', ja:'ja-JP', fr:'fr-FR', pt:'pt-PT', it:'it-IT', de:'de-DE'};
const escape = value => String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
class Element {
    constructor() { this.style = {}; this.value = ''; this.listeners = {}; this.classList = {add(){},remove(){}}; }
    set textContent(value) { this.text = String(value); this.innerHTML = escape(value); }
    get textContent() { return this.text; }
    addEventListener(event, fn) { this.listeners[event] = fn; }
    getContext() { return {}; }
}
function runtime(page, language) {
    const resources = {};
    for (const code of new Set(['en', language])) resources[code] = {
        admin_usage: JSON.parse(fs.readFileSync(path.join(root, `locales/${code}/admin_usage.json`),'utf8')),
    };
    const tr = create({version:1, language, locales:regions, resources});
    const elements = new Map();
    const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
    const context = vm.createContext({
        AurvekI18n:{...tr, t:tr.render}, document:{getElementById:get, querySelector:get, querySelectorAll:()=>[],
            createElement:()=>new Element(), addEventListener(){}, documentElement:{}},
        window:{location:{}}, URLSearchParams, console, setTimeout(){}, clearTimeout(){},
        FormGuard:{markClean(){}}, NotificationModal:{toast(){},error(){},warning(){}},
        getComputedStyle:()=>({getPropertyValue:()=>''}),
        Chart: function(_, config) { context.chartConfig = config; this.destroy = ()=>{}; },
    });
    const template = fs.readFileSync(path.join(root, `templates/${page}.html`),'utf8');
    const script = template.split('{% block scripts_inline %}')[1].match(/<script>([\s\S]*?)<\/script>/)[1];
    vm.runInContext(script, context);
    return {context, get, tr};
}

for (const language of Object.keys(regions)) {
    test(`${language}: personal usage translates complete summaries and plural counts`, () => {
        const {context:c,get,tr} = runtime('my_usage',language);
        c.updateStats({tokens_in:31,tokens_out:42,total_cost:1.234});
        assert.equal(get('statTokensBreakdown').textContent,tr.render('admin_usage.token_breakdown',{input:'31',output:'42'}));
        for (const count of [0,1,2]) {
            c.updateWellbeingSummary({sessions:count});
            assert.equal(get('wellbeingSummaryText').textContent,tr.render('admin_usage.wellbeing.inactive_summary',{count,sessions:String(count)}));
            c.updateWellbeingSummary({sessions:count,active_session:{id:7,current_severity:'intense'}});
            assert.equal(get('wellbeingSummaryText').textContent,tr.render('admin_usage.wellbeing.active_summary',{count,sessions:String(count),status:tr.render('admin_usage.wellbeing.intense')}));
            c.updateUsageByType([{type:'ai_tokens',operations:count,total_cost:1.234}]);
            assert.ok(get('usageByType').innerHTML.includes(tr.render('admin_usage.operation_count',{count,number:String(count)})));
        }
        assert.equal(get('statCost').textContent,tr.formatCurrency(1.234,'USD',2));
    });
    test(`${language}: team zero limit, wire statuses, estimates and creator text`, async () => {
        const {context:c,get,tr} = runtime('user_team_consumption',language);
        for (const [status,key] of [['Active','active'],['At Limit','at_limit'],['Blocked','blocked'],['Over Limit','over_limit']]) {
            c.updateUsersTable([{username:'A " <tag>', email:'mail',status,limit:0,limit_action:'auto_refill',this_month_spent:1.2345}]);
            const html = get('usersTableBody').innerHTML;
            assert.ok(html.includes(tr.render('admin_usage.team.status.'+key)));
            assert.ok(html.includes(tr.formatCurrency(0,'USD',2)));
            assert.ok(!html.includes(tr.render('admin_usage.unlimited')));
            assert.ok(html.includes(tr.render('admin_usage.team.action.auto_refill')));
            assert.ok(html.includes('&quot;'));
            assert.ok(!html.includes('<tag>'));
        }
        c.updateActivityList([{username:'<author>',prompt_name:'Original <prompt>',cost:0.015,timestamp:'2026-09-01T12:00:00'}]);
        assert.ok(get('activityList').innerHTML.includes(escape(tr.render('admin_usage.team.activity',{name:'<author>',prompt:'Original <prompt>'}))));
        assert.ok(get('activityList').innerHTML.includes(tr.render('admin_usage.team.estimated_amount',{amount:tr.formatCurrency(0.015,'USD',2)})));
        c.fetch = async()=>({ok:false,json:async()=>({error:'<img src=x onerror=bad>'})});
        await c.loadDashboardData();
        assert.ok(!get('usersTableBody').innerHTML.includes('<img'));
        c.exportCSV(); assert.equal(c.window.location.href,'/api/user/team-billing?format=csv');
    });
    test(`${language}: admin chart and type badges keep numeric data and localize presentation`, () => {
        const {context:c,get,tr} = runtime('admin_usage',language);
        const day = {date:'2026-09-01',total_cost:0.012345,operations:2,tokens_in:1,tokens_out:2,unique_users:0};
        c.updateDailyChart([day]);
        assert.equal(c.chartConfig.data.datasets[0].data[0],day.total_cost);
        assert.equal(c.chartConfig.data.labels[0],new Date('2026-09-01T00:00:00').toLocaleDateString(regions[language],{year:'numeric',month:'short',day:'numeric'}));
        c.updateTopUsers([{username:'<user>',email:'',operations:1,tokens:1,total_cost:1,types:['ai_tokens','tts']}]);
        assert.ok(get('topUsersTable').innerHTML.includes(tr.render('admin_usage.text_to_speech')));
        assert.ok(!get('topUsersTable').innerHTML.includes('ai tokens'));
        c.updateDailyTable([day]); assert.ok(get('dailyTable').innerHTML.includes('>0</td>'));
    });
    test(`${language}: pricing and storage localize fractional display while preserving decimal inputs and wire bytes`, async () => {
        const {context:c,get,tr} = runtime('admin_pricing',language);
        get('pricing_margin_free').value='12.345'; get('pricing_margin_paid').value='10'; get('pricing_commission').value='30';
        c.updatePreview();
        assert.equal(get('pricing_margin_free').value,'12.345');
        assert.equal(get('previewFreeTotag').textContent,tr.formatCurrency(11.2345,'USD',2));
        assert.equal(get('previewMarginFree').textContent,tr.render('admin_usage.pricing.margin_label',{percent:tr.formatNumber(.12345,{style:'percent',maximumFractionDigits:20})}));
        const {context:s,get:sg,tr:st} = runtime('admin_storage_quotas',language);
        const bytes = 1.5*1024**3;
        assert.equal(s.bytesToGbInputValue(bytes),'1.5');
        const row=s.renderUserRow({username:'A " <name>',user_id:7,quota_bytes:bytes,total_bytes:bytes/2,uploads_bytes:bytes/2,generated_bytes:0,percent:50,is_override:true});
        assert.ok(row.includes('value="1.5"'));
        assert.ok(row.includes('aria-valuenow="50"'));
        assert.ok(row.includes('&quot;'));
        let sent;
        s.secureFetch=async(url,options)=>{sent=JSON.parse(options.body);return {ok:true,json:async()=>({success:true,default_quota_bytes:bytes})};};
        s.loadUsers=()=>{};s.loadStats=()=>{};
        let toast;s.NotificationModal.toast=message=>{toast=message;};
        sg('defaultQuotaGb').value='1.5';
        await s.saveDefaultQuota({preventDefault(){}});
        assert.equal(sent.default_quota_bytes,bytes);
        assert.equal(toast,st.render('admin_usage.storage.default_saved',{quota:s.formatQuota(bytes)}));
        assert.equal(sg('defaultQuotaGb').value,'1.5');
    });
}
