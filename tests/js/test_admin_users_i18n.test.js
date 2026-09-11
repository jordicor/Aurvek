const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const test=require('node:test');
const vm=require('node:vm');
const {create}=require('../../data/static/js/common/i18n.js');
const root=path.resolve(__dirname,'../..');
const regions={en:'en-US',es:'es-ES',ja:'ja-JP',fr:'fr-FR',pt:'pt-PT',it:'it-IT',de:'de-DE'};
const listSource=fs.readFileSync(path.join(root,'data/static/js/users-list.js'),'utf8');
const editSource=fs.readFileSync(path.join(root,'templates/edit_user.html'),'utf8');
const createSource=fs.readFileSync(path.join(root,'templates/create_user.html'),'utf8');
function extract(source,start,end){return source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));}
class Element{
    constructor(){this.value='';this.style={};this.dataset={};this.attributes={};this.classList={add(){},remove(){},toggle(){}};}
    set textContent(value){this.text=String(value);this.innerHTML=this.text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
    get textContent(){return this.text;}
    addEventListener(){}
    setAttribute(k,v){this.attributes[k]=v;}
    querySelector(){return new Element();}
    querySelectorAll(){return [];}
}
function runtime(language){
    const resources={};for(const code of new Set(['en',language]))resources[code]={
        admin_users:JSON.parse(fs.readFileSync(path.join(root,`locales/${code}/admin_users.json`),'utf8')),
        common:JSON.parse(fs.readFileSync(path.join(root,`locales/${code}/common.json`),'utf8')),
    };
    const tr=create({version:1,language,locales:regions,resources});
    const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
    let checked=[];const messages=[];
    const c=vm.createContext({AurvekI18n:{...tr,t:tr.render},document:{getElementById:get,
        createElement:()=>new Element(),querySelector:()=>null,querySelectorAll:selector=>selector==='.user-checkbox:checked'?checked:[],addEventListener(){}},
        window:{scrollTo(){},location:{}},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},
        console,NotificationModal:{confirm:(...args)=>messages.push(['confirm',...args]),success:(...args)=>messages.push(['success',...args]),error:(...args)=>messages.push(['error',...args])}});
    vm.runInContext(listSource,c);
    return {c,get,tr,messages,setChecked:items=>{checked=items;}};
}
for(const language of Object.keys(regions)){
    test(`${language}: activation messages preserve account identifiers and boolean payloads`,async()=>{
        const {c,tr,messages,setChecked}=runtime(language);
        const name='<OriginalUser>';
        c.toggleUserActivation({dataset:{username:name,enabled:'true'}});
        assert.equal(messages[0][2],tr.render('admin_users.flow.disable_confirm',{name}));
        const writes=[];c.secureFetch=async(url,options)=>{writes.push([url,JSON.parse(options.body)]);return {ok:true,json:async()=>({success:true,username:name,is_enabled:false})};};
        c.updateSelectedCount=()=>{};c.applyFiltersAndSort=()=>{};
        await c.setUsersActivation([name],false);
        assert.deepEqual(writes,[['/admin/users/%3COriginalUser%3E/activation',{enabled:false}]]);
        assert.equal(messages.at(-1)[2],tr.render('admin_users.flow.disabled_count',{count:1}));
        for(const count of [1,2]){
            setChecked(Array.from({length:count},()=>({value:'OriginalUser'})));
            c.setSelectedUsersActivation(true);
            assert.equal(messages.at(-1)[2],tr.render('admin_users.flow.enable_selected',{count}));
        }
        c.secureFetch=async()=>({ok:false,json:async()=>({error:'<img onerror=bad>'})});
        await c.setUsersActivation([name],false);
        const failure=messages.at(-1);assert.equal(failure.length,3);assert.ok(failure[2].includes('<img onerror=bad>'));
        // Error text uses the modal's normal text boundary, never allowHtml.
    });
    test(`${language}: selected labels, magic-link state and accessibility are localized`,()=>{
        const {c,tr,get,setChecked}=runtime(language);
        setChecked([{value:'OriginalUser'}]);c.updateSelectedCount();
        assert.equal(get('selectedCount').textContent,tr.render('admin_users.flow.delete_label',{number:'1'}));
        c.showMagicLink('https://example.test/?token=original','OriginalUser',false,false);
        assert.equal(get('magicLink').value,'https://example.test/?token=original');
        assert.equal(get('usernameMagicLink').textContent,tr.render('admin_users.flow.magic_link_for',{name:'OriginalUser'}));
        const button=new Element();c.updateActivationButton(button,true);
        assert.equal(button.dataset.enabled,'true');assert.equal(button.title,tr.render('admin_users.deactivate_user'));
        assert.ok(c.renderAccountStatusHtml(false).includes(tr.render('admin_users.account_disabled')));
        assert.equal(c.escapeHtml('A " <name>'), 'A &quot; &lt;name&gt;');
    });
    test(`${language}: rate-limit details translate text and escape IP attributes`,()=>{
        const {c,tr,get}=runtime(language);
        vm.runInContext(extract(editSource,'    function escapeHtml(text)','    async function loadRateLimitStatus()'),c);
        c.renderRateLimitStatus({limits:{'pair_fail:login:secret':{blocked:true,retry_after_seconds:1,attempts_in_window:2,limit:3,window_minutes:1}},blocked_login_ips:[{ip:'::1" data-x="bad',bucket:'ip_fail',attempts:2}]});
        assert.ok(get('rateLimitStatus').innerHTML.includes(tr.render('admin_users.flow.blocked_seconds',{count:1})));
        assert.ok(get('rateLimitStatus').innerHTML.includes(tr.render('admin_users.flow.login_pair')));
        assert.ok(!get('rateLimitStatus').innerHTML.includes('secret'));
        assert.ok(get('blockedIpList').innerHTML.includes('data-ip="::1&quot; data-x=&quot;bad"'));
        assert.ok(get('blockedIpList').innerHTML.includes(tr.render('admin_users.flow.use_ip')));
    });
    test(`${language}: installed phone widget gets localized countries without changing dialing configuration`,()=>{
        const {c,tr}=runtime(language);let options;
        const flag=new Element();c.phoneInputField={parentElement:{querySelector:()=>flag}};
        c.phoneInputJS=null;
        c.window.intlTelInputGlobals={getCountryData:()=>[{iso2:'es',dialCode:'34'},{iso2:'jp',dialCode:'81'}]};
        c.window.intlTelInput=(_,value)=>{options=value;return {};};
        vm.runInContext(extract(createSource,'        function initPhoneInput()','        function toggleAllowFileUpload()'),c);
        c.initPhoneInput();
        assert.equal(options.localizedCountries.es,new Intl.DisplayNames([regions[language]],{type:'region'}).of('ES'));
        assert.equal(options.initialCountry,'auto');
        assert.ok(options.utilsScript.includes('/17.0.13/'));
        assert.equal(flag.attributes['aria-label'],tr.render('admin_users.flow.phone_country'));
    });
}
