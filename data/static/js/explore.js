const mt = (key, params) => AurvekI18n.t('marketplace.' + key, params);
const mh = (key, params) => escapeAttr(mt(key, params));

/* ============================================================
   PROMPT & PACK EXPLORER - Frontend Logic
   Vanilla JS: fetch, filter, paginate, detail modal
   ============================================================ */

const ExploreState = {
    activeTab: 'prompts',    // 'prompts' | 'packs'
    activeFilter: null,      // 'mine' | 'favorites' | null
    prompts: [],
    packs: [],
    categories: [],
    activeCategory: null,
    searchQuery: '',
    currentPage: 1,
    totalPages: 1,
    total: 0,
    limit: 24,
    modalItem: null,
    modalType: null,
    modalLandingUrl: '',
    iframeActive: false,
    iframeLandingItems: [],  // items with landings (for left/right nav)
    iframeCurrentIndex: 0,
    previewItem: null,
    previewType: null,
    purchaseRequestPending: false
};

const ExploreRequests = {
    prompts: { generation: 0, controller: null },
    packs: { generation: 0, controller: null }
};

// Debounce utility
function debounce(fn, ms) {
    let timer;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}

// ============================================================
// INITIALIZATION
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    loadCategories();
    loadPrompts();
    setupEventListeners();
});

function setupEventListeners() {
    window.addEventListener('message', handleLandingPurchaseRequest);
    const searchInput = document.getElementById('exploreSearch');
    if (searchInput) {
        const loadSearchResults = debounce(() => {
            if (ExploreState.activeTab === 'prompts') {
                loadPrompts();
            } else {
                loadPacks();
            }
        }, 300);

        searchInput.addEventListener('input', () => {
            ExploreState.searchQuery = searchInput.value;
            ExploreState.currentPage = 1;
            invalidateExploreRequests();
            loadSearchResults();
        });
    }

    const categoryChips = document.getElementById('categoryChips');
    if (categoryChips) {
        categoryChips.addEventListener('click', (event) => {
            const chip = event.target.closest('[data-chip-action]');
            if (!chip) return;

            if (chip.dataset.chipAction === 'category') {
                const categoryId = chip.dataset.categoryId
                    ? Number(chip.dataset.categoryId)
                    : null;
                selectCategory(categoryId, chip);
            } else {
                selectFilter(chip.dataset.filter || null, chip);
            }
        });
    }

    const grid = document.getElementById('exploreGrid');
    if (grid) {
        grid.addEventListener('click', (event) => {
            const favoriteButton = event.target.closest('[data-favorite-prompt-id]');
            if (favoriteButton) {
                event.stopPropagation();
                toggleExploreFavorite(
                    Number(favoriteButton.dataset.favoritePromptId),
                    favoriteButton
                );
                return;
            }

            const card = event.target.closest('[data-explore-item-id]');
            if (!card) return;
            const items = card.dataset.exploreItemType === 'prompt'
                ? ExploreState.prompts
                : ExploreState.packs;
            const item = items.find(candidate => (
                String(candidate.id) === card.dataset.exploreItemId
            ));
            if (item) openExploreItem(item, card.dataset.exploreItemType);
        });
        grid.addEventListener('error', handleExploreImageError, true);
    }

    const pagination = document.getElementById('explorePagination');
    if (pagination) {
        pagination.addEventListener('click', (event) => {
            const button = event.target.closest('[data-page]');
            if (button && !button.disabled) goToPage(Number(button.dataset.page));
        });
    }

    const modalContent = document.getElementById('modalContent');
    if (modalContent) {
        modalContent.addEventListener('click', handleModalAction);
        modalContent.addEventListener('error', handleExploreImageError, true);
    }

    // Close modal on backdrop click
    const backdrop = document.getElementById('exploreModalBackdrop');
    if (backdrop) {
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) closeModal();
        });
    }

    // Close modal on Escape key
    document.addEventListener('keydown', (e) => {
        if (ExploreState.iframeActive) {
            if (e.key === 'Escape') closeLandingPreview();
            else if (e.key === 'ArrowLeft') navigatePreview(-1);
            else if (e.key === 'ArrowRight') navigatePreview(1);
            return;
        }
        if (e.key === 'Escape') closeModal();
    });
}

function handleLandingPurchaseRequest(event) {
    const iframe = document.getElementById('landingPreviewIframe');
    const item = ExploreState.previewItem;
    if (
        !ExploreState.iframeActive ||
        ExploreState.previewType !== 'prompt' ||
        !iframe ||
        event.source !== iframe.contentWindow ||
        !event.data ||
        event.data.type !== 'aurvek-purchase-request' ||
        Number(event.data.promptId) !== Number(item && item.id) ||
        ExploreState.purchaseRequestPending
    ) {
        return;
    }

    ExploreState.purchaseRequestPending = true;
    const resetPending = () => {
        ExploreState.purchaseRequestPending = false;
    };
    NotificationModal.confirm(
        mt('explore.checkout_title'),
        mt('explore.checkout_confirm', {name: item.name || mt('explore.this_prompt')}),
        () => {
            resetPending();
            window.location.assign(`/purchase/prompt/${Number(item.id)}`);
        },
        resetPending
    );
}

// ============================================================
// TAB SWITCHING
// ============================================================

function switchTab(tab, tabEl) {
    if (tab === ExploreState.activeTab) return;
    ExploreState.activeTab = tab;
    ExploreState.currentPage = 1;
    ExploreState.searchQuery = '';
    ExploreState.activeCategory = null;
    ExploreState.activeFilter = null;

    // Reset search input
    const searchInput = document.getElementById('exploreSearch');
    if (searchInput) {
        searchInput.value = '';
        searchInput.placeholder = tab === 'prompts' ? mt('explore.search') : mt('explore.search_packs');
        searchInput.setAttribute('aria-label', searchInput.placeholder);
    }

    // Update tab buttons
    document.querySelectorAll('.explore-tab').forEach(t => t.classList.remove('active'));
    if (tabEl) tabEl.classList.add('active');

    // Render appropriate chips and load content
    if (tab === 'prompts') {
        renderCategories();
        loadPrompts();
    } else {
        renderPackChips();
        loadPacks();
    }
}

