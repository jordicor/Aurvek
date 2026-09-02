(function() {
    'use strict';

    const POLL_INTERVAL_MS = 3000;
    const ACTIVE_CALL_STATUSES = new Set([
        'created', 'dispatching', 'dispatch_unknown', 'queued', 'initiated', 'ringing', 'in_progress'
    ]);
    const HANGUP_CALL_STATUSES = new Set(['queued', 'initiated', 'ringing', 'in_progress']);

    const state = {
        account: null,
        assignmentPending: false,
        binding: null,
        calls: [],
        controller: null,
        conversationId: null,
        generation: 0,
        jobs: [],
        historyController: null,
        historyRequestId: 0,
        menuController: null,
        modal: null,
        modalOpen: false,
        pollTimer: null
    };

    function byId(id) {
        return document.getElementById(id);
    }

    function currentConversationId() {
        const value = window.currentConversationId;
        if (value === null || value === undefined || value === '') return null;
        const parsed = Number(value);
        return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
    }

    function currentConversationFlags() {
        const conversationId = currentConversationId();
        const selected = conversationId === null ? null : document.querySelector(
            `.active-chat[data-conversation-id="${conversationId}"]`
        );
        const incognitoBadge = byId('incognito-chat-badge');
        const lockedBanner = byId('locked-conversation-banner');
        return {
            incognito: Boolean(
                selected?.dataset?.isIncognito === 'true' ||
                (incognitoBadge && !incognitoBadge.hidden)
            ),
            locked: Boolean(
                selected?.dataset?.locked === 'true' ||
                (lockedBanner && lockedBanner.style.display !== 'none')
            )
        };
    }

    function browserTimeZone() {
        try {
            return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
        } catch (_error) {
            return 'UTC';
        }
    }

    function setStatus(message, kind) {
        const element = byId('phone-call-status');
        if (!element) return;
        element.textContent = String(message || '');
        element.className = 'phone-call-status';
        if (message) element.classList.add('is-visible', `is-${kind || 'info'}`);
    }

    function safeServerMessage(response, payload) {
        if (response && [400, 403, 404, 409, 422, 503].includes(response.status)) {
            const detail = payload && typeof payload.detail === 'string'
                ? payload.detail.trim()
                : '';
            if (/incognito/i.test(detail)) {
                return 'Phone calls are unavailable in incognito conversations.';
            }
            if (/conversation is locked/i.test(detail)) {
                return 'This conversation is locked.';
            }
            if (/claimed concurrently|unclaimed scheduled call/i.test(detail)) {
                return 'That scheduled call has already started and can no longer be canceled.';
            }
            if (/calls from aurvek to your phone are disabled|outbound calls?.*disabled|allow_outbound/i.test(detail)) {
                return 'Calls from Aurvek to your phone are disabled for this conversation.';
            }
            if (/no active phone binding|active phone binding not found/i.test(detail)) {
                return 'Choose a conversation for phone calls before starting a call.';
            }
            if (/e\.?164|canonical|phone contact|profile phone|destination country/i.test(detail)) {
                return 'Your saved phone number cannot be used for this call. Check it in Settings.';
            }
            if (/dst|fold|time\s*zone|scheduled_at|ambiguous|does not exist/i.test(detail)) {
                return 'That time cannot be scheduled. Choose a different time and try again.';
            }
        }
        if (response && response.status === 401) return 'Your session has expired.';
        if (response && response.status >= 500) {
            return 'The phone service could not complete that request. Please try again later.';
        }
        return 'The phone request could not be completed.';
    }

    async function requestJson(url, options) {
        const requestOptions = { ...(options || {}) };
        const headers = { ...(requestOptions.headers || {}) };
        if (requestOptions.body !== undefined) headers['Content-Type'] = 'application/json';
        const method = String(requestOptions.method || 'GET').toUpperCase();
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const csrfToken = document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
            if (!csrfToken) throw new Error('Phone request security is unavailable.');
            headers['X-GPTSub-CSRF'] = csrfToken;
        }
        requestOptions.headers = headers;
        requestOptions.credentials = 'include';
        const fetcher = typeof window.secureFetch === 'function'
            ? window.secureFetch
            : window.fetch.bind(window);
        const response = await fetcher(url, requestOptions);
        if (!response) throw new Error('Your session is no longer available.');
        let payload = null;
        try {
            payload = await response.json();
        } catch (_error) {
            payload = null;
        }
        if (!response.ok) {
            const error = new Error(safeServerMessage(response, payload));
            error.status = response.status;
            throw error;
        }
        return payload || {};
    }

    function mutationOptions(payload) {
        return {
            method: 'POST',
            body: JSON.stringify(payload || {}),
            signal: state.controller?.signal
        };
    }

    function isCurrent(generation, conversationId) {
        return state.modalOpen &&
            generation === state.generation &&
            Number(conversationId) === Number(state.conversationId) &&
            Number(conversationId) === Number(currentConversationId());
    }

    function abortModalRequests() {
        if (state.controller) state.controller.abort();
        state.controller = null;
        if (state.historyController) state.historyController.abort();
        state.historyController = null;
        state.historyRequestId += 1;
        if (state.pollTimer) clearTimeout(state.pollTimer);
        state.pollTimer = null;
    }

    function showActionError(error) {
        if (error && error.name === 'AbortError') return;
        setStatus(error instanceof Error ? error.message : 'The request failed.', 'error');
    }

    function friendlyStatus(value) {
        const labels = {
            created: 'Preparing',
            dispatching: 'Preparing',
            dispatch_unknown: 'Checking status',
            queued: 'Preparing',
            initiated: 'Calling',
            ringing: 'Ringing',
            in_progress: 'In call',
            completed: 'Completed',
            busy: 'Busy',
            no_answer: 'No answer',
            machine: 'Voicemail',
            failed: 'Failed',
            canceled: 'Canceled',
            unresolved: 'Status unavailable'
        };
        return labels[String(value || '').toLowerCase()] || 'Preparing';
    }

    function formatInstant(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        return new Intl.DateTimeFormat(undefined, {
            dateStyle: 'medium',
            timeStyle: 'short'
        }).format(date);
    }

    function activeCall() {
        return state.calls.find(call => ACTIVE_CALL_STATUSES.has(String(call.status || '').toLowerCase())) || null;
    }

    function jobDueTime(job) {
        const value = job?.scheduled_at_utc ? new Date(job.scheduled_at_utc).getTime() : Number.NaN;
        return Number.isFinite(value) ? value : null;
    }

    function preparingJob() {
        return state.jobs.find(job => {
            const status = String(job.status || '').toLowerCase();
            if (status === 'dispatching') return true;
            if (status !== 'scheduled') return false;
            const dueAt = jobDueTime(job);
            return dueAt === null || dueAt <= Date.now() + 30000;
        }) || null;
    }

    function scheduledJob() {
        return state.jobs.find(job => {
            if (String(job.status || '').toLowerCase() !== 'scheduled') return false;
            const dueAt = jobDueTime(job);
            return dueAt !== null && dueAt > Date.now() + 30000;
        }) || null;
    }

    function jobConversationTitle(job) {
        const title = String(
            job?.conversation_title || job?.prompt_name || ''
        ).trim();
        return title || 'another conversation';
    }

    function accountBindingConversationId() {
        const parsed = Number(state.account?.active_binding?.conversation_id);
        return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
    }

    function hasCurrentBinding() {
        return Boolean(
            state.binding &&
            Number(state.binding.conversation_id) === Number(state.conversationId)
        );
    }

    function hasReductiveActions() {
        return Boolean(hasCurrentBinding() || activeCall() || preparingJob() || scheduledJob());
    }

    function beginHistoryRequest() {
        if (state.historyController) state.historyController.abort();
        state.historyController = new AbortController();
        state.historyRequestId += 1;
        return {
            id: state.historyRequestId,
            signal: state.historyController.signal
        };
    }

    function renderTarget() {
        const phone = state.account?.phone || {};
        const eligible = phone.eligible === true;
        const target = byId('phone-call-target');
        const profileLink = byId('phone-call-profile-link');
        const moveNote = byId('phone-call-move-note');
        const actionSection = byId('phone-action-section');
        const assignmentControl = byId('phone-conversation-assignment-control');
        const assignmentButton = byId('phone-conversation-assignment');
        const assignmentIcon = byId('phone-conversation-assignment-icon');
        const assignmentLabel = byId('phone-conversation-assignment-label');
        const flags = currentConversationFlags();
        if (
            !target || !profileLink || !moveNote || !actionSection ||
            !assignmentControl || !assignmentButton || !assignmentIcon || !assignmentLabel
        ) return;

        if (!phone.configured) {
            target.textContent = 'Add your phone number in Settings to receive calls.';
        } else if (!eligible) {
            target.textContent = 'Verify your phone number in Settings to receive calls.';
        } else {
            const masked = String(phone.masked || '').trim();
            target.textContent = masked
                ? `Aurvek will call ${masked}.`
                : 'Aurvek will call your saved phone number.';
        }
        profileLink.hidden = eligible;
        actionSection.hidden = !eligible && !hasReductiveActions();

        const assignedElsewhere = accountBindingConversationId();
        const scheduled = scheduledJob();
        const scheduledElsewhere = scheduled &&
            Number(scheduled.conversation_id) !== Number(state.conversationId);
        if (eligible && !flags.locked && scheduledElsewhere) {
            moveNote.textContent = `Calling now from this conversation will cancel the scheduled call for ${jobConversationTitle(scheduled)}.`;
            moveNote.hidden = false;
        } else if (eligible && !flags.locked && assignedElsewhere && assignedElsewhere !== Number(state.conversationId)) {
            moveNote.textContent = 'Starting or scheduling a call will make this the conversation used for phone calls.';
            moveNote.hidden = false;
        } else {
            moveNote.textContent = '';
            moveNote.hidden = true;
        }

        const assigned = hasCurrentBinding();
        assignmentControl.hidden = !(assigned || (eligible && !flags.locked));
        assignmentButton.disabled = state.assignmentPending;
        assignmentButton.className = assigned
            ? 'btn btn-outline-danger phone-call-assignment-action is-assigned'
            : 'btn btn-outline-primary phone-call-assignment-action is-unassigned';
        assignmentButton.setAttribute('aria-label', assigned
            ? 'Stop using this conversation for phone calls'
            : 'Use this conversation for phone calls');
        assignmentIcon.className = assigned ? 'fas fa-phone-slash' : 'fas fa-phone';
        assignmentLabel.textContent = assigned
            ? 'Stop using this conversation for phone calls'
            : 'Use this conversation for phone calls';
    }

    function renderCallState() {
        const call = activeCall();
        const pendingJob = preparingJob();
        const scheduled = scheduledJob();
        const activeCard = byId('phone-active-card');
        const activeTitle = byId('phone-active-title');
        const activeSummary = byId('phone-active-summary');
        const hangup = byId('phone-hangup');
        const scheduledCard = byId('phone-scheduled-card');
        const scheduledSummary = byId('phone-scheduled-summary');
        const cancelScheduled = byId('phone-cancel-scheduled');
        const callNow = byId('phone-call-now');
        const scheduleForm = byId('phone-schedule-form');
        const flags = currentConversationFlags();
        byId('phone-action-title').textContent = flags.locked
            ? 'Phone activity'
            : 'When should we call?';

        activeCard.hidden = !call && !pendingJob;
        activeTitle.textContent = call
            ? (String(call.status || '').toLowerCase() === 'in_progress' ? 'Call in progress' : friendlyStatus(call.status))
            : 'Preparing call';
        if (call) {
            activeSummary.textContent = String(call.status || '').toLowerCase() === 'in_progress'
                ? 'Connected'
                : 'Aurvek is calling your phone';
        } else if (pendingJob) {
            const details = [
                'Starting shortly',
                formatInstant(pendingJob.scheduled_at_utc)
            ].filter(Boolean);
            if (Number(pendingJob.conversation_id) !== Number(state.conversationId)) {
                details.push(jobConversationTitle(pendingJob));
            }
            activeSummary.textContent = details.join(' · ');
        } else {
            activeSummary.textContent = '';
        }
        hangup.hidden = !call || !HANGUP_CALL_STATUSES.has(String(call.status || '').toLowerCase());
        hangup.dataset.callId = call?.id || '';

        scheduledCard.hidden = !scheduled;
        if (scheduled) {
            const details = [formatInstant(scheduled.scheduled_at_utc) || 'Scheduled'];
            if (Number(scheduled.conversation_id) !== Number(state.conversationId)) {
                details.push(jobConversationTitle(scheduled));
            }
            scheduledSummary.textContent = details.join(' · ');
        } else {
            scheduledSummary.textContent = '';
        }
        cancelScheduled.dataset.jobId = scheduled?.id || '';
        callNow.hidden = flags.locked;
        scheduleForm.hidden = flags.locked || Boolean(scheduled);

        const eligible = state.account?.phone?.eligible === true;
        callNow.disabled = flags.locked || !eligible || Boolean(call || pendingJob);
        byId('phone-schedule-submit').disabled = flags.locked || !eligible || Boolean(call || pendingJob || scheduled);
        updateMenuStateFromData();
    }

    function render() {
        renderTarget();
        renderCallState();
        schedulePoll();
    }

    async function refreshAll() {
        const generation = state.generation;
        const conversationId = state.conversationId;
        const historyRequest = beginHistoryRequest();
        const common = { method: 'GET', signal: state.controller?.signal };
        const [accountPayload, bindingPayload, historyPayload, globalPayload] = await Promise.all([
            requestJson('/api/telephony/me', common),
            requestJson(`/api/conversations/${conversationId}/phone-bindings`, common),
            requestJson(`/api/conversations/${conversationId}/phone-calls?limit=100`, {
                method: 'GET',
                signal: historyRequest.signal
            }),
            requestJson('/api/telephony/calls?limit=100', {
                method: 'GET',
                signal: historyRequest.signal
            })
        ]);
        if (!isCurrent(generation, conversationId) || historyRequest.id !== state.historyRequestId) return;
        state.account = accountPayload || {};
        state.binding = bindingPayload.binding || null;
        state.calls = Array.isArray(historyPayload.calls) ? historyPayload.calls : [];
        state.jobs = Array.isArray(globalPayload.jobs) ? globalPayload.jobs : [];
        render();
    }

    async function refreshHistory(options) {
        const quiet = Boolean(options?.quiet);
        const generation = state.generation;
        const conversationId = state.conversationId;
        const historyRequest = beginHistoryRequest();
        if (!quiet) setStatus('Refreshing call status…', 'info');
        const [payload, globalPayload] = await Promise.all([
            requestJson(
                `/api/conversations/${conversationId}/phone-calls?limit=100`,
                { method: 'GET', signal: historyRequest.signal }
            ),
            requestJson('/api/telephony/calls?limit=100', {
                method: 'GET',
                signal: historyRequest.signal
            })
        ]);
        if (!isCurrent(generation, conversationId) || historyRequest.id !== state.historyRequestId) return;
        state.calls = Array.isArray(payload.calls) ? payload.calls : [];
        state.jobs = Array.isArray(globalPayload.jobs) ? globalPayload.jobs : [];
        renderTarget();
        renderCallState();
        schedulePoll();
        if (!quiet) setStatus('', 'info');
    }

    function schedulePoll() {
        if (state.pollTimer) clearTimeout(state.pollTimer);
        state.pollTimer = null;
        if (!state.modalOpen) return;
        let delay = null;
        if (activeCall() || preparingJob()) {
            delay = POLL_INTERVAL_MS;
        } else {
            const scheduled = scheduledJob();
            const dueAt = scheduled?.scheduled_at_utc
                ? new Date(scheduled.scheduled_at_utc).getTime()
                : Number.NaN;
            if (Number.isFinite(dueAt)) {
                delay = Math.max(POLL_INTERVAL_MS, Math.min(60000, dueAt - Date.now()));
            }
        }
        if (delay === null) return;
        state.pollTimer = window.setTimeout(() => {
            refreshHistory({ quiet: true }).catch(showActionError);
        }, delay);
    }

    function newIdempotencyKey(prefix) {
        const random = window.crypto && typeof window.crypto.randomUUID === 'function'
            ? window.crypto.randomUUID()
            : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        return `phone-ui:${prefix}:${state.conversationId}:${random}`.slice(0, 128);
    }

    function dispatchBindingChanged(previousConversationId) {
        window.dispatchEvent(new CustomEvent('aurvek:phone-binding-changed', {
            detail: {
                previousConversationId: previousConversationId || null,
                conversationId: hasCurrentBinding() ? state.conversationId : null,
                binding: state.binding
            }
        }));
    }

    async function ensureSelfBinding() {
        if (hasCurrentBinding()) return state.binding;
        const previousConversationId = accountBindingConversationId();
        const payload = await requestJson(
            `/api/conversations/${state.conversationId}/phone-bindings/self`,
            mutationOptions({ timezone_name: browserTimeZone() })
        );
        state.binding = payload.binding || null;
        state.account = {
            ...(state.account || {}),
            active_binding: state.binding
                ? { id: state.binding.id, conversation_id: state.conversationId }
                : null
        };
        dispatchBindingChanged(previousConversationId);
        renderTarget();
        return state.binding;
    }

    async function assignConversation() {
        const flags = currentConversationFlags();
        if (flags.incognito || flags.locked || state.account?.phone?.eligible !== true) {
            setStatus(
                flags.incognito
                    ? 'Phone calls are unavailable in incognito conversations.'
                    : (flags.locked
                        ? 'This conversation is locked.'
                        : 'Verify your phone number in Settings to receive calls.'),
                'error'
            );
            return;
        }
        if (hasCurrentBinding()) {
            setStatus('This conversation is already used for phone calls.', 'success');
            return;
        }
        const button = byId('phone-conversation-assignment');
        const displacedSchedule = scheduledJob();
        const displacedTitle = displacedSchedule &&
            Number(displacedSchedule.conversation_id) !== Number(state.conversationId)
            ? jobConversationTitle(displacedSchedule)
            : '';
        state.assignmentPending = true;
        button.disabled = true;
        setStatus('Assigning this conversation to phone calls…', 'info');
        try {
            await ensureSelfBinding();
            await refreshHistory({ quiet: true });
            setStatus(
                displacedTitle
                    ? `This conversation will now be used for phone calls. The scheduled call for ${displacedTitle} was canceled.`
                    : 'This conversation will now be used for phone calls.',
                'success'
            );
        } catch (error) {
            showActionError(error);
        } finally {
            state.assignmentPending = false;
            renderTarget();
        }
    }

    async function callNow(event) {
        const flags = currentConversationFlags();
        if (flags.incognito || flags.locked) {
            setStatus(
                flags.incognito
                    ? 'Phone calls are unavailable in incognito conversations.'
                    : 'This conversation is locked. You can only end or remove existing phone activity.',
                'error'
            );
            return;
        }
        const button = event.currentTarget;
        button.disabled = true;
        setStatus('Preparing the call…', 'info');
        try {
            await ensureSelfBinding();
            await requestJson(
                `/api/conversations/${state.conversationId}/phone-calls`,
                mutationOptions({ idempotency_key: newIdempotencyKey('now') })
            );
            await refreshHistory({ quiet: true });
            setStatus('Calling you now.', 'success');
        } catch (error) {
            showActionError(error);
        } finally {
            renderCallState();
        }
    }

    async function scheduleCall(event) {
        event.preventDefault();
        const flags = currentConversationFlags();
        if (flags.incognito || flags.locked) {
            setStatus(
                flags.incognito
                    ? 'Phone calls are unavailable in incognito conversations.'
                    : 'This conversation is locked. You can only end or remove existing phone activity.',
                'error'
            );
            return;
        }
        const form = byId('phone-schedule-form');
        if (!form.reportValidity()) return;
        const submit = byId('phone-schedule-submit');
        submit.disabled = true;
        setStatus('Scheduling your call…', 'info');
        try {
            await ensureSelfBinding();
            await requestJson(
                `/api/conversations/${state.conversationId}/phone-calls`,
                mutationOptions({
                    idempotency_key: newIdempotencyKey('schedule'),
                    scheduled_at: byId('phone-schedule-at').value,
                    timezone_name: browserTimeZone(),
                    fold: 0
                })
            );
            await refreshHistory({ quiet: true });
            setStatus('Call scheduled.', 'success');
        } catch (error) {
            showActionError(error);
        } finally {
            renderCallState();
        }
    }

    async function cancelScheduled() {
        const jobId = byId('phone-cancel-scheduled').dataset.jobId;
        if (!jobId) return;
        setStatus('Canceling the scheduled call…', 'info');
        try {
            await requestJson(
                `/api/phone-call-jobs/${encodeURIComponent(jobId)}/cancel`,
                { method: 'POST', signal: state.controller?.signal }
            );
            await refreshHistory({ quiet: true });
            setStatus('Scheduled call canceled.', 'success');
        } catch (error) {
            showActionError(error);
        }
    }

    async function hangupCall() {
        const callId = byId('phone-hangup').dataset.callId;
        if (!callId) return;
        setStatus('Ending the call…', 'info');
        try {
            const result = await requestJson(
                `/api/phone-calls/${encodeURIComponent(callId)}/hangup`,
                { method: 'POST', signal: state.controller?.signal }
            );
            await refreshHistory({ quiet: true });
            setStatus(result.requested === false ? 'The call is already ending.' : 'Hangup requested.', 'success');
        } catch (error) {
            showActionError(error);
        }
    }

    async function unassignConversation() {
        if (!state.binding) return;
        const previousConversationId = state.conversationId;
        const button = byId('phone-conversation-assignment');
        state.assignmentPending = true;
        button.disabled = true;
        setStatus('Removing this phone assignment…', 'info');
        try {
            await requestJson(
                `/api/conversations/${state.conversationId}/phone-bindings/${state.binding.id}`,
                { method: 'DELETE', signal: state.controller?.signal }
            );
            state.binding = null;
            state.account = { ...(state.account || {}), active_binding: null };
            dispatchBindingChanged(previousConversationId);
            await refreshHistory({ quiet: true });
            renderTarget();
            setStatus('This conversation is no longer used for phone calls.', 'success');
        } catch (error) {
            showActionError(error);
        } finally {
            state.assignmentPending = false;
            renderTarget();
        }
    }

    async function toggleConversationAssignment() {
        if (hasCurrentBinding()) {
            await unassignConversation();
        } else {
            await assignConversation();
        }
    }

    function updateMenuStateFromData() {
        const label = byId('phone-call-menu-state');
        if (!label) return;
        const call = activeCall();
        if (call) {
            label.textContent = friendlyStatus(call.status);
        } else if (preparingJob()) {
            label.textContent = 'Preparing';
        } else if (scheduledJob()) {
            label.textContent = 'Scheduled';
        } else {
            label.textContent = '';
        }
    }

    function syncLauncher() {
        const button = byId('plus-phone-call');
        const label = byId('phone-call-menu-state');
        if (!button) return;
        const conversationId = currentConversationId();
        const flags = currentConversationFlags();
        button.hidden = Boolean(window.admin_view) || flags.incognito;
        button.disabled = !conversationId;
        if (label && (flags.locked || !conversationId)) {
            label.textContent = flags.locked ? 'Locked' : 'Select a chat';
        }
        const invalidModalContext = flags.incognito || conversationId !== state.conversationId;
        const modalElement = byId('phoneCallModal');
        if (invalidModalContext && (state.modalOpen || modalElement?.classList.contains('show'))) {
            invalidateModalSession();
            state.modal?.hide();
        } else if (state.modalOpen && conversationId === state.conversationId) {
            renderTarget();
            renderCallState();
            if (flags.locked) {
                setStatus(
                    'This conversation is locked. You can only end or remove existing phone activity.',
                    'info'
                );
            }
        }
    }

    async function refreshMenuBinding() {
        syncLauncher();
        const button = byId('plus-phone-call');
        const label = byId('phone-call-menu-state');
        const conversationId = currentConversationId();
        if (!button || button.hidden || button.disabled || !conversationId) return;
        if (state.menuController) state.menuController.abort();
        state.menuController = new AbortController();
        try {
            const [payload, globalPayload] = await Promise.all([
                requestJson(
                    `/api/conversations/${conversationId}/phone-calls?limit=20`,
                    { method: 'GET', signal: state.menuController.signal }
                ),
                requestJson('/api/telephony/calls?limit=100', {
                    method: 'GET',
                    signal: state.menuController.signal
                })
            ]);
            if (Number(conversationId) !== Number(currentConversationId())) return;
            const calls = Array.isArray(payload.calls) ? payload.calls : [];
            const jobs = Array.isArray(globalPayload.jobs) ? globalPayload.jobs : [];
            const active = calls.find(call => ACTIVE_CALL_STATUSES.has(String(call.status || '').toLowerCase())) || null;
            const preparing = jobs.some(job => {
                const status = String(job.status || '').toLowerCase();
                const dueAt = jobDueTime(job);
                return status === 'dispatching' ||
                    (status === 'scheduled' && (dueAt === null || dueAt <= Date.now() + 30000));
            });
            const scheduled = jobs.some(job => {
                const dueAt = jobDueTime(job);
                return String(job.status || '').toLowerCase() === 'scheduled' &&
                    dueAt !== null && dueAt > Date.now() + 30000;
            });
            label.textContent = active
                ? friendlyStatus(active.status)
                : (preparing ? 'Preparing' : (scheduled ? 'Scheduled' : ''));
        } catch (error) {
            if (error && error.name !== 'AbortError') label.textContent = '';
        }
    }

    function setScheduleMinimum() {
        const input = byId('phone-schedule-at');
        if (!input) return;
        const date = new Date(Date.now() + 60000);
        const pad = value => String(value).padStart(2, '0');
        input.min = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
    }

    async function openModal() {
        syncLauncher();
        const conversationId = currentConversationId();
        const flags = currentConversationFlags();
        if (!conversationId) return;
        if (flags.incognito) {
            if (typeof window.NotificationModal !== 'undefined') {
                window.NotificationModal.error(
                    'Phone calls',
                    'Phone calls are unavailable in incognito conversations.'
                );
            }
            return;
        }
        abortModalRequests();
        state.generation += 1;
        state.account = null;
        state.binding = null;
        state.calls = [];
        state.jobs = [];
        state.conversationId = conversationId;
        state.controller = new AbortController();
        state.modalOpen = true;
        state.modal = bootstrap.Modal.getOrCreateInstance(byId('phoneCallModal'));
        setStatus('', 'info');
        setScheduleMinimum();
        byId('phone-call-target').textContent = 'Checking your phone number…';
        byId('phone-action-section').hidden = true;
        state.modal.show();
        try {
            await refreshAll();
            if (flags.locked && state.modalOpen) {
                setStatus(
                    'This conversation is locked. You can only end or remove existing phone activity.',
                    'info'
                );
            }
        } catch (error) {
            showActionError(error);
        }
    }

    async function openForAssignment() {
        await openModal();
        if (!state.modalOpen || state.account?.phone?.eligible !== true) return;
        if (currentConversationFlags().locked) {
            setStatus(
                'This conversation is locked. You can only end or remove existing phone activity.',
                'info'
            );
            return;
        }
        if (hasCurrentBinding()) {
            setStatus('This conversation is already used for phone calls.', 'success');
            return;
        }
        await assignConversation();
    }

    function invalidateModalSession() {
        if (state.modalOpen) {
            state.modalOpen = false;
            state.generation += 1;
        }
        abortModalRequests();
    }

    function closeModal() {
        invalidateModalSession();
        refreshMenuBinding();
    }

    async function refresh() {
        await refreshMenuBinding();
        if (state.modalOpen) {
            try {
                await refreshAll();
            } catch (error) {
                showActionError(error);
            }
        }
    }

    function bindEvents() {
        const button = byId('plus-phone-call');
        const modal = byId('phoneCallModal');
        if (!button || !modal) return;
        button.addEventListener('click', openModal);
        modal.addEventListener('shown.bs.modal', syncLauncher);
        modal.addEventListener('hidden.bs.modal', closeModal);
        byId('phone-call-now').addEventListener('click', callNow);
        byId('phone-schedule-form').addEventListener('submit', scheduleCall);
        byId('phone-cancel-scheduled').addEventListener('click', cancelScheduled);
        byId('phone-hangup').addEventListener('click', hangupCall);
        byId('phone-conversation-assignment').addEventListener('click', toggleConversationAssignment);
        window.addEventListener('aurvek:conversation-changed', () => {
            syncLauncher();
            refreshMenuBinding();
        });
        document.addEventListener('click', event => {
            if (event.target.closest('.list-group-item-action, #plus-menu-btn')) {
                window.setTimeout(() => {
                    syncLauncher();
                    if (!state.modalOpen) refreshMenuBinding();
                }, 0);
            }
        }, true);
        refreshMenuBinding();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindEvents);
    } else {
        bindEvents();
    }

    window.AurvekPhoneCall = Object.freeze({
        close: () => state.modal?.hide(),
        open: openModal,
        openForAssignment,
        refresh
    });
})();
