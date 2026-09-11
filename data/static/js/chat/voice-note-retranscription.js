(function (global) {
    'use strict';

    const ACTIVE_STATUSES = new Set(['queued', 'transcribing', 'comparing']);
    const POLL_INTERVAL_MS = 2500;
    const state = {
        dialog: null,
        nodes: null,
        currentMessageId: null,
        currentRevisionId: null,
        activeByMessage: new Map(),
        pollTimer: null,
        generation: 0,
        modelsPromise: null,
    };

    function createElement(tagName, className, text) {
        const element = document.createElement(tagName);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = String(text);
        return element;
    }

    function csrfToken() {
        return document.querySelector('meta[name="aurvek-csrf-token"]')?.content || '';
    }

    function revisionIdFrom(payload) {
        if (!payload || typeof payload !== 'object') return null;
        const detail = payload.detail && typeof payload.detail === 'object'
            ? payload.detail
            : {};
        const revision = payload.revision && typeof payload.revision === 'object'
            ? payload.revision
            : {};
        const candidates = [
            payload.revision_id,
            payload.active_revision_id,
            revision.id,
            detail.revision_id,
            detail.active_revision_id,
        ];
        for (const candidate of candidates) {
            const parsed = Number(candidate);
            if (Number.isInteger(parsed) && parsed > 0) return parsed;
        }
        return null;
    }

    function normalizeComparisonModels(payload) {
        const rows = Array.isArray(payload)
            ? payload
            : (Array.isArray(payload?.models)
                ? payload.models
                : (Array.isArray(payload?.comparison_models)
                    ? payload.comparison_models
                    : []));
        return rows.flatMap(row => {
            const id = Number(row?.id);
            if (!Number.isInteger(id) || id <= 0) return [];
            const displayName = String(row?.display_name || row?.model || '').trim();
            const machine = String(row?.machine || '').trim();
            const model = String(row?.model || '').trim();
            const identity = [machine, model].filter(Boolean).join(' · ');
            const name = displayName && identity && displayName !== model
                ? `${displayName} · ${identity}`
                : (displayName || identity || AurvekI18n.t('chat_widgets.retranscription.model_name', {id}));
            const inputCost = Number(row?.input_token_cost);
            const outputCost = Number(row?.output_token_cost);
            const hasPricing = Number.isFinite(inputCost) && inputCost >= 0 &&
                Number.isFinite(outputCost) && outputCost >= 0;
            const pricing = hasPricing
                ? AurvekI18n.t('chat_widgets.retranscription.model_pricing', {
                    input: inputCost.toLocaleString(AurvekI18n.locale, {maximumFractionDigits: 6}),
                    output: outputCost.toLocaleString(AurvekI18n.locale, {maximumFractionDigits: 6})
                })
                : '';
            const label = `${name}${pricing}`;
            return [{id, label}];
        });
    }

    function errorMessage(payload, status) {
        const code = String(payload?.error_code || payload?.detail?.error_code || '').toLowerCase();
        if (code === 'storage_quota_exceeded') {
            return AurvekI18n.t('chat_widgets.retranscription.storage_limit');
        }
        if (status === 409) return AurvekI18n.t('chat_widgets.retranscription.already_active');
        return AurvekI18n.t('chat_widgets.error.request_failed');
    }

    async function requestJson(url, options = {}) {
        if (typeof global.secureFetch !== 'function') {
            throw new Error(AurvekI18n.t('chat_widgets.error.security_unavailable'));
        }
        const headers = {...(options.headers || {})};
        const method = String(options.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            headers['X-GPTSub-CSRF'] = csrfToken();
        }
        const response = await global.secureFetch(url, {...options, headers});
        if (!response) {
            const error = new Error(AurvekI18n.t('common.session.expired_message'));
            error.code = 'session_expired';
            throw error;
        }
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            payload = {};
        }
        if (!response.ok) {
            const error = new Error(errorMessage(payload, response.status));
            error.status = response.status;
            error.payload = payload;
            throw error;
        }
        return payload;
    }

    function stopPolling() {
        if (state.pollTimer !== null) {
            global.clearTimeout(state.pollTimer);
            state.pollTimer = null;
        }
        state.generation += 1;
    }

    function dialogIsOpen() {
        return Boolean(
            state.dialog &&
            (state.dialog.open || state.dialog.hasAttribute('open'))
        );
    }

    function setStatus(message, isError = false) {
        if (!state.nodes) return;
        state.nodes.status.setAttribute('role', isError ? 'alert' : 'status');
        state.nodes.status.setAttribute('aria-live', isError ? 'assertive' : 'polite');
        state.nodes.status.classList.toggle('is-error', isError);
        state.nodes.status.textContent = message || '';
    }

    function setReviewVisible(visible) {
        state.nodes.review.hidden = !visible;
        state.nodes.config.hidden = visible;
    }

    function setDecisionBusy(busy) {
        state.nodes.accept.disabled = busy;
        state.nodes.reject.disabled = busy;
    }

    function appendField(parent, labelText, control) {
        const field = createElement('label', 'voice-note-retranscription-field');
        field.append(createElement('span', null, labelText), control);
        parent.appendChild(field);
        return field;
    }

    function createTranscriptDetails(label, className) {
        const details = createElement('details', 'voice-note-transcript-details');
        const summary = createElement('summary', null, label);
        const transcript = createElement('pre', className);
        transcript.tabIndex = 0;
        transcript.setAttribute('role', 'region');
        transcript.setAttribute('aria-label', AurvekI18n.t('chat_widgets.retranscription.transcript_label', {label}));
        details.append(summary, transcript);
        return {details, transcript};
    }

    function ensureDialog() {
        if (state.dialog) return state.dialog;

        const dialog = createElement('dialog', 'voice-note-retranscription-dialog');
        dialog.id = 'voice-note-retranscription-dialog';
        dialog.setAttribute('aria-labelledby', 'voice-note-retranscription-title');
        dialog.setAttribute(
            'aria-describedby',
            'voice-note-retranscription-description voice-note-retranscription-cost-notice'
        );

        const shell = createElement('div', 'voice-note-retranscription-shell');
        const header = createElement('header', 'voice-note-retranscription-header');
        const title = createElement('h2', null, AurvekI18n.t('chat_widgets.retranscription.title'));
        title.id = 'voice-note-retranscription-title';
        const close = createElement('button', 'voice-note-retranscription-close', '×');
        close.type = 'button';
        close.setAttribute('aria-label', AurvekI18n.t('chat_widgets.retranscription.close'));
        header.append(title, close);

        const description = createElement(
            'p',
            'voice-note-retranscription-description',
            AurvekI18n.t('chat_widgets.retranscription.description')
        );
        description.id = 'voice-note-retranscription-description';
        const costNotice = createElement(
            'p',
            'voice-note-retranscription-cost-notice',
            AurvekI18n.t('chat_widgets.retranscription.cost_notice')
        );
        costNotice.id = 'voice-note-retranscription-cost-notice';

        const form = createElement('form', 'voice-note-retranscription-config');
        const stt = createElement('select');
        stt.name = 'stt_engine';
        stt.append(
            new Option(AurvekI18n.t('chat_widgets.retranscription.engine_configured'), 'configured'),
            new Option('Deepgram', 'deepgram'),
            new Option('ElevenLabs', 'elevenlabs')
        );
        appendField(form, AurvekI18n.t('chat_widgets.retranscription.engine_label'), stt);

        const model = createElement('select');
        model.name = 'comparison_llm_id';
        model.disabled = true;
        model.appendChild(new Option(AurvekI18n.t('chat_widgets.retranscription.no_comparison'), ''));
        const modelField = appendField(form, AurvekI18n.t('chat_widgets.retranscription.judge_label'), model);
        const modelHelp = createElement(
            'small',
            'voice-note-retranscription-model-help',
            AurvekI18n.t('chat_widgets.retranscription.models_loading')
        );
        modelField.appendChild(modelHelp);

        const start = createElement('button', 'voice-note-retranscription-primary', AurvekI18n.t('chat_widgets.retranscription.start'));
        start.type = 'submit';
        form.appendChild(start);

        const status = createElement('p', 'voice-note-retranscription-status');
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');

        const review = createElement('section', 'voice-note-retranscription-review');
        review.hidden = true;
        const verdict = createElement('h3', 'voice-note-retranscription-verdict');
        const rationale = createElement('p', 'voice-note-retranscription-rationale');
        const oldTranscript = createTranscriptDetails(
            AurvekI18n.t('chat_widgets.retranscription.previous'),
            'voice-note-transcript-old'
        );
        const newTranscript = createTranscriptDetails(
            AurvekI18n.t('chat_widgets.retranscription.new'),
            'voice-note-transcript-new'
        );
        const decisions = createElement('div', 'voice-note-retranscription-decisions');
        const reject = createElement('button', 'voice-note-retranscription-secondary', AurvekI18n.t('chat_widgets.retranscription.keep_previous'));
        reject.type = 'button';
        const accept = createElement('button', 'voice-note-retranscription-primary', AurvekI18n.t('chat_widgets.retranscription.use_new'));
        accept.type = 'button';
        decisions.append(reject, accept);
        review.append(
            verdict,
            rationale,
            oldTranscript.details,
            newTranscript.details,
            decisions
        );

        shell.append(header, description, costNotice, form, status, review);
        dialog.appendChild(shell);
        document.body.appendChild(dialog);

        state.dialog = dialog;
        state.nodes = {
            form,
            config: form,
            stt,
            model,
            modelHelp,
            start,
            status,
            review,
            verdict,
            rationale,
            oldTranscript: oldTranscript.transcript,
            newTranscript: newTranscript.transcript,
            reject,
            accept,
        };

        close.addEventListener('click', closeDialog);
        dialog.addEventListener('cancel', event => {
            event.preventDefault();
            closeDialog();
        });
        dialog.addEventListener('close', stopPolling);
        form.addEventListener('submit', beginRetranscription);
        reject.addEventListener('click', () => decide('reject'));
        accept.addEventListener('click', () => decide('accept'));
        return dialog;
    }

    function closeDialog() {
        if (!state.dialog) return;
        if (typeof state.dialog.close === 'function') {
            state.dialog.close();
        } else {
            state.dialog.removeAttribute('open');
            stopPolling();
        }
    }

    function resetDialog() {
        const nodes = state.nodes;
        nodes.form.reset();
        nodes.start.disabled = true;
        nodes.verdict.textContent = '';
        nodes.rationale.textContent = '';
        nodes.oldTranscript.textContent = '';
        nodes.newTranscript.textContent = '';
        setDecisionBusy(false);
        setReviewVisible(false);
        setStatus(AurvekI18n.t('chat_widgets.retranscription.checking_saved'));
        state.currentRevisionId = null;
    }

    function renderComparisonModels(models) {
        const noComparison = new Option(AurvekI18n.t('chat_widgets.retranscription.no_comparison'), '');
        const options = models.map(item => new Option(item.label, String(item.id)));
        state.nodes.model.replaceChildren(noComparison, ...options);
        state.nodes.model.disabled = false;
        state.nodes.modelHelp.textContent = models.length
            ? AurvekI18n.t('chat_widgets.retranscription.model_prices')
            : AurvekI18n.t('chat_widgets.retranscription.no_models');
    }

    async function loadComparisonModels() {
        if (!state.modelsPromise) {
            state.modelsPromise = requestJson('/api/messaging-voice-notes/comparison-models')
                .then(normalizeComparisonModels)
                .catch(error => {
                    state.modelsPromise = null;
                    throw error;
                });
        }
        const models = await state.modelsPromise;
        renderComparisonModels(models);
    }

    function statusLabel(status) {
        return {
            queued: AurvekI18n.t('chat_widgets.retranscription.status_queued'),
            transcribing: AurvekI18n.t('chat_widgets.retranscription.status_transcribing'),
            comparing: AurvekI18n.t('chat_widgets.retranscription.status_comparing'),
            ready: AurvekI18n.t('chat_widgets.retranscription.status_ready'),
            accepted: AurvekI18n.t('chat_widgets.retranscription.status_accepted'),
            rejected: AurvekI18n.t('chat_widgets.retranscription.status_rejected'),
            failed: AurvekI18n.t('chat_widgets.retranscription.status_failed'),
            stale: AurvekI18n.t('chat_widgets.retranscription.status_stale'),
        }[status] || AurvekI18n.t('chat_widgets.retranscription.status_unknown');
    }

    function failedRevisionMessage(revision) {
        const code = String(revision?.error_code || '').toLowerCase();
        if (code === 'storage_quota_exceeded') {
            return AurvekI18n.t('chat_widgets.retranscription.storage_limit');
        }
        return statusLabel('failed');
    }

    function verdictLabel(verdict) {
        const normalized = String(verdict || '').toLowerCase();
        const key = {
            better: 'verdict_better',
            equal: 'verdict_equal',
            worse: 'verdict_worse',
            uncertain: 'verdict_uncertain',
        }[normalized];
        return key
            ? AurvekI18n.t(`chat_widgets.retranscription.${key}`)
            : AurvekI18n.t('chat_widgets.retranscription.not_compared');
    }

    function reviewRationale(revision) {
        if (revision?.rationale_source !== 'aurvek') {
            return revision?.rationale || AurvekI18n.t('chat_widgets.retranscription.no_comparison_requested');
        }
        const key = {
            comparison_incomplete: 'comparison_incomplete',
            comparison_no_rationale: 'comparison_no_rationale',
        }[revision.rationale_code] || 'comparison_failed';
        const message = AurvekI18n.t(`chat_widgets.retranscription.${key}`);
        return revision.generated_rationale ? `${message}\n\n${revision.generated_rationale}` : message;
    }

    function showReview(revision) {
        state.currentRevisionId = Number(revision.id) || state.currentRevisionId;
        const verdict = verdictLabel(revision.verdict);
        const hasConfidence = revision.confidence !== null &&
            revision.confidence !== undefined &&
            revision.confidence !== '';
        const confidence = hasConfidence ? Number(revision.confidence) : Number.NaN;
        const confidenceLabel = Number.isFinite(confidence)
            ? AurvekI18n.t('chat_widgets.retranscription.confidence', {percent: Math.round(Math.max(0, Math.min(confidence, 1)) * 100)})
            : '';
        state.nodes.verdict.textContent = AurvekI18n.t('chat_widgets.retranscription.verdict', {verdict, confidence: confidenceLabel});
        state.nodes.rationale.textContent = reviewRationale(revision);
        state.nodes.oldTranscript.textContent = revision.old_transcript || '';
        state.nodes.newTranscript.textContent = revision.new_transcript || '';
        setReviewVisible(true);
        setDecisionBusy(false);
        setStatus(statusLabel('ready'));
    }

    function schedulePoll(revisionId, generation) {
        if (generation !== state.generation || !dialogIsOpen()) return;
        state.pollTimer = global.setTimeout(
            () => pollRevision(revisionId, generation),
            POLL_INTERVAL_MS
        );
    }

    async function pollRevision(revisionId, generation = state.generation) {
        if (generation !== state.generation || !dialogIsOpen()) return;
        try {
            const payload = await requestJson(
                `/api/messaging-voice-notes/revisions/${encodeURIComponent(revisionId)}`
            );
            if (generation !== state.generation || !dialogIsOpen()) return;
            const revision = payload?.revision && typeof payload.revision === 'object'
                ? payload.revision
                : payload;
            const status = String(revision?.status || '').toLowerCase();
            setStatus(statusLabel(status));
            if (ACTIVE_STATUSES.has(status)) {
                schedulePoll(revisionId, generation);
                return;
            }

            state.activeByMessage.delete(state.currentMessageId);
            if (status === 'ready') {
                showReview({...revision, id: revision.id || revisionId});
            } else if (status === 'failed') {
                setReviewVisible(false);
                state.nodes.start.disabled = false;
                setStatus(failedRevisionMessage(revision), true);
            } else if (status === 'accepted') {
                if (typeof global.refreshActiveConversation === 'function') {
                    await global.refreshActiveConversation();
                }
            } else if (['rejected', 'stale'].includes(status)) {
                setDecisionBusy(true);
            } else {
                state.nodes.start.disabled = false;
            }
        } catch (error) {
            if (generation !== state.generation) return;
            setStatus(error.message, true);
            if (!error.status || error.status >= 500) {
                schedulePoll(revisionId, generation);
            } else {
                setReviewVisible(false);
                state.nodes.start.disabled = false;
            }
        }
    }

    async function beginRetranscription(event) {
        event.preventDefault();
        if (!state.currentMessageId || state.nodes.start.disabled) return;
        const messageId = state.currentMessageId;
        const generation = state.generation;
        const nodes = state.nodes;
        nodes.start.disabled = true;
        setStatus(AurvekI18n.t('chat_widgets.retranscription.starting'));
        const modelId = Number(nodes.model.value);
        const body = {
            stt_engine: nodes.stt.value,
            comparison_llm_id: Number.isInteger(modelId) && modelId > 0 ? modelId : null,
        };

        try {
            const payload = await requestJson(
                `/api/messaging-voice-notes/${encodeURIComponent(messageId)}/retranscribe`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(body),
                }
            );
            const revisionId = revisionIdFrom(payload);
            if (!revisionId) throw new Error(AurvekI18n.t('chat_widgets.retranscription.missing_revision'));
            state.activeByMessage.set(messageId, revisionId);
            if (generation !== state.generation || state.currentMessageId !== messageId) return;
            state.currentRevisionId = revisionId;
            nodes.config.hidden = true;
            setStatus(AurvekI18n.t('chat_widgets.retranscription.queued'));
            await pollRevision(revisionId, generation);
        } catch (error) {
            const activeRevisionId = error.status === 409
                ? revisionIdFrom(error.payload)
                : null;
            if (activeRevisionId) {
                state.activeByMessage.set(messageId, activeRevisionId);
                if (generation !== state.generation || state.currentMessageId !== messageId) return;
                state.currentRevisionId = activeRevisionId;
                nodes.config.hidden = true;
                setStatus(AurvekI18n.t('chat_widgets.retranscription.continuing'));
                await pollRevision(activeRevisionId, generation);
                return;
            }
            if (generation !== state.generation || state.currentMessageId !== messageId) return;
            nodes.start.disabled = false;
            setStatus(error.message, true);
        }
    }

    async function decide(decision) {
        const revisionId = state.currentRevisionId;
        const messageId = state.currentMessageId;
        const generation = state.generation;
        if (!revisionId || !['accept', 'reject'].includes(decision)) return;
        if (
            decision === 'accept' &&
            typeof global.confirm === 'function' &&
            !global.confirm(
                AurvekI18n.t('chat_widgets.retranscription.replace_confirm')
            )
        ) {
            return;
        }
        setDecisionBusy(true);
        setStatus(AurvekI18n.t(decision === 'accept' ? 'chat_widgets.retranscription.applying' : 'chat_widgets.retranscription.keeping'));
        try {
            await requestJson(
                `/api/messaging-voice-notes/revisions/${encodeURIComponent(revisionId)}/decision`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({decision}),
                }
            );
            state.activeByMessage.delete(messageId);
            if (generation !== state.generation || state.currentMessageId !== messageId) {
                if (decision === 'accept' && typeof global.refreshActiveConversation === 'function') {
                    await global.refreshActiveConversation();
                }
                return;
            }
            state.nodes.review.hidden = true;
            setStatus(statusLabel(decision === 'accept' ? 'accepted' : 'rejected'));
            if (decision === 'accept' && typeof global.refreshActiveConversation === 'function') {
                await global.refreshActiveConversation();
            }
        } catch (error) {
            if (generation !== state.generation || state.currentMessageId !== messageId) return;
            setDecisionBusy(false);
            setStatus(error.message, true);
            if (error.status === 409) {
                await pollRevision(revisionId, generation);
            }
        }
    }

    async function resumeLatestRevision(messageId, generation) {
        try {
            const payload = await requestJson(
                `/api/messaging-voice-notes/${encodeURIComponent(messageId)}`
            );
            if (generation !== state.generation || state.currentMessageId !== messageId) return;
            const voiceNote = payload?.voice_note && typeof payload.voice_note === 'object'
                ? payload.voice_note
                : payload;
            if (voiceNote?.audio_available === false || Number(voiceNote?.audio_available) === 0) {
                state.nodes.start.disabled = true;
                setStatus(AurvekI18n.t('chat_widgets.retranscription.audio_unavailable'), true);
                return;
            }
            const revisionId = Number(voiceNote?.latest_revision_id);
            const status = String(voiceNote?.latest_revision_status || '').toLowerCase();
            if (!Number.isInteger(revisionId) || revisionId <= 0) {
                state.nodes.start.disabled = false;
                setStatus(AurvekI18n.t('chat_widgets.retranscription.choose_candidate'));
                state.nodes.stt.focus();
                return;
            }
            if (ACTIVE_STATUSES.has(status) || status === 'ready') {
                state.currentRevisionId = revisionId;
                if (ACTIVE_STATUSES.has(status)) {
                    state.activeByMessage.set(messageId, revisionId);
                }
                state.nodes.config.hidden = true;
                setStatus(
                    status === 'ready'
                        ? AurvekI18n.t('chat_widgets.retranscription.loading_candidate')
                        : AurvekI18n.t('chat_widgets.retranscription.continuing')
                );
                await pollRevision(revisionId, generation);
                return;
            }
            state.nodes.start.disabled = false;
            setStatus(AurvekI18n.t('chat_widgets.retranscription.choose_another'));
            state.nodes.stt.focus();
        } catch (error) {
            if (generation !== state.generation || state.currentMessageId !== messageId) return;
            state.nodes.start.disabled = true;
            if (!error.status || error.status >= 500) {
                setStatus(AurvekI18n.t('chat_widgets.retranscription.check_failed_retry'), true);
                state.pollTimer = global.setTimeout(
                    () => resumeLatestRevision(messageId, generation),
                    POLL_INTERVAL_MS
                );
            } else {
                setStatus(error.message, true);
            }
        }
    }

    function open(messageId) {
        const parsedMessageId = Number(messageId);
        if (!Number.isInteger(parsedMessageId) || parsedMessageId <= 0) return;
        const dialog = ensureDialog();
        stopPolling();
        state.currentMessageId = parsedMessageId;
        resetDialog();

        if (typeof dialog.showModal === 'function') {
            if (!dialog.open) dialog.showModal();
        } else {
            dialog.setAttribute('open', '');
        }

        loadComparisonModels().catch(() => {
            state.nodes.model.disabled = true;
            state.nodes.modelHelp.textContent =
                AurvekI18n.t('chat_widgets.retranscription.models_unavailable');
        });

        const activeRevisionId = state.activeByMessage.get(parsedMessageId);
        if (activeRevisionId) {
            state.currentRevisionId = activeRevisionId;
            state.nodes.config.hidden = true;
            setStatus(AurvekI18n.t('chat_widgets.retranscription.checking_active'));
            pollRevision(activeRevisionId, state.generation);
        } else {
            resumeLatestRevision(parsedMessageId, state.generation);
        }
    }

    global.AurvekVoiceNoteRetranscription = {open};
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = {
            failedRevisionMessage,
            normalizeComparisonModels,
            reviewRationale,
            revisionIdFrom,
            verdictLabel,
        };
    }
})(typeof window !== 'undefined' ? window : globalThis);