// ============================================================
// DATA FETCHING
// ============================================================

async function loadCategories() {
    try {
        const res = await fetch('/api/explore/categories');
        if (!res.ok) return;
        const data = await res.json();
        ExploreState.categories = data;
        if (ExploreState.activeTab === 'prompts') renderCategories();
    } catch (err) {
        console.error('Failed to load categories:', err);
    }
}

function invalidateExploreRequests() {
    Object.values(ExploreRequests).forEach(requestState => {
        requestState.generation += 1;
        if (requestState.controller) requestState.controller.abort();
        requestState.controller = null;
    });
}

function startExploreRequest(resource) {
    Object.entries(ExploreRequests).forEach(([name, requestState]) => {
        if (requestState.controller) requestState.controller.abort();
        requestState.controller = null;
        if (name !== resource) requestState.generation += 1;
    });

    const requestState = ExploreRequests[resource];
    requestState.generation += 1;
    const controller = new AbortController();
    requestState.controller = controller;

    return {
        resource,
        generation: requestState.generation,
        controller,
        snapshot: {
            activeTab: ExploreState.activeTab,
            activeFilter: ExploreState.activeFilter,
            activeCategory: ExploreState.activeCategory,
            searchQuery: ExploreState.searchQuery.trim(),
            currentPage: ExploreState.currentPage,
            limit: ExploreState.limit
        }
    };
}

function isExploreRequestCurrent(request) {
    const requestState = ExploreRequests[request.resource];
    const snapshot = request.snapshot;
    return requestState.generation === request.generation
        && requestState.controller === request.controller
        && !request.controller.signal.aborted
        && ExploreState.activeTab === request.resource
        && ExploreState.activeTab === snapshot.activeTab
        && ExploreState.activeFilter === snapshot.activeFilter
        && ExploreState.activeCategory === snapshot.activeCategory
        && ExploreState.searchQuery.trim() === snapshot.searchQuery
        && ExploreState.currentPage === snapshot.currentPage
        && ExploreState.limit === snapshot.limit;
}

function finishExploreRequest(request) {
    const requestState = ExploreRequests[request.resource];
    if (
        requestState.generation !== request.generation
        || requestState.controller !== request.controller
    ) {
        return;
    }
    requestState.controller = null;
    if (ExploreState.activeTab === request.resource) showLoading(false);
}

async function loadPrompts() {
    const request = startExploreRequest('prompts');
    showLoading(true);

    const params = new URLSearchParams({
        page: request.snapshot.currentPage,
        limit: request.snapshot.limit
    });

    if (request.snapshot.activeCategory) {
        params.set('category', request.snapshot.activeCategory);
    }
    if (request.snapshot.activeFilter === 'mine') {
        params.set('mine', '1');
    }
    if (request.snapshot.activeFilter === 'favorites') {
        params.set('favorites', '1');
    }
    if (request.snapshot.searchQuery) {
        params.set('search', request.snapshot.searchQuery);
    }

    try {
        const res = await fetch(`/api/explore/prompts?${params}`, {
            signal: request.controller.signal
        });
        if (!res.ok) throw new Error('Failed to fetch prompts');
        const data = await res.json();
        if (!isExploreRequestCurrent(request)) return;

        ExploreState.prompts = data.prompts;
        ExploreState.total = data.total;
        ExploreState.totalPages = data.total_pages;
        ExploreState.currentPage = data.page;

        renderPrompts();
        renderPagination();
        updateResultsBar();
        buildLandingItemsList();
    } catch (err) {
        if (err.name !== 'AbortError' && isExploreRequestCurrent(request)) {
            console.error('Failed to load prompts:', err);
            showEmptyState(mt('explore.load_prompts_error'));
        }
    } finally {
        finishExploreRequest(request);
    }
}

async function loadPacks() {
    const request = startExploreRequest('packs');
    showLoading(true);

    const params = new URLSearchParams({
        page: request.snapshot.currentPage,
        limit: request.snapshot.limit
    });

    if (request.snapshot.activeFilter === 'mine') {
        params.set('mine', '1');
    }
    if (request.snapshot.searchQuery) {
        params.set('search', request.snapshot.searchQuery);
    }

    try {
        const res = await fetch(`/api/explore/packs?${params}`, {
            signal: request.controller.signal
        });
        if (!res.ok) throw new Error('Failed to fetch packs');
        const data = await res.json();
        if (!isExploreRequestCurrent(request)) return;

        ExploreState.packs = data.packs;
        ExploreState.total = data.total;
        ExploreState.totalPages = data.pages;
        ExploreState.currentPage = data.page;

        renderPacks();
        renderPagination();
        updateResultsBar();
        buildLandingItemsList();
    } catch (err) {
        if (err.name !== 'AbortError' && isExploreRequestCurrent(request)) {
            console.error('Failed to load packs:', err);
            showEmptyState(mt('explore.load_packs_error'));
        }
    } finally {
        finishExploreRequest(request);
    }
}

// ============================================================
// RENDERING - Categories
// ============================================================

