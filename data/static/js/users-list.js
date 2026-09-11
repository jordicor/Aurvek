/**
 * Users List - Management functionality
 * Handles filtering, sorting, selection and actions for users table
 */

// State management
const UsersListState = {
    users: [],
    sortColumn: 'username',
    sortDirection: 'asc',
    filters: {
        search: '',
        status: '',
        account: '',
        prompt: '',
        llm: '',
        balance: '',
        role: ''
    }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', function() {
    initializeUsersData();
    populateFilterDropdowns();
    setupEventListeners();
    applyFiltersAndSort();
    initUltraAdminState();
});

/**
 * Extract users data from table rows into state
 */
function initializeUsersData() {
    const rows = document.querySelectorAll('#usersTableBody tr[data-username]');
    UsersListState.users = Array.from(rows).map(row => {
        const tokens = parseInt(row.dataset.tokens) || 0;

        // Format tokens cell with abbreviation
        const tokensCell = row.querySelector('.tokens-cell');
        if (tokensCell) {
            tokensCell.textContent = formatNumber(tokens);
        }

        return {
            element: row,
            username: row.dataset.username,
            magicLink: row.dataset.magicLink || '',
            role: row.dataset.role || '',
            enabled: row.dataset.enabled === 'true',
            account: row.dataset.account || (row.dataset.enabled === 'true' ? 'enabled' : 'disabled'),
            phone: row.dataset.phone || '',
            status: row.dataset.status,
            prompt: row.dataset.prompt,
            llm: row.dataset.llm,
            tokens: tokens,
            cost: parseFloat(row.dataset.cost) || 0,
            balance: parseFloat(row.dataset.balance) || 0,
            chats: parseInt(row.dataset.chats) || 0
        };
    });
}

/**
 * Populate filter dropdowns with unique values from data
 */
function populateFilterDropdowns() {
    const prompts = [...new Set(UsersListState.users.map(u => u.prompt))].sort();
    const llms = [...new Set(UsersListState.users.map(u => u.llm))].sort();

    const promptSelect = document.getElementById('filterPrompt');
    const llmSelect = document.getElementById('filterLLM');

    if (promptSelect) {
        promptSelect.innerHTML = '<option value="">' + AurvekI18n.t('admin_users.all_prompts') + '</option>';
        prompts.forEach(p => {
            if (p) promptSelect.innerHTML += `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`;
        });
    }

    if (llmSelect) {
        llmSelect.innerHTML = '<option value="">' + AurvekI18n.t('admin_users.all_llms') + '</option>';
        llms.forEach(l => {
            if (l) llmSelect.innerHTML += `<option value="${escapeHtml(l)}">${escapeHtml(l)}</option>`;
        });
    }
}

/**
 * Setup event listeners for filters and sorting
 */
