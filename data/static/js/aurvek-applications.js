/* Browser host for the existing Aurvek chat. Credentials stay on your server. */
(function (global) {
    'use strict';
    const version = 'aurvek_embed.v1';
    const applicationVersion = 'aurvek_applications.v1';
    const names = new Set(['attachment', 'dictation', 'voice', 'export_pdf', 'export_mp3']);
    const alias = value => typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
    const fail = code => Object.assign(new Error(code), { code });

    function bootstrapUrl(options, profile = false) {
        const { bootstrap, appId, contextId } = options || {};
        let url;
        try { url = new URL(bootstrap?.embed_url); } catch (_) { throw fail('invalid_bootstrap'); }
        if (!appId || (!profile && !contextId) || (profile && bootstrap?.surface !== 'profile') ||
            bootstrap?.contract_version !== applicationVersion || bootstrap.frame_contract_version !== version ||
            typeof bootstrap.ticket !== 'string' || !bootstrap.ticket || bootstrap.ticket.length > 512 ||
            !/^[A-Za-z0-9_-]{8,128}$/.test(bootstrap.frame_instance_id) ||
            url.protocol !== 'https:' || url.pathname !== (profile ? '/embed/profile/bootstrap' : '/embed/bootstrap') || url.search || url.hash || url.username || url.password) {
            throw fail('invalid_bootstrap');
        }
        return url;
    }
    function submitBootstrap(doc, container, target, url, bootstrap) {
        const form = doc.createElement('form');
        form.method = 'POST'; form.action = url.href; form.target = target; form.hidden = true;
        const ticket = doc.createElement('input');
        ticket.type = 'hidden'; ticket.name = 'ticket'; ticket.value = bootstrap.ticket;
        form.append(ticket); container.append(form);
        try { form.submit(); }
        finally { ticket.value = ''; form.remove(); }
    }

    function mountProfile(container, options) {
        const url = bootstrapUrl(options, true);
        if (!container?.append) throw fail('invalid_bootstrap');
        const {bootstrap, appId, onEvent = () => {}} = options;
        const frame = document.createElement('iframe');
        frame.name = `aurvek-profile-${global.crypto.randomUUID()}`;
        frame.title = options.title || 'Profile'; frame.referrerPolicy = 'strict-origin';
        frame.style.cssText = 'width:100%;height:100%;min-height:480px;border:0;display:block';
        let destroyed = false, resolveReady, rejectReady;
        const pending = new Map();
        const ready = new Promise((resolve, reject) => {resolveReady = resolve; rejectReady = reject;});
        ready.catch(() => {});
        const timeout = global.setTimeout(() => {rejectReady(fail('ready_timeout')); destroy();}, 20000);
        function receive(event) {
            if (destroyed || event.source !== frame.contentWindow || event.origin !== url.origin) return;
            const data = event.data;
            if (!data || typeof data !== 'object' || Array.isArray(data)) return;
            try { if (JSON.stringify(data).length > 2048) return; } catch (_) { return; }
            if (data.contract_version !== version || data.surface !== 'profile' || data.app_id !== appId || data.frame_instance_id !== bootstrap.frame_instance_id) return;
            if (!['ready','profile_saved','error','resize','language_changed'].includes(data.event)) return;
            if (['ready','profile_saved'].includes(data.event) && (!Number.isSafeInteger(data.version) || data.version < 1)) return;
            if (data.event === 'resize' && (!Number.isFinite(data.height) || data.height < 0 || data.height > 20000)) return;
            if (data.event === 'ready') {global.clearTimeout(timeout); resolveReady(data);}
            const item = pending.get(data.request_id);
            if (item && data.event === 'language_changed') {global.clearTimeout(item.timer); pending.delete(data.request_id); item.resolve(data);}
            try { onEvent(data); } catch (error) { global.console?.error('Aurvek onEvent failed', error); }
        }
        function destroy() {
            if (destroyed) return; destroyed = true; global.clearTimeout(timeout);
            global.removeEventListener('message', receive); frame.remove(); rejectReady(fail('destroyed'));
            for (const item of pending.values()) {global.clearTimeout(item.timer); item.reject(fail('destroyed'));} pending.clear();
        }
        global.addEventListener('message', receive); container.append(frame);
        try {submitBootstrap(document, container, frame.name, url, bootstrap);} catch (error) {destroy(); throw error;}
        return Object.freeze({ready, destroy, close: async () => destroy(),
            async setLanguage(uiLanguage) {
                if (!['en','es','ja','fr','pt','it','de'].includes(uiLanguage)) throw fail('invalid_language');
                await ready; if (destroyed) throw fail('destroyed');
                const requestId = global.crypto.randomUUID();
                return new Promise((resolve, reject) => {
                    const timer = global.setTimeout(() => {pending.delete(requestId); reject(fail('request_timeout'));}, 20000);
                    pending.set(requestId, {resolve, reject, timer});
                    frame.contentWindow.postMessage({contract_version: version, surface: 'profile', app_id: appId,
                        frame_instance_id: bootstrap.frame_instance_id, event: 'set_ui_language', ui_language: uiLanguage, request_id: requestId}, url.origin);
                });
            }});
    }

    // Call directly from a user click. The blank window opens before any async
    // work; its document posts a fresh ticket and never retains an opener.
    function openWindow(getFreshOptions, options = {}) {
        if (typeof getFreshOptions !== 'function') return Promise.reject(fail('fresh_bootstrap_required'));
        const popup = global.open('about:blank', '_blank');
        if (!popup) return Promise.reject(fail('popup_blocked'));
        popup.opener = null;
        let timer;
        return (async () => {
            try {
                const doc = popup.document;
                doc.title = options.title || 'Aurvek';
                const policy = doc.createElement('meta'); policy.name = 'referrer'; policy.content = 'strict-origin';
                doc.head.append(policy);
                const status = doc.createElement('p'); status.setAttribute('role', 'status');
                status.textContent = options.loadingText || 'Aurvek'; doc.body.append(status);
                const fresh = await Promise.race([Promise.resolve().then(getFreshOptions), new Promise((_, reject) => {
                    timer = global.setTimeout(() => reject(fail('bootstrap_timeout')), 60000);
                })]);
                if (popup.closed) throw fail('popup_closed');
                if (popup.location.href !== 'about:blank') throw fail('popup_changed');
                const url = bootstrapUrl(fresh);
                submitBootstrap(doc, doc.body, '_self', url, fresh.bootstrap);
                // Submission is not proof of a loaded chat. Top-level chat owns
                // its own session/errors and sends no metadata to its opener.
                return { status: 'submitted' };
            } catch (error) {
                try { if (!popup.closed && popup.location.href === 'about:blank') popup.close(); } catch (_) { /* User navigated elsewhere. */ }
                throw error;
            } finally { global.clearTimeout(timer); }
        })();
    }

    function mount(container, options) {
        const { bootstrap, appId, contextId, onEvent = () => {} } = options || {};
        const url = bootstrapUrl(options);
        if (!container?.append) throw fail('invalid_bootstrap');
        let presentation = options.presentation || 'full';
        if (!['full', 'compact'].includes(presentation)) throw fail('invalid_presentation');
        const timeout = options.timeoutMs ?? 20000;
        if (!Number.isFinite(timeout) || timeout < 1000 || timeout > 120000) throw fail('invalid_timeout');
        const frame = document.createElement('iframe');
        frame.name = `aurvek-${global.crypto.randomUUID()}`;
        frame.title = options.title || 'Aurvek';
        // A form POST navigates this frame without a src attribute. Delegate
        // media to the actual chat origin instead of the implicit 'src' origin.
        frame.allow = `microphone ${url.origin}; autoplay ${url.origin}`;
        frame.referrerPolicy = 'strict-origin';
        frame.style.cssText = 'width:100%;height:100%;min-height:320px;border:0;display:block';
        let current = null;
        let destroyed = false;
        let initialized = false;
        let resolveReady, rejectReady;
        const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
        // Consumers may attach their handler after mounting; still reject ready normally.
        ready.catch(() => {});
        const pending = new Map();
        const readyTimer = global.setTimeout(() => {
            rejectReady(fail('ready_timeout'));
            destroy();
        }, timeout);
        function notify(data) {
            if (destroyed) return;
            try { onEvent(data); } catch (error) { global.console?.error('Aurvek onEvent failed', error); }
        }
        function application(value) {
            return value?.contract_version === applicationVersion && value.app_id === appId &&
                value.context_id === contextId && Number.isSafeInteger(value.conversation_id) && value.conversation_id > 0 &&
                typeof value.assistant_id === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value.assistant_id);
        }
        function receive(event) {
            if (destroyed || event.source !== frame.contentWindow || event.origin !== url.origin) return;
            const data = event.data;
            if (!data || typeof data !== 'object' || Array.isArray(data)) return;
            try { if (new TextEncoder().encode(JSON.stringify(data)).length > 32768) return; } catch (_) { return; }
            if (data.contract_version !== version || data.app_id !== appId || data.frame_instance_id !== bootstrap.frame_instance_id) return;
            if (data.event === 'ready') {
                if (!application(data.application) || String(data.application.conversation_id) !== data.conversation_id ||
                    typeof data.external_project_id !== 'string') return;
                // A handoff reload authorizes the destination in this same frame/context.
                current = data;
                global.clearTimeout(readyTimer);
                if (!initialized) { initialized = true; resolveReady(data); }
                void request('set_presentation', { presentation }, ['action_completed']).catch(error => notify({ event: 'error', code: error.code }));
            } else {
                if (!current || data.external_project_id !== current.external_project_id || data.conversation_id !== current.conversation_id) return;
                if (data.application && !application(data.application)) return;
                if (data.event === 'session_expired') {
                    for (const item of pending.values()) { global.clearTimeout(item.timer); item.reject(fail('session_expired')); }
                    pending.clear();
                }
            }
            const item = pending.get(data.request_id);
            if (item && (item.events.includes(data.event) || ['error', 'handoff_failed'].includes(data.event))) {
                // Closing means the server and local audio have actually settled.
                if (item.event === 'request_activity_close' && data.event === 'activity_changed' && !data.activity_closed) return;
                pending.delete(data.request_id);
                global.clearTimeout(item.timer);
                if (['error', 'handoff_failed'].includes(data.event)) item.reject(fail(data.code || data.event));
                else item.resolve(data);
            }
            notify(data);
        }
        async function request(event, details, events, deadline = timeout) {
            await ready;
            if (destroyed) throw fail('destroyed');
            const requestId = global.crypto.randomUUID();
            return new Promise((resolve, reject) => {
                const timer = global.setTimeout(() => { pending.delete(requestId); reject(fail('request_timeout')); }, deadline);
                pending.set(requestId, { event, events, timer, resolve, reject });
                frame.contentWindow.postMessage({ contract_version: version, event, request_id: requestId,
                    app_id: appId, external_project_id: current.external_project_id, conversation_id: current.conversation_id,
                    frame_instance_id: bootstrap.frame_instance_id, ...details }, url.origin);
            });
        }
        function destroy() {
            if (destroyed) return;
            destroyed = true;
            global.clearTimeout(readyTimer);
            global.removeEventListener('message', receive);
            rejectReady(fail('destroyed'));
            for (const item of pending.values()) { global.clearTimeout(item.timer); item.reject(fail('destroyed')); }
            pending.clear();
            frame.remove();
        }
        global.addEventListener('message', receive);
        container.append(frame);
        try { submitBootstrap(document, container, frame.name, url, bootstrap); }
        catch (error) { destroy(); throw error; }
        return Object.freeze({
            ready,
            state: () => request('request_state', {}, ['state']),
            focus: () => request('request_focus', {}, ['action_completed']),
            stop: () => request('request_activity_close', {}, ['activity_changed'], 35000),
            handoff(assistantId, settings = {}) {
                if (!alias(assistantId) || (settings.mode && !['resume', 'new'].includes(settings.mode)) ||
                    (settings.operationId !== undefined && !/^[A-Za-z0-9._:-]{8,128}$/.test(settings.operationId)) ||
                    (settings.conversationId !== undefined && (!Number.isSafeInteger(settings.conversationId) || settings.conversationId <= 0 || settings.mode === 'new')) ||
                    (settings.note !== undefined && settings.note !== null && (typeof settings.note !== 'string' || settings.note.length > 2000))) {
                    return Promise.reject(fail('invalid_handoff'));
                }
                return request('request_handoff', { assistant_id: assistantId, mode: settings.mode || 'resume',
                    operation_id: settings.operationId || global.crypto.randomUUID(),
                    ...(settings.conversationId ? { destination_conversation_id: settings.conversationId } : {}),
                    ...(settings.note !== undefined ? { note: settings.note } : {}) }, ['assistant_changed'], 45000);
            },
            back: () => request('request_return', { operation_id: global.crypto.randomUUID() }, ['assistant_changed'], 45000),
            setEntry: assistantId => assistantId === null || alias(assistantId)
                ? request('request_entry_preference', { channel: 'web', assistant_id: assistantId }, ['state']) : Promise.reject(fail('invalid_assistant')),
            setLanguage: uiLanguage => ['en', 'es', 'ja', 'fr', 'pt', 'it', 'de'].includes(uiLanguage)
                ? request('set_ui_language', { ui_language: uiLanguage }, ['capabilities_changed']) : Promise.reject(fail('invalid_language')),
            setPresentation(value) {
                if (!['full', 'compact'].includes(value)) return Promise.reject(fail('invalid_presentation'));
                presentation = value;
                return request('set_presentation', { presentation }, ['action_completed']);
            },
            action(name) {
                if (!names.has(name)) return Promise.reject(fail('unsupported_action'));
                return request('request_action', { action: name }, ['action_completed'], 35000);
            },
            async close() { if (initialized && !destroyed) await this.stop(); destroy(); },
            destroy,
        });
    }
    global.AurvekApplications = Object.freeze({ mount, mountProfile, openWindow });
})(window);