function renderCategories() {
    const container = document.getElementById('categoryChips');
    if (!container) return;
    container.style.display = '';

    const noFilter = !ExploreState.activeFilter && !ExploreState.activeCategory;

    // "All" chip
    let html = `<button class="category-chip ${noFilter ? 'active' : ''}" data-chip-action="category">
        <i class="fas fa-globe"></i> ${mh('explore.all')}
    </button>`;

    // Special filter chips
    html += `<button class="category-chip ${ExploreState.activeFilter === 'mine' ? 'active' : ''}" data-chip-action="filter" data-filter="mine">
        <i class="fas fa-user"></i> ${mh('explore.my_prompts')}
    </button>`;
    html += `<button class="category-chip ${ExploreState.activeFilter === 'favorites' ? 'active' : ''}" data-chip-action="filter" data-filter="favorites">
        <i class="fas fa-star"></i> ${mh('explore.favorites')}
    </button>`;

    // Visual divider
    html += '<span class="chip-divider"></span>';

    ExploreState.categories.forEach(cat => {
        if (cat.is_age_restricted) return;
        html += `<button class="category-chip ${ExploreState.activeCategory === cat.id ? 'active' : ''}" data-chip-action="category" data-category-id="${escapeAttr(String(cat.id))}">
            <i class="fas ${escapeAttr(cat.icon || 'fa-tag')}"></i> ${escapeHtml(cat.name)}
            <span class="chip-count">${escapeHtml(AurvekI18n.formatNumber(cat.count))}</span>
        </button>`;
    });

    const ageRestricted = ExploreState.categories.filter(c => c.is_age_restricted);
    if (ageRestricted.length > 0) {
        ageRestricted.forEach(cat => {
            html += `<button class="category-chip ${ExploreState.activeCategory === cat.id ? 'active' : ''}" data-chip-action="category" data-category-id="${escapeAttr(String(cat.id))}" title="${mh('explore.age_restricted')}">
                <i class="fas ${escapeAttr(cat.icon || 'fa-tag')}"></i> ${escapeHtml(cat.name)}
                <span class="chip-count">${escapeHtml(AurvekI18n.formatNumber(cat.count))}</span>
            </button>`;
        });
    }

    container.innerHTML = html;
}

function renderPackChips() {
    const container = document.getElementById('categoryChips');
    if (!container) return;
    container.style.display = '';

    const noFilter = !ExploreState.activeFilter;

    let html = `<button class="category-chip ${noFilter ? 'active' : ''}" data-chip-action="filter">
        <i class="fas fa-globe"></i> ${mh('explore.all')}
    </button>`;
    html += `<button class="category-chip ${ExploreState.activeFilter === 'mine' ? 'active' : ''}" data-chip-action="filter" data-filter="mine">
        <i class="fas fa-user"></i> ${mh('explore.my_packs')}
    </button>`;

    container.innerHTML = html;
}

function selectCategory(categoryId, chipEl) {
    ExploreState.activeCategory = categoryId;
    ExploreState.activeFilter = null;
    ExploreState.currentPage = 1;

    // Update active chip visual
    document.querySelectorAll('.category-chip').forEach(c => c.classList.remove('active'));
    if (chipEl) chipEl.classList.add('active');

    loadPrompts();
}

function selectFilter(filter, chipEl) {
    ExploreState.activeFilter = filter;
    ExploreState.activeCategory = null;
    ExploreState.currentPage = 1;

    // Update active chip visual
    document.querySelectorAll('.category-chip').forEach(c => c.classList.remove('active'));
    if (chipEl) chipEl.classList.add('active');

    if (ExploreState.activeTab === 'prompts') {
        loadPrompts();
    } else {
        loadPacks();
    }
}

// ============================================================
// RENDERING - Prompt Cards
// ============================================================

function renderPrompts() {
    const grid = document.getElementById('exploreGrid');
    if (!grid) return;

    if (ExploreState.prompts.length === 0) {
        showEmptyState(mt('explore.no_prompts'));
        return;
    }

    let html = '';
    ExploreState.prompts.forEach(prompt => {
        const avatarHtml = prompt.image_url
            ? `<img src="${escapeAttr(prompt.image_url)}" alt="" class="card-avatar" loading="lazy" data-placeholder-name="${escapeAttr(prompt.name || '')}">`
            : generatePlaceholder(prompt.name);

        const descSnippet = prompt.description
            ? escapeHtml(prompt.description.substring(0, 120))
            : mh('explore.no_description');

        const tagsHtml = (prompt.categories || [])
            .slice(0, 3)
            .map(c => `<span class="card-tag"><i class="fas ${escapeAttr(c.icon || 'fa-tag')}"></i> ${escapeHtml(c.name)}</span>`)
            .join('');

        let paidBadge = '';
        if (prompt.is_paid && prompt.purchase_price > 0) {
            paidBadge = '<span class="card-paid-badge card-badge-vip">' + mh('explore.vip') + '</span>';
        } else if (prompt.is_paid) {
            paidBadge = '<span class="card-paid-badge card-badge-premium">' + mh('explore.premium') + '</span>';
        }

        let visibilityBadge = '';
        if (ExploreState.activeFilter === 'mine') {
            if (!prompt.is_public) {
                visibilityBadge = '<span class="card-visibility-badge private"><i class="fas fa-lock"></i> ' + mh('explore.private') + '</span>';
            } else if (prompt.is_unlisted) {
                visibilityBadge = '<span class="card-visibility-badge unlisted"><i class="fas fa-eye-slash"></i> ' + mh('explore.unlisted') + '</span>';
            }
        }

        const favClass = prompt.is_favorite ? 'is-favorite' : '';
        const favIcon = prompt.is_favorite ? 'fas' : 'far';

        html += `<div class="prompt-card" data-explore-item-type="prompt" data-explore-item-id="${escapeAttr(String(prompt.id))}">
            ${paidBadge}
            ${visibilityBadge}
            <button class="explore-fav-btn ${favClass}" data-favorite-prompt-id="${escapeAttr(String(prompt.id))}" title="${prompt.is_favorite ? mh('explore.remove_favorite') : mh('explore.add_favorite')}">
                <i class="${favIcon} fa-star"></i>
            </button>
            ${avatarHtml}
            <div class="card-name">${escapeHtml(prompt.name)}</div>
            <div class="card-description">${descSnippet}</div>
            <div class="card-tags">${tagsHtml}</div>
        </div>`;
    });

    grid.innerHTML = html;
}

// ============================================================
// RENDERING - Pack Cards
// ============================================================