function setupEventListeners() {
    // Filter inputs
    const searchInput = document.getElementById('filterSearch');
    const statusSelect = document.getElementById('filterStatus');
    const accountSelect = document.getElementById('filterAccount');
    const promptSelect = document.getElementById('filterPrompt');
    const llmSelect = document.getElementById('filterLLM');
    const balanceSelect = document.getElementById('filterBalance');
    const roleSelect = document.getElementById('filterRole');

    if (searchInput) searchInput.addEventListener('input', debounce(onFilterChange, 200));
    if (statusSelect) statusSelect.addEventListener('change', onFilterChange);
    if (accountSelect) accountSelect.addEventListener('change', onFilterChange);
    if (promptSelect) promptSelect.addEventListener('change', onFilterChange);
    if (llmSelect) llmSelect.addEventListener('change', onFilterChange);
    if (balanceSelect) balanceSelect.addEventListener('change', onFilterChange);
    if (roleSelect) roleSelect.addEventListener('change', onFilterChange);

    // Sortable headers
    document.querySelectorAll('th[data-sort]').forEach(th => {
        th.addEventListener('click', () => onSortClick(th.dataset.sort));
    });

    // Select all checkbox
    const selectAll = document.getElementById('selectAll');
    if (selectAll) selectAll.addEventListener('change', toggleSelectAll);

    // Individual checkboxes
    document.querySelectorAll('.user-checkbox').forEach(cb => {
        cb.addEventListener('change', updateSelectedCount);
    });

    // Delete form confirmation (AJAX-based to handle errors gracefully)
    const usersForm = document.getElementById('usersForm');
    if (usersForm) {
        usersForm.addEventListener('submit', function(e) {
            e.preventDefault();
            const checked = document.querySelectorAll('.user-checkbox:checked');
            const count = checked.length;
            if (count === 0) return;

            NotificationModal.confirm(AurvekI18n.t('admin_users.delete_users'), AurvekI18n.t('admin_users.flow.delete_confirm', {count}), async () => {
                try {
                    const formData = new FormData(usersForm);
                    const response = await secureFetch('/admin/delete-users', {
                        method: 'POST',
                        body: formData
                    });
                    if (!response) return;

                    const data = await response.json();
                    if (response.ok) {
                        NotificationModal.success(AurvekI18n.t('admin_users.deleted'), data.message || AurvekI18n.t('admin_users.users_deleted_successfully'));
                        setTimeout(() => window.location.reload(), 1000);
                    } else {
                        NotificationModal.error(AurvekI18n.t('admin_users.delete_failed'), data.detail || data.error || AurvekI18n.t('admin_users.could_not_delete_users'));
                    }
                } catch (error) {
                    NotificationModal.error(AurvekI18n.t('admin_users.error'), AurvekI18n.t('admin_users.an_unexpected_error_occurred'));
                }
            }, null, { type: 'error', confirmText: AurvekI18n.t('admin_users.delete_2') });
        });
    }
}

/**
 * Handle filter change
 */
function onFilterChange() {
    UsersListState.filters.search = (document.getElementById('filterSearch')?.value || '').toLowerCase();
    UsersListState.filters.status = document.getElementById('filterStatus')?.value || '';
    UsersListState.filters.account = document.getElementById('filterAccount')?.value || '';
    UsersListState.filters.prompt = document.getElementById('filterPrompt')?.value || '';
    UsersListState.filters.llm = document.getElementById('filterLLM')?.value || '';
    UsersListState.filters.balance = document.getElementById('filterBalance')?.value || '';
    UsersListState.filters.role = document.getElementById('filterRole')?.value || '';

    applyFiltersAndSort();
}

/**
 * Handle sort header click
 */
function onSortClick(column) {
    if (UsersListState.sortColumn === column) {
        UsersListState.sortDirection = UsersListState.sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        UsersListState.sortColumn = column;
        UsersListState.sortDirection = 'asc';
    }

    updateSortIndicators();
    applyFiltersAndSort();
}

/**
 * Update sort indicators in headers
 */
