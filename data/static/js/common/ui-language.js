/* Native interface language selector. Embedded chats remain host-controlled. */
(function(root) {
    'use strict';

    const supportedLanguages = new Set(['en', 'es', 'ja', 'fr', 'pt', 'it', 'de']);
    let selectedLanguage = root.AurvekI18n?.language || document.documentElement.lang || 'en';
    let requestPending = false;

    const tr = key => root.AurvekI18n.t(`navigation.${key}`);

    function languageOptions() {
        return document.querySelectorAll('.ui-language-option');
    }

    function updateSelection(language) {
        languageOptions().forEach(option => {
            const active = option.dataset.uiLanguage === language;
            option.classList.toggle('active', active);
            option.setAttribute('aria-current', active ? 'true' : 'false');
            const check = option.querySelector('.ui-language-check');
            if (check) check.hidden = !active;
        });
    }

    function chatActivityInProgress() {
        const message = document.getElementById('message-text');
        const previews = document.getElementById('image-previews');
        const recordingControls = document.getElementById('audio-recording-controls');
        const phoneActivity = document.getElementById('phone-active-card');
        return Boolean(
            message?.value.trim()
            || document.getElementById('send-button')?.dataset.streaming === 'true'
            || (typeof attachedFiles !== 'undefined' && Array.isArray(attachedFiles) && attachedFiles.length)
            || (typeof Config !== 'undefined' && Array.isArray(Config.attachedFiles) && Config.attachedFiles.length)
            || (previews && previews.children.length > 0)
            || (recordingControls && !recordingControls.classList.contains('hidden'))
            || (typeof recordingStatus !== 'undefined' && recordingStatus !== 'idle')
            || root.WellbeingVoiceActive === true
            || (phoneActivity && !phoneActivity.hidden)
        );
    }

    function showMessage(type, key) {
        if (typeof NotificationModal !== 'undefined' && typeof NotificationModal[type] === 'function') {
            NotificationModal[type](tr('menu.language'), tr(key));
        }
    }

    async function csrfToken() {
        let initData = root.__userInitData;
        if (root.__userInitPromise) {
            try {
                initData = await root.__userInitPromise;
            } catch (_error) {
                // The page meta token remains a valid fallback.
            }
        }
        return initData?.session?.csrf_token
            || document.querySelector('meta[name="aurvek-csrf-token"]')?.content
            || '';
    }

    function setPending(pending) {
        requestPending = pending;
        languageOptions().forEach(option => {
            option.setAttribute('aria-disabled', pending ? 'true' : 'false');
        });
    }

    async function selectLanguage(option) {
        const language = option.dataset.uiLanguage;
        if (requestPending || !supportedLanguages.has(language) || language === selectedLanguage) return;
        if (chatActivityInProgress()) {
            showMessage('warning', 'language.finish_activity');
            return;
        }

        const settingsSelect = document.getElementById('uiLanguage');
        const settingsValueAtRequest = settingsSelect?.value;
        setPending(true);
        try {
            const token = await csrfToken();
            if (!token) throw new Error('csrf_unavailable');
            const response = await root.fetch('/api/ui-language', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'X-GPTSub-CSRF': token,
                },
                body: JSON.stringify({ ui_language: language }),
            });
            const payload = await response.json().catch(() => null);
            if (!response.ok || payload?.success !== true || payload.ui_language !== language) {
                throw new Error('save_failed');
            }

            selectedLanguage = language;
            updateSelection(language);
            if (settingsSelect && settingsSelect.value === settingsValueAtRequest) {
                settingsSelect.value = language;
                root.FormGuard?.markFieldsClean('#editProfileForm', ['ui_language']);
            }

            if (chatActivityInProgress()) {
                showMessage('warning', 'language.finish_activity');
                return;
            }
            if (root.currentConversationId !== null && root.currentConversationId !== undefined) {
                localStorage.setItem('restoreConversationId', String(root.currentConversationId));
            }
            if (root.FormGuard?.reloadIfClean) root.FormGuard.reloadIfClean();
            else root.location.reload();
        } catch (_error) {
            showMessage('error', 'language.change_failed');
        } finally {
            setPending(false);
        }
    }

    function init() {
        updateSelection(selectedLanguage);
        languageOptions().forEach(option => {
            option.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                void selectLanguage(option);
            });
        });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(window);