function renderPacks() {
    const grid = document.getElementById('exploreGrid');
    if (!grid) return;

    if (ExploreState.packs.length === 0) {
        showEmptyState(mt('explore.no_packs'));
        return;
    }

    let html = '';
    ExploreState.packs.forEach(pack => {
        const coverHtml = pack.has_cover_image
            ? `<div class="pack-card-cover"><img src="/api/packs/${encodeURIComponent(pack.id)}/cover/512" alt="" loading="lazy"></div>`
            : `<div class="pack-card-cover pack-cover-placeholder"><span>${escapeHtml(pack.name ? pack.name.charAt(0).toUpperCase() : '?')}</span></div>`;

        const priceLabel = pack.is_paid ? AurvekI18n.formatCurrency(pack.price) : mt('store.free');
        const itemCount = pack.item_count || 0;
        const creator = pack.created_by_username || mt('explore.unknown');

        const descSnippet = pack.description
            ? escapeHtml(pack.description.substring(0, 100))
            : '';

        let visibilityBadge = '';
        if (ExploreState.activeFilter === 'mine') {
            if (pack.status === 'draft') {
                visibilityBadge = '<span class="card-visibility-badge draft"><i class="fas fa-pencil-alt"></i> ' + mh('explore.draft') + '</span>';
            } else if (!pack.is_public) {
                visibilityBadge = '<span class="card-visibility-badge private"><i class="fas fa-lock"></i> ' + mh('explore.private') + '</span>';
            }
        }

        html += `<div class="pack-card" data-explore-item-type="pack" data-explore-item-id="${escapeAttr(String(pack.id))}">
            ${visibilityBadge}
            ${coverHtml}
            <div class="pack-card-body">
                <div class="card-name">${escapeHtml(pack.name)}</div>
                ${descSnippet ? `<div class="card-description">${descSnippet}</div>` : ''}
                <div class="pack-card-meta">
                    <span>${mh('explore.prompt_count', {count: itemCount, number: AurvekI18n.formatNumber(itemCount)})}</span>
                    <span>${mh('explore.by_creator', {name: '@' + creator})}</span>
                </div>
                <div class="pack-card-price">${escapeHtml(priceLabel)}</div>
            </div>
        </div>`;
    });

    grid.innerHTML = html;
}

// ============================================================
// RENDERING - Shared Helpers
// ============================================================

function generatePlaceholder(name) {
    const initial = name ? name.charAt(0).toUpperCase() : '?';
    return `<div class="card-avatar-placeholder">${escapeHtml(initial)}</div>`;
}

function handleExploreImageError(event) {
    const image = event.target;
    if (!image.classList.contains('card-avatar') && !image.classList.contains('modal-avatar')) {
        return;
    }

    const placeholder = document.createElement('div');
    placeholder.className = image.classList.contains('modal-avatar')
        ? 'modal-avatar-placeholder'
        : 'card-avatar-placeholder';
    const name = image.dataset.placeholderName || '';
    placeholder.textContent = name ? name.charAt(0).toUpperCase() : '?';
    image.replaceWith(placeholder);
}

function showEmptyState(message) {
    const grid = document.getElementById('exploreGrid');
    if (!grid) return;
    grid.innerHTML = `<div class="explore-empty" style="grid-column: 1 / -1;">
        <i class="fas fa-search"></i>
        <p>${escapeHtml(message)}</p>
    </div>`;
}

function showLoading(show) {
    const loader = document.getElementById('exploreLoader');
    const grid = document.getElementById('exploreGrid');
    if (loader) loader.style.display = show ? 'flex' : 'none';
    if (grid && show) grid.innerHTML = '';
}

function updateResultsBar() {
    const bar = document.getElementById('resultsInfo');
    if (!bar) return;
    if (ExploreState.total === 0) {
        bar.textContent = '';
        return;
    }
    const start = (ExploreState.currentPage - 1) * ExploreState.limit + 1;
    const end = Math.min(ExploreState.currentPage * ExploreState.limit, ExploreState.total);
    const key = ExploreState.activeTab === 'prompts' ? 'explore.results_prompts' : 'explore.results_packs';
    bar.textContent = mt(key, {count: ExploreState.total, start: AurvekI18n.formatNumber(start), end: AurvekI18n.formatNumber(end), total: AurvekI18n.formatNumber(ExploreState.total)});
}

// ============================================================
// RENDERING - Pagination
// ============================================================

function renderPagination() {
    const container = document.getElementById('explorePagination');
    if (!container) return;

    if (ExploreState.totalPages <= 1) {
        container.innerHTML = '';
        return;
    }

    const { currentPage, totalPages } = ExploreState;
    let html = '';

    // Previous button
    html += `<button class="page-btn" aria-label="${mh('explore.previous')}" data-page="${currentPage - 1}" ${currentPage <= 1 ? 'disabled' : ''}>
        <i class="fas fa-chevron-left"></i>
    </button>`;

    // Page numbers with ellipsis
    const pages = getVisiblePages(currentPage, totalPages);
    pages.forEach(p => {
        if (p === '...') {
            html += `<span class="page-btn" style="cursor:default;border:none;">...</span>`;
        } else {
            html += `<button class="page-btn ${p === currentPage ? 'active' : ''}" data-page="${p}">${escapeHtml(AurvekI18n.formatNumber(p))}</button>`;
        }
    });

    // Next button
    html += `<button class="page-btn" aria-label="${mh('explore.next')}" data-page="${currentPage + 1}" ${currentPage >= totalPages ? 'disabled' : ''}>
        <i class="fas fa-chevron-right"></i>
    </button>`;

    container.innerHTML = html;
}

function getVisiblePages(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);

    const pages = [];
    pages.push(1);

    if (current > 3) pages.push('...');

    const start = Math.max(2, current - 1);
    const end = Math.min(total - 1, current + 1);
    for (let i = start; i <= end; i++) pages.push(i);

    if (current < total - 2) pages.push('...');

    pages.push(total);
    return pages;
}

function goToPage(page) {
    if (page < 1 || page > ExploreState.totalPages || page === ExploreState.currentPage) return;
    ExploreState.currentPage = page;
    if (ExploreState.activeTab === 'prompts') {
        loadPrompts();
    } else {
        loadPacks();
    }
    // Scroll to top of grid
    const hero = document.querySelector('.explore-hero');
    if (hero) hero.scrollIntoView({ behavior: 'smooth' });
}

// ============================================================
// DETAIL MODAL - Prompts
// ============================================================

