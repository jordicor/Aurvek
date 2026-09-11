/**
 * Settings page - Tab management, URL hash deep-linking, lazy initialization
 */
(function() {
    'use strict';

    const t = (key, params = {}) => window.AurvekI18n.t(`profile.${key}`, params);

    const TAB_MAP = {
        '#profile': 'profile-tab',
        '#calls': 'calls-tab',
        '#usage': 'usage-tab',
        '#wellbeing': 'wellbeing-tab',
        '#memory': 'memory-tab',
        '#api-keys': 'api-keys-tab'
    };

    const initialized = {
        profile: false,
        calls: false,
        usage: false,
        wellbeing: false,
        memory: false,
        'api-keys': false
    };

    let chartJsLoaded = false;

    // Activate tab from URL hash
    function activateFromHash() {
        const hash = window.location.hash || '#profile';
        const tabId = TAB_MAP[hash];
        if (tabId) {
            const tabEl = document.getElementById(tabId);
            if (tabEl) {
                const tab = new bootstrap.Tab(tabEl);
                tab.show();
            }
        }
    }

    // Update URL hash on tab change
    function setupHashSync() {
        const tabEls = document.querySelectorAll('#settingsTabs button[data-bs-toggle="tab"]');
        tabEls.forEach(tabEl => {
            tabEl.addEventListener('shown.bs.tab', function(event) {
                const target = event.target.getAttribute('data-bs-target');
                history.replaceState(null, '', target);
                initTab(target.replace('#', ''));
            });
        });
    }

    // Lazy init each tab on first view
    function initTab(tabName) {
        if (initialized[tabName]) return;
        initialized[tabName] = true;

        switch(tabName) {
            case 'profile':
                // edit_profile.js initializes on DOMContentLoaded, already fired
                break;
            case 'calls':
                loadCallsTab();
                break;
            case 'usage':
                loadUsageTab();
                break;
            case 'wellbeing':
                loadWellbeingTab();
                break;
            case 'memory':
                break;
            case 'api-keys':
                // api-credentials.js initializes on DOMContentLoaded, already fired
                // But if it hasn't been visible yet, we may need to trigger it
                break;
        }
    }

    // --- Calls Tab ---
    let callsLoadController = null;
    let callsLoadGeneration = 0;

    function phoneCallStatus(value) {
        const labels = {
            created: t('calls.status.preparing'),
            dispatching: t('calls.status.preparing'),
            dispatch_unknown: t('calls.status.checking'),
            queued: t('calls.status.preparing'),
            initiated: t('calls.status.calling'),
            ringing: t('calls.status.ringing'),
            in_progress: t('calls.status.in_call'),
            completed: t('calls.status.completed'),
            busy: t('calls.status.busy'),
            no_answer: t('calls.status.no_answer'),
            machine: t('calls.status.voicemail'),
            failed: t('calls.status.failed'),
            canceled: t('calls.status.canceled'),
            unresolved: t('calls.status.unavailable')
        };
        return labels[String(value || '').toLowerCase()] || t('calls.status.unknown');
    }

    function phoneCallDirection(value) {
        if (String(value || '').toLowerCase() === 'inbound') return t('calls.direction.incoming');
        if (String(value || '').toLowerCase() === 'outbound') return t('calls.direction.outgoing');
        return t('calls.direction.phone');
    }

    function phoneCallDate(value) {
        if (!value) return '';
        const raw = String(value);
        const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(raw)
            ? raw
            : `${raw.replace(' ', 'T')}Z`);
        return Number.isNaN(date.getTime()) ? raw : date.toLocaleString(window.AurvekI18n.locale);
    }

    function phoneCallDuration(value) {
        if (value === null || value === undefined || value === '') return '';
        const total = Number(value);
        if (!Number.isFinite(total) || total < 0) return '';
        const rounded = Math.round(total);
        const minutes = Math.floor(rounded / 60);
        const seconds = rounded % 60;
        if (minutes === 0) return t('calls.seconds', { count: seconds });
        if (seconds === 0) return t('calls.minutes', { count: minutes });
        return t('calls.duration', { minutes, seconds });
    }

    function phoneCallCost(call) {
        const value = call.total_cost ?? call.customer_cost ?? call.final_cost ?? call.estimated_cost;
        if (value === null || value === undefined || value === '') return '';
        const amount = Number(value);
        if (!Number.isFinite(amount)) return '';
        const currency = String(call.currency || 'USD');
        try {
            return new Intl.NumberFormat(window.AurvekI18n.locale, {
                style: 'currency',
                currency
            }).format(amount);
        } catch (_error) {
            return `${amount.toFixed(4)} ${currency}`;
        }
    }

    function callConversationTitle(call) {
        return call.conversation_title || call.conversation_name || call.chat_name || call.prompt_name ||
            (call.conversation_id
                ? t('calls.conversation_number', { id: call.conversation_id })
                : t('calls.conversation'));
    }

    function openCallConversation(conversationId) {
        if (!conversationId) return;
        window.location.href = `/chat?conversation_id=${encodeURIComponent(conversationId)}`;
    }

    async function cancelScheduledCall(job, button) {
        const status = document.getElementById('settings-calls-status');
        const csrfToken = document.querySelector('meta[name="aurvek-csrf-token"]')?.content;
        if (!csrfToken) {
            status.hidden = false;
            status.textContent = t('calls.cancel_security_error');
            return;
        }
        button.disabled = true;
        try {
            const fetcher = typeof secureFetch === 'function' ? secureFetch : fetch;
            const response = await fetcher(
                `/api/phone-call-jobs/${encodeURIComponent(job.id)}/cancel`,
                {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'X-GPTSub-CSRF': csrfToken}
                }
            );
            if (!response || !response.ok) throw new Error(t('calls.cancel_failed'));
            await loadCallsTab();
        } catch (error) {
            status.hidden = false;
            status.textContent = t('calls.cancel_failed');
            button.disabled = false;
        }
    }

    function renderCalls(calls, jobs) {
        const list = document.getElementById('settings-calls-list');
        const status = document.getElementById('settings-calls-status');
        if (!list || !status) return;
        list.replaceChildren();
        const scheduled = jobs.filter(job => {
            const dueAt = job.scheduled_at_utc ? new Date(job.scheduled_at_utc).getTime() : Number.NaN;
            return String(job.status || '').toLowerCase() === 'scheduled' &&
                !job.call_id && Number.isFinite(dueAt) && dueAt > Date.now();
        });
        if (calls.length === 0 && scheduled.length === 0) {
            status.textContent = t('calls.empty');
            status.hidden = false;
            return;
        }
        status.textContent = '';
        status.hidden = true;

        scheduled.forEach(job => {
            const item = document.createElement('article');
            item.className = 'settings-call-item';
            const main = document.createElement('div');
            main.className = 'settings-call-main';
            const title = document.createElement('p');
            title.className = 'settings-call-title';
            title.textContent = t('calls.scheduled_title', { conversation: callConversationTitle(job) });
            const meta = document.createElement('p');
            meta.className = 'settings-call-meta';
            const prompt = job.prompt_name && job.prompt_name !== callConversationTitle(job)
                ? job.prompt_name
                : '';
            meta.textContent = [
                t('calls.scheduled_for', { date: phoneCallDate(job.scheduled_at_utc) }),
                prompt
            ].filter(Boolean).join(' · ');
            main.append(title, meta);

            const actions = document.createElement('div');
            actions.className = 'settings-call-actions';
            const badge = document.createElement('span');
            badge.className = 'settings-call-status';
            badge.textContent = t('calls.scheduled');
            const cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'btn btn-sm btn-outline-danger';
            cancel.textContent = t('action.cancel');
            cancel.addEventListener('click', () => cancelScheduledCall(job, cancel));
            actions.append(badge, cancel);
            item.append(main, actions);
            list.appendChild(item);
        });

        calls.forEach(call => {
            const item = document.createElement('article');
            item.className = 'settings-call-item';
            const main = document.createElement('div');
            main.className = 'settings-call-main';
            const title = document.createElement('p');
            title.className = 'settings-call-title';
            title.textContent = t('calls.call_title', {
                conversation: callConversationTitle(call),
                direction: phoneCallDirection(call.direction)
            });
            const meta = document.createElement('p');
            meta.className = 'settings-call-meta';
            const prompt = call.prompt_name && call.prompt_name !== callConversationTitle(call)
                ? call.prompt_name
                : '';
            const parts = [
                phoneCallDate(call.started_at || call.answered_at || call.initiated_at || call.created_at),
                phoneCallDuration(call.duration_seconds),
                phoneCallCost(call),
                prompt
            ].filter(Boolean);
            meta.textContent = parts.join(' · ');
            main.append(title, meta);

            const actions = document.createElement('div');
            actions.className = 'settings-call-actions';
            const badge = document.createElement('span');
            badge.className = 'settings-call-status';
            badge.textContent = phoneCallStatus(call.status);
            actions.appendChild(badge);
            if (call.conversation_id) {
                const open = document.createElement('button');
                open.type = 'button';
                open.className = 'btn btn-sm btn-outline-primary';
                open.textContent = t('calls.open_conversation');
                open.addEventListener('click', () => openCallConversation(call.conversation_id));
                actions.appendChild(open);
            }
            item.append(main, actions);
            list.appendChild(item);
        });
    }

    async function loadCallsTab() {
        const list = document.getElementById('settings-calls-list');
        const status = document.getElementById('settings-calls-status');
        if (!list || !status) return;
        if (callsLoadController) callsLoadController.abort();
        callsLoadController = new AbortController();
        callsLoadGeneration += 1;
        const generation = callsLoadGeneration;
        const signal = callsLoadController.signal;
        status.hidden = false;
        status.textContent = t('calls.loading');
        list.replaceChildren();
        try {
            const fetcher = typeof secureFetch === 'function' ? secureFetch : fetch;
            const response = await fetcher('/api/telephony/calls?limit=100', {
                credentials: 'include',
                signal
            });
            if (!response || !response.ok) throw new Error(t('calls.load_failed'));
            const payload = await response.json();
            if (signal.aborted || generation !== callsLoadGeneration) return;
            const calls = Array.isArray(payload) ? payload : (Array.isArray(payload.calls) ? payload.calls : []);
            const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
            renderCalls(calls, jobs);
        } catch (_error) {
            if (signal.aborted || generation !== callsLoadGeneration) return;
            status.textContent = t('calls.load_failed');
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.className = 'btn btn-sm btn-outline-primary ms-2';
            retry.textContent = t('action.try_again');
            retry.addEventListener('click', loadCallsTab);
            status.appendChild(retry);
        }
    }

    // --- Usage Tab (adapted from my_usage.html inline JS) ---
    let spendingChart = null;

    function loadUsageTab() {
        // Load Chart.js dynamically if not loaded
        if (!chartJsLoaded) {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js';
            script.onload = function() {
                chartJsLoaded = true;
                loadUsageData();
            };
            document.head.appendChild(script);
        } else {
            loadUsageData();
        }
    }

    async function loadUsageData() {
        const dateRangeEl = document.getElementById('usageDateRange');
        if (!dateRangeEl) return;
        const days = dateRangeEl.value;
        const params = new URLSearchParams();
        params.append('days', days === 'all' ? '0' : days);

        try {
            const fetcher = typeof secureFetch === 'function' ? secureFetch : fetch;
            const response = await fetcher('/api/my-usage?' + params.toString(), { credentials: 'include' });
            if (!response || !response.ok) throw new Error('Failed to load data');
            const data = await response.json();

            updateBalance(data.balance);
            updateStats(data.stats);
            updateStorage(data.storage);
            updateUsageByType(data.by_type);
            updateChart(data.daily);
            updateDailyBreakdown(data.daily);
        } catch (error) {
            console.error('Error loading usage data:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error(t('modal.error'), t('usage.load_failed'));
            }
        }
    }

    function updateBalance(balance) {
        const el = document.getElementById('usageCurrentBalance');
        if (el) el.textContent = formatCurrency(balance);
    }

    function updateStats(stats) {
        const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        setEl('statOperations', formatNumber(stats.total_operations || 0));
        setEl('statTokens', formatNumber(stats.total_tokens || 0));
        setEl('statTokensBreakdown', t('usage.tokens_breakdown', {
            input: formatNumber(stats.tokens_in || 0),
            output: formatNumber(stats.tokens_out || 0)
        }));
        setEl('statCost', formatCurrency(stats.total_cost));
        setEl('statAvgDaily', formatCurrency(stats.avg_daily));
    }

    // --- Storage quota card (fed by the `storage` object in /api/my-usage) ---
    const GB_BYTES = 1024 * 1024 * 1024;

    function formatStorageBytes(bytes) {
        const value = Math.max(0, Number(bytes) || 0);
        if (value >= GB_BYTES) return formatDecimal(value / GB_BYTES) + ' GB';
        if (value >= 1024 * 1024) return formatDecimal(value / (1024 * 1024)) + ' MB';
        if (value >= 1024) return formatDecimal(value / 1024) + ' KB';
        return formatDecimal(Math.round(value), 0) + ' B';
    }

    function updateStorage(storage) {
        const card = document.getElementById('storageCard');
        if (!card) return;
        if (!storage || typeof storage.used_bytes !== 'number') {
            // Older cached response without storage data: keep the card hidden.
            card.classList.add('d-none');
            return;
        }

        const amount = document.getElementById('storageAmount');
        const bar = document.getElementById('storageBar');
        const fill = document.getElementById('storageBarFill');
        const breakdown = document.getElementById('storageBreakdown');
        const used = storage.used_bytes || 0;
        const quota = storage.quota_bytes || 0;

        card.classList.remove('d-none', 'info', 'success', 'warning', 'danger');
        breakdown.textContent = t('usage.storage_breakdown', {
            uploads: formatStorageBytes(storage.uploads_bytes || 0),
            generated: formatStorageBytes(storage.generated_bytes || 0)
        });

        if (quota === 0) {
            // Unlimited quota: used bytes only, no bar, no "of".
            amount.textContent = formatStorageBytes(used);
            card.classList.add('info');
            bar.classList.add('d-none');
            return;
        }

        const percent = (used / quota) * 100;
        const clamped = Math.max(0, Math.min(100, percent));
        amount.textContent = t('usage.storage_of', {
            used: formatStorageBytes(used),
            quota: `${formatDecimal(quota / GB_BYTES)} GB`
        });
        bar.classList.remove('d-none');
        bar.setAttribute('aria-valuenow', String(Math.round(clamped)));
        bar.setAttribute('aria-label', t('usage.storage_percent', { percent: formatDecimal(percent) }));
        fill.style.width = clamped + '%';
        fill.classList.remove('warn', 'danger');
        if (percent >= 95) {
            fill.classList.add('danger');
            card.classList.add('danger');
        } else if (percent >= 80) {
            fill.classList.add('warn');
            card.classList.add('warning');
        } else {
            card.classList.add('success');
        }
    }

    function updateUsageByType(byType) {
        const container = document.getElementById('usageByType');
        if (!container) return;

        if (!byType || byType.length === 0) {
            container.replaceChildren();
            const empty = document.createElement('div');
            empty.className = 'text-center text-muted py-4';
            empty.textContent = t('usage.empty');
            container.appendChild(empty);
            return;
        }

        const typeIcons = {
            'ai_tokens': 'fa-robot', 'tts': 'fa-volume-up', 'stt': 'fa-microphone',
            'image': 'fa-image', 'video': 'fa-video', 'domain': 'fa-globe'
        };
        const typeLabels = {
            'ai_tokens': t('usage.type.ai'), 'tts': t('usage.type.tts'), 'stt': t('usage.type.stt'),
            'image': t('usage.type.image'), 'video': t('usage.type.video'), 'domain': t('usage.type.domain')
        };

        container.replaceChildren();
        byType.forEach(item => {
            const row = document.createElement('div');
            row.className = 'usage-item';
            const details = document.createElement('div');
            details.className = 'details';
            const badge = document.createElement('span');
            badge.className = `type-badge ${item.type}`;
            const icon = document.createElement('i');
            icon.className = `fas ${typeIcons[item.type] || 'fa-circle'}`;
            badge.append(icon, document.createTextNode(` ${typeLabels[item.type] || t('usage.by_type')}`));
            const operations = document.createElement('span');
            operations.className = 'ops';
            operations.textContent = t('usage.operations', { count: Number(item.operations) || 0 });
            const cost = document.createElement('div');
            cost.className = 'cost';
            cost.textContent = formatCurrency(item.total_cost);
            details.append(badge, operations);
            row.append(details, cost);
            container.appendChild(row);
        });
    }

    function updateChart(daily) {
        const canvas = document.getElementById('usageSpendingChart');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');

        if (spendingChart) spendingChart.destroy();
        if (!daily || daily.length === 0) return;

        const sorted = [...daily].sort((a, b) => a.date.localeCompare(b.date));
        const labels = sorted.map(d => formatDateShort(d.date));
        const costData = sorted.map(d => d.total_cost);

        spendingChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    label: t('usage.daily_spending'),
                    data: costData,
                    borderColor: 'rgb(250, 166, 26)',
                    backgroundColor: 'rgba(250, 166, 26, 0.15)',
                    fill: true,
                    tension: 0.3,
                    pointRadius: 3,
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: ctx => formatCurrency(ctx.parsed.y) } }
                },
                scales: {
                    x: {
                        ticks: {
                            color: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#72767d',
                            maxTicksLimit: 8
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' }
                    },
                    y: {
                        ticks: {
                            color: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() || '#72767d',
                            callback: v => formatCurrency(v)
                        },
                        grid: { color: 'rgba(255,255,255,0.05)' }
                    }
                }
            }
        });
    }

    function updateDailyBreakdown(daily) {
        const container = document.getElementById('usageDailyBreakdown');
        if (!container) return;

        if (!daily || daily.length === 0) {
            container.replaceChildren();
            const empty = document.createElement('div');
            empty.className = 'text-center text-muted py-4';
            empty.textContent = t('usage.no_activity');
            container.appendChild(empty);
            return;
        }

        const sorted = [...daily].sort((a, b) => b.date.localeCompare(a.date)).slice(0, 14);
        container.replaceChildren();
        sorted.forEach(item => {
            const row = document.createElement('div');
            row.className = 'daily-item';
            const date = document.createElement('div');
            date.className = 'date';
            date.textContent = formatDateLong(item.date);
            const stats = document.createElement('div');
            stats.className = 'stats';
            const values = [
                [formatNumber(item.operations), t('usage.ops_short')],
                [formatNumber(item.tokens_in + item.tokens_out), t('usage.tokens_short')],
                [formatCurrency(item.total_cost), '']
            ];
            values.forEach(([value, suffix]) => {
                const stat = document.createElement('span');
                stat.className = 'stat-val';
                const strong = document.createElement('strong');
                strong.textContent = value;
                stat.append(strong, document.createTextNode(suffix ? ` ${suffix}` : ''));
                stats.appendChild(stat);
            });
            row.append(date, stats);
            container.appendChild(row);
        });
    }

    // --- Break Reminders Tab ---
    async function loadWellbeingTab() {
        setupWellbeingHandlers();
        await loadWellbeingPreferences();
    }

    function setupWellbeingHandlers() {
        const form = document.getElementById('wellbeingPreferencesForm');
        const resetBtn = document.getElementById('wellbeingResetSessionBtn');
        if (form && !form.dataset.bound) {
            form.dataset.bound = '1';
            FormGuard.watch(form);
            form.addEventListener('submit', saveWellbeingPreferences);
        }
        if (resetBtn && !resetBtn.dataset.bound) {
            resetBtn.dataset.bound = '1';
            resetBtn.addEventListener('click', resetWellbeingSession);
        }
    }

    async function wellbeingFetch(url, options = {}) {
        const fetcher = typeof secureFetch === 'function' ? secureFetch : fetch;
        return fetcher(url, {
            credentials: 'include',
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...(options.headers || {})
            }
        });
    }

    async function loadWellbeingPreferences() {
        const form = document.getElementById('wellbeingPreferencesForm');
        const stateBeforeLoad = readWellbeingPreferenceInputs();
        try {
            const response = await wellbeingFetch('/api/wellbeing/preferences');
            if (!response.ok) throw new Error(t('wellbeing.load_failed'));
            const data = await response.json();
            const currentState = readWellbeingPreferenceInputs();
            renderWellbeingPreferences(data.preferences || {});
            FormGuard.markClean(form);
            if (!sameWellbeingPreferenceInputs(currentState, stateBeforeLoad)) {
                renderWellbeingPreferences(currentState);
            }
            renderWellbeingStatus(data.status || {});
        } catch (error) {
            console.error('Error loading break reminder settings:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error(t('modal.error'), t('wellbeing.load_failed'));
            }
        }
    }

    function renderWellbeingPreferences(preferences) {
        const remindersEnabled = document.getElementById('wellbeingRemindersEnabled');
        const intenseEnabled = document.getElementById('wellbeingIntenseEnabled');
        const preferredMinutes = document.getElementById('wellbeingPreferredSoftMinutes');
        if (remindersEnabled) remindersEnabled.checked = preferences.reminders_enabled !== false;
        if (intenseEnabled) intenseEnabled.checked = preferences.intense_reminders_enabled !== false;
        if (preferredMinutes) preferredMinutes.value = preferences.preferred_soft_minutes || '';
    }

    function readWellbeingPreferenceInputs() {
        const preferredMinutes = document.getElementById('wellbeingPreferredSoftMinutes');
        return {
            reminders_enabled: document.getElementById('wellbeingRemindersEnabled')?.checked,
            intense_reminders_enabled: document.getElementById('wellbeingIntenseEnabled')?.checked,
            preferred_soft_minutes: preferredMinutes?.value || ''
        };
    }

    function sameWellbeingPreferenceInputs(left, right) {
        return left.reminders_enabled === right.reminders_enabled
            && left.intense_reminders_enabled === right.intense_reminders_enabled
            && left.preferred_soft_minutes === right.preferred_soft_minutes;
    }

    function renderWellbeingStatus(status) {
        const session = status.session || {};
        const setEl = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        setEl('wellbeingActiveMinutes', session.active_minutes !== undefined ? formatNumber(session.active_minutes) : '-');
        setEl('wellbeingUserMessages', session.user_messages_count !== undefined ? formatNumber(session.user_messages_count) : '-');
        setEl('wellbeingRemindersShown', session.reminders_shown !== undefined ? formatNumber(session.reminders_shown) : '-');
        const severity = String(session.current_severity || 'normal').toLowerCase();
        const severityKeys = new Set(['normal', 'soft', 'strong', 'mandatory']);
        setEl('wellbeingSeverity', t(`wellbeing.severity.${severityKeys.has(severity) ? severity : 'normal'}`));

        const statusText = document.getElementById('wellbeingStatusText');
        if (statusText) {
            if (!session.id) {
                statusText.textContent = t('wellbeing.no_active_session');
            } else if (status.active_pause && status.pause_until) {
                statusText.textContent = t('wellbeing.pause_until', {
                    time: new Date(status.pause_until).toLocaleTimeString(window.AurvekI18n.locale)
                });
            } else {
                statusText.textContent = t('wellbeing.session_started', {
                    date: formatDateTime(session.started_at)
                });
            }
        }
    }

    async function saveWellbeingPreferences(event) {
        event.preventDefault();
        const form = document.getElementById('wellbeingPreferencesForm');
        const submittedState = readWellbeingPreferenceInputs();
        const payload = {
            reminders_enabled: submittedState.reminders_enabled,
            intense_reminders_enabled: submittedState.intense_reminders_enabled,
            preferred_soft_minutes: submittedState.preferred_soft_minutes
                ? Number(submittedState.preferred_soft_minutes)
                : null
        };
        try {
            const response = await wellbeingFetch('/api/wellbeing/preferences', {
                method: 'PUT',
                body: JSON.stringify(payload)
            });
            if (!response.ok) throw new Error(t('wellbeing.save_failed'));
            const data = await response.json();
            const currentState = readWellbeingPreferenceInputs();
            renderWellbeingPreferences(data.preferences || {});
            FormGuard.markClean(form);
            if (!sameWellbeingPreferenceInputs(currentState, submittedState)) {
                renderWellbeingPreferences(currentState);
            }
            renderWellbeingStatus(data.status || {});
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.success(t('modal.saved'), t('wellbeing.saved'));
            }
        } catch (error) {
            console.error('Error saving break reminder settings:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error(t('modal.error'), t('wellbeing.save_failed'));
            }
        }
    }

    async function resetWellbeingSession() {
        try {
            const response = await wellbeingFetch('/api/wellbeing/reset-session', {
                method: 'POST',
                body: JSON.stringify({})
            });
            if (!response.ok) throw new Error(t('wellbeing.reset_failed'));
            const status = await response.json();
            renderWellbeingStatus(status || {});
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.success(t('modal.reset'), t('wellbeing.reset'));
            }
        } catch (error) {
            console.error('Error resetting break reminder counter:', error);
            if (typeof NotificationModal !== 'undefined') {
                NotificationModal.error(t('modal.error'), t('wellbeing.reset_failed'));
            }
        }
    }

    function formatDateTime(value) {
        if (!value) return '-';
        const normalized = String(value).replace(' ', 'T') + (String(value).includes('Z') ? '' : 'Z');
        const date = new Date(normalized);
        return Number.isNaN(date.getTime()) ? value : date.toLocaleString(window.AurvekI18n.locale);
    }

    function formatNumber(num) {
        return new Intl.NumberFormat(window.AurvekI18n.locale, {
            notation: 'compact',
            maximumFractionDigits: 1
        }).format(Number(num) || 0);
    }

    function formatDecimal(value, maximumFractionDigits = 1) {
        return new Intl.NumberFormat(window.AurvekI18n.locale, {
            maximumFractionDigits
        }).format(Number(value) || 0);
    }

    function formatCurrency(value) {
        return new Intl.NumberFormat(window.AurvekI18n.locale, {
            style: 'currency',
            currency: 'USD'
        }).format(Number(value) || 0);
    }

    function formatDateShort(dateStr) {
        const date = new Date(dateStr + 'T00:00:00');
        return date.toLocaleDateString(window.AurvekI18n.locale, { month: 'short', day: 'numeric' });
    }

    function formatDateLong(dateStr) {
        const date = new Date(dateStr + 'T00:00:00');
        return date.toLocaleDateString(window.AurvekI18n.locale, { weekday: 'short', month: 'short', day: 'numeric' });
    }

    // Expose loadUsageData for the date range filter
    window.loadUsageData = loadUsageData;

    // Init
    document.addEventListener('DOMContentLoaded', function() {
        setupHashSync();
        activateFromHash();
        // Always init profile tab (default)
        const hash = window.location.hash || '#profile';
        initTab(hash.replace('#', ''));
    });
})();