function updateSortIndicators() {
    document.querySelectorAll('th[data-sort]').forEach(th => {
        th.classList.remove('sort-asc', 'sort-desc');
        if (th.dataset.sort === UsersListState.sortColumn) {
            th.classList.add(UsersListState.sortDirection === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    });
}

/**
 * Apply filters and sorting to table
 */
function applyFiltersAndSort() {
    const { filters, sortColumn, sortDirection } = UsersListState;

    // Filter users
    let filtered = UsersListState.users.filter(user => {
        // Search filter (username or phone)
        if (filters.search) {
            const searchMatch = user.username.toLowerCase().includes(filters.search) ||
                               user.phone.toLowerCase().includes(filters.search);
            if (!searchMatch) return false;
        }

        // Status filter
        if (filters.status && user.status !== filters.status) return false;

        // Account enabled/disabled filter
        if (filters.account && user.account !== filters.account) return false;

        // Prompt filter
        if (filters.prompt && user.prompt !== filters.prompt) return false;

        // LLM filter
        if (filters.llm && user.llm !== filters.llm) return false;

        // Balance filter
        if (filters.balance) {
            switch (filters.balance) {
                case 'positive': if (user.balance <= 0) return false; break;
                case 'zero': if (user.balance !== 0) return false; break;
                case 'negative': if (user.balance >= 0) return false; break;
            }
        }

        // Role filter
        if (filters.role && user.role !== filters.role) return false;

        return true;
    });

    // Sort users
    filtered.sort((a, b) => {
        let valA = a[sortColumn];
        let valB = b[sortColumn];

        // Handle string comparison
        if (typeof valA === 'string') {
            valA = valA.toLowerCase();
            valB = valB.toLowerCase();
        }

        let result = 0;
        if (valA < valB) result = -1;
        else if (valA > valB) result = 1;

        return sortDirection === 'asc' ? result : -result;
    });

    // Update DOM
    const tbody = document.getElementById('usersTableBody');
    if (tbody) {
        // Hide all rows first
        UsersListState.users.forEach(user => {
            user.element.style.display = 'none';
        });

        // Show and reorder filtered rows
        filtered.forEach(user => {
            user.element.style.display = '';
            tbody.appendChild(user.element);
        });
    }

    // Update stats
    updateStats(filtered.length);

    // Update select all state
    updateSelectAllState();
}

/**
 * Update stats display
 */
function updateStats(filteredCount) {
    const total = UsersListState.users.length;
    const active = UsersListState.users.filter(u => u.status === 'active').length;
    const expired = UsersListState.users.filter(u => u.status === 'expired').length;
    const password = UsersListState.users.filter(u => u.status === 'password').length;
    const noLink = UsersListState.users.filter(u => u.status === 'no_link').length;
    const disabled = UsersListState.users.filter(u => !u.enabled).length;

    document.getElementById('statTotal').textContent = AurvekI18n.formatNumber(total);
    document.getElementById('statActive').textContent = AurvekI18n.formatNumber(active);
    document.getElementById('statExpired').textContent = AurvekI18n.formatNumber(expired);

    const statPassword = document.getElementById('statPassword');
    if (statPassword) statPassword.textContent = AurvekI18n.formatNumber(password);
    const statNoLink = document.getElementById('statNoLink');
    if (statNoLink) statNoLink.textContent = AurvekI18n.formatNumber(noLink);
    const statDisabled = document.getElementById('statDisabled');
    if (statDisabled) statDisabled.textContent = AurvekI18n.formatNumber(disabled);

    const filteredStat = document.getElementById('statFiltered');
    const filteredContainer = document.getElementById('statFilteredContainer');

    if (filteredContainer) {
        if (filteredCount !== total) {
            filteredContainer.style.display = 'inline-flex';
            if (filteredStat) filteredStat.textContent = AurvekI18n.formatNumber(filteredCount);
        } else {
            filteredContainer.style.display = 'none';
        }
    }
}

/**
 * Toggle select all checkbox
 */
function toggleSelectAll() {
    const selectAll = document.getElementById('selectAll');
    const visibleCheckboxes = getVisibleCheckboxes();

    visibleCheckboxes.forEach(cb => {
        cb.checked = selectAll.checked;
    });

    updateSelectedCount();
}

/**
 * Update selected count and button state
 */
function updateSelectedCount() {
    const checkedCount = document.querySelectorAll('.user-checkbox:checked').length;
    const countSpan = document.getElementById('selectedCount');
    const deleteBtn = document.getElementById('deleteBtn');
    const disableBtn = document.getElementById('disableBtn');
    const enableBtn = document.getElementById('enableBtn');

    const number = AurvekI18n.formatNumber(checkedCount);
    if (countSpan) countSpan.textContent = AurvekI18n.t('admin_users.flow.delete_label', {number});
    const disableLabel = document.getElementById('disableSelectedLabel');
    const enableLabel = document.getElementById('enableSelectedLabel');
    if (disableLabel) disableLabel.textContent = AurvekI18n.t('admin_users.flow.disable_label', {number});
    if (enableLabel) enableLabel.textContent = AurvekI18n.t('admin_users.flow.enable_label', {number});
    if (deleteBtn) deleteBtn.disabled = checkedCount === 0;
    if (disableBtn) disableBtn.disabled = checkedCount === 0;
    if (enableBtn) enableBtn.disabled = checkedCount === 0;

    updateSelectAllState();
}

/**
 * Update select all checkbox state based on visible selections
 */
function updateSelectAllState() {
    const selectAll = document.getElementById('selectAll');
    if (!selectAll) return;

    const visibleCheckboxes = getVisibleCheckboxes();
    const checkedVisible = visibleCheckboxes.filter(cb => cb.checked).length;

    selectAll.checked = visibleCheckboxes.length > 0 && checkedVisible === visibleCheckboxes.length;
    selectAll.indeterminate = checkedVisible > 0 && checkedVisible < visibleCheckboxes.length;
}

/**
 * Get visible (not hidden by filter) checkboxes
 */
function getVisibleCheckboxes() {
    return Array.from(document.querySelectorAll('.user-checkbox')).filter(cb => {
        return cb.closest('tr').style.display !== 'none';
    });
}

/**
 * Show magic link for a user
 */
function showMagicLink(magicLink, username, isExpired, isPasswordOnly) {
    const container = document.getElementById('magicLinkContainer');
    const input = document.getElementById('magicLink');
    const actionButton = document.getElementById('actionButton');
    const title = document.getElementById('usernameMagicLink');

    if (!container || !input || !actionButton || !title) return;

    title.textContent = AurvekI18n.t('admin_users.flow.magic_link_for', {name: username});
    container.style.display = 'block';

    const copyMessage = document.getElementById('copyMessage');
    if (copyMessage) copyMessage.style.display = 'none';

    if (!magicLink) {
        input.value = isPasswordOnly
            ? AurvekI18n.t('admin_users.no_magic_link_password_only_user')
            : AurvekI18n.t('admin_users.no_magic_link_generated');
        input.style.opacity = '0.6';
        actionButton.innerHTML = '<i class="fas fa-magic me-1"></i> ' + AurvekI18n.t('admin_users.generate') + '';
        actionButton.className = 'btn btn-info';
        actionButton.onclick = function() { renewMagicLink(username); };
    } else if (isExpired) {
        input.value = magicLink;
        input.style.opacity = '1';
        actionButton.innerHTML = '<i class="fas fa-sync me-1"></i> ' + AurvekI18n.t('admin_users.renew') + '';
        actionButton.className = 'btn btn-warning';
        actionButton.onclick = function() { renewMagicLink(username); };
    } else {
        input.value = magicLink;
        input.style.opacity = '1';
        actionButton.innerHTML = '<i class="fas fa-copy me-1"></i> ' + AurvekI18n.t('admin_users.copy') + '';
        actionButton.className = 'btn btn-success';
        actionButton.onclick = copyToClipboard;
    }

    window.scrollTo({ top: 0, behavior: 'smooth' });
}

/**
 * Close magic link container
 */
function closeMagicLink() {
    const container = document.getElementById('magicLinkContainer');
    if (container) container.style.display = 'none';
}

/**
 * Copy magic link to clipboard
 */
function copyToClipboard() {
    const input = document.getElementById('magicLink');
    if (!input) return;

    input.select();
    document.execCommand('copy');

    const copyMessage = document.getElementById('copyMessage');
    if (copyMessage) copyMessage.style.display = 'block';

    const actionButton = document.getElementById('actionButton');
    if (actionButton) {
        actionButton.innerHTML = '<i class="fas fa-check me-1"></i> ' + AurvekI18n.t('admin_users.copied') + '';
        setTimeout(() => {
            actionButton.innerHTML = '<i class="fas fa-copy me-1"></i> ' + AurvekI18n.t('admin_users.copy') + '';
        }, 2000);
    }
}

/**
 * Renew magic link for a user
 */
async function renewMagicLink(username) {
    const actionButton = document.getElementById('actionButton');
    if (!actionButton) return;

    actionButton.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> ' + AurvekI18n.t('admin_users.renewing') + '';
    actionButton.disabled = true;

    try {
        const response = await secureFetch('/admin/renew-token/' + encodeURIComponent(username), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        if (!response) {
            actionButton.disabled = false;
            actionButton.innerHTML = '<i class="fas fa-sync me-1"></i> ' + AurvekI18n.t('admin_users.renew') + '';
            return;
        }

        const data = await response.json();
        actionButton.disabled = false;

        if (data.error) {
            NotificationModal.error(AurvekI18n.t('admin_users.renewal_failed'), data.error);
            actionButton.innerHTML = '<i class="fas fa-sync me-1"></i> ' + AurvekI18n.t('admin_users.renew') + '';
        } else {
            showMagicLink(data.magic_link, username, false, false);

            // Update the row status
            const row = document.querySelector(`tr[data-username="${username}"]`);
            if (row) {
                row.dataset.status = 'active';
                row.dataset.magicLink = data.magic_link;
                const statusCell = row.querySelector('.status-cell');
                if (statusCell) {
                    statusCell.innerHTML = '<span class="status-active" title="' + escapeHtml(AurvekI18n.t('admin_users.magic_link_active')) + '"><i class="fas fa-check-circle"></i></span>';
                }
                const usernameLink = row.querySelector('.username-link');
                if (usernameLink) {
                    usernameLink.onclick = function() {
                        showMagicLink(data.magic_link, username, false, false);
                    };
                }

                // Update state
                const user = UsersListState.users.find(u => u.username === username);
                if (user) {
                    user.status = 'active';
                    user.magicLink = data.magic_link;
                }

                // Recalculate stats
                updateStats(getVisibleCheckboxes().length);
            }
        }
    } catch (error) {
        console.error('Error:', error);
        actionButton.disabled = false;
        actionButton.innerHTML = '<i class="fas fa-sync me-1"></i> ' + AurvekI18n.t('admin_users.renew') + '';
        NotificationModal.error(AurvekI18n.t('admin_users.error'), AurvekI18n.t('admin_users.an_error_occurred_while_renewing_the_magic_link'));
    }
}

function getUserStateByUsername(username) {
    return UsersListState.users.find(user => user.username === username);
}

function renderAccountStatusHtml(isEnabled) {
    return isEnabled
        ? '<span class="status-active" title="' + escapeHtml(AurvekI18n.t('admin_users.account_enabled')) + '"><i class="fas fa-user-check"></i></span>'
        : '<span class="status-disabled" title="' + escapeHtml(AurvekI18n.t('admin_users.account_disabled')) + '"><i class="fas fa-user-slash"></i></span>';
}

function updateActivationButton(button, isEnabled) {
    if (!button) return;

    button.dataset.enabled = isEnabled ? 'true' : 'false';
    button.title = isEnabled ? AurvekI18n.t('admin_users.deactivate_user') : AurvekI18n.t('admin_users.activate_user');
    button.classList.toggle('btn-outline-warning', isEnabled);
    button.classList.toggle('btn-outline-success', !isEnabled);
    button.innerHTML = isEnabled
        ? '<i class="fas fa-user-slash"></i>'
        : '<i class="fas fa-user-check"></i>';
}

function setActivationControlsDisabled(disabled) {
    document.querySelectorAll('.activation-toggle').forEach(button => {
        button.disabled = disabled;
    });

    const disableBtn = document.getElementById('disableBtn');
    const enableBtn = document.getElementById('enableBtn');
    if (disableBtn) disableBtn.disabled = disabled;
    if (enableBtn) enableBtn.disabled = disabled;
}

function updateUserActivationInUI(username, isEnabled) {
    const user = getUserStateByUsername(username);
    const row = user?.element;
    if (!row) return;

    row.dataset.enabled = isEnabled ? 'true' : 'false';
    row.dataset.account = isEnabled ? 'enabled' : 'disabled';
    row.classList.toggle('user-disabled', !isEnabled);

    const accountCell = row.querySelector('.account-cell');
    if (accountCell) accountCell.innerHTML = renderAccountStatusHtml(isEnabled);

    const activationButton = row.querySelector('.activation-toggle');
    updateActivationButton(activationButton, isEnabled);

    user.enabled = isEnabled;
    user.account = isEnabled ? 'enabled' : 'disabled';
}

async function setUsersActivation(usernames, enabled) {
    setActivationControlsDisabled(true);

    const errors = [];
    let updatedCount = 0;

    for (const username of usernames) {
        try {
            const response = await secureFetch('/admin/users/' + encodeURIComponent(username) + '/activation', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled })
            });
            if (!response) {
                setActivationControlsDisabled(false);
                updateSelectedCount();
                return;
            }

            const data = await response.json();
            if (response.ok && data.success) {
                updateUserActivationInUI(data.username || username, Boolean(data.is_enabled));
                updatedCount++;
            } else {
                errors.push(`${username}: ${data.error || data.detail || AurvekI18n.t('admin_users.request_failed')}`);
            }
        } catch (error) {
            errors.push(`${username}: ${AurvekI18n.t('admin_users.an_unexpected_error_occurred')}`);
        }
    }

    setActivationControlsDisabled(false);
    updateSelectedCount();
    applyFiltersAndSort();

    if (updatedCount > 0) {
        NotificationModal.success(AurvekI18n.t('admin_users.users_updated'), AurvekI18n.t(enabled ? 'admin_users.flow.enabled_count' : 'admin_users.flow.disabled_count', {count: updatedCount}));
    }
    if (errors.length > 0) {
        NotificationModal.error(AurvekI18n.t('admin_users.activation_update_failed'), errors.join('\n'));
    }
}