function openModal(prompt) {
    const backdrop = document.getElementById('exploreModalBackdrop');
    const modalContent = document.getElementById('modalContent');
    if (!backdrop || !modalContent) return;

    const avatarHtml = prompt.image_fullsize_url || prompt.image_url
        ? `<img src="${escapeAttr(prompt.image_fullsize_url || prompt.image_url)}" alt="" class="modal-avatar" data-placeholder-name="${escapeAttr(prompt.name || '')}">`
        : generateModalPlaceholder(prompt.name);

    const tagsHtml = (prompt.categories || [])
        .map(c => `<span class="modal-tag"><i class="fas ${escapeAttr(c.icon || 'fa-tag')}"></i> ${escapeHtml(c.name)}</span>`)
        .join('');

    const description = prompt.description || mt('explore.no_description');
    const creatorName = prompt.creator_name || mt('explore.unknown');

    const landingUrl = prompt.public_id && prompt.slug
        ? `/p/${encodeURIComponent(prompt.public_id)}/${encodeURIComponent(prompt.slug)}/`
        : null;

    const landingBtn = landingUrl
        ? `<a href="${escapeAttr(landingUrl)}" target="_blank" class="modal-secondary-btn"><i class="fas fa-external-link-alt"></i> ${mh('explore.landing_page')}</a>`
        : '';

    const modalFavClass = prompt.is_favorite ? 'is-favorite' : '';
    const modalFavIcon = prompt.is_favorite ? 'fas' : 'far';

    let visibilityNotice = '';
    if (ExploreState.activeFilter === 'mine') {
        if (!prompt.is_public) {
            visibilityNotice = '<div class="visibility-notice"><i class="fas fa-lock"></i> ' + mh('explore.prompt_private') + '</div>';
        } else if (prompt.is_unlisted) {
            visibilityNotice = '<div class="visibility-notice"><i class="fas fa-eye-slash"></i> ' + mh('explore.prompt_unlisted') + '</div>';
        }
    }

    ExploreState.modalItem = prompt;
    ExploreState.modalType = 'prompt';
    ExploreState.modalLandingUrl = landingUrl || '';

    modalContent.innerHTML = `
        <button class="modal-close-btn" data-modal-action="close" title="${mh('explore.close')}"><i class="fas fa-times"></i></button>
        <button class="modal-fav-btn ${modalFavClass}" data-modal-action="favorite" data-favorite-prompt-id="${escapeAttr(String(prompt.id))}" title="${prompt.is_favorite ? mh('explore.remove_favorite') : mh('explore.add_favorite')}">
            <i class="${modalFavIcon} fa-star"></i>
        </button>
        <div class="modal-header-section">
            ${avatarHtml}
            <div class="modal-info">
                <h2 class="modal-prompt-name">${escapeHtml(prompt.name)}</h2>
                <div class="modal-creator">${mh('explore.by_creator', {name: '@' + creatorName})}</div>
            </div>
        </div>
        ${visibilityNotice}
        <div class="modal-description">${escapeHtml(description)}</div>
        <div class="modal-tags">${tagsHtml}</div>
        ${(prompt.purchase_price > 0 && !prompt.user_has_access && !prompt.is_mine)
            ? `<div class="modal-price-info"><i class="fas fa-crown"></i> ${mh('explore.one_time', {price: AurvekI18n.formatCurrency(prompt.purchase_price)})}</div>`
            : ''}
        ${getPromptCTA(prompt)}
        <div class="modal-secondary-actions">
            ${landingBtn}
            <button class="modal-secondary-btn" data-modal-action="share">
                <i class="fas fa-share-alt"></i> ${mh('explore.share')}
            </button>
        </div>
    `;

    backdrop.classList.add('active');
    document.body.style.overflow = 'hidden';
}

function getPromptCTA(prompt) {
    if (prompt.user_has_access || prompt.is_mine) {
        return `<button class="modal-cta" data-modal-action="chat">
            <i class="fas fa-comments"></i> ${mh('explore.chat_now')}
        </button>`;
    }
    if (prompt.purchase_price > 0) {
        return `<button class="modal-cta modal-cta-vip" data-modal-action="purchase-prompt">
            <i class="fas fa-crown"></i> ${mh('explore.unlock_vip')}
        </button>`;
    }
    return `<button class="modal-cta" data-modal-action="chat">
        <i class="fas fa-comments"></i> ${mh('explore.chat_now')}
    </button>`;
}

async function purchasePrompt(promptId) {
    try {
        const res = await fetch('/api/prompts/' + promptId + '/purchase', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include'
        });
        const data = await res.json();
        if (data.checkout_url) {
            window.location = data.checkout_url;
        } else if (data.free_purchase) {
            window.location = '/chat';
        } else if (data.redirect) {
            window.location = data.redirect;
        } else if (data.message) {
            alert(data.message);
        } else if (data.detail) {
            alert(data.detail);
        }
    } catch (err) {
        console.error('Purchase failed:', err);
        alert(mt('explore.purchase_retry'));
    }
}

// ============================================================
// DETAIL MODAL - Packs
// ============================================================

