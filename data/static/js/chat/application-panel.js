/* Application navigation around the native chat; never a second chat client. */
(function (global) {
    'use strict';
    const embed = global.AurvekEmbed;
    if (!embed?.config.application) return;
    const config = embed.config;
    const base = `/embed/${encodeURIComponent(config.app_id)}/${encodeURIComponent(config.frame_instance_id)}`;
    const reloadUrl = `${base}/chat`;
    const storageKey = `aurvek.handoff.${config.app_id}.${config.frame_instance_id}`;
    const label = (key, params) => global.AurvekI18n.t(`application.${key}`, params);
    const alias = value => typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
    const conversationId = value => Number.isSafeInteger(value) && value > 0;
    const requestIdValid = value => typeof value === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(value);
    let state = null;
    let busy = false;
    let reloading = false;
    let statusKey = 'loading';
    let disabledControls = [];
    const node = id => document.getElementById(`application-${id}`);
    function status(key) {
        statusKey = key;
        if (node('status')) node('status').textContent = key ? label(key) : '';
    }
    function localize() {
        document.querySelectorAll('[data-application-text]').forEach(element => {
            element.textContent = label(element.dataset.applicationText);
        });
        node('assistant')?.setAttribute('aria-label', label('assistant'));
        status(statusKey);
        renderEntry();
    }
    function controls() {
        document.querySelectorAll('#application-panel button, #application-panel select').forEach(element => {
            element.disabled = busy || reloading || !state || embed.isExpired();
        });
        if (node('back')) node('back').disabled = busy || reloading || !state?.previous_assistant_id || embed.isExpired();
        node('panel')?.setAttribute('aria-busy', String(busy));
    }
    function lock(value) {
        busy = value;
        if (value) {
            disabledControls = Array.from(document.querySelectorAll('#form-message button, #form-message textarea, #form-message input'))
                .map(element => [element, element.disabled]);
            disabledControls.forEach(([element]) => { element.disabled = true; });
        } else {
            disabledControls.forEach(([element, disabled]) => { element.disabled = disabled || embed.isExpired(); });
            disabledControls = [];
        }
        controls();
    }
    function metadata(value) {
        if (!value || value.app_id !== config.app_id || value.context_id !== config.application.context_id ||
            !alias(value.assistant_id) || !conversationId(value.conversation_id) ||
            typeof value.display_name !== 'string' || value.display_name.length > 120) throw new Error('invalid_state');
        return { contract_version: 'aurvek_applications.v1', app_id: value.app_id,
            context_id: value.context_id, assistant_id: value.assistant_id,
            conversation_id: value.conversation_id, display_name: value.display_name };
    }
    function render() {
        if (!state) return;
        const select = node('assistant');
        if (select) {
            const previous = select.value;
            select.replaceChildren();
            for (const assistant of state.assistants) {
                const option = document.createElement('option');
                option.value = assistant.assistant_id;
                option.textContent = assistant.display_name;
                select.append(option);
            }
            select.value = state.assistants.some(item => item.assistant_id === previous) ? previous : state.application.assistant_id;
        }
        if (node('active')) node('active').textContent = state.application.display_name;
        renderEntry();
        controls();
    }
    function renderEntry() {
        if (state && node('entry-value')) {
            const saved = state.assistants.find(item => item.assistant_id === state.entry_preferences.web);
            node('entry-value').textContent = saved ? label('next_entry', { name: saved.display_name })
                : label(state.entry_preferences.web ? 'unavailable_entry' : 'default_entry');
        }
    }
    function reconcile() {
        if (reloading) return;
        reloading = true;
        controls();
        status('reconcile');
        global.location.replace(reloadUrl);
    }
    async function readState(requestId) {
        try {
            const response = await global.fetch(`${base}/application/state`, { cache: 'no-store' });
            if (!response.ok) {
                if (response.status === 404 || response.status === 409) reconcile();
                throw new Error('service_unavailable');
            }
            const result = await response.json();
            const application = metadata(result.application);
            if (String(application.conversation_id) !== config.conversation_id) {
                reconcile();
                return null;
            }
            if (!Array.isArray(result.assistants) || result.assistants.length > 256) throw new Error('invalid_state');
            const assistants = result.assistants.map(item => {
                if (!alias(item.assistant_id) || typeof item.display_name !== 'string' || item.display_name.length > 120) throw new Error('invalid_state');
                return { assistant_id: item.assistant_id, display_name: item.display_name };
            });
            const allowed = value => value === null || assistants.some(item => item.assistant_id === value);
            const entry = result.entry_preferences?.web ?? null;
            const previous = result.previous_assistant_id ?? null;
            const previousConversation = result.previous_conversation_id ?? null;
            const lastOperation = result.last_handoff_operation_id ?? null;
            if (!(entry === null || alias(entry)) || !allowed(previous) || !(previousConversation === null || conversationId(previousConversation)) ||
                !(lastOperation === null || (typeof lastOperation === 'string' && lastOperation.length >= 8 && lastOperation.length <= 128))) throw new Error('invalid_state');
            state = { application, assistants, entry_preferences: { web: entry },
                previous_assistant_id: previous, previous_conversation_id: previousConversation,
                last_handoff_operation_id: lastOperation };
            render();
            status('');
            embed.emit('state', state, requestId);
            return state;
        } catch (_) {
            status('error');
            embed.emit('error', { code: 'application_state_unavailable' }, requestId);
            return null;
        }
    }
    function validHandoff(value) {
        return !!value && alias(value.assistant_id) && typeof value.operation_id === 'string' &&
            /^[A-Za-z0-9._:-]{8,128}$/.test(value.operation_id) && ['resume', 'new'].includes(value.mode) &&
            (value.destination_conversation_id === undefined || (value.mode === 'resume' && conversationId(value.destination_conversation_id))) &&
            (value.note === undefined || value.note === null || (typeof value.note === 'string' && value.note.length <= 2000));
    }
    function remember(value, requestId) {
        try {
            global.sessionStorage.setItem(storageKey, JSON.stringify({
                source_conversation_id: config.conversation_id, assistant_id: value.assistant_id,
                operation_id: value.operation_id, request_id: requestId
            }));
        } catch (_) { /* Navigation remains functional when storage is unavailable. */ }
    }
    async function handoff(value, requestId) {
        if (!validHandoff(value) || busy || reloading || embed.isExpired()) {
            embed.emit('handoff_failed', { code: busy ? 'operation_in_progress' : 'invalid_request' }, requestId);
            return false;
        }
        lock(true);
        status('switching');
        let submitted = false;
        let navigating = false;
        try {
            if (!await embed.stopActivity()) {
                // A stale frame may reject even the stop request. Re-resolve
                // its destination before offering another action on the source.
                await readState();
                throw new Error('activity_close_pending');
            }
            const body = { operation_id: value.operation_id, source_conversation_id: Number(config.conversation_id),
                assistant_id: value.assistant_id, mode: value.mode };
            if (value.destination_conversation_id !== undefined) body.conversation_id = value.destination_conversation_id;
            if (value.note !== undefined) body.note = value.note;
            remember(value, requestId);
            submitted = true;
            const response = await global.fetch(`${base}/application/handoff`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
            if (!response.ok) {
                if (response.status === 404 || response.status === 409 || response.status >= 500) {
                    navigating = true;
                    reconcile();
                    return false;
                }
                submitted = false;
                throw new Error('handoff_rejected');
            }
            // The destination bootstrap, loaded at this frame's fixed URL,
            // reauthorizes and initializes the ordinary chat from scratch.
            navigating = true;
            reconcile();
            return true;
        } catch (error) {
            if (reloading) return false;
            if (submitted) {
                navigating = true;
                reconcile();
            } else {
                try { global.sessionStorage.removeItem(storageKey); } catch (_) { /* Optional storage. */ }
                status('error');
                embed.emit('handoff_failed', { code: error.message === 'activity_close_pending' ? error.message : 'handoff_rejected' }, requestId);
            }
            return false;
        } finally {
            if (!navigating && !reloading) lock(false);
        }
    }
    async function setEntry(assistantId, requestId) {
        if (busy || reloading || embed.isExpired() || !(assistantId === null || alias(assistantId))) return false;
        busy = true;
        controls();
        try {
            const response = await global.fetch(`${base}/application/entry`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ channel: 'web', assistant_id: assistantId })
            });
            if (!response.ok) throw new Error('entry_preference_failed');
            if (!await readState(requestId)) return false;
            status('saved');
            return true;
        } catch (_) {
            status('error');
            embed.emit('error', { code: 'entry_preference_failed' }, requestId);
            return false;
        } finally {
            busy = false;
            controls();
        }
    }
    async function onReady() {
        const current = await readState();
        if (!current) return;
        let previous;
        try {
            previous = JSON.parse(global.sessionStorage.getItem(storageKey) || 'null');
            global.sessionStorage.removeItem(storageKey);
        } catch (_) { return; }
        if (!previous || !requestIdValid(previous.operation_id)) return;
        const requestId = requestIdValid(previous.request_id) ? previous.request_id : undefined;
        const changed = previous.assistant_id === current.application.assistant_id &&
            previous.operation_id === current.last_handoff_operation_id;
        embed.emit(changed ? 'assistant_changed' : 'handoff_failed', changed
            ? { application: current.application, operation_id: previous.operation_id }
            : { code: 'handoff_not_confirmed', operation_id: previous.operation_id }, requestId);
    }
    function runtimeHandoff(result) {
        if (!result || result.completed !== true ||
                result.source_conversation_id !== Number(embed.config.conversation_id) ||
                !Number.isSafeInteger(result.conversation_id) || result.conversation_id <= 0 ||
                !alias(result.assistant_id) || !requestIdValid(result.operation_id)) return false;
        try {
            global.sessionStorage.setItem(storageKey, JSON.stringify({
                operation_id: result.operation_id, assistant_id: result.assistant_id
            }));
        } catch (_) { /* The new frame state still confirms its own scope. */ }
        lock(true);
        reloading = true;
        // The server already moved this exact frame after settling A. Do not
        // call stop/replay with A's now-stale headers or trust a returned URL.
        global.location.replace(`${base}/chat`);
        return true;
    }
    function runtimeHandoffFailed(result) {
        status('error');
        embed.emit('handoff_failed', { code: result?.code || 'handoff_rejected' });
    }
    async function back(operationId, requestId) {
        const current = state || await readState();
        if (!current?.previous_assistant_id || !current.previous_conversation_id) {
            embed.emit('handoff_failed', { code: 'previous_assistant_unavailable' }, requestId);
            return false;
        }
        return handoff({ assistant_id: current.previous_assistant_id, mode: 'resume', operation_id: operationId,
            destination_conversation_id: current.previous_conversation_id }, requestId);
    }
    global.AurvekApplication = Object.freeze({ validHandoff, handoff, back, setEntry, readState, onReady, localize,
        runtimeHandoff, runtimeHandoffFailed });
    document.addEventListener('DOMContentLoaded', () => {
        localize();
        controls();
        const navigate = (assistantId, mode, destination) => {
            const operationId = global.crypto.randomUUID();
            void handoff({ assistant_id: assistantId, mode, operation_id: operationId,
                ...(destination ? { destination_conversation_id: destination } : {}) });
        };
        node('resume')?.addEventListener('click', () => navigate(node('assistant').value, 'resume'));
        node('fresh')?.addEventListener('click', () => navigate(node('assistant').value, 'new'));
        node('back')?.addEventListener('click', () => { void back(global.crypto.randomUUID()); });
        node('entry')?.addEventListener('click', () => { void setEntry(node('assistant').value); });
        node('reset')?.addEventListener('click', () => { void setEntry(null); });
        document.getElementById('form-message')?.addEventListener('submit', event => {
            if (busy) { event.preventDefault(); event.stopImmediatePropagation(); }
        }, true);
    });
})(window);
