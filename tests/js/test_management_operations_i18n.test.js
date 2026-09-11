const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const {create} = require('../../data/static/js/common/i18n.js');
const root = path.resolve(__dirname, '../..');
const locales = {en:'en-US',es:'es-ES',ja:'ja-JP',fr:'fr-FR',pt:'pt-PT',it:'it-IT',de:'de-DE'};
function runtime(name, language) {
    const resources={};
    for (const lang of new Set(['en',language])) resources[lang]=Object.fromEntries(['common','admin_operations'].map(domain=>[domain,JSON.parse(fs.readFileSync(path.join(root,`locales/${lang}/${domain}.json`),'utf8'))]));
    const tr=create({version:1,language,locales,resources});
    const nodes=new Map();
    const make=()=>({style:{},value:'',disabled:false,classList:{add(){},remove(){}},addEventListener(){},
        set textContent(v){this.text=String(v);this.innerHTML=this.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');},get textContent(){return this.text;}});
    const get=id=>{if(!nodes.has(id))nodes.set(id,make());return nodes.get(id);};
    const c=vm.createContext({AurvekI18n:{...tr,t:tr.render},URLSearchParams,console,
        document:{getElementById:get,querySelectorAll:()=>[],createElement:make,addEventListener(){}},
        window:{},location:{reload(){}},sessionStorage:{getItem(){return null;},setItem(){}},setTimeout(){},clearTimeout(){},setInterval(){},
        NotificationModal:{confirm(title,message,callback){c.confirmation={title,message,callback};},error(title,message){c.errorMessage=message;}},
    });
    // These two server-provided page sizes are fixture data, not translated code.
    const html=fs.readFileSync(path.join(root,`templates/${name}.html`),'utf8')
        .replace('{{ events_default_limit or 100 }}','100').replace('{{ blocked_ips_default_limit or 200 }}','200');
    for(const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(match[1],c);
    return {c,get,tr};
}
for(const language of Object.keys(locales)) {
    test(`${language}: session states and thresholds are labels; event filtering remains technical`,async()=>{
        const {c,get,tr}=runtime('admin_session_health',language);
        assert.match(c.severityBadge('intense'),/severity-intense/);
        assert.ok(c.severityBadge('intense').includes(tr.render('admin_operations.intense')));
        assert.equal(c.eventLabel('pause_started'),tr.render('admin_operations.pause_started'));
        assert.equal(c.eventLabel('reminder_dismissed'),tr.render('admin_operations.dismissed'));
        assert.equal(c.eventLabel('reminder_snoozed'),tr.render('admin_operations.snoozed'));
        assert.equal(c.eventLabel('session_reset'),tr.render('admin_operations.session_reset'));
        assert.equal(c.thresholdLabel('wellbeing_soft_user_messages'),tr.render('admin_operations.soft_user_messages'));
        assert.ok(!c.severityBadge('<img>').includes('<img>'));
        c.renderLiveSessions([{username:'Original <person>',current_severity:'strong'}]);
        assert.ok(get('liveSessionsTable').innerHTML.includes('Original &lt;person&gt;'));
        get('eventTypeFilter').value='pause_started';get('severityFilter').value='strong';
        let requested;c.fetch=async url=>{requested=url;return {ok:true,json:async()=>({events:[],total:0})};};
        await c.loadReminderEvents(1);
        assert.ok(requested.includes('event_type=pause_started'));
        assert.ok(requested.includes('severity=strong'));
        assert.equal(c.formatNumber(12345),tr.formatNumber(12345,{notation:'compact',maximumFractionDigits:1}));
        assert.equal(c.formatDateTime('2026-09-01T12:00:00+02:00'),new Date('2026-09-01T12:00:00+02:00').toLocaleString(locales[language]));
    });
    test(`${language}: destructive confirmations preserve requested IDs and booleans`,async()=>{
        const {c,tr}=runtime('admin_chat',language);
        c.toggleLock(41,true);
        assert.equal(c.confirmation.message,tr.render('admin_operations.confirm.lock',{id:41}));
        let sent;c.fetch=async(url,options)=>{sent={url,body:JSON.parse(options.body)};return {ok:true};};
        await c.confirmation.callback();assert.equal(sent.url,'/api/conversations/41/lock');assert.equal(sent.body.lock,true);
        c.document.querySelectorAll=()=>[{value:'41'},{value:'73'}];
        c.bulkAction('delete');assert.equal(c.confirmation.message,tr.render('admin_operations.confirm.bulk_delete',{count:2}));
        await c.confirmation.callback();assert.deepEqual(sent.body.conversation_ids,[41,73]);
        c.deleteConversation(41);assert.equal(c.confirmation.message,tr.render('admin_operations.confirm.delete',{id:41}));
        c.fetch=async()=>{throw Error('untranslated network detail');};await c.confirmation.callback();
        assert.equal(c.errorMessage,tr.render('common.error.generic'));
    });
    test(`${language}: security states, scores, and confirmed IP actions remain safe`,async()=>{
        const {c,get,tr}=runtime('admin/security_operations',language);
        assert.equal(c.securityLabel('missing_credentials'),tr.render('admin_operations.security.missing_credentials'));
        assert.equal(c.securityLabel('not-known'),tr.render('admin_operations.unknown'));
        let sent;c.fetch=async(url,options)=>{sent={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>({cloudflare_deleted:true,cloudflare_rule_id:'original-rule'})};};
        c.refreshSecurityPanel=async()=>{};
        await c.unblockIp('203.0.113.7');
        assert.equal(sent.body.ip,'203.0.113.7');
        assert.ok(get('alertContainer').innerHTML.includes(tr.render('admin_operations.security.ip_and_rule_unblocked',{ip:'203.0.113.7',rule:'original-rule'})));
        c.fetch=async()=>{throw Error('raw network detail');};
        await assert.rejects(c.fetchJson('/fixture'),error=>error.message===tr.render('common.error.generic'));
    });
}