function openPackModal(pack) {
    const backdrop = document.getElementById('exploreModalBackdrop');
    const modalContent = document.getElementById('modalContent');
    if (!backdrop || !modalContent) return;

    const coverHtml = pack.has_cover_image
        ? `<img src="/api/packs/${encodeURIComponent(pack.id)}/cover/512" alt="" class="pack-modal-cover">`
        : `<div class="pack-modal-cover-placeholder"><span>${escapeHtml(pack.name ? pack.name.charAt(0).toUpperCase() : '?')}</span></div>`;

    const description = pack.description || '';
    const creator = pack.created_by_username || mt('explore.unknown');
    const itemCount = pack.item_count || 0;
    const priceLabel = pack.is_paid ? AurvekI18n.formatCurrency(pack.price) : mt('store.free');
    const slug = pack.slug || '';
    const publicId = pack.public_id || '';

    // Parse tags
    let tags = [];
    if (pack.tags) {
        try {
            tags = typeof pack.tags === 'string' ? JSON.parse(pack.tags) : pack.tags;
        } catch (e) { /* ignore */ }
    }
    const tagsHtml = tags.map(t => `<span class="modal-tag">#${escapeHtml(t)}</span>`).join('');

    const landingUrl = publicId && slug ? `/pack/${encodeURIComponent(publicId)}/${encodeURIComponent(slug)}/` : null;
    const landingBtn = landingUrl
        ? `<a href="${escapeAttr(landingUrl)}" target="_blank" class="modal-secondary-btn"><i class="fas fa-external-link-alt"></i> ${mh('explore.view_landing')}</a>`
        : '';

    let packVisibilityNotice = '';
    if (ExploreState.activeFilter === 'mine') {
        if (pack.status === 'draft') {
            packVisibilityNotice = '<div class="visibility-notice"><i class="fas fa-pencil-alt"></i> ' + mh('explore.pack_draft') + '</div>';
        } else if (!pack.is_public) {
            packVisibilityNotice = '<div class="visibility-notice"><i class="fas fa-lock"></i> ' + mh('explore.pack_private') + '</div>';
        }
    }

    ExploreState.modalItem = pack;
    ExploreState.modalType = 'pack';
    ExploreState.modalLandingUrl = landingUrl || '';

    modalContent.innerHTML = `
        <button class="modal-close-btn" data-modal-action="close" title="${mh('explore.close')}"><i class="fas fa-times"></i></button>
        <div class="pack-modal-header">
            ${coverHtml}
            <div class="modal-info">
                <h2 class="modal-prompt-name">${escapeHtml(pack.name)}</h2>
                <div class="modal-creator">${mh('explore.by_creator', {name: '@' + creator})} &mdash; ${mh('explore.prompt_count', {count: itemCount, number: AurvekI18n.formatNumber(itemCount)})}</div>
                <div class="pack-modal-price">${escapeHtml(priceLabel)}</div>
            </div>
        </div>
        ${packVisibilityNotice}
        ${description ? `<div class="modal-description">${escapeHtml(description)}</div>` : ''}
        ${tagsHtml ? `<div class="modal-tags">${tagsHtml}</div>` : ''}
        <div class="pack-modal-prompts" id="packModalPrompts">
            <div class="explore-loading" style="padding:1rem 0"><div class="spinner"></div></div>
        </div>
        ${!pack.is_paid && pack.id ? `
        <button class="modal-cta" style="width:100%;cursor:pointer" data-modal-action="claim-pack">
            <i class="fas fa-rocket"></i> ${mh('explore.get_pack_price', {price: mt('store.free')})}
        </button>` : pack.is_paid && pack.id ? `
        <button class="modal-cta" style="width:100%;cursor:pointer" id="packPurchaseBtn" data-modal-action="purchase-pack">
            <i class="fas fa-shopping-cart"></i> ${mh('explore.get_pack_price', {price: priceLabel})}
        </button>
        <div id="packPurchaseError" class="modal-error" style="display:none;color:#f04747;padding:0.5rem 0;text-align:center;font-size:0.9rem;"></div>` : landingUrl ? `
        <a href="${escapeAttr(landingUrl)}" target="_blank" class="modal-cta" style="text-decoration:none;text-align:center;display:block">
            <i class="fas fa-rocket"></i> ${mh('explore.get_pack')}
        </a>` : ''}
        <div class="modal-secondary-actions">
            ${landingBtn}
            <button class="modal-secondary-btn" data-modal-action="share">
                <i class="fas fa-share-alt"></i> ${mh('explore.share')}
            </button>
        </div>
    `;

    backdrop.classList.add('active');
    document.body.style.overflow = 'hidden';

    // Load pack items asynchronously
    if (pack.id) loadPackModalItems(pack.id);
}

async function loadPackModalItems(packId) {
    const container = document.getElementById('packModalPrompts');
    if (!container) return;

    try {
        const res = await fetch(`/api/explore/packs/${packId}/items`);
        if (!res.ok) {
            container.innerHTML = '';
            return;
        }
        const items = await res.json();

        if (!items.length) {
            container.innerHTML = '';
            return;
        }

        let html = '<h3 class="pack-modal-prompts-title">' + mh('explore.included') + '</h3>';
        items.forEach(item => {
            const initial = item.prompt_name ? item.prompt_name.charAt(0).toUpperCase() : '?';
            const desc = item.prompt_description ? escapeHtml(item.prompt_description.substring(0, 60)) : '';
            html += `<div class="pack-modal-prompt-row">
                <div class="pack-modal-prompt-avatar">${escapeHtml(initial)}</div>
                <div class="pack-modal-prompt-info">
                    <div class="pack-modal-prompt-name">${escapeHtml(item.prompt_name)}</div>
                    ${desc ? `<div class="pack-modal-prompt-desc">${desc}</div>` : ''}
                </div>
            </div>`;
        });

        container.innerHTML = html;
    } catch (err) {
        console.error('Failed to load pack items:', err);
        container.innerHTML = '';
    }
}

// ============================================================
// MODAL - Shared
// ============================================================

function generateModalPlaceholder(name) {
    const initial = name ? name.charAt(0).toUpperCase() : '?';
    return `<div class="modal-avatar-placeholder">${escapeHtml(initial)}</div>`;
}

function handleModalAction(event) {
    const actionElement = event.target.closest('[data-modal-action]');
    if (!actionElement) return;

    const item = ExploreState.modalItem;
    const action = actionElement.dataset.modalAction;
    if (action === 'close') {
        closeModal();
    } else if (action === 'favorite' && item && ExploreState.modalType === 'prompt') {
        toggleExploreFavorite(item.id, actionElement);
    } else if (action === 'share' && item) {
        sharePrompt(item.name, ExploreState.modalLandingUrl);
    } else if (action === 'chat' && item) {
        chatWithPrompt(item.id, item.name);
    } else if (action === 'purchase-prompt' && item) {
        purchasePrompt(item.id);
    } else if (action === 'claim-pack' && item) {
        claimFreePack(item.id, ExploreState.modalLandingUrl);
    } else if (action === 'purchase-pack' && item) {
        purchasePack(item.id, ExploreState.modalLandingUrl);
    }
}