function toggleUserActivation(button) {
    const username = button?.dataset?.username;
    if (!username) return;

    const currentlyEnabled = button.dataset.enabled === 'true';
    const enabled = !currentlyEnabled;
    const action = enabled ? AurvekI18n.t('admin_users.enable_2') : AurvekI18n.t('admin_users.disable_2');
    const message = enabled
        ? AurvekI18n.t('admin_users.flow.enable_confirm', {name: username})
        : AurvekI18n.t('admin_users.flow.disable_confirm', {name: username});

    NotificationModal.confirm(
        AurvekI18n.t(enabled ? 'admin_users.activate_user' : 'admin_users.deactivate_user'),
        message,
        () => setUsersActivation([username], enabled),
        null,
        { type: enabled ? 'success' : 'warning', confirmText: action }
    );
}

function setSelectedUsersActivation(enabled) {
    const usernames = Array.from(document.querySelectorAll('.user-checkbox:checked')).map(cb => cb.value);
    if (usernames.length === 0) return;

    const action = enabled ? AurvekI18n.t('admin_users.enable_2') : AurvekI18n.t('admin_users.disable_2');
    const message = enabled
        ? AurvekI18n.t('admin_users.flow.enable_selected', {count: usernames.length})
        : AurvekI18n.t('admin_users.flow.disable_selected', {count: usernames.length});

    NotificationModal.confirm(
        AurvekI18n.t(enabled ? 'admin_users.flow.enable_users' : 'admin_users.flow.disable_users'),
        message,
        () => setUsersActivation(usernames, enabled),
        null,
        { type: enabled ? 'success' : 'warning', confirmText: action }
    );
}

