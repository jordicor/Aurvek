(() => {
    'use strict';
    const config = JSON.parse(document.getElementById('twilio-page-config').textContent);
    const feedback = document.getElementById('connection-feedback');
    async function mutate(action, body) {
        const buttons = Array.from(document.querySelectorAll('button'));
        buttons.forEach(button => { button.disabled = true; });
        try {
            const response = await fetch(config.base + '/' + action, {
                method: 'POST', credentials: 'same-origin',
                headers: {'Content-Type': 'application/json', 'X-GPTSub-CSRF': config.csrf},
                body: JSON.stringify({...body, expected_version: config.version})
            });
            const result = await response.json();
            if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : config.error);
            window.location.reload();
        } catch (error) {
            feedback.textContent = error.message;
            feedback.className = 'alert alert-danger mt-3';
            buttons.forEach(button => { button.disabled = false; });
        }
    }
    document.getElementById('twilio-credentials').addEventListener('submit', event => {
        event.preventDefault();
        const input = document.getElementById('auth-token');
        const token = input.value.trim();
        input.value = '';
        mutate('credentials', {account_sid: document.getElementById('account-sid').value.trim(), auth_token: token});
    });
    document.getElementById('sync-numbers')?.addEventListener('click', () => mutate('sync', {}));
    document.getElementById('disconnect')?.addEventListener('click', () => mutate('disconnect', {}));
    document.getElementById('twilio-number')?.addEventListener('submit', event => {
        event.preventDefault();
        const selected = document.querySelector('input[name="number_sid"]:checked');
        if (!selected) return;
        mutate('activate', {number_sid: selected.value,
            confirm_replace: document.getElementById('confirm-replace').checked,
            phone_auth: document.getElementById('linked-caller').checked ? 'linked_caller' : 'pin',
            phone_language: document.getElementById('phone-language').value,
            allow_outbound: document.getElementById('allow-outbound').checked});
    });
})();
