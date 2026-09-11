(() => {
    'use strict';
    const config = JSON.parse(document.getElementById('funding-page-config').textContent);
    const forms = Array.from(document.querySelectorAll('.funding-account-form'));
    document.getElementById('funding-account')?.addEventListener('change', event => {
        // Keep each account's unsaved fields intact when changing selection.
        forms.forEach(form => form.classList.toggle('d-none', form.dataset.accountIndex !== event.target.value));
    });
    forms.forEach(form => {
        const active = form.elements.namedItem('active');
        const unlimited = form.elements.namedItem('unlimited');
        const limit = form.elements.namedItem('monthly_limit');
        const feedback = form.querySelector('.funding-feedback');
        const syncLimit = () => {
            limit.disabled = unlimited.checked;
            limit.required = !unlimited.checked;
        };
        unlimited.addEventListener('change', syncLimit);
        form.addEventListener('submit', async event => {
            event.preventDefault();
            if (!config.configured) return;
            if (!form.reportValidity()) return;
            const body = {active: active.checked, monthly_limit: unlimited.checked ? null : Number(limit.value),
                expected_version: Number(form.dataset.version)};
            const controls = Array.from(form.elements);
            controls.forEach(control => { control.disabled = true; });
            feedback.className = 'funding-feedback alert d-none mt-3 mb-0';
            let saved = false;
            try {
                const response = await fetch(config.base + encodeURIComponent(form.dataset.accountId), {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-GPTSub-CSRF': config.csrf},
                    body: JSON.stringify(body)
                });
                const result = await response.json();
                if (!response.ok) throw new Error(result.detail || result.message || config.error);
                const account = result.accounts.find(item => item.external_user_id === form.dataset.accountId);
                if (!account) throw new Error(config.error);
                form.dataset.version = String(account.funding.version);
                active.checked = account.funding.active;
                unlimited.checked = account.funding.monthly_limit === null;
                limit.value = account.funding.monthly_limit ?? '';
                document.getElementById('funding-balance').textContent = new Intl.NumberFormat(AurvekI18n.locale,
                    {style: 'currency', currency: 'USD'}).format(result.payer.balance);
                feedback.textContent = config.saved;
                feedback.className = 'funding-feedback alert alert-success mt-3 mb-0';
                saved = true;
            } catch (error) {
                feedback.textContent = error.message || config.error;
                feedback.className = 'funding-feedback alert alert-danger mt-3 mb-0';
            } finally {
                controls.forEach(control => { control.disabled = false; });
                syncLimit();
                if (saved) window.FormGuard?.markClean(form);
                else window.FormGuard?.resumeAfterSubmit(form);
            }
        });
    });
})();