/**
 * Reset all filters
 */
function resetFilters() {
    document.getElementById('filterSearch').value = '';
    document.getElementById('filterStatus').value = '';
    document.getElementById('filterAccount').value = '';
    document.getElementById('filterPrompt').value = '';
    document.getElementById('filterLLM').value = '';
    document.getElementById('filterBalance').value = '';
    document.getElementById('filterRole').value = '';

    UsersListState.filters = {
        search: '',
        status: '',
        account: '',
        prompt: '',
        llm: '',
        balance: '',
        role: ''
    };

    applyFiltersAndSort();
}

// Utility functions

/**
 * Debounce function for search input
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Copy phone number to clipboard
 */
function copyPhone(element) {
    const phone = element.dataset.phone;
    if (!phone) return;

    navigator.clipboard.writeText(phone).then(() => {
        // Visual feedback
        const icon = element.querySelector('i');
        const originalClass = icon.className;
        icon.className = 'fas fa-check';
        element.classList.add('copied');

        setTimeout(() => {
            icon.className = originalClass;
            element.classList.remove('copied');
        }, 1500);
    }).catch(err => {
        console.error('Failed to copy phone:', err);
    });
}

/**
 * Format number with K, M, B abbreviations
 */
function formatNumber(num) {
    return AurvekI18n.formatNumber(num, {notation: 'compact', maximumFractionDigits: 1});
}

