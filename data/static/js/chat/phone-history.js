(function() {
    'use strict';
    const tr = (key, params) => AurvekI18n.t(`chat_widgets.phone_history.${key}`, params);

    const calls = new Map();
    const renderedMarkerIds = new Set();
    let detailModal = null;
    let detailModalElement = null;
    let detailBody = null;
    let detailTitle = null;
    const pendingDeletes = new Set();
    let conversationGeneration = 0;
    const TERMINAL_CALL_STATUSES = new Set([
        'completed', 'busy', 'no_answer', 'machine', 'failed', 'canceled', 'unresolved'
    ]);

    function asText(value, fallback = '—') {
        if (value === null || value === undefined || value === '') return fallback;
        return String(value);
    }

    function localDateTime(value) {
        if (!value) return '—';
        const raw = String(value);
        const parsed = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw) ? raw : `${raw.replace(' ', 'T')}Z`);
        if (Number.isNaN(parsed.getTime())) return raw;
        return parsed.toLocaleString(AurvekI18n.locale);
    }

    function isAdminView() {
        return window.isAdmin === true || window.admin_view === true;
    }

    function technicalLabel(value) {
        return asText(value, tr('unknown')).replaceAll('_', ' ');
    }

    function statusLabel(value) {
        const labels = {
            created: tr('status_preparing'), dispatching: tr('status_preparing'),
            dispatch_unknown: tr('status_checking'), queued: tr('status_preparing'),
            initiated: tr('status_calling'), ringing: tr('status_ringing'),
            in_progress: tr('status_in_call'), completed: tr('status_completed'),
            busy: tr('status_busy'), no_answer: tr('status_no_answer'),
            machine: tr('status_voicemail'), failed: tr('status_failed'),
            canceled: tr('status_canceled'), unresolved: tr('status_unavailable')
        };
        const normalized = String(value || '').toLowerCase();
        return labels[normalized] || technicalLabel(value);
    }

    function directionLabel(value) {
        if (String(value || '').toLowerCase() === 'inbound') return tr('incoming');
        if (String(value || '').toLowerCase() === 'outbound') return tr('outgoing');
        return tr('phone');
    }

    function formatDuration(value) {
        if (value === null || value === undefined || value === '') return '—';
        const total = Math.max(0, Number(value));
        if (!Number.isFinite(total)) return '—';
        const rounded = Math.round(total);
        const minutes = Math.floor(rounded / 60);
        const seconds = rounded % 60;
        if (minutes === 0) return tr('seconds', {count: seconds});
        if (seconds === 0) return tr('minutes', {count: minutes});
        return tr('duration', {minutes, seconds});
    }

    function activeConversationId() {
        const value = window.currentConversationId;
        if (value === null || value === undefined || value === '') return null;
        return String(value);
    }

    function isOriginConversationCurrent(originConversationId, originGeneration) {
        return conversationGeneration === originGeneration &&
            activeConversationId() === originConversationId;
    }

    function appendField(parent, label, value) {
        const item = document.createElement('div');
        item.className = 'phone-history-field';
        const term = document.createElement('dt');
        term.textContent = label;
        const description = document.createElement('dd');
        description.textContent = asText(value);
        item.append(term, description);
        parent.appendChild(item);
    }

    function makeButton(label, className, action) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = className;
        button.textContent = label;
        button.addEventListener('click', action);
        return button;
    }

    function ensureDetailModal() {
        if (detailModalElement) return;

        detailModalElement = document.createElement('div');
        detailModalElement.className = 'modal fade';
        detailModalElement.id = 'phoneHistoryDetailModal';
        detailModalElement.tabIndex = -1;
        detailModalElement.setAttribute('aria-hidden', 'true');
        detailModalElement.setAttribute('aria-labelledby', 'phoneHistoryDetailTitle');

        const dialog = document.createElement('div');
        dialog.className = 'modal-dialog modal-dialog-centered modal-lg modal-dialog-scrollable';
        const content = document.createElement('div');
        content.className = 'modal-content phone-history-detail-content';
        const header = document.createElement('div');
        header.className = 'modal-header';
        detailTitle = document.createElement('h5');
        detailTitle.className = 'modal-title';
        detailTitle.id = 'phoneHistoryDetailTitle';
        detailTitle.textContent = tr('phone_call');
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close';
        close.setAttribute('data-bs-dismiss', 'modal');
        close.setAttribute('aria-label', tr('close_details'));
        detailBody = document.createElement('div');
        detailBody.className = 'modal-body phone-history-detail-body';
        header.append(detailTitle, close);
        content.append(header, detailBody);
        dialog.appendChild(content);
        detailModalElement.appendChild(dialog);
        document.body.appendChild(detailModalElement);
        detailModal = bootstrap.Modal.getOrCreateInstance(detailModalElement);
    }

    function formatCost(value, currency) {
        if (value === null || value === undefined || value === '') return '—';
        const number = Number(value);
        if (!Number.isFinite(number)) return '—';
        try {
            return new Intl.NumberFormat(AurvekI18n.locale, {
                style: 'currency',
                currency: asText(currency, 'USD')
            }).format(number);
        } catch (_error) {
            return `${number.toFixed(4)} ${asText(currency, 'USD')}`;
        }
    }

    function safeRecordingUrl(value) {
        try {
            const url = new URL(String(value), window.location.origin);
            if (url.origin !== window.location.origin) return null;
            if (!url.pathname.startsWith('/api/phone-calls/')) return null;
            if (!url.pathname.endsWith('/recording')) return null;
            return `${url.pathname}${url.search}`;
        } catch (_error) {
            return null;
        }
    }

    function safeServerMessage(response, payload, scope) {
        if (response && [400, 403, 404, 409, 422].includes(response.status)) {
            if (response.status === 404 && scope === 'recording') {
                return tr('audio_unavailable');
            }
            if (response.status === 404) return tr('call_unavailable');
        }
        if (response?.status === 401) return AurvekI18n.t('common.session.expired_message');
        if (response?.status >= 500) {
            return tr('deletion_failed_later');
        }
        return tr('deletion_failed');
    }

    async function deletePhoneData(call, scope, button, status) {
        const callId = String(call?.id || '');
        const requestKey = `${callId}:${scope}`;
        if (!callId || pendingDeletes.has(requestKey)) return;
        const originConversationId = activeConversationId();
        const originGeneration = conversationGeneration;
        if (!window.confirm(tr(scope === 'recording' ? 'delete_audio_confirm' : 'delete_call_confirm'))) return;

        const csrfToken = document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
        if (!csrfToken) {
            status.textContent = tr('deletion_security');
            return;
        }
        pendingDeletes.add(requestKey);
        button.disabled = true;
        status.textContent = tr('deletion_starting');
        const suffix = scope === 'recording' ? '/recording' : '';
        try {
            const fetcher = typeof window.secureFetch === 'function'
                ? window.secureFetch
                : window.fetch.bind(window);
            const response = await fetcher(
                `/api/phone-calls/${encodeURIComponent(callId)}${suffix}`,
                {
                    method: 'DELETE',
                    credentials: 'include',
                    headers: {'X-GPTSub-CSRF': csrfToken}
                }
            );
            if (!response) {
                const error = new Error(AurvekI18n.t('common.session.expired_message'));
                error.code = 'session_expired';
                throw error;
            }
            let payload = null;
            try {
                payload = await response.json();
            } catch (_error) {
                payload = null;
            }
            if (!response.ok) throw new Error(safeServerMessage(response, payload, scope));
            status.textContent = scope === 'recording'
                ? tr('audio_deletion_scheduled') : tr('call_deletion_scheduled');
            if (isOriginConversationCurrent(originConversationId, originGeneration) &&
                typeof window.refreshActiveConversation === 'function') {
                try {
                    await window.refreshActiveConversation();
                } catch (_error) {
                    status.textContent += ' ' + tr('reload_history');
                }
            }
        } catch (error) {
            status.textContent = error instanceof Error
                ? error.message
                : tr('deletion_failed');
            button.disabled = false;
        } finally {
            pendingDeletes.delete(requestKey);
        }
    }

    function renderDeletion(call) {
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = tr('saved_audio_and_call');
        const fields = document.createElement('dl');
        fields.className = 'phone-history-summary';
        const recording = call.recording && typeof call.recording === 'object'
            ? call.recording
            : {};
        appendField(
            fields,
            tr('saved_audio'), recording.present ? tr('available') : tr('not_available')
        );
        if (recording.present && isAdminView()) {
            appendField(fields, tr('recording_status'), technicalLabel(recording.status));
        }

        const purge = call.purge && typeof call.purge === 'object' ? call.purge : null;
        if (purge && isAdminView()) {
            appendField(fields, tr('deletion_status'), technicalLabel(purge.status));
            appendField(fields, tr('deletion_scope'), technicalLabel(purge.scope));
            appendField(fields, tr('deletion_attempt'), Number(purge.attempt) || 0);
            if (purge.status === 'needs_attention') appendField(fields, tr('deletion_note'), tr('deletion_pending_review'));
        }
        section.append(heading, fields);

        const actions = document.createElement('div');
        actions.className = 'd-flex flex-wrap gap-2';
        const status = document.createElement('p');
        status.className = 'phone-call-muted mb-0';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        const deletionBlocked = purge && ['scheduled', 'running', 'needs_attention']
            .includes(String(purge.status));
        if (deletionBlocked) {
            status.textContent = purge.status === 'needs_attention'
                ? tr('deletion_pending_review') : tr('deletion_pending');
        } else {
            if (recording.present && TERMINAL_CALL_STATUSES.has(String(call.status))) {
                actions.appendChild(makeButton(
                    tr('delete_audio'),
                    'btn btn-outline-danger btn-sm',
                    event => deletePhoneData(call, 'recording', event.currentTarget, status)
                ));
            }
            if (TERMINAL_CALL_STATUSES.has(String(call.status))) {
                actions.appendChild(makeButton(
                    tr('delete_call'),
                    'btn btn-danger btn-sm',
                    event => deletePhoneData(call, 'call', event.currentTarget, status)
                ));
            }
        }
        if (actions.childElementCount > 0) section.appendChild(actions);
        section.appendChild(status);
        return section;
    }

    function renderTimeline(call) {
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = tr('timeline');
        const list = document.createElement('ol');
        list.className = 'phone-history-timeline';
        const entries = Array.isArray(call.timeline) ? call.timeline : [];
        entries.forEach(entry => {
            const item = document.createElement('li');
            const event = document.createElement('span');
            event.textContent = technicalLabel(entry?.event);
            const timestamp = document.createElement('time');
            timestamp.dateTime = asText(entry?.at, '');
            timestamp.textContent = localDateTime(entry?.at);
            item.append(event, timestamp);
            list.appendChild(item);
        });
        if (entries.length === 0) {
            const empty = document.createElement('p');
            empty.className = 'phone-call-muted';
            empty.textContent = tr('timeline_empty');
            section.append(heading, empty);
        } else {
            section.append(heading, list);
        }
        return section;
    }

    function renderCosts(call) {
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = tr('cost');
        const summary = document.createElement('p');
        const cost = call.final_cost ?? call.estimated_cost;
        summary.textContent = call.final_cost === null || call.final_cost === undefined
            ? tr('estimated_cost', {cost: formatCost(cost, call.currency)})
            : tr('final_cost', {cost: formatCost(cost, call.currency)});
        section.append(heading, summary);

        const components = Array.isArray(call.cost_components) ? call.cost_components : [];
        if (components.length > 0) {
            const list = document.createElement('ul');
            list.className = 'phone-history-costs';
            components.forEach(component => {
                const item = document.createElement('li');
                const label = [component.provider, component.component_type]
                    .filter(Boolean)
                    .join(' · ');
                item.textContent = tr('cost_item', {label: label || tr('usage'), cost: formatCost(component.customer_charge, component.currency)});
                list.appendChild(item);
            });
            section.appendChild(list);
        }
        return section;
    }

    function countsLabel(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return '—';
        const entries = Object.entries(value);
        if (entries.length === 0) return '—';
        return entries
            .map(([label, count]) => `${technicalLabel(label)}: ${Number(count) || 0}`)
            .join(' · ');
    }

    function renderProvenance(call) {
        const summary = call.provenance_summary || {};
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = tr('message_provenance');
        const fields = document.createElement('dl');
        fields.className = 'phone-history-summary';
        appendField(fields, tr('messages'), Number(summary.total_messages) || 0);
        appendField(fields, tr('phone_messages'), Number(summary.phone_messages) || 0);
        appendField(fields, tr('interrupted'), Number(summary.interrupted_messages) || 0);
        appendField(fields, tr('played_audio'), tr('milliseconds', {count: Number(summary.played_ms) || 0}));
        appendField(fields, tr('delivery'), countsLabel(summary.delivery_states));
        appendField(fields, tr('participants'), countsLabel(summary.participants));
        appendField(fields, tr('origin_channels'), countsLabel(summary.origin_channels));
        const turnIds = Array.isArray(summary.turn_ids) ? summary.turn_ids : [];
        const visibleTurnIds = turnIds.slice(0, 12);
        const turnsLabel = visibleTurnIds.length > 0
            ? `${visibleTurnIds.join(' · ')}${turnIds.length > visibleTurnIds.length
                ? tr('more_turns', {count: turnIds.length - visibleTurnIds.length})
                : ''}`
            : '—';
        appendField(fields, tr('turns'), turnsLabel);
        section.append(heading, fields);
        return section;
    }

    function renderAudio(call) {
        const tracks = Array.isArray(call.audio?.tracks) ? call.audio.tracks : [];
        if (tracks.length === 0) return null;
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = tr('saved_audio');
        section.appendChild(heading);
        tracks.forEach(track => {
            const url = safeRecordingUrl(track?.url);
            if (!url) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'phone-history-audio-track';
            const label = document.createElement('span');
            const trackLabels = {mixed: tr('full_call'), participant: tr('you'), assistant: tr('assistant')};
            label.textContent = trackLabels[String(track.track || '').toLowerCase()] || tr('audio');
            const audio = document.createElement('audio');
            audio.controls = true;
            audio.preload = 'none';
            audio.src = url;
            wrapper.append(label, audio);
            section.appendChild(wrapper);
        });
        return section.childElementCount > 1 ? section : null;
    }

    function showCall(callId) {
        const call = calls.get(String(callId));
        if (!call) return;
        ensureDetailModal();
        const direction = directionLabel(call.direction);
        detailTitle.textContent = tr('direction_call', {direction});
        detailBody.replaceChildren();

        const summary = document.createElement('dl');
        summary.className = 'phone-history-summary';
        appendField(summary, tr('status'), statusLabel(call.status));
        appendField(summary, tr('date'), localDateTime(
            call.answered_at || call.initiated_at || call.created_at
        ));
        appendField(summary, tr('duration_label'), formatDuration(call.duration_seconds));
        appendField(summary, tr('cost'), formatCost(
            call.final_cost ?? call.estimated_cost,
            call.currency
        ));
        if (isAdminView()) {
            appendField(summary, tr('status_code'), technicalLabel(call.status));
            appendField(summary, tr('direction'), technicalLabel(call.direction));
            appendField(summary, tr('answered_by'), technicalLabel(call.answered_by));
            appendField(summary, tr('ended'), localDateTime(call.ended_at));
            if (call.termination_reason) {
                appendField(summary, tr('end_reason'), technicalLabel(call.termination_reason));
            }
            if (call.error_code) {
                appendField(summary, tr('error_code'), technicalLabel(call.error_code));
            }
        }
        detailBody.appendChild(summary);
        const audio = renderAudio(call);
        if (audio) detailBody.appendChild(audio);
        if (isAdminView()) {
            detailBody.append(
                renderTimeline(call),
                renderProvenance(call),
                renderCosts(call)
            );
        }
        detailBody.appendChild(renderDeletion(call));
        detailModal.show();
    }

    function markerAlreadyExists(markerId) {
        if (renderedMarkerIds.has(markerId)) return true;
        return Array.from(document.querySelectorAll('[data-phone-marker-id]'))
            .some(element => element.dataset.phoneMarkerId === markerId);
    }

    function createMarker(marker) {
        const callId = String(marker.phone_call_id || '');
        const call = calls.get(callId);
        const element = document.createElement('div');
        element.className = `phone-history-marker is-${marker.kind === 'end' ? 'end' : 'start'}`;
        element.dataset.phoneMarkerId = String(marker.id);
        element.dataset.phoneCallId = callId;
        element.setAttribute('role', 'group');

        const rule = document.createElement('span');
        rule.className = 'phone-history-marker-rule';
        const label = document.createElement('span');
        label.className = 'phone-history-marker-label';
        const icon = document.createElement('i');
        icon.className = 'fas fa-phone';
        icon.setAttribute('aria-hidden', 'true');
        const text = document.createElement('span');
        const isEnd = marker.kind === 'end';
        text.textContent = tr(isEnd ? 'call_ended' : 'call_started');
        label.append(icon, text);
        if (!marker.transcript_present) {
            const noTranscript = document.createElement('span');
            noTranscript.className = 'phone-history-no-transcript';
            noTranscript.textContent = tr('no_transcript');
            label.appendChild(noTranscript);
        }
        if (marker.occurred_at) {
            const time = document.createElement('time');
            time.dateTime = String(marker.occurred_at);
            time.textContent = localDateTime(marker.occurred_at);
            label.appendChild(time);
        }
        if (!isEnd && call &&
            !(TERMINAL_CALL_STATUSES.has(String(call.status)) && call.ended_at)) {
            const callState = document.createElement('span');
            callState.className = 'phone-history-call-state';
            callState.textContent = statusLabel(call.status);
            label.appendChild(callState);
        }
        if (call) {
            label.appendChild(makeButton(tr('details'), 'phone-history-detail-button', () => showCall(callId)));
        }
        element.append(rule, label, rule.cloneNode());
        return element;
    }

    function renderPage(payload, container) {
        if (!container) return;
        const pageCalls = Array.isArray(payload?.calls) ? payload.calls : [];
        pageCalls.forEach(call => {
            if (call?.id) calls.set(String(call.id), call);
        });

        const markers = Array.isArray(payload?.markers) ? payload.markers : [];
        const afterCursor = new Map();
        markers.forEach(marker => {
            const markerId = String(marker?.id || '');
            if (!markerId || markerAlreadyExists(markerId)) return;
            const node = createMarker(marker);
            const anchorId = Number(marker.anchor_message_id);
            const anchors = Number.isInteger(anchorId) && anchorId > 0
                ? Array.from(container.querySelectorAll(`.message[data-message-id="${anchorId}"]`))
                : [];
            const anchor = marker.placement === 'before'
                ? anchors[0]
                : anchors[anchors.length - 1];
            if (!anchor) {
                container.appendChild(node);
            } else if (marker.placement === 'before') {
                anchor.parentNode.insertBefore(node, anchor);
            } else {
                const cursor = afterCursor.get(anchorId) || anchor;
                cursor.after(node);
                afterCursor.set(anchorId, node);
            }
            renderedMarkerIds.add(markerId);
        });
    }

    function reset() {
        renderedMarkerIds.clear();
        calls.clear();
        detailModal?.hide();
    }

    window.addEventListener('aurvek:conversation-changed', () => {
        conversationGeneration += 1;
    });

    window.AurvekPhoneHistory = Object.freeze({
        renderPage,
        reset,
        showCall
    });
})();