function closeModal() {
    const backdrop = document.getElementById('exploreModalBackdrop');
    if (backdrop) {
        backdrop.classList.remove('active');
        document.body.style.overflow = '';
    }
    ExploreState.modalItem = null;
    ExploreState.modalType = null;
    ExploreState.modalLandingUrl = '';
}

async function chatWithPrompt(promptId, promptName) {
    try {
        const formData = new FormData();
        formData.append('prompt_id', promptId);

        const res = await fetch('/api/select-prompt', {
            method: 'POST',
            body: formData
        });

        if (!res.ok) {
            const err = await res.json();
            NotificationModal.error(mt('explore.error'), err.detail || mt('explore.select_failed'));
            return;
        }

        // Redirect to chat and auto-start new conversation
        window.location.href = '/chat?autostart=1';
    } catch (err) {
        console.error('Failed to select prompt:', err);
        NotificationModal.error(mt('explore.error'), mt('explore.connection_error'));
    }
}

async function claimFreePack(packId, landingUrl) {
    try {
        const res = await fetch(`/api/packs/${packId}/claim-free`, { method: 'POST' });
        if (res.status === 401) {
            // Not logged in: redirect to landing page for registration
            if (landingUrl) {
                window.location.href = landingUrl;
            } else {
                NotificationModal.warning(mt('explore.login_required'), mt('explore.login_claim'));
            }
            return;
        }
        if (!res.ok) {
            const err = await res.json();
            NotificationModal.error(mt('explore.error'), err.detail || mt('explore.claim_failed'));
            return;
        }
        const data = await res.json();
        window.location.href = data.redirect || '/chat';
    } catch (err) {
        console.error('Failed to claim pack:', err);
        NotificationModal.error(mt('explore.error'), mt('explore.connection_error'));
    }
}

async function purchasePack(packId, landingUrl) {
    const btn = document.getElementById('packPurchaseBtn');
    const errEl = document.getElementById('packPurchaseError');
    if (errEl) errEl.style.display = 'none';

    // Disable button to prevent double-clicks
    let originalBtnHtml = '';
    if (btn) {
        originalBtnHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + mh('explore.processing');
    }

    try {
        const res = await fetch(`/api/packs/${packId}/purchase`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        if (res.status === 401) {
            // Not logged in: redirect to landing page for registration
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = originalBtnHtml;
            }
            if (landingUrl) {
                window.location.href = landingUrl;
            } else {
                NotificationModal.warning(mt('explore.login_required'), mt('explore.login_purchase'));
            }
            return;
        }

        const data = await res.json();

        if (res.ok && data.checkout_url) {
            window.location.href = data.checkout_url;
            return;
        }
        if (res.ok && data.free_purchase) {
            window.location.href = data.redirect || '/chat';
            return;
        }
        if (res.ok && data.redirect) {
            window.location.href = data.redirect;
            return;
        }

        // Error: re-enable button so user can retry
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = originalBtnHtml;
        }
        const msg = data.detail || data.message || mt('explore.purchase_failed');
        if (errEl) {
            errEl.textContent = msg;
            errEl.style.display = 'block';
        } else {
            NotificationModal.error(mt('explore.error'), msg);
        }
    } catch (err) {
        // Re-enable button on error so user can retry
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = originalBtnHtml;
        }
        console.error('Failed to purchase pack:', err);
        if (errEl) {
            errEl.textContent = mt('explore.connection_error');
            errEl.style.display = 'block';
        } else {
            NotificationModal.error(mt('explore.error'), mt('explore.connection_error'));
        }
    }
}

async function sharePrompt(name, landingUrl) {
    const shareUrl = landingUrl || window.location.href;
    const shareText = mt('explore.share_text', {name});

    if (navigator.share) {
        try {
            await navigator.share({ title: name, text: shareText, url: shareUrl });
        } catch (e) {
            // User cancelled share - no action needed
        }
    } else {
        // Fallback: copy to clipboard
        try {
            await navigator.clipboard.writeText(shareUrl);
            NotificationModal.toast(mt('explore.link_copied'), 'success');
        } catch (e) {
            // Double fallback: show URL in a modal so user can select and copy
            const safeUrl = shareUrl.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            NotificationModal.info(mt('explore.share_link'), `<input class="form-control" aria-label="${mh('explore.share_link')}" value="${safeUrl}" readonly>`, { allowHtml: true });
        }
    }
}

// ============================================================
// FAVORITES TOGGLE
// ============================================================

async function toggleExploreFavorite(promptId, btnEl) {
    // Optimistic UI update
    const isFav = btnEl.classList.contains('is-favorite');
    const icon = btnEl.querySelector('i');

    btnEl.classList.toggle('is-favorite');
    icon.className = isFav ? 'far fa-star' : 'fas fa-star';
    btnEl.title = isFav ? mt('explore.add_favorite') : mt('explore.remove_favorite');

    try {
        const res = await fetch(`/api/home/favorites/${promptId}`, { method: 'POST' });
        if (!res.ok) throw new Error('Failed to toggle favorite');
        const data = await res.json();

        // Update local state
        const prompt = ExploreState.prompts.find(p => p.id === promptId);
        if (prompt) prompt.is_favorite = data.is_favorite;

        // Sync all buttons for this prompt (card + modal may both be visible)
        document.querySelectorAll(`.explore-fav-btn, .modal-fav-btn`).forEach(btn => {
            if (btn.dataset.favoritePromptId === String(promptId)) {
                const ico = btn.querySelector('i');
                if (data.is_favorite) {
                    btn.classList.add('is-favorite');
                    ico.className = 'fas fa-star';
                    btn.title = mt('explore.remove_favorite');
                } else {
                    btn.classList.remove('is-favorite');
                    ico.className = 'far fa-star';
                    btn.title = mt('explore.add_favorite');
                }
            }
        });

        // Update badge visibility in the card
        renderPrompts();
    } catch (err) {
        console.error('Failed to toggle favorite:', err);
        // Revert optimistic update
        btnEl.classList.toggle('is-favorite');
        icon.className = isFav ? 'fas fa-star' : 'far fa-star';
        btnEl.title = isFav ? mt('explore.remove_favorite') : mt('explore.add_favorite');
    }
}

