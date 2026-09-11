// Runs the actual inline script from a real Jinja render, with bounded DOM/network fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {create} = require('../../data/static/js/common/i18n.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const i18n = create(input.payload);
class Element {
    constructor() { this.children=[]; this.dataset={}; this.value=''; this.checked=false; this.disabled=false; this.textContent=''; this.innerHTML=''; this.handlers={}; this.style={}; this.classes=new Set(); this.classList={add:(...values)=>values.forEach(value=>this.classes.add(value)),remove:(...values)=>values.forEach(value=>this.classes.delete(value)),contains:value=>this.classes.has(value)}; }
    addEventListener(event,callback) { this.handlers[event]=callback; }
    appendChild(child) { this.children.push(child); return child; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children=children; }
    scrollIntoView() {}
    showModal() { this.shown=true; }
    querySelector(selector) { return this.fields?.[selector] || new Element(); }
    closest() { return this.row; }
}
const elements=new Map();
const el=id=>{if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);};
const deleteForm=new Element();deleteForm.submit=()=>deleteForm.submitted=true;
const numberButton=new Element();const numberRow=new Element();numberButton.row=numberRow;
numberRow.dataset={numberId:'7',bindingCount:'12345'};
numberRow.fields=Object.fromEntries(['.number-enabled','.number-inbound','.number-default','.number-repair'].map(key=>[key,new Element()]));
numberRow.fields['.number-inbound'].checked=true;
const notice=new Element();notice.dataset.key='unknown_caller';notice.value='Original caller notice 日本語';
const document={
    getElementById:el,
    querySelectorAll:selector=>selector==='.elevenlabs-delete-form'?[deleteForm]:selector==='.save-number'?[numberButton]:selector==='.technical-notice'?[notice]:[],
    querySelector:()=>new Element(),
    createElement:()=>new Element(), createTextNode:text=>({textContent:text}),addEventListener:()=>{},
};
const requests=[]; const dialogs=[];let confirm=true;let nextResponse={ok:true,data:{}};
const context={document,console,Intl,Set,Map,Date,Promise,JSON,Number,String,Boolean,URLSearchParams,encodeURIComponent,
    AurvekI18n:i18n, bootstrap:{Tooltip:function(){}}, setTimeout:callback=>callback(),
    fetch:async(url,options={})=>{requests.push({url,options});return {ok:nextResponse.ok,json:async()=>nextResponse.data};},
    NotificationModal:{confirm:(title,message,yes,no,options)=>{dialogs.push({title,message,options});if(confirm)yes();else no?.();},error:(title,message)=>dialogs.push({title,message})},
    location:{reload:()=>{}},prompt:()=>null,
};
context.window=context;vm.createContext(context);vm.runInContext(input.script,context);
const evaluate=code=>vm.runInContext(code,context);
const submit=async id=>el(id).handlers.submit({preventDefault(){}});
const allText=node=>[node.textContent,...(node.children || []).map(allText)].join(' ');
(async()=>{
    if(input.name==='admin_elevenlabs') {
        deleteForm.handlers.submit.call(deleteForm,{preventDefault(){}});
        assert.equal(dialogs[0].title,i18n.t('admin_voice_integrations.ui.delete_agent'));
        assert.equal(dialogs[0].message,i18n.t('admin_voice_integrations.ui.delete_this_agent'));
        assert.equal(deleteForm.submitted,true);
    } else if(input.name==='admin_elevenlabs_voices') {
        nextResponse={ok:true,data:{voices:[{voice_id:'voice-wire-1',name:'<operator> 日本語',category:'premade',labels:['<author>'],labels_raw:{gender:'female'},description:'<description>',preview_url:'https://example.test/preview.mp3'},{voice_id:'voice-wire-2',name:'Second',category:'professional',labels:[],labels_raw:{gender:'male'},description:'',preview_url:''}]}};
        await context.fetchElevenLabsVoices();
        assert.ok(el('voicesTableBody').innerHTML.includes('&lt;operator&gt; 日本語'));
        assert.ok(el('voicesTableBody').innerHTML.includes(i18n.t('admin_voice_integrations.ui.premade')));
        assert.ok(el('voicesTableBody').innerHTML.includes(`title="${i18n.t('admin_voice_integrations.ui.preview_voice')}"`));
        context.toggleVoice('voice-wire-2'); assert.equal(el('saveBtn').disabled,false);
        nextResponse={ok:true,data:{success:true}};await context.saveSelection();
        const payload=JSON.parse(requests.at(-1).options.body);
        assert.deepEqual(payload.api_voice_ids,['voice-wire-1','voice-wire-2']);
        assert.deepEqual(payload.voices.map(v=>v.voice_id),payload.api_voice_ids);
        assert.equal(payload.voices[0].name,'<operator> 日本語');
        el('searchFilter').value='no-match'; context.applyFilters();
        assert.ok(el('voicesTableBody').innerHTML.includes(i18n.t('admin_voice_integrations.ui.no_matching_voices')));
        assert.equal(el('selectAllVisible').checked,false);
        nextResponse={ok:false,data:{}};await context.fetchElevenLabsVoices();
        assert.equal(dialogs.at(-1).message,i18n.t('admin_voice_integrations.error.fetch_detail',{detail:i18n.t('admin_voice_integrations.error.fetch')}));
    } else {
        confirm=false; await el('sync-numbers').handlers.click(); assert.equal(requests.length,0);
        confirm=true;nextResponse={ok:true,data:{synced:2}};await el('sync-numbers').handlers.click();
        assert.equal(requests[0].options.headers['X-GPTSub-CSRF'],'csrf-fixture');
        assert.equal(requests[0].options.credentials,'same-origin');
        assert.equal(el('telephony-feedback').textContent,i18n.t('admin_telephony.notice.numbers_synced',{count:2,number:i18n.formatNumber(2)}));
        await numberButton.handlers.click();
        assert.equal(dialogs.at(-1).message,i18n.t('admin_telephony.confirm.disable_assigned',{count:12345,number:i18n.formatNumber(12345)}));
        assert.deepEqual(JSON.parse(requests.at(-1).options.body),{enabled:false,inbound_enabled:true,is_outbound_default:false,confirm_affected_bindings:true,repair_webhook:false});
        for(const [id,value] of Object.entries({billing_provider:'twilio',billing_component:'pstn',billing_direction:'outbound',billing_from_country:'US',billing_to_country:'ES',billing_unit:'minute',billing_provider_rate:'0.001234',billing_customer_rate:'0.001567',billing_currency:'USD',billing_service_id:'13'}))el(id).value=value;
        await submit('billing-rate-form');const rate=JSON.parse(requests.at(-1).options.body);
        assert.equal(rate.unit,'minute');assert.equal(rate.provider_rate_per_unit,0.001234);assert.equal(rate.service_id,13);
        for(const direction of ['inbound','outbound']) {el(direction+'_greetings').value='Original greeting 日本語';el(direction+'_mode').value='fixed';el(direction+'_fixed_index').value='1';}
        el('global_voice_id').value='13';await submit('global-audio-form');const audio=JSON.parse(requests.at(-1).options.body);
        assert.equal(audio.inbound.phrases[0].literal_text,'Original greeting 日本語');assert.equal(audio.inbound.fixed_index,0);assert.equal(audio.notices.unknown_caller,notice.value);
        context.renderOperations('calls',{items:[{id:'call-1',conversation_id:41,contact_name:'Original',direction:'outbound',status:'in_progress',duration_seconds:1234,estimated_cost:0.001234,currency:'USD',can_retry_hangup:false}],offset:0,limit:50,total:1,next_offset:null});
        const callText=allText(el('calls-operation-body'));assert.ok(callText.includes(i18n.t('admin_telephony.state.in_progress')));assert.ok(callText.includes(i18n.formatCurrency(0.001234,'USD',8)));
        assert.ok(allText(el('calls-operation-pager')).includes(i18n.t('admin_telephony.ui.pagination_range',{first:i18n.formatNumber(1),last:i18n.formatNumber(1),total:i18n.formatNumber(1)})));
        context.renderOperations('jobs',{items:[],offset:0,limit:50,total:0,next_offset:null});assert.ok(allText(el('jobs-operation-body')).includes(i18n.t('admin_telephony.ui.no_results')));
        assert.equal(evaluate("phoneDate('2026-09-08 14:30:00')"),new Intl.DateTimeFormat(i18n.locale,{dateStyle:'medium',timeStyle:'medium',timeZone:'UTC'}).format(new Date('2026-09-08T14:30:00Z'))+' UTC');
        assert.equal(evaluate("phoneAmount(0.01234567, 'CRED')"), i18n.formatNumber(0.01234567,{maximumFractionDigits:8})+' CRED');
        nextResponse={ok:true,data:{call:{id:'call-1',conversation_id:41,status:'completed',duration_seconds:12},events:[{received_at:'2026-09-08 14:30:00',event_type:'call_status',signature_valid:true,payload:{wire:'id'}}],transcript:[{date:'2026-09-08 14:30:01',participant:'caller',message:'Original <script> 日本語',delivery_state:'consumed',interrupted:true,played_ms:250}],recordings:[{status:'available',duration_seconds:12,tracks:['participant']}],costs:[{component_type:'tts',quantity:12,unit:'character',provider_cost:0.00001,customer_charge:0.00002,currency:'USD',state:'settled'}]}};
        await context.showCallDetail('call-1');
        const detailText=allText(el('phone-call-detail-content'));
        assert.ok(detailText.includes('Original <script> 日本語'));
        assert.ok(detailText.includes(i18n.t('admin_telephony.detail.valid_signature')));
        assert.ok(detailText.includes(i18n.t('admin_telephony.state.consumed')));
        assert.ok(detailText.includes(i18n.formatCurrency(0.00001,'USD',8)));
        assert.equal(el('phone-call-detail').shown,true);
        el('paid_test_conversation').value='41';el('paid_test_key').value='idempotency-fixture';
        nextResponse={ok:true,data:{created:false,job_id:'existing-job'}};
        await submit('paid-test-call-form');
        assert.deepEqual(JSON.parse(requests.at(-1).options.body),{confirm_paid_call:true,conversation_id:41,idempotency_key:'idempotency-fixture'});
        assert.equal(el('telephony-feedback').textContent,i18n.t('admin_telephony.notice.job_exists',{id:'existing-job'}));
        nextResponse={ok:false,data:{detail:[{msg:'English validator detail'}]}};
        await assert.rejects(context.telephonyMutation('/fixture'),{message:i18n.t('admin_telephony.error.invalid')});
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
