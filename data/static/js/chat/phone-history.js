(function() {
    'use strict';

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
        return parsed.toLocaleString();
    }

    function isAdminView() {
        return window.isAdmin === true || window.admin_view === true;
    }

    function technicalLabel(value) {
        return asText(value, 'unknown').replaceAll('_', ' ');
    }

    function statusLabel(value) {
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
        const normalized = String(value || '').toLowerCase();
        return labels[normalized] || technicalLabel(value);
    }

    function directionLabel(value) {
        if (String(value || '').toLowerCase() === 'inbound') return 'Incoming';
        if (String(value || '').toLowerCase() === 'outbound') return 'Outgoing';
        return 'Phone';
    }

    function formatDuration(value) {
        if (value === null || value === undefined || value === '') return '—';
        const total = Math.max(0, Number(value));
        if (!Number.isFinite(total)) return '—';
        const rounded = Math.round(total);
        const minutes = Math.floor(rounded / 60);
        const seconds = rounded % 60;
        if (minutes === 0) return `${seconds} sec`;
        if (seconds === 0) return `${minutes} min`;
        return `${minutes} min ${seconds} sec`;
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
        detailTitle.textContent = 'Phone call';
        const close = document.createElement('button');
        close.type = 'button';
        close.className = 'btn-close';
        close.setAttribute('data-bs-dismiss', 'modal');
        close.setAttribute('aria-label', 'Close phone call details');
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
            return new Intl.NumberFormat(undefined, {
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
            const detail = payload && typeof payload.detail === 'string'
                ? payload.detail.trim()
                : '';
            if (detail && isAdminView()) return detail.slice(0, 300);
            if (response.status === 404 && scope === 'recording') {
                return 'Saved audio is no longer available.';
            }
            if (response.status === 404) return 'This call is no longer available.';
        }
        if (response?.status === 401) return 'Your session has expired.';
        if (response?.status >= 500) {
            return 'The deletion could not be started. Please try again later.';
        }
        return 'The deletion could not be started.';
    }

    async function deletePhoneData(call, scope, button, status) {
        const callId = String(call?.id || '');
        const requestKey = `${callId}:${scope}`;
        if (!callId || pendingDeletes.has(requestKey)) return;
        const originConversationId = activeConversationId();
        const originGeneration = conversationGeneration;
        const noun = scope === 'recording' ? 'saved audio' : 'phone call and its data';
        if (!window.confirm(`Delete this ${noun}? This action cannot be undone.`)) return;

        const csrfToken = document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
        if (!csrfToken) {
            status.textContent = 'Deletion security is unavailable. Reload the page and try again.';
            return;
        }
        pendingDeletes.add(requestKey);
        button.disabled = true;
        status.textContent = 'Starting deletion…';
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
            if (!response) throw new Error('Your session is no longer available.');
            let payload = null;
            try {
                payload = await response.json();
            } catch (_error) {
                payload = null;
            }
            if (!response.ok) throw new Error(safeServerMessage(response, payload, scope));
            status.textContent = scope === 'recording'
                ? 'Audio deletion scheduled.'
                : 'Call deletion scheduled.';
            if (isOriginConversationCurrent(originConversationId, originGeneration) &&
                typeof window.refreshActiveConversation === 'function') {
                try {
                    await window.refreshActiveConversation();
                } catch (_error) {
                    status.textContent += ' Reload the conversation to update the history.';
                }
            }
        } catch (error) {
            status.textContent = error instanceof Error
                ? error.message
                : 'The deletion could not be started.';
            button.disabled = false;
        } finally {
            pendingDeletes.delete(requestKey);
        }
    }

    function renderDeletion(call) {
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = 'Saved audio and call';
        const fields = document.createElement('dl');
        fields.className = 'phone-history-summary';
        const recording = call.recording && typeof call.recording === 'object'
            ? call.recording
            : {};
        appendField(
            fields,
            'Saved audio',
            recording.present ? 'Available' : 'Not available'
        );
        if (recording.present && isAdminView()) {
            appendField(fields, 'Recording status', technicalLabel(recording.status));
        }

        const purge = call.purge && typeof call.purge === 'object' ? call.purge : null;
        if (purge && isAdminView()) {
            appendField(fields, 'Deletion status', technicalLabel(purge.status));
            appendField(fields, 'Deletion scope', technicalLabel(purge.scope));
            appendField(fields, 'Deletion attempt', Number(purge.attempt) || 0);
            if (purge.error) appendField(fields, 'Deletion note', purge.error);
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
                ? 'Deletion is pending review.'
                : 'Deletion is pending.';
        } else {
            if (recording.present && TERMINAL_CALL_STATUSES.has(String(call.status))) {
                actions.appendChild(makeButton(
                    'Delete audio',
                    'btn btn-outline-danger btn-sm',
                    event => deletePhoneData(call, 'recording', event.currentTarget, status)
                ));
            }
            if (TERMINAL_CALL_STATUSES.has(String(call.status))) {
                actions.appendChild(makeButton(
                    'Delete call',
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
        heading.textContent = 'Timeline';
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
            empty.textContent = 'No timeline events are available.';
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
        heading.textContent = 'Cost';
        const summary = document.createElement('p');
        const cost = call.final_cost ?? call.estimated_cost;
        summary.textContent = call.final_cost === null || call.final_cost === undefined
            ? `Estimated: ${formatCost(cost, call.currency)}`
            : `Final: ${formatCost(cost, call.currency)}`;
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
                item.textContent = `${label || 'Usage'}: ${formatCost(component.customer_charge, component.currency)}`;
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
        heading.textContent = 'Message provenance';
        const fields = document.createElement('dl');
        fields.className = 'phone-history-summary';
        appendField(fields, 'Messages', Number(summary.total_messages) || 0);
        appendField(fields, 'Phone messages', Number(summary.phone_messages) || 0);
        appendField(fields, 'Interrupted', Number(summary.interrupted_messages) || 0);
        appendField(fields, 'Played audio', `${Number(summary.played_ms) || 0} ms`);
        appendField(fields, 'Delivery', countsLabel(summary.delivery_states));
        appendField(fields, 'Participants', countsLabel(summary.participants));
        appendField(fields, 'Origin channels', countsLabel(summary.origin_channels));
        const turnIds = Array.isArray(summary.turn_ids) ? summary.turn_ids : [];
        const visibleTurnIds = turnIds.slice(0, 12);
        const turnsLabel = visibleTurnIds.length > 0
            ? `${visibleTurnIds.join(' · ')}${turnIds.length > visibleTurnIds.length
                ? ` · +${turnIds.length - visibleTurnIds.length} more`
                : ''}`
            : '—';
        appendField(fields, 'Turns', turnsLabel);
        section.append(heading, fields);
        return section;
    }

    function renderAudio(call) {
        const tracks = Array.isArray(call.audio?.tracks) ? call.audio.tracks : [];
        if (tracks.length === 0) return null;
        const section = document.createElement('section');
        section.className = 'phone-history-detail-section';
        const heading = document.createElement('h6');
        heading.textContent = 'Saved audio';
        section.appendChild(heading);
        tracks.forEach(track => {
            const url = safeRecordingUrl(track?.url);
            if (!url) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'phone-history-audio-track';
            const label = document.createElement('span');
            const trackLabels = {mixed: 'Full call', participant: 'You', assistant: 'Assistant'};
            label.textContent = trackLabels[String(track.track || '').toLowerCase()] || 'Audio';
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
        detailTitle.textContent = direction === 'Phone' ? 'Phone call' : `${direction} call`;
        detailBody.replaceChildren();

        const summary = document.createElement('dl');
        summary.className = 'phone-history-summary';
        appendField(summary, 'Status', statusLabel(call.status));
        appendField(summary, 'Date', localDateTime(
            call.answered_at || call.initiated_at || call.created_at
        ));
        appendField(summary, 'Duration', formatDuration(call.duration_seconds));
        appendField(summary, 'Cost', formatCost(
            call.final_cost ?? call.estimated_cost,
            call.currency
        ));
        if (isAdminView()) {
            appendField(summary, 'Status code', technicalLabel(call.status));
            appendField(summary, 'Direction', technicalLabel(call.direction));
            appendField(summary, 'Answered by', technicalLabel(call.answered_by));
            appendField(summary, 'Ended', localDateTime(call.ended_at));
            if (call.termination_reason) {
                appendField(summary, 'End reason', technicalLabel(call.termination_reason));
            }
            if (call.error_code) {
                appendField(summary, 'Error code', technicalLabel(call.error_code));
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
        text.textContent = isEnd ? 'Phone call ended' : 'Phone call started';
        label.append(icon, text);
        if (!marker.transcript_present) {
            const noTranscript = document.createElement('span');
            noTranscript.className = 'phone-history-no-transcript';
            noTranscript.textContent = 'No transcript';
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
            label.appendChild(makeButton('Details', 'phone-history-detail-button', () => showCall(callId)));
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
