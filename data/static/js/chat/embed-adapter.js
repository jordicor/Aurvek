/* aurvek_embed.v1: metadata-only bridge; the native chat owns all turns. */
(function (global) {
    'use strict';
    const configNode = document.getElementById('aurvek-embed-config');
    if (!configNode) return;
    const config = Object.freeze(JSON.parse(configNode.textContent));
    const version = 'aurvek_embed.v1';
    const base = `/embed/${encodeURIComponent(config.app_id)}/${encodeURIComponent(config.frame_instance_id)}`;
    const nativeFetch = global.fetch.bind(global);
    const languages = new Set(['en', 'es', 'ja', 'fr', 'pt', 'it', 'de']);
    let languageRequest = 0;
    let statusKey = 'idle';
    let activity = 'idle';
    let expired = false;
    let stopPromise = null;
    let pendingConversationUpdate = false;
    let ready = false;
    let lastGenerating = false;
    const t = key => global.AurvekI18n.t(`embed.${key}`);
    const scopeHeaders = headers => {
        const result = new Headers(headers || {});
        result.set('X-Aurvek-UI-Language', global.AurvekI18n.language);
        result.set('X-Aurvek-Embed-Frame', config.frame_instance_id);
        result.set('X-Aurvek-Embed-Project', config.external_project_id);
        result.set('X-Aurvek-Embed-Conversation', config.conversation_id);
        result.set('X-GPTSub-CSRF', config.csrf_token);
        return result;
    };
    function emit(event, details = {}, requestId) {
        if (global.parent === global) return;
        global.parent.postMessage({
            contract_version: version, event,
            app_id: config.app_id, external_project_id: config.external_project_id,
            conversation_id: config.conversation_id, frame_instance_id: config.frame_instance_id,
            ...(requestId ? { request_id: requestId } : {}), ...details
        }, config.parent_origin);
    }
    function status(key) {
        statusKey = key;
        const element = document.getElementById('embed-session-status');
        if (element) element.textContent = key === 'idle' ? '' : t(key);
    }
    function sessionExpired() {
        if (expired) return;
        expired = true;
        document.querySelectorAll('#form-message button, #form-message textarea, #form-message input').forEach(el => { el.disabled = true; });
        status('expired');
        emit('session_expired');
    }
    global.fetch = async function (input, options) {
        const url = new URL(input instanceof Request ? input.url : input, global.location.href);
        if (url.origin !== global.location.origin) return nativeFetch(input, options);
        const init = { ...options, headers: scopeHeaders(options?.headers || (input instanceof Request ? input.headers : undefined)) };
        const method = (init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
        const sending = method === 'POST' && url.pathname === `/api/conversations/${config.conversation_id}/messages`;
        if (sending) pendingConversationUpdate = true;
        const response = await nativeFetch(input, init);
        if (response.status === 401) sessionExpired();
        if (response.status === 403) {
            try {
                const failure = await response.clone().json();
                if (failure.error === 'membership_inactive') sessionExpired();
            } catch (_) { /* A non-JSON edge response is not proof of expiry. */ }
        }
        if (response.status === 404 && !config.application && url.pathname.startsWith(base + '/')) sessionExpired();
        if (response.status >= 500) status('error');
        if (sending && response.ok) void readActivity().catch(() => {});
        return response;
    };
    async function readActivity(requestId, closed = false) {
        const response = await global.fetch(`${base}/activity`, { cache: 'no-store' });
        if (!response.ok) throw new Error('service_unavailable');
        const result = await response.json();
        if (!['idle', 'generating', 'call', 'ending'].includes(result.activity)) throw new Error('invalid_state');
        activity = result.activity === 'idle' ? (global.getAudioActivity?.() || 'idle') : result.activity;
        if (activity === 'idle') activity = global.AurvekChatActions?.activity() || 'idle';
        status(expired ? 'expired' : activity);
        emit('activity_changed', { activity, ...(closed && activity === 'idle' ? { activity_closed: true } : {}) }, requestId);
        if (pendingConversationUpdate && activity === 'idle') {
            pendingConversationUpdate = false;
            emit('conversation_updated');
        }
        return activity;
    }
    async function stopActivity(requestId) {
        if (!stopPromise) {
            stopPromise = (async () => {
                status('ending');
                global.cancelAudioRecording?.();
                global.stopAllAudio?.();
                await global.AurvekChatActions?.stopExport();
                const response = await global.fetch(`${base}/activity/stop`, { method: 'POST' });
                if (!response.ok) throw new Error('service_unavailable');
                // A signal receipt is not completion. Check the server's settled
                // activity for up to 30 seconds, without starting another turn.
                const deadline = Date.now() + 30000;
                do {
                    if (await readActivity() === 'idle') return;
                    await new Promise(resolve => global.setTimeout(resolve, 500));
                } while (Date.now() < deadline);
                throw new Error('activity_close_pending');
            })().finally(() => { stopPromise = null; });
        }
        try {
            await stopPromise;
            await readActivity(requestId, true);
            return true;
        } catch (error) {
            status('error');
            emit('error', { code: error.message === 'activity_close_pending' ? 'activity_close_pending' : 'service_unavailable' }, requestId);
            return false;
        }
    }
    function syncSendControl(generating) {
        const button = document.getElementById('send-button');
        if (button) {
            button.dataset.embedLabel = t(generating ? 'stop' : 'send');
            button.setAttribute('aria-label', t(generating ? 'stop' : 'send'));
        }
        const finished = lastGenerating && !generating;
        lastGenerating = generating;
        if (ready && finished && pendingConversationUpdate && !stopPromise) void readActivity().catch(() => {});
    }
    function setText(selector, key) {
        document.querySelectorAll(selector).forEach(el => { if (el.textContent !== t(key)) el.textContent = t(key); });
    }
    function localize() {
        document.documentElement.lang = global.AurvekI18n.language;
        document.querySelectorAll('[data-embed-text]').forEach(el => {
            if (el.closest('#chat-messages-container')) return;
            const value = t(el.dataset.embedText);
            if (el.textContent !== value) el.textContent = value;
        });
        const input = document.getElementById('message-text');
        if (input && input.placeholder !== t('write')) input.placeholder = t('write');
        if (input) input.setAttribute('aria-label', t('write'));
        syncSendControl(document.getElementById('send-button')?.dataset.streaming === 'true');
        setText('#loading-indicator .visually-hidden, #loading-indicator > div:not(.spinner-border)', 'loading');
        setText('#locked-conversation-banner .api-keys-banner-content span', 'locked');
        setText('#api-keys-required-banner .api-keys-banner-content', 'keys');
        setText('#insufficient-balance-message', 'balance');
        setText('#provider-health-banner-text', 'provider');
        const title = document.getElementById('chat-title');
        if (title) title.removeAttribute('title');
        for (const [id, key] of [['scroll-top-btn', 'top'], ['scroll-bottom-btn', 'bottom']]) {
            const element = document.getElementById(id);
            if (element) {
                element.title = t(key);
                element.setAttribute('aria-label', t(key));
            }
        }
    }
    async function setLanguage(selected, requestId) {
        const request = ++languageRequest;
        try {
            if (selected !== global.AurvekI18n.language) {
                // Locale requests apply their effects only after winning the
                // host-selection race, including failed/expired responses.
                const response = await nativeFetch(`${base}/session?ui_language=${encodeURIComponent(selected)}`, {
                    cache: 'no-store', headers: scopeHeaders(),
                });
                if (request !== languageRequest) return;
                if (response.status === 401 || (response.status === 404 && !config.application)) sessionExpired();
                if (response.status === 403) {
                    try {
                        const failure = await response.clone().json();
                        if (request !== languageRequest) return;
                        if (failure.error === 'membership_inactive') sessionExpired();
                    } catch (_) { /* Non-JSON edge responses do not prove expiry. */ }
                }
                if (!response.ok) throw new Error('language_unavailable');
                const result = await response.json();
                if (request !== languageRequest) return;
                if (!result.i18n || result.i18n.language !== selected) throw new Error('language_unavailable');
                global.AurvekI18n.setPayload(result.i18n);
            }
            if (request !== languageRequest) return;
            localize();
            global.AurvekApplication?.localize();
            status(statusKey);
            emit('capabilities_changed', { capabilities: config.capabilities, ui_language: global.AurvekI18n.language }, requestId);
        } catch (_) {
            if (request !== languageRequest) return;
            emit('error', { code: 'service_unavailable' }, requestId);
        }
    }
    function validMessage(event) {
        if (event.source !== global.parent || global.parent === global || event.origin !== config.parent_origin) return false;
        const data = event.data;
        if (!data || typeof data !== 'object' || Array.isArray(data)) return false;
        try { if (new TextEncoder().encode(JSON.stringify(data)).length > 8192) return false; } catch (_) { return false; }
        if (data.contract_version !== version || data.frame_instance_id !== config.frame_instance_id ||
            data.app_id !== config.app_id || data.external_project_id !== config.external_project_id ||
            data.conversation_id !== config.conversation_id) return false;
        if (!['request_state', 'set_ui_language', 'request_activity_close', 'request_handoff', 'request_return',
            'request_entry_preference', 'request_focus', 'request_action', 'set_presentation'].includes(data.event)) return false;
        if (typeof data.request_id !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(data.request_id)) return false;
        const extra = {
            set_ui_language: ['ui_language'],
            request_handoff: ['operation_id', 'assistant_id', 'mode', 'destination_conversation_id', 'note'],
            request_entry_preference: ['channel', 'assistant_id'], request_return: ['operation_id'],
            request_action: ['action'], set_presentation: ['presentation'],
        };
        if (['request_return', 'request_action', 'set_presentation', 'request_focus'].includes(data.event) && !config.application) return false;
        if (data.event === 'request_return' && (typeof data.operation_id !== 'string' || !/^[A-Za-z0-9._:-]{8,128}$/.test(data.operation_id))) return false;
        if (data.event === 'request_action' && !['attachment', 'dictation', 'voice', 'export_pdf', 'export_mp3'].includes(data.action)) return false;
        if (data.event === 'set_presentation' && !['full', 'compact'].includes(data.presentation)) return false;
        if (data.event === 'request_handoff' && (!config.application || !global.AurvekApplication?.validHandoff(data))) return false;
        if (data.event === 'request_entry_preference' && (!config.application || data.channel !== 'web' ||
            !(data.assistant_id === null || (typeof data.assistant_id === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(data.assistant_id))))) return false;
        const allowed = new Set(['contract_version', 'event', 'frame_instance_id', 'app_id', 'external_project_id', 'conversation_id', 'request_id', ...(extra[data.event] || [])]);
        return Object.keys(data).every(key => allowed.has(key));
    }
    global.addEventListener('message', event => {
        if (!validMessage(event)) return;
        const data = event.data;
        if (data.event === 'set_ui_language') {
            if (!languages.has(data.ui_language)) return;
            void setLanguage(data.ui_language, data.request_id);
        } else if (data.event === 'request_activity_close') {
            void stopActivity(data.request_id);
        } else if (data.event === 'request_handoff') {
            void global.AurvekApplication.handoff(data, data.request_id);
        } else if (data.event === 'request_return') {
            void global.AurvekApplication.back(data.operation_id, data.request_id);
        } else if (data.event === 'request_entry_preference') {
            void global.AurvekApplication.setEntry(data.assistant_id, data.request_id);
        } else if (data.event === 'set_presentation') {
            document.documentElement.dataset.embedPresentation = data.presentation;
            emit('action_completed', { action: 'presentation', presentation: data.presentation }, data.request_id);
        } else if (data.event === 'request_focus') {
            document.getElementById('message-text')?.focus();
            emit('action_completed', { action: 'focus' }, data.request_id);
        } else if (data.event === 'request_action') {
            void global.AurvekChatActions.run(data.action, { host: true }).then(result => {
                emit('action_completed', { action: data.action, ...result }, data.request_id);
            }).catch(error => emit('error', { code: error.code || 'action_unavailable' }, data.request_id));
        } else {
            void readActivity(data.request_id).catch(() => emit('error', { code: 'service_unavailable' }, data.request_id));
            if (config.application) void global.AurvekApplication?.readState(data.request_id);
        }
    });
    global.AurvekEmbed = Object.freeze({
        config, sessionUrl: `${base}/session`, t, sessionExpired, stopActivity, syncSendControl, emit,
        requestHeaders: scopeHeaders,
        language: () => global.AurvekI18n.language,
        isExpired: () => expired,
        resourceUrl(value) {
            const url = new URL(value, global.location.href);
            if (url.origin !== global.location.origin) return '';
            const attachment = config.capabilities.attachments &&
                /^\/api\/attachments\/[A-Za-z0-9_-]+\/(content|download)$/.test(url.pathname);
            const conversationPrefix = `/api/conversations/${config.conversation_id}/`;
            const media = config.application && config.capabilities.text &&
                url.pathname === `${conversationPrefix}media/content`;
            const audio = config.application && config.capabilities.voice &&
                url.pathname === `${conversationPrefix}voice/audio`;
            const exportMatch = url.pathname.match(new RegExp(`^${conversationPrefix}exports/(pdf|mp3)/[A-Za-z0-9_-]{32}/content$`));
            const exported = config.application && exportMatch && config.capabilities[`export_${exportMatch[1]}`] &&
                (exportMatch[1] !== 'mp3' || config.capabilities.tts);
            if (!attachment && !media && !audio && !exported) return '';
            url.searchParams.set('embed_frame', config.frame_instance_id);
            url.searchParams.set('embed_project', config.external_project_id);
            url.searchParams.set('embed_conversation', config.conversation_id);
            return url.href;
        },
        chatReady() {
            ready = true;
            localize();
            const application = config.application ? {
                contract_version: 'aurvek_applications.v1', app_id: config.app_id,
                context_id: config.application.context_id, assistant_id: config.application.assistant_id,
                conversation_id: config.application.conversation_id, display_name: config.application.display_name
            } : null;
            emit('ready', { capabilities: config.capabilities, ui_language: global.AurvekI18n.language,
                ...(application ? { application } : {}) });
            if (config.application) void global.AurvekApplication?.onReady();
            void readActivity().catch(() => { status('error'); });
        }
    });
    document.addEventListener('DOMContentLoaded', localize);
})(window);