// ==========================================================
// Ultra Admin+ — Privilege Elevation
// ==========================================================

let ultraAdminCountdown = null;

/**
 * Initialize Ultra Admin+ state on page load
 */
function initUltraAdminState() {
    const btn = document.getElementById('ultraAdminBtn');
    if (!btn) return;

    const isElevated = btn.dataset.elevated === 'true';
    const ttl = parseInt(btn.dataset.ttl) || 0;

    if (isElevated && ttl > 0) {
        activateUltraAdminUI(ttl);
    }
}

/**
 * Open the Ultra Admin+ modal
 */
function openUltraAdminModal() {
    document.getElementById('ultraAdminStep1').style.display = 'block';
    document.getElementById('ultraAdminStep2').style.display = 'none';
    const codeInput = document.getElementById('ultraAdminCodeInput');
    if (codeInput) codeInput.value = '';
    new bootstrap.Modal(document.getElementById('ultraAdminModal')).show();
}

/**
 * Request elevation code from server
 */
async function requestUltraAdminCode() {
    try {
        const response = await secureFetch('/api/ultra-admin/request-code', { method: 'POST' });
        if (!response) return;

        const data = await response.json();

        if (response.ok) {
            document.getElementById('ultraAdminStep1').style.display = 'none';
            document.getElementById('ultraAdminStep2').style.display = 'block';
            document.getElementById('ultraAdminEmailHint').textContent = AurvekI18n.t('admin_users.flow.code_sent', {email: data.email_hint || ''});
            document.getElementById('ultraAdminCodeInput').focus();
        } else if (response.status === 429) {
            NotificationModal.warning(AurvekI18n.t('admin_users.cooldown'), data.error || AurvekI18n.t('admin_users.please_wait_before_requesting_another_code'));
        } else if (response.status === 409) {
            NotificationModal.warning(AurvekI18n.t('admin_users.unavailable'), data.error || AurvekI18n.t('admin_users.another_admin_is_currently_elevated'));
        } else {
            NotificationModal.error(AurvekI18n.t('admin_users.error'), data.error || AurvekI18n.t('admin_users.could_not_send_code'));
        }
    } catch (error) {
        NotificationModal.error(AurvekI18n.t('admin_users.error'), AurvekI18n.t('admin_users.unexpected_error_requesting_code'));
    }
}

