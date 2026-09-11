(function() {
    'use strict';

    const STATUS_INTERVAL_MS = 60000;
    const ACTIVITY_POST_MIN_INTERVAL_MS = 60000;
    const DEFAULT_IDLE_GAP_MINUTES = 25;
    const PAUSE_MINUTES = 5;
    const REAL_ACTIVITY_EVENTS = [
        'mousemove',
        'mousedown',
        'keydown',
        'input',
        'click',
        'scroll',
        'touchstart',
        'wheel'
    ];

    let statusTimer = null;
    let idleTimer = null;
    let pauseTimer = null;
    let modalInstance = null;
    let modalShowing = false;
    let lastShownKey = null;
    let latestStatus = null;
    let lastInteractionAt = 0;
    let lastActivitySentAt = 0;
    let afkReported = false;

    function isEligible() {
        return !window.admin_view && window.user_id && typeof window.currentConversationId !== 'undefined';
    }

    function request(url, options) {
        const fetcher = typeof window.secureFetch === 'function' ? window.secureFetch : window.fetch;
        return fetcher(url, {
            credentials: 'include',
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...(options && options.headers ? options.headers : {})
            }
        });
    }

    function currentConversation() {
        const id = window.currentConversationId;
        return id === null || id === undefined || id === '' ? null : Number(id);
    }

    function hasWindowFocus() {
        return document.visibilityState === 'visible' && (!document.hasFocus || document.hasFocus());
    }

    function idleTimeoutMs() {
        const configured = latestStatus && Number(latestStatus.idle_gap_minutes);
        const minutes = Number.isFinite(configured) && configured > 0 ? configured : DEFAULT_IDLE_GAP_MINUTES;
        return minutes * 60 * 1000;
    }

    function hasRecentInteraction(now) {
        const ref = now || Date.now();
        return lastInteractionAt > 0 && ref - lastInteractionAt < idleTimeoutMs();
    }

    function hasExternalActiveUse() {
        return Boolean(window.WellbeingVoiceActive);
    }

    function isClientActive() {
        return hasWindowFocus() && (hasExternalActiveUse() || hasRecentInteraction());
    }

    function scheduleIdleCheck() {
        if (idleTimer) {
            clearTimeout(idleTimer);
            idleTimer = null;
        }
        if (!hasWindowFocus() || hasExternalActiveUse() || lastInteractionAt <= 0) {
            return;
        }

        const remaining = idleTimeoutMs() - (Date.now() - lastInteractionAt);
        idleTimer = setTimeout(function() {
            if (!hasWindowFocus() || hasExternalActiveUse()) {
                return;
            }
            if (!hasRecentInteraction()) {
                reportInactive('client_idle');
                return;
            }
            scheduleIdleCheck();
        }, Math.max(1000, remaining + 250));
    }

    function buildClientMetadata(extra) {
        return {
            client_active: isClientActive(),
            visibility_state: document.visibilityState,
            window_focused: hasWindowFocus(),
            last_interaction_at: lastInteractionAt ? new Date(lastInteractionAt).toISOString() : null,
            ...(extra || {})
        };
    }

    async function fetchStatus() {
        if (!isEligible()) return;
        const params = new URLSearchParams();
        const conversationId = currentConversation();
        if (conversationId) params.set('conversation_id', conversationId);
        params.set('client_active', isClientActive() ? '1' : '0');

        try {
            const response = await request('/api/wellbeing/status?' + params.toString(), { method: 'GET' });
            if (!response || !response.ok) return;
            latestStatus = await response.json();
            applyPauseState(latestStatus);
            maybeShowReminder(latestStatus);
            scheduleIdleCheck();
        } catch (error) {
            console.debug('Wellbeing status check failed:', error);
        }
    }

    async function recordClientActivity(activityType, metadata) {
        if (!isEligible()) return false;
        const conversationId = currentConversation();
        const payload = {
            activity_type: activityType || 'user_interaction',
            metadata: buildClientMetadata(metadata)
        };
        if (conversationId) {
            payload.conversation_id = conversationId;
        }

        try {
            const response = await request('/api/wellbeing/activity', {
                method: 'POST',
                body: JSON.stringify(payload)
            });
            if (!response || !response.ok) return false;
            latestStatus = await response.json();
            applyPauseState(latestStatus);
            maybeShowReminder(latestStatus);
            scheduleIdleCheck();
            return true;
        } catch (error) {
            console.debug('Wellbeing activity update failed:', error);
            return false;
        }
    }

    async function reportInactive(reason) {
        if (!isEligible() || afkReported) return;
        afkReported = true;
        if (idleTimer) {
            clearTimeout(idleTimer);
            idleTimer = null;
        }
        const recorded = await recordClientActivity(reason || 'client_afk', {
            reason: reason || 'client_afk'
        });
        if (!recorded) {
            afkReported = false;
        }
    }

    function markInteraction(eventType, shouldPost) {
        if (!isEligible() || !hasWindowFocus()) return;
        const now = Date.now();
        lastInteractionAt = now;
        afkReported = false;
        scheduleIdleCheck();

        if (!shouldPost || now - lastActivitySentAt < ACTIVITY_POST_MIN_INTERVAL_MS) {
            return;
        }
        lastActivitySentAt = now;
        recordClientActivity('user_interaction', { event_type: eventType || 'interaction' });
    }

    function handleActivityEvent(event) {
        markInteraction(event && event.type, true);
    }

    async function recordAction(action, extra) {
        const status = latestStatus || {};
        const session = status.session || {};
        const reminder = status.reminder || {};
        try {
            const response = await request('/api/wellbeing/events', {
                method: 'POST',
                body: JSON.stringify({
                    action,
                    session_id: session.id,
                    conversation_id: currentConversation(),
                    severity: reminder.severity || session.current_severity,
                    threshold_key: reminder.threshold_key,
                    threshold_value: reminder.threshold_value,
                    observed_value: reminder.observed_value,
                    ...(extra || {})
                })
            });
            if (response && response.ok) {
                latestStatus = await response.json();
                applyPauseState(latestStatus);
            }
        } catch (error) {
            console.debug('Wellbeing action failed:', error);
        }
    }

    function ensureModal() {
        let modal = document.getElementById('wellbeingReminderModal');
        if (modal) return modal;

        modal = document.createElement('div');
        modal.className = 'modal fade';
        modal.id = 'wellbeingReminderModal';
        modal.tabIndex = -1;
        modal.setAttribute('aria-hidden', 'true');
        modal.innerHTML = `
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="wellbeingReminderTitle"></h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        <p class="mb-0" id="wellbeingReminderText"></p>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-outline-secondary" id="wellbeingContinueBtn"></button>
                        <button type="button" class="btn btn-outline-primary" id="wellbeingSnoozeBtn">Remind me later</button>
                        <button type="button" class="btn btn-primary" id="wellbeingPauseBtn"></button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
        modal.querySelector('#wellbeingReminderTitle').textContent = AurvekI18n.t('chat_widgets.wellbeing.title');
        modal.querySelector('#wellbeingContinueBtn').textContent = AurvekI18n.t('chat_widgets.wellbeing.continue');
        modal.querySelector('#wellbeingPauseBtn').textContent = AurvekI18n.t(
            'chat_widgets.wellbeing.pause_minutes',
            {count: PAUSE_MINUTES}
        );
        modal.querySelector('.btn-close').setAttribute('aria-label', AurvekI18n.t('common.action.close'));
        modal.addEventListener('hidden.bs.modal', function() {
            modalShowing = false;
        });
        return modal;
    }

    function reminderTitle(severity) {
        if (severity === 'strong') return AurvekI18n.t('chat_widgets.wellbeing.title_strong');
        if (severity === 'intense') return AurvekI18n.t('chat_widgets.wellbeing.title_intense');
        return AurvekI18n.t('chat_widgets.wellbeing.title');
    }

    function reminderText(reminder) {
        const defaultKeys = {
            notice_soft: 'chat_widgets.wellbeing.notice_soft',
            notice_intense: 'chat_widgets.wellbeing.notice_intense',
            notice_strong: 'chat_widgets.wellbeing.notice_strong'
        };
        const key = reminder && defaultKeys[reminder.text_key];
        if (key) return AurvekI18n.t(key);
        return reminder?.text || AurvekI18n.t('chat_widgets.wellbeing.break_suggestion');
    }

    async function maybeShowReminder(status) {
        const reminder = status && status.reminder;
        const session = status && status.session;
        if (!isClientActive() || !reminder || !reminder.should_show || !session || modalShowing) return;

        const reminderKey = [
            session.id,
            reminder.severity,
            reminder.mode || '',
            reminder.requires_pause ? 'requires_pause' : 'optional',
            reminder.threshold_key || '',
            reminder.observed_value || ''
        ].join(':');
        if (reminderKey === lastShownKey) return;
        lastShownKey = reminderKey;

        const modal = ensureModal();
        modal.querySelector('#wellbeingReminderTitle').textContent = reminderTitle(reminder.severity);
        modal.querySelector('#wellbeingReminderText').textContent = reminderText(reminder);

        const continueBtn = modal.querySelector('#wellbeingContinueBtn');
        const snoozeBtn = modal.querySelector('#wellbeingSnoozeBtn');
        const pauseBtn = modal.querySelector('#wellbeingPauseBtn');
        const closeBtn = modal.querySelector('.btn-close');

        continueBtn.style.display = reminder.requires_pause ? 'none' : '';
        closeBtn.style.display = reminder.requires_pause ? 'none' : '';
        snoozeBtn.style.display = reminder.allow_snooze && !reminder.requires_pause ? '' : 'none';
        snoozeBtn.textContent = AurvekI18n.t('chat_widgets.wellbeing.remind_in_minutes', {count: reminder.snooze_minutes || 10});

        continueBtn.onclick = async function() {
            markInteraction('wellbeing_continue', false);
            await recordAction('reminder_dismissed');
            modalInstance.hide();
        };
        closeBtn.onclick = async function() {
            markInteraction('wellbeing_close', false);
            await recordAction('reminder_dismissed');
        };
        snoozeBtn.onclick = async function() {
            markInteraction('wellbeing_snooze', false);
            await recordAction('reminder_snoozed', { snooze_minutes: reminder.snooze_minutes || 10 });
            modalInstance.hide();
        };
        pauseBtn.onclick = async function() {
            markInteraction('wellbeing_pause', false);
            await recordAction('pause_started', { pause_minutes: PAUSE_MINUTES });
            modalInstance.hide();
        };

        if (!isClientActive()) return;
        await recordAction('reminder_shown');
        modalShowing = true;
        modalInstance = bootstrap.Modal.getOrCreateInstance(modal, {
            backdrop: reminder.requires_pause ? 'static' : true,
            keyboard: !reminder.requires_pause
        });
        modalInstance.show();
    }

    function applyPauseState(status) {
        const activePause = Boolean(status && status.active_pause);
        const pauseUntil = status && status.pause_until ? new Date(status.pause_until).getTime() : null;
        const controls = [
            document.getElementById('message-text'),
            document.getElementById('send-button'),
            document.getElementById('chat-files'),
            document.getElementById('plus-voice-call')
        ].filter(Boolean);

        controls.forEach(function(control) {
            if (activePause) {
                if (!control.dataset.wellbeingOriginalDisabled) {
                    control.dataset.wellbeingOriginalDisabled = control.disabled ? '1' : '0';
                }
                control.disabled = true;
            } else if (control.dataset.wellbeingOriginalDisabled) {
                control.disabled = control.dataset.wellbeingOriginalDisabled === '1';
                delete control.dataset.wellbeingOriginalDisabled;
            }
        });

        if (pauseTimer) {
            clearTimeout(pauseTimer);
            pauseTimer = null;
        }
        if (activePause && pauseUntil) {
            const delay = Math.max(1000, pauseUntil - Date.now() + 500);
            pauseTimer = setTimeout(async function() {
                await recordAction('pause_completed');
                await fetchStatus();
            }, delay);
        }
    }

    function scheduleAfterSendCheck() {
        markInteraction('message_submit', false);
        setTimeout(fetchStatus, 2500);
        setTimeout(fetchStatus, 8000);
    }

    function statusTick() {
        if (hasExternalActiveUse()) {
            recordClientActivity('voice_call_active', { source: 'voice_call' });
            return;
        }
        fetchStatus();
    }

    function init() {
        if (!isEligible()) return;
        fetchStatus();
        statusTimer = setInterval(statusTick, STATUS_INTERVAL_MS);

        REAL_ACTIVITY_EVENTS.forEach(function(eventName) {
            document.addEventListener(eventName, handleActivityEvent, { passive: true, capture: true });
        });

        const form = document.getElementById('form-message');
        if (form) {
            form.addEventListener('submit', scheduleAfterSendCheck);
        }
        document.addEventListener('visibilitychange', function() {
            if (document.visibilityState === 'visible') {
                fetchStatus();
                scheduleIdleCheck();
            } else {
                reportInactive('visibility_hidden');
            }
        });
        window.addEventListener('blur', function() {
            reportInactive('focus_lost');
        });
        window.addEventListener('focus', function() {
            fetchStatus();
            scheduleIdleCheck();
        });
    }

    window.WellbeingReminders = {
        refresh: fetchStatus,
        recordPresence: fetchStatus,
        recordActivity: recordClientActivity,
        reportInactive,
        get latestStatus() {
            return latestStatus;
        },
        get clientActive() {
            return isClientActive();
        }
    };

    document.addEventListener('DOMContentLoaded', init);
})();
