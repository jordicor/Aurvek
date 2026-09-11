const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const output = path.resolve(process.env.EMBED_QA_OUTPUT || 'docs/embed-evidence');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
    const browser = await chromium.launch({channel: 'msedge', headless: true});
    try {
        const page = await browser.newPage({viewport: {width: 1200, height: 820}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.goto('http://127.0.0.1:18793/qa/es');
        await page.waitForFunction(() => window.fixtureEvents.some(item => item.event === 'ready'));
        const frame = page.frames().find(frame => frame.url().includes('/embed/'));
        await frame.evaluate(() => {
            const original = 'Contenido original 日本語\n\n```python\nprint("original")\n```';
            addMessage('bot', original, {timestamp: {originalUtc: '2026-09-08 13:25:00'}, isNewMessage: false}, false, null, false, null, 99,
                [{url:'https://example.test/source', title:'Original source title'}]);
            window.f4CodeNode = document.querySelector('.code-block code');
            window.f4Code = f4CodeNode.textContent;
            window.f4Draft = 'Borrador sin enviar 日本語';
            const input = document.getElementById('message-text');
            input.value = f4Draft;
            input.focus(); input.setSelectionRange(2, 9);
            window.f4MessageNode = document.querySelector('[data-message-id="99"]');
            const statusHost = document.createElement('div');
            statusHost.id = 'f4-progress';
            f4MessageNode.querySelector('.message-content').appendChild(statusHost);
            renderGranSabioStatus(statusHost, {phase:'qa',iteration:1,max_iterations:3,score:8.4,min_score:8});
            finalizeGranSabioStatus(statusHost, {approved:true,final_score:8.4,iterations:1,total_cost:0.0234});
            toggleSendButton(true);
        });
        const results = [];
        for (const language of ['en','es','ja','fr','pt','it','de']) {
            await page.evaluate(language => {
                const iframe = document.querySelector('iframe');
                iframe.contentWindow.postMessage({contract_version:'aurvek_embed.v1',event:'set_ui_language',app_id:'sample',external_project_id:'layout-fixture',conversation_id:'12',frame_instance_id:'frame_instance_es',request_id:`f4-${language}`,ui_language:language},location.origin);
            }, language);
            await page.waitForFunction(language => window.fixtureEvents.some(item => item.event === 'capabilities_changed' && item.request_id === `f4-${language}` && item.ui_language === language),language);
            const result = await frame.evaluate(language => {
                const input = document.getElementById('message-text');
                const button = document.getElementById('send-button');
                return {
                    language: AurvekI18n.language,
                    lang: document.documentElement.lang,
                    codeSame: f4CodeNode === document.querySelector('.code-block code') && f4CodeNode.textContent === f4Code,
                    messageSame: f4MessageNode === document.querySelector('[data-message-id="99"]'),
                    draftSame: input.value === f4Draft && input.selectionStart === 2 && input.selectionEnd === 9,
                    streaming: button.dataset.streaming === 'true',
                    sendTranslated: button.dataset.embedLabel === AurvekI18n.t('embed.stop'),
                    codeTitleTranslated: document.querySelector('.copy-button').title === AurvekI18n.t('chat.copy_code'),
                    sourcesTranslated: document.querySelector('.sources-header span').textContent === AurvekI18n.t('chat.sources',{count:1}),
                    summaryTranslated: document.querySelector('.gransabio-title').textContent.includes(AurvekI18n.t('chat.gransabio.approved')),
                    summary: document.querySelector('.gransabio-title').textContent,
                    sidebarHidden: getComputedStyle(document.getElementById('sidebar')).display === 'none',
                    brandingHidden: !/aurvek/i.test(document.title + document.body.innerText),
                    overflow: document.body.scrollWidth > innerWidth + 1,
                };
            },language);
            for (const [key,value] of Object.entries(result)) {
                if (typeof value === 'boolean') assert.equal(value,key === 'overflow' ? false : true,`${language}: ${key}`);
            }
            assert.equal(result.language, language);
            assert.equal(result.lang, language);
            results.push(result);
        }
        // The real modal retains form controls and handlers while its owned
        // labels change. No message or paid operation is sent by this fixture.
        await frame.evaluate(() => {
            toggleSendButton(false);
            NotificationModal.confirm(() => AurvekI18n.t('chat.pdf_large_title'),
                '<label id="f4-page-label" for="f4-page"></label><input id="f4-page" type="number" value="17">',
                () => { window.f4Confirmed = true; }, null, {allowHtml:true,hideOnConfirm:false});
            AurvekI18n.bindText(document.getElementById('f4-page-label'),'chat.from_page');
            window.f4PageInput = document.getElementById('f4-page');
        });
        await page.evaluate(() => document.querySelector('iframe').contentWindow.postMessage({contract_version:'aurvek_embed.v1',event:'set_ui_language',app_id:'sample',external_project_id:'layout-fixture',conversation_id:'12',frame_instance_id:'frame_instance_es',request_id:'f4-modal',ui_language:'ja'},location.origin));
        await page.waitForFunction(() => window.fixtureEvents.some(item => item.request_id === 'f4-modal'));
        assert.deepEqual(await frame.evaluate(() => ({same: f4PageInput === document.getElementById('f4-page'), value:f4PageInput.value,
            label:document.getElementById('f4-page-label').textContent === AurvekI18n.t('chat.from_page'),
            title:document.getElementById('notificationModalLabel').textContent === AurvekI18n.t('chat.pdf_large_title')})),{same:true,value:'17',label:true,title:true});
        await frame.locator('#notificationModalConfirmBtn').click();
        assert.equal(await frame.evaluate(() => window.f4Confirmed),true);
        await frame.evaluate(() => NotificationModal.hide());
        await frame.locator('#notificationModal').waitFor({state:'hidden'});
        await frame.locator('.modal-backdrop').waitFor({state:'detached'});
        fs.mkdirSync(output,{recursive:true});
        await page.screenshot({path:path.join(output, 'desktop-ja.png')});
        await page.setViewportSize({width:390,height:844});
        await page.screenshot({path:path.join(output, 'mobile-ja.png')});
        const mobileOverflow = await frame.evaluate(() => document.body.scrollWidth > innerWidth + 1);
        assert.equal(mobileOverflow,false);
        assert.deepEqual(errors,[]);
        fs.writeFileSync(path.join(output, 'result.json'),JSON.stringify({results,modal:true,mobileOverflow,errors},null,2));
        process.stdout.write(JSON.stringify({languages:results.map(item=>item.language),checks:'live controls, original code/history, draft/caret, streaming, pipeline summary, modal state/actions, branding, mobile width',errors}));
    } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack); process.exitCode=1; });