/**
 * Verify the entered code
 */
async function verifyUltraAdminCode() {
    const code = document.getElementById('ultraAdminCodeInput').value.trim();
    if (code.length !== 6 || !/^\d{6}$/.test(code)) {
        NotificationModal.warning(AurvekI18n.t('admin_users.invalid_code'), AurvekI18n.t('admin_users.enter_a_6_digit_numeric_code'));
        return;
    }

    try {
        const response = await secureFetch('/api/ultra-admin/verify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        if (!response) return;

        const data = await response.json();

        if (response.ok && data.status === 'elevated') {
            const modal = bootstrap.Modal.getInstance(document.getElementById('ultraAdminModal'));
            if (modal) modal.hide();
            NotificationModal.success(AurvekI18n.t('admin_users.ultra_admin_active'), AurvekI18n.t('admin_users.elevated_privileges_granted_for_30_minutes'));
            activateUltraAdminUI(data.ttl);
        } else {
            NotificationModal.error(AurvekI18n.t('admin_users.verification_failed'), data.error || AurvekI18n.t('admin_users.incorrect_code'));
            document.getElementById('ultraAdminCodeInput').value = '';
            document.getElementById('ultraAdminCodeInput').focus();
        }
    } catch (error) {
        NotificationModal.error(AurvekI18n.t('admin_users.error'), AurvekI18n.t('admin_users.unexpected_error_verifying_code'));
    }
}

/**
 * Activate the Ultra Admin+ UI (button changes to active state with countdown)
 */
function activateUltraAdminUI(ttlSeconds) {
    const btn = document.getElementById('ultraAdminBtn');
    if (!btn) return;

    btn.classList.remove('btn-outline-warning', 'btn-sm');
    btn.classList.add('btn-warning', 'btn-sm');
    btn.onclick = null;
    btn.style.cursor = 'default';

    let remaining = ttlSeconds;
    updateUltraAdminTimer(btn, remaining);

    ultraAdminCountdown = setInterval(() => {
        remaining--;
        if (remaining <= 0) {
            deactivateUltraAdminUI();
        } else {
            updateUltraAdminTimer(btn, remaining);
        }
    }, 1000);
}

/**
 * Update the countdown timer display
 */
function updateUltraAdminTimer(btn, seconds) {
    const min = Math.floor(seconds / 60);
    const sec = seconds % 60;
    btn.innerHTML =
        '<i class="fas fa-bolt me-1"></i>' +
        escapeHtml(AurvekI18n.t('admin_users.flow.timer', {time: AurvekI18n.formatNumber(min) + ':' + AurvekI18n.formatNumber(sec, {minimumIntegerDigits: 2})})) + ' ' +
        '<i class="fas fa-times-circle ms-1" style="cursor:pointer;" onclick="revokeUltraAdmin(event)" title="' + escapeHtml(AurvekI18n.t('admin_users.deactivate_user')) + '"></i>';
}

/**
 * Deactivate the Ultra Admin+ UI (return to normal state)
 */
function deactivateUltraAdminUI() {
    clearInterval(ultraAdminCountdown);
    ultraAdminCountdown = null;

    const btn = document.getElementById('ultraAdminBtn');
    if (!btn) return;

    btn.classList.remove('btn-warning');
    btn.classList.add('btn-outline-warning');
    btn.innerHTML = '<i class="fas fa-bolt me-1"></i><span id="ultraAdminLabel">' + AurvekI18n.t('admin_users.ultra_admin') + '</span>';
    btn.onclick = openUltraAdminModal;
    btn.style.cursor = 'pointer';
}

/**
 * Revoke Ultra Admin+ elevation
 */
async function revokeUltraAdmin(event) {
    if (event) event.stopPropagation();
    try {
        await secureFetch('/api/ultra-admin/revoke', { method: 'POST' });
        deactivateUltraAdminUI();
        NotificationModal.info(AurvekI18n.t('admin_users.ultra_admin_deactivated'), AurvekI18n.t('admin_users.privileges_returned_to_normal'));
    } catch (error) {
        NotificationModal.error(AurvekI18n.t('admin_users.error'), AurvekI18n.t('admin_users.could_not_revoke_elevation'));
    }
}
