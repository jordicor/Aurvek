/**
 * Prompt List - Management functionality
 * Handles filtering, sorting, selection and actions for prompts table
 */

// State management
const PromptListState = {
    prompts: [],
    sortColumn: 'name',
    sortDirection: 'asc',
    filters: {
        search: '',
        status: '',
        voice: '',
        owner: ''
    }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', function() {
    initializePromptsData();
    populateFilterDropdowns();
    setupEventListeners();
    applyFiltersAndSort();
});

/**
 * Extract prompts data from table rows into state
 */
function initializePromptsData() {
    const rows = document.querySelectorAll('#promptsTableBody tr[data-id]');
    PromptListState.prompts = Array.from(rows).map(row => {
        return {
            element: row,
            id: row.dataset.id,
            name: row.dataset.name || '',
            description: row.dataset.description || '',
            voice: row.dataset.voice || '',
            public: row.dataset.public,
            owner: row.dataset.owner || ''
        };
    });
}

/**
 * Populate filter dropdowns with unique values from data
 */
function populateFilterDropdowns() {
    // Voices
    const voices = [...new Set(PromptListState.prompts.map(p => p.voice))].filter(v => v).sort();

    // Owners (only if element exists - admin only)
    const owners = [...new Set(PromptListState.prompts.map(p => p.owner))].filter(o => o).sort();

    const voiceSelect = document.getElementById('filterVoice');
    const ownerSelect = document.getElementById('filterOwner');

    if (voiceSelect) {
        voiceSelect.replaceChildren(new Option(AurvekI18n.t('creator.filters.all_voices'), ''));
        voices.forEach(v => {
            voiceSelect.add(new Option(v, v));
        });
    }

    if (ownerSelect) {
        ownerSelect.replaceChildren(new Option(AurvekI18n.t('creator.filters.all_owners'), ''));
        owners.forEach(o => {
            ownerSelect.add(new Option(o, o));
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
    const voiceSelect = document.getElementById('filterVoice');
    const ownerSelect = document.getElementById('filterOwner');

    if (searchInput) searchInput.addEventListener('input', debounce(onFilterChange, 200));
    if (statusSelect) statusSelect.addEventListener('change', onFilterChange);
    if (voiceSelect) voiceSelect.addEventListener('change', onFilterChange);
    if (ownerSelect) ownerSelect.addEventListener('change', onFilterChange);

    // Sortable headers
    document.querySelectorAll('th[data-sort]').forEach(th => {
        th.addEventListener('click', () => onSortClick(th.dataset.sort));
    });

    // Select all checkbox
    const selectAll = document.getElementById('selectAll');
    if (selectAll) selectAll.addEventListener('change', toggleSelectAll);

    // Individual checkboxes
    document.querySelectorAll('.prompt-checkbox').forEach(cb => {
        cb.addEventListener('change', updateSelectedCount);
    });

    // Delete form confirmation
    const promptsForm = document.getElementById('promptsForm');
    if (promptsForm) {
        let confirmedSubmit = false;
        promptsForm.addEventListener('submit', function(e) {
            const count = document.querySelectorAll('.prompt-checkbox:checked').length;
            if (count === 0) {
                e.preventDefault();
                return;
            }
            if (confirmedSubmit) {
                confirmedSubmit = false;
                return;
            }
            e.preventDefault();
            const form = this;
            NotificationModal.confirm(AurvekI18n.t('creator.prompts.delete_many_title'), AurvekI18n.t('creator.prompts.delete_many_confirm', { count }), () => {
                confirmedSubmit = true;
                form.requestSubmit();
            }, null, { type: 'error', confirmText: AurvekI18n.t('creator.action.delete') });
        });
    }
}

/**
 * Handle filter change
 */
function onFilterChange() {
    PromptListState.filters.search = (document.getElementById('filterSearch')?.value || '').toLowerCase();
    PromptListState.filters.status = document.getElementById('filterStatus')?.value || '';
    PromptListState.filters.voice = document.getElementById('filterVoice')?.value || '';
    PromptListState.filters.owner = document.getElementById('filterOwner')?.value || '';

    applyFiltersAndSort();
}

/**
 * Handle sort header click
 */
function onSortClick(column) {
    if (PromptListState.sortColumn === column) {
        PromptListState.sortDirection = PromptListState.sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        PromptListState.sortColumn = column;
        PromptListState.sortDirection = 'asc';
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
        if (th.dataset.sort === PromptListState.sortColumn) {
            th.classList.add(PromptListState.sortDirection === 'asc' ? 'sort-asc' : 'sort-desc');
        }
    });
}

/**
 * Apply filters and sorting to table
 */
function applyFiltersAndSort() {
    const { filters, sortColumn, sortDirection } = PromptListState;

    // Filter prompts
    let filtered = PromptListState.prompts.filter(prompt => {
        // Search filter (name or description)
        if (filters.search) {
            const searchMatch = prompt.name.toLowerCase().includes(filters.search) ||
                               prompt.description.toLowerCase().includes(filters.search);
            if (!searchMatch) return false;
        }

        // Status filter (public/private)
        if (filters.status && prompt.public !== filters.status) return false;

        // Voice filter
        if (filters.voice && prompt.voice !== filters.voice) return false;

        // Owner filter
        if (filters.owner && prompt.owner !== filters.owner) return false;

        return true;
    });

    // Sort prompts
    filtered.sort((a, b) => {
        let valA, valB;

        switch (sortColumn) {
            case 'name':
                valA = a.name.toLowerCase();
                valB = b.name.toLowerCase();
                break;
            case 'voice':
                valA = a.voice.toLowerCase();
                valB = b.voice.toLowerCase();
                break;
            case 'public':
                valA = a.public;
                valB = b.public;
                break;
            case 'owner':
                valA = a.owner.toLowerCase();
                valB = b.owner.toLowerCase();
                break;
            default:
                valA = a.name.toLowerCase();
                valB = b.name.toLowerCase();
        }

        let result = 0;
        if (valA < valB) result = -1;
        else if (valA > valB) result = 1;

        return sortDirection === 'asc' ? result : -result;
    });

    // Update DOM
    const tbody = document.getElementById('promptsTableBody');
    if (tbody) {
        // Hide all rows first
        PromptListState.prompts.forEach(prompt => {
            prompt.element.style.display = 'none';
        });

        // Show and reorder filtered rows
        filtered.forEach(prompt => {
            prompt.element.style.display = '';
            tbody.appendChild(prompt.element);
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
    const total = PromptListState.prompts.length;
    const publicCount = PromptListState.prompts.filter(p => p.public === 'public').length;
    const privateCount = PromptListState.prompts.filter(p => p.public === 'private').length;

    const statTotal = document.getElementById('statTotal');
    const statPublic = document.getElementById('statPublic');
    const statPrivate = document.getElementById('statPrivate');

    if (statTotal) statTotal.textContent = AurvekI18n.t('creator.stats.total_count', {count: AurvekI18n.formatNumber(total)});
    if (statPublic) statPublic.textContent = AurvekI18n.t('creator.stats.public_count', {count: AurvekI18n.formatNumber(publicCount)});
    if (statPrivate) statPrivate.textContent = AurvekI18n.t('creator.stats.private_count', {count: AurvekI18n.formatNumber(privateCount)});

    const filteredStat = document.getElementById('statFiltered');
    const filteredContainer = document.getElementById('statFilteredContainer');

    if (filteredContainer) {
        if (filteredCount !== total) {
            filteredContainer.style.display = 'inline-flex';
            if (filteredStat) filteredStat.textContent = AurvekI18n.t('creator.stats.shown_count', {count: AurvekI18n.formatNumber(filteredCount)});
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
    const checkedCount = document.querySelectorAll('.prompt-checkbox:checked').length;
    const countSpan = document.getElementById('selectedCount');
    const deleteBtn = document.getElementById('deleteBtn');

    if (countSpan) countSpan.textContent = AurvekI18n.t('creator.action.delete_count', {count: AurvekI18n.formatNumber(checkedCount)});
    if (deleteBtn) deleteBtn.disabled = checkedCount === 0;

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
    return Array.from(document.querySelectorAll('.prompt-checkbox')).filter(cb => {
        return cb.closest('tr').style.display !== 'none';
    });
}

/**
 * Reset all filters
 */
function resetFilters() {
    const searchInput = document.getElementById('filterSearch');
    const statusSelect = document.getElementById('filterStatus');
    const voiceSelect = document.getElementById('filterVoice');
    const ownerSelect = document.getElementById('filterOwner');

    if (searchInput) searchInput.value = '';
    if (statusSelect) statusSelect.value = '';
    if (voiceSelect) voiceSelect.value = '';
    if (ownerSelect) ownerSelect.value = '';

    PromptListState.filters = {
        search: '',
        status: '',
        voice: '',
        owner: ''
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
    return div.innerHTML;
}