// ============================================================
// LANDING PREVIEW OVERLAY
// ============================================================

function buildLandingItemsList() {
    // Build list of items with landing pages for iframe navigation
    if (ExploreState.activeTab === 'prompts') {
        ExploreState.iframeLandingItems = ExploreState.prompts
            .filter(p => p.has_landing_page && p.public_id && p.slug)
            .map(p => ({...p, _type: 'prompt'}));
    } else {
        ExploreState.iframeLandingItems = ExploreState.packs
            .filter(p => (p.has_landing_page || p.has_custom_landing) && p.public_id && p.slug)
            .map(p => ({...p, _type: 'pack'}));
    }
}

function openExploreItem(item, type) {
    // Dispatcher: iframe preview for items with landing, modal for others
    if (type === 'prompt') {
        if (item.has_landing_page && item.public_id && item.slug) {
            openLandingPreview(item, type);
        } else {
            openModal(item);
        }
    } else {
        if ((item.has_landing_page || item.has_custom_landing) && item.public_id && item.slug) {
            openLandingPreview(item, type);
        } else {
            openPackModal(item);
        }
    }
}

function openLandingPreview(item, type) {
    const overlay = document.getElementById('landingPreviewOverlay');
    if (!overlay) return;

    // Find index in landing items list
    const idx = ExploreState.iframeLandingItems.findIndex(i => i.id === item.id && i._type === type);
    ExploreState.iframeCurrentIndex = idx >= 0 ? idx : 0;
    ExploreState.iframeActive = true;

    // Public embed mode keeps owner-only preview semantics while allowing the
    // isolated landing origin to be framed by the primary Explore page.
    const url = type === 'prompt'
        ? `/p/${encodeURIComponent(item.public_id)}/${encodeURIComponent(item.slug)}/?embed=1`
        : `/pack/${encodeURIComponent(item.public_id)}/${encodeURIComponent(item.slug)}/?embed=1`;

    const iframe = document.getElementById('landingPreviewIframe');
    iframe.src = url;

    updatePreviewBar(item, type);
    updatePreviewNavigation();

    overlay.classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeLandingPreview() {
    const overlay = document.getElementById('landingPreviewOverlay');
    if (!overlay) return;

    overlay.classList.remove('active');
    document.body.style.overflow = '';
    ExploreState.iframeActive = false;
    ExploreState.previewItem = null;
    ExploreState.previewType = null;
    ExploreState.purchaseRequestPending = false;

    // Clear iframe to stop any running content
    const iframe = document.getElementById('landingPreviewIframe');
    if (iframe) iframe.src = 'about:blank';
}

function navigatePreview(direction) {
    const items = ExploreState.iframeLandingItems;
    if (items.length <= 1) return;

    let newIndex = ExploreState.iframeCurrentIndex + direction;
    // Wrap around
    if (newIndex < 0) newIndex = items.length - 1;
    if (newIndex >= items.length) newIndex = 0;

    ExploreState.iframeCurrentIndex = newIndex;
    const item = items[newIndex];
    const type = item._type;

    const url = type === 'prompt'
        ? `/p/${encodeURIComponent(item.public_id)}/${encodeURIComponent(item.slug)}/?embed=1`
        : `/pack/${encodeURIComponent(item.public_id)}/${encodeURIComponent(item.slug)}/?embed=1`;

    document.getElementById('landingPreviewIframe').src = url;
    updatePreviewBar(item, type);
    updatePreviewNavigation();
}

function updatePreviewBar(item, type) {
    const nameEl = document.getElementById('previewItemName');
    const creatorEl = document.getElementById('previewItemCreator');
    const ctaBtn = document.getElementById('previewCtaBtn');

    if (nameEl) nameEl.textContent = item.name || '';
    if (creatorEl) creatorEl.textContent = mt('explore.by_creator', {name: '@' + (item.creator_name || item.created_by_username || mt('explore.unknown'))});
    ExploreState.previewItem = item;
    ExploreState.previewType = type;

    // CTA button
    if (ctaBtn) {
        if (type === 'prompt') {
            if (item.user_has_access || item.is_mine) {
                ctaBtn.textContent = mt('explore.chat_now');
            } else if (item.purchase_price > 0) {
                ctaBtn.textContent = mt('explore.unlock_vip');
            } else {
                ctaBtn.textContent = mt('explore.chat_now');
            }
        } else {
            // Pack
            if (item.is_paid) {
                ctaBtn.textContent = mt('explore.buy_price', {price: AurvekI18n.formatCurrency(item.price)});
            } else {
                ctaBtn.textContent = mt('explore.get_free');
            }
        }
    }
}

function updatePreviewNavigation() {
    const items = ExploreState.iframeLandingItems;
    const counter = document.getElementById('previewCounter');
    const prevBtn = document.getElementById('previewPrevBtn');
    const nextBtn = document.getElementById('previewNextBtn');

    if (counter) {
        counter.textContent = items.length > 1
            ? mt('explore.counter', {current: AurvekI18n.formatNumber(ExploreState.iframeCurrentIndex + 1), total: AurvekI18n.formatNumber(items.length)})
            : '';
    }

    if (prevBtn) prevBtn.style.display = items.length > 1 ? '' : 'none';
    if (nextBtn) nextBtn.style.display = items.length > 1 ? '' : 'none';
}

function previewCtaAction() {
    const item = ExploreState.previewItem;
    const type = ExploreState.previewType;
    if (!item || !type) return;

    closeLandingPreview();
    if (type === 'prompt') {
        if (item.user_has_access || item.is_mine || !(item.purchase_price > 0)) {
            chatWithPrompt(item.id, item.name);
        } else {
            purchasePrompt(item.id);
        }
    } else if (item.is_paid) {
        purchasePack(item.id, '');
    } else {
        claimFreePack(item.id, '');
    }
}

// ============================================================
// UTILITIES
// ============================================================

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
